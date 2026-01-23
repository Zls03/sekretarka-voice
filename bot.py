import os
import asyncio
import aiohttp
from dotenv import load_dotenv
from loguru import logger
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
import openai

load_dotenv()

SYSTEM_PROMPT = """Jesteś asystentką AI odbierającą telefony dla firmy. 
Mówisz po polsku, jesteś uprzejma i pomocna.
Odpowiadaj krótko i zwięźle - max 2-3 zdania.
"""

ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "NacdHGUYR1k3M0FAbAia")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

client = openai.OpenAI(api_key=OPENAI_API_KEY)

# Przechowuj audio tymczasowo
audio_cache = {}


async def text_to_speech(text: str) -> bytes:
    """Synteza mowy przez ElevenLabs - MP3 dla Twilio Play"""
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "text": text,
                "model_id": "eleven_flash_v2_5",
                "output_format": "mp3_44100_128"
            }
        ) as response:
            if response.status == 200:
                audio_data = await response.read()
                logger.info(f"🔊 ElevenLabs zwrócił {len(audio_data)} bytes MP3")
                return audio_data
            else:
                error = await response.text()
                logger.error(f"ElevenLabs error: {response.status} - {error}")
                return b""


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/audio/{audio_id}")
async def get_audio(audio_id: str):
    """Serwuj audio dla Twilio"""
    if audio_id in audio_cache:
        audio_data = audio_cache[audio_id]
        del audio_cache[audio_id]  # Usuń po użyciu
        return Response(content=audio_data, media_type="audio/mpeg")
    return Response(status_code=404)


@app.post("/twilio/incoming")
async def twilio_incoming(request: Request):
    host = request.headers.get("host", "localhost")
    
    # Generuj powitanie
    logger.info("🎙️ Generuję powitanie...")
    audio = await text_to_speech("Dzień dobry, w czym mogę pomóc?")
    
    if audio:
        # Zapisz audio i stwórz URL
        audio_id = f"greeting_{id(audio)}"
        audio_cache[audio_id] = audio
        audio_url = f"https://{host}/audio/{audio_id}"
        
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>{audio_url}</Play>
    <Gather input="speech" language="pl-PL" speechTimeout="2" action="https://{host}/twilio/respond" method="POST">
        <Say language="pl-PL" voice="Google.pl-PL-Wavenet-E">Słucham.</Say>
    </Gather>
</Response>"""
        
        logger.info(f"📞 Incoming call, audio URL: {audio_url}")
    else:
        # Fallback do Twilio Say
        twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say language="pl-PL" voice="Google.pl-PL-Wavenet-E">Dzień dobry, w czym mogę pomóc?</Say>
    <Gather input="speech" language="pl-PL" speechTimeout="2" action="/twilio/respond" method="POST">
        <Say language="pl-PL" voice="Google.pl-PL-Wavenet-E">Słucham.</Say>
    </Gather>
</Response>"""
        logger.info("📞 Incoming call (fallback to Twilio Say)")
    
    return Response(content=twiml, media_type="application/xml")


@app.post("/twilio/respond")
async def twilio_respond(request: Request):
    """Obsłuż odpowiedź użytkownika"""
    form = await request.form()
    speech_result = form.get("SpeechResult", "")
    host = request.headers.get("host", "localhost")
    
    logger.info(f"📝 User said: {speech_result}")
    
    if speech_result:
        # Odpowiedź AI
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": speech_result}
            ],
            max_tokens=150
        )
        ai_response = response.choices[0].message.content
        logger.info(f"🤖 AI: {ai_response}")
        
        # Generuj audio
        audio = await text_to_speech(ai_response)
        
        if audio:
            audio_id = f"response_{id(audio)}"
            audio_cache[audio_id] = audio
            audio_url = f"https://{host}/audio/{audio_id}"
            
            twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>{audio_url}</Play>
    <Gather input="speech" language="pl-PL" speechTimeout="2" action="https://{host}/twilio/respond" method="POST">
        <Pause length="1"/>
    </Gather>
</Response>"""
        else:
            twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say language="pl-PL" voice="Google.pl-PL-Wavenet-E">{ai_response}</Say>
    <Gather input="speech" language="pl-PL" speechTimeout="2" action="https://{host}/twilio/respond" method="POST">
        <Pause length="1"/>
    </Gather>
</Response>"""
    else:
        twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say language="pl-PL" voice="Google.pl-PL-Wavenet-E">Przepraszam, nie usłyszałam. Czy możesz powtórzyć?</Say>
    <Gather input="speech" language="pl-PL" speechTimeout="3" action="/twilio/respond" method="POST">
        <Pause length="1"/>
    </Gather>
</Response>"""
    
    return Response(content=twiml, media_type="application/xml")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8765))
    uvicorn.run(app, host="0.0.0.0", port=port)