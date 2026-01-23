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

ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "NacdHGUYR1k3M0FAbAia")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

oai_client = openai.OpenAI(api_key=OPENAI_API_KEY)
conversations = {}

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
- GREETING: powitanie (cześć, dzień dobry, halo, witam)
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
    if intent == "GREETING":
        return f"Witam! Tu {BUSINESS_DATA['name']}. W czym mogę pomóc?"
    
    elif intent == "ASK_HOURS":
        return "Pracujemy od poniedziałku do piątku od dziewiątej do siedemnastej. W czwartki dłużej, do dziewiętnastej. W soboty od dziesiątej do czternastej. W niedziele zamknięte."
    
    elif intent == "ASK_SERVICES":
        services = BUSINESS_DATA["services"]
        parts = [f"{s['name']} za {s['price']} złotych" for s in services]
        return "Oferujemy: " + ", ".join(parts) + ". Czym mogę służyć?"
    
    elif intent == "ASK_ADDRESS":
        return f"Znajdujemy się pod adresem {BUSINESS_DATA['address']}. Zapraszamy!"
    
    elif intent == "BOOK_APPOINTMENT":
        return "Chętnie umówię wizytę. Na jaką usługę i na kiedy?"
    
    elif intent == "GOODBYE":
        return "Dziękuję za telefon. Do usłyszenia!"
    
    else:
        return "Przepraszam, nie zrozumiałam. Mogę pomóc z godzinami otwarcia, cennikiem lub umówieniem wizyty."


async def process_user_input(user_text: str) -> str:
    intent = await detect_intent(user_text)
    return get_response_for_intent(intent, user_text)


# ==========================================
# TTS - ElevenLabs mulaw (oficjalny format dla Twilio)
# ==========================================
async def text_to_speech_mulaw(text: str) -> bytes:
    """ElevenLabs TTS -> ulaw_8000 dla Twilio bidirectional"""
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
                "output_format": "ulaw_8000"  # RAW mulaw bez headerów!
            }
        ) as response:
            if response.status == 200:
                audio_data = await response.read()
                logger.info(f"🔊 ElevenLabs: {len(audio_data)} bytes mulaw")
                return audio_data
            else:
                error = await response.text()
                logger.error(f"ElevenLabs error: {response.status} - {error}")
                return b""


async def send_audio_to_twilio(websocket: WebSocket, audio_data: bytes, stream_sid: str):
    """Wyślij CAŁY payload na raz (jak w oficjalnej dokumentacji ElevenLabs)"""
    
    # Oficjalny sposób - cały payload base64 encoded, wysłany na raz
    media_message = {
        "event": "media",
        "streamSid": stream_sid,
        "media": {
            "payload": base64.b64encode(audio_data).decode("ascii")
        }
    }
    
    await websocket.send_text(json.dumps(media_message))
    logger.info(f"📤 Sent {len(audio_data)} bytes to Twilio")


# ==========================================
# DEEPGRAM WebSocket STT - Nova-3
# ==========================================
class DeepgramTranscriber:
    def __init__(self, on_transcript):
        self.on_transcript = on_transcript
        self.ws = None
        self.session = None
        self.is_connected = False
        
    async def connect(self):
        url = (
            "wss://api.deepgram.com/v1/listen"
            "?model=nova-3-general"  # nova-3 może nie być dostępny, używamy nova-2
            "&language=pl"
            "&encoding=mulaw"
            "&sample_rate=8000"
            "&endpointing=300"
            "&interim_results=false"
            "&punctuate=true"
        )
        
        self.session = aiohttp.ClientSession()
        self.ws = await self.session.ws_connect(
            url,
            headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"}
        )
        self.is_connected = True
        logger.info("🎤 Deepgram connected")
        
        asyncio.create_task(self._listen())
        
    async def _listen(self):
        try:
            async for msg in self.ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    
                    if data.get("type") == "Results":
                        channel = data.get("channel", {})
                        alternatives = channel.get("alternatives", [{}])
                        transcript = alternatives[0].get("transcript", "")
                        is_final = data.get("is_final", False)
                        
                        if transcript and is_final:
                            logger.info(f"📝 Deepgram: {transcript}")
                            await self.on_transcript(transcript)
                            
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error(f"Deepgram error")
                    break
        except Exception as e:
            logger.error(f"Deepgram listen error: {e}")
        finally:
            self.is_connected = False
            
    async def send_audio(self, audio_data: bytes):
        if self.ws and self.is_connected:
            await self.ws.send_bytes(audio_data)
            
    async def close(self):
        if self.ws:
            await self.ws.close()
        if self.session:
            await self.session.close()
        self.is_connected = False


# ==========================================
# ENDPOINTS
# ==========================================
@app.get("/health")
async def health():
    return {"status": "ok", "business": BUSINESS_DATA["name"]}


@app.post("/twilio/incoming")
async def twilio_incoming(request: Request):
    host = request.headers.get("host", "localhost")
    form = await request.form()
    call_sid = form.get("CallSid", "unknown")
    
    logger.info(f"📞 Incoming call: {call_sid}")
    
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://{host}/ws/voice" />
    </Connect>
</Response>"""
    
    return Response(content=twiml, media_type="application/xml")


@app.websocket("/ws/voice")
async def voice_websocket(websocket: WebSocket):
    await websocket.accept()
    logger.info("🎤 Twilio WebSocket connected")
    
    stream_sid = None
    call_sid = None
    
    async def on_transcript(transcript: str):
        if not transcript or len(transcript.strip()) < 2:
            return
        
        logger.info(f"💬 Processing: {transcript}")
        
        # Intent-based response
        response = await process_user_input(transcript)
        logger.info(f"📤 Response: {response}")
        
        # TTS
        audio = await text_to_speech_mulaw(response)
        
        if audio and stream_sid:
            await send_audio_to_twilio(websocket, audio, stream_sid)
    
    deepgram = DeepgramTranscriber(on_transcript)
    
    try:
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)
            event = data.get("event")
            
            if event == "connected":
                logger.info("✅ Stream connected")
                
            elif event == "start":
                stream_sid = data.get("streamSid")
                start_data = data.get("start", {})
                call_sid = start_data.get("callSid", "unknown")
                logger.info(f"📞 Stream: {stream_sid}")
                
                # Connect Deepgram
                await deepgram.connect()
                
                # Powitanie
                greeting = f"Dzień dobry, tu {BUSINESS_DATA['name']}. W czym mogę pomóc?"
                audio = await text_to_speech_mulaw(greeting)
                if audio:
                    await send_audio_to_twilio(websocket, audio, stream_sid)
                    logger.info("✅ Greeting sent")
                
            elif event == "media":
                payload = data.get("media", {}).get("payload", "")
                audio_chunk = base64.b64decode(payload)
                await deepgram.send_audio(audio_chunk)
                
            elif event == "stop":
                logger.info("📞 Stream stop")
                break
                
    except Exception as e:
        logger.error(f"❌ Error: {e}")
    finally:
        await deepgram.close()
        logger.info("👋 Closed")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8765))
    uvicorn.run(app, host="0.0.0.0", port=port)