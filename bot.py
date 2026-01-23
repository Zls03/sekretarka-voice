import os
import json
import asyncio
import aiohttp
from dotenv import load_dotenv
from loguru import logger
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
import openai

load_dotenv()

ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "NacdHGUYR1k3M0FAbAia")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

oai_client = openai.OpenAI(api_key=OPENAI_API_KEY)

# Cache dla audio
audio_cache = {}

# ==========================================
# ŹRÓDŁO PRAWDY - dane firmy
# ==========================================
BUSINESS_DATA = {
    "name": "Salon Fryzjerski Anna",
    "working_hours": {
        "monday": {"open": "09:00", "close": "17:00"},
        "tuesday": {"open": "09:00", "close": "17:00"},
        "wednesday": {"open": "09:00", "close": "17:00"},
        "thursday": {"open": "09:00", "close": "19:00"},
        "friday": {"open": "09:00", "close": "17:00"},
        "saturday": {"open": "10:00", "close": "14:00"},
        "sunday": None
    },
    "services": [
        {"name": "Strzyżenie damskie", "duration": 60, "price": 80},
        {"name": "Strzyżenie męskie", "duration": 30, "price": 50},
        {"name": "Koloryzacja", "duration": 120, "price": 200},
        {"name": "Modelowanie", "duration": 45, "price": 60},
    ],
    "address": "ul. Kwiatowa 15, Warszawa",
}

# ==========================================
# INTENTY
# ==========================================
INTENT_PROMPT = """Określ intent użytkownika. Odpowiedz TYLKO jednym słowem.

Możliwe intenty:
- GREETING: powitanie (cześć, dzień dobry, halo)
- ASK_HOURS: pytanie o godziny otwarcia
- ASK_SERVICES: pytanie o usługi/cennik
- ASK_ADDRESS: pytanie o adres
- BOOK_APPOINTMENT: chce umówić wizytę
- GOODBYE: pożegnanie (do widzenia, dziękuję)
- OTHER: inne

Wypowiedź: "{text}"
Intent:"""


async def detect_intent(text: str) -> str:
    response = oai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": INTENT_PROMPT.format(text=text)}],
        max_tokens=20
    )
    intent = response.choices[0].message.content.strip().upper()
    logger.info(f"🎯 Intent: {intent}")
    return intent


def get_response_for_intent(intent: str, user_text: str) -> str:
    """Odpowiedź z DANYCH - bez halucynacji!"""
    
    if intent == "GREETING":
        return f"Dzień dobry! Tu {BUSINESS_DATA['name']}. W czym mogę pomóc?"
    
    elif intent == "ASK_HOURS":
        return "Pracujemy od poniedziałku do piątku od dziewiątej do siedemnastej. W czwartki dłużej, do dziewiętnastej. W soboty od dziesiątej do czternastej. W niedziele zamknięte."
    
    elif intent == "ASK_SERVICES":
        services = BUSINESS_DATA["services"]
        parts = [f"{s['name']} za {s['price']} złotych" for s in services]
        return "Oferujemy: " + ", ".join(parts) + ". Którą usługą jest Pan zainteresowany?"
    
    elif intent == "ASK_ADDRESS":
        return f"Znajdujemy się pod adresem {BUSINESS_DATA['address']}. Zapraszamy!"
    
    elif intent == "BOOK_APPOINTMENT":
        return "Chętnie umówię wizytę. Na jaką usługę i na kiedy?"
    
    elif intent == "GOODBYE":
        return "Dziękuję za telefon. Do usłyszenia!"
    
    else:
        return "Przepraszam, czy mógłby Pan powtórzyć? Mogę pomóc z informacjami o godzinach otwarcia, usługach lub umówieniem wizyty."


async def process_user_input(user_text: str) -> str:
    intent = await detect_intent(user_text)
    response = get_response_for_intent(intent, user_text)
    logger.info(f"📤 Response: {response}")
    return response


# ==========================================
# TTS - ElevenLabs MP3 (działa!)
# ==========================================
async def text_to_speech(text: str) -> bytes:
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


# ==========================================
# ENDPOINTS
# ==========================================
@app.get("/health")
async def health():
    return {"status": "ok", "business": BUSINESS_DATA["name"]}


@app.get("/audio/{audio_id}")
async def get_audio(audio_id: str):
    if audio_id in audio_cache:
        audio_data = audio_cache.pop(audio_id)
        return Response(content=audio_data, media_type="audio/mpeg")
    return Response(status_code=404)


@app.post("/twilio/incoming")
async def twilio_incoming(request: Request):
    host = request.headers.get("host", "localhost")
    form = await request.form()
    call_sid = form.get("CallSid", "unknown")
    
    logger.info(f"📞 Incoming call: {call_sid}")
    
    # Powitanie
    greeting = f"Dzień dobry, tu {BUSINESS_DATA['name']}. W czym mogę pomóc?"
    audio = await text_to_speech(greeting)
    
    if audio:
        audio_id = f"greeting_{call_sid}"
        audio_cache[audio_id] = audio
        
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>https://{host}/audio/{audio_id}</Play>
    <Gather input="speech" language="pl-PL" speechTimeout="2" action="https://{host}/twilio/gather/{call_sid}" method="POST">
        <Pause length="10"/>
    </Gather>
    <Say language="pl-PL">Do widzenia.</Say>
</Response>"""
    else:
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say language="pl-PL" voice="Google.pl-PL-Wavenet-E">{greeting}</Say>
    <Gather input="speech" language="pl-PL" speechTimeout="2" action="https://{host}/twilio/gather/{call_sid}" method="POST">
        <Pause length="10"/>
    </Gather>
</Response>"""
    
    return Response(content=twiml, media_type="application/xml")


@app.post("/twilio/gather/{call_sid}")
async def twilio_gather(call_sid: str, request: Request):
    host = request.headers.get("host", "localhost")
    form = await request.form()
    speech_result = form.get("SpeechResult", "")
    
    logger.info(f"📝 User: {speech_result}")
    
    if speech_result and len(speech_result.strip()) > 2:
        # Intent-based response z DANYCH
        response = await process_user_input(speech_result)
        
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