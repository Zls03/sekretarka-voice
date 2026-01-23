import os
import json
import base64
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

oai_client = openai.OpenAI(api_key=OPENAI_API_KEY)

# Cache
audio_cache = {}
conversations = {}
pending_responses = {}  # call_sid -> audio_id


async def transcribe_with_deepgram(audio_data: bytes) -> str:
    """Transkrypcja przez Deepgram Nova-3"""
    url = "https://api.deepgram.com/v1/listen?model=nova-2&language=pl&encoding=mulaw&sample_rate=8000"
    
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            headers={
                "Authorization": f"Token {DEEPGRAM_API_KEY}",
                "Content-Type": "audio/mulaw"
            },
            data=audio_data
        ) as response:
            if response.status == 200:
                result = await response.json()
                transcript = result.get("results", {}).get("channels", [{}])[0].get("alternatives", [{}])[0].get("transcript", "")
                logger.info(f"📝 Deepgram: {transcript}")
                return transcript
            else:
                error = await response.text()
                logger.error(f"Deepgram error: {response.status} - {error}")
                return ""


async def get_ai_response(call_sid: str, user_message: str) -> str:
    """Odpowiedź od GPT"""
    if call_sid not in conversations:
        conversations[call_sid] = [{"role": "system", "content": SYSTEM_PROMPT}]
    
    conversations[call_sid].append({"role": "user", "content": user_message})
    
    response = oai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=conversations[call_sid],
        max_tokens=150
    )
    
    assistant_message = response.choices[0].message.content
    conversations[call_sid].append({"role": "assistant", "content": assistant_message})
    
    logger.info(f"🤖 GPT: {assistant_message}")
    return assistant_message


async def text_to_speech(text: str) -> bytes:
    """Synteza mowy przez ElevenLabs"""
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
                logger.info(f"🔊 ElevenLabs: {len(audio_data)} bytes")
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
    """Serwuj audio"""
    if audio_id in audio_cache:
        audio_data = audio_cache.pop(audio_id)
        return Response(content=audio_data, media_type="audio/mpeg")
    return Response(status_code=404)


@app.post("/twilio/incoming")
async def twilio_incoming(request: Request):
    """Początek rozmowy - powitanie + Gather"""
    host = request.headers.get("host", "localhost")
    form = await request.form()
    call_sid = form.get("CallSid", "unknown")
    
    logger.info(f"📞 Incoming call: {call_sid}")
    
    # Generuj powitanie
    audio = await text_to_speech("Dzień dobry, w czym mogę pomóc?")
    
    if audio:
        audio_id = f"greeting_{call_sid}"
        audio_cache[audio_id] = audio
        
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>https://{host}/audio/{audio_id}</Play>
    <Gather input="speech" language="pl-PL" speechTimeout="2" action="https://{host}/twilio/gather/{call_sid}" method="POST">
        <Pause length="10"/>
    </Gather>
    <Say language="pl-PL">Przepraszam, nie usłyszałam odpowiedzi. Do widzenia.</Say>
</Response>"""
    else:
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say language="pl-PL" voice="Google.pl-PL-Wavenet-E">Dzień dobry, w czym mogę pomóc?</Say>
    <Gather input="speech" language="pl-PL" speechTimeout="2" action="https://{host}/twilio/gather/{call_sid}" method="POST">
        <Pause length="10"/>
    </Gather>
</Response>"""
    
    return Response(content=twiml, media_type="application/xml")


@app.post("/twilio/gather/{call_sid}")
async def twilio_gather(call_sid: str, request: Request):
    """Obsłuż mowę użytkownika przez Twilio Gather (ich STT) + Deepgram backup"""
    host = request.headers.get("host", "localhost")
    form = await request.form()
    speech_result = form.get("SpeechResult", "")
    
    logger.info(f"📝 Twilio STT: {speech_result}")
    
    if speech_result and len(speech_result.strip()) > 2:
        # Odpowiedź AI
        response = await get_ai_response(call_sid, speech_result)
        
        # TTS
        audio = await text_to_speech(response)
        
        if audio:
            audio_id = f"resp_{call_sid}_{id(audio)}"
            audio_cache[audio_id] = audio
            
            twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>https://{host}/audio/{audio_id}</Play>
    <Gather input="speech" language="pl-PL" speechTimeout="2" action="https://{host}/twilio/gather/{call_sid}" method="POST">
        <Pause length="10"/>
    </Gather>
    <Say language="pl-PL">Czy mogę jeszcze w czymś pomóc?</Say>
    <Gather input="speech" language="pl-PL" speechTimeout="2" action="https://{host}/twilio/gather/{call_sid}" method="POST">
        <Pause length="5"/>
    </Gather>
</Response>"""
        else:
            twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say language="pl-PL" voice="Google.pl-PL-Wavenet-E">{response}</Say>
    <Gather input="speech" language="pl-PL" speechTimeout="2" action="https://{host}/twilio/gather/{call_sid}" method="POST">
        <Pause length="10"/>
    </Gather>
</Response>"""
    else:
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say language="pl-PL" voice="Google.pl-PL-Wavenet-E">Przepraszam, nie usłyszałam. Czy możesz powtórzyć?</Say>
    <Gather input="speech" language="pl-PL" speechTimeout="3" action="https://{host}/twilio/gather/{call_sid}" method="POST">
        <Pause length="10"/>
    </Gather>
</Response>"""
    
    return Response(content=twiml, media_type="application/xml")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8765))
    uvicorn.run(app, host="0.0.0.0", port=port)