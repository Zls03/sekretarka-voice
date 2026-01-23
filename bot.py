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

# ==========================================
# KONFIGURACJA
# ==========================================
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "NacdHGUYR1k3M0FAbAia")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

oai_client = openai.OpenAI(api_key=OPENAI_API_KEY)

# ==========================================
# ŹRÓDŁO PRAWDY - dane firmy (później z bazy)
# ==========================================
BUSINESS_DATA = {
    "name": "Salon Fryzjerski Anna",
    "working_hours": "od poniedziałku do piątku od dziewiątej do siedemnastej, w czwartki do dziewiętnastej, w soboty od dziesiątej do czternastej",
    "services": [
        {"name": "Strzyżenie damskie", "price": 80},
        {"name": "Strzyżenie męskie", "price": 50},
        {"name": "Koloryzacja", "price": 200},
        {"name": "Modelowanie", "price": 60},
    ],
    "address": "ulica Kwiatowa 15, Warszawa",
}

# ==========================================
# INTENT DETECTION (GPT → JSON only)
# ==========================================
INTENT_PROMPT = """Rozpoznaj intencję użytkownika. Odpowiedz TYLKO jednym słowem z listy:
GREETING, ASK_HOURS, ASK_SERVICES, ASK_ADDRESS, BOOK, GOODBYE, OTHER

Tekst: "{text}"
Intencja:"""

async def detect_intent(text: str) -> str:
    try:
        response = oai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": INTENT_PROMPT.format(text=text)}],
            max_tokens=10,
            temperature=0
        )
        return response.choices[0].message.content.strip().upper()
    except Exception as e:
        logger.error(f"Intent error: {e}")
        return "OTHER"

# ==========================================
# BACKEND GENERUJE TEKST (nie GPT!)
# ==========================================
def generate_response(intent: str) -> str:
    """Backend generuje odpowiedź - ZERO halucynacji"""
    
    responses = {
        "GREETING": f"Dzień dobry, tu {BUSINESS_DATA['name']}. W czym mogę pomóc?",
        "ASK_HOURS": f"Pracujemy {BUSINESS_DATA['working_hours']}. W niedziele zamknięte.",
        "ASK_SERVICES": "Oferujemy: " + ", ".join([f"{s['name']} za {s['price']} złotych" for s in BUSINESS_DATA['services']]) + ". Którą usługą jest Pan zainteresowany?",
        "ASK_ADDRESS": f"Znajdujemy się pod adresem {BUSINESS_DATA['address']}.",
        "BOOK": "Chętnie umówię wizytę. Na jaką usługę i kiedy?",
        "GOODBYE": "Dziękuję za telefon. Do widzenia!",
        "OTHER": "Przepraszam, czy mógłby Pan powtórzyć? Mogę pomóc z godzinami, cennikiem lub rezerwacją."
    }
    
    return responses.get(intent, responses["OTHER"])

# ==========================================
# TTS - ElevenLabs → mulaw 8000
# ==========================================
async def text_to_speech(text: str) -> bytes:
    """ElevenLabs TTS - STREAMING endpoint z optimize_streaming_latency"""
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}/stream?output_format=ulaw_8000&optimize_streaming_latency=3"
    
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json",
                "accept": "audio/wav"  # ← WAŻNE!
            },
            json={
                "text": text,
                "model_id": "eleven_flash_v2_5"
            }
        ) as response:
            if response.status == 200:
                audio = await response.read()
                logger.info(f"🔊 TTS: {len(audio)} bytes")
                return audio
            else:
                error = await response.text()
                logger.error(f"TTS error: {response.status} - {error}")
                return b""
# ==========================================
# DEEPGRAM STT - Nova-3 General
# ==========================================
class DeepgramSTT:
    def __init__(self, on_transcript):
        self.on_transcript = on_transcript
        self.ws = None
        self.session = None
        
    async def connect(self):
        # Nova-3 General dla polskiego!
        url = (
            "wss://api.deepgram.com/v1/listen"
            "?model=nova-3-general"  # zmień na nova-3-general jak będzie dostępny
            "&language=pl"
            "&encoding=mulaw"
            "&sample_rate=8000"
            "&endpointing=400"
            "&interim_results=false"
        )
        
        self.session = aiohttp.ClientSession()
        try:
            self.ws = await self.session.ws_connect(
                url,
                headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"}
            )
            logger.info("🎤 Deepgram connected")
            asyncio.create_task(self._listen())
        except Exception as e:
            logger.error(f"Deepgram connect error: {e}")
            
    async def _listen(self):
        try:
            async for msg in self.ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get("type") == "Results":
                        alt = data.get("channel", {}).get("alternatives", [{}])[0]
                        transcript = alt.get("transcript", "")
                        if transcript and data.get("is_final"):
                            logger.info(f"📝 STT: {transcript}")
                            await self.on_transcript(transcript)
        except Exception as e:
            logger.error(f"Deepgram listen error: {e}")
            
    async def send(self, audio: bytes):
        if self.ws:
            await self.ws.send_bytes(audio)
            
    async def close(self):
        if self.ws:
            await self.ws.close()
        if self.session:
            await self.session.close()

# ==========================================
# TWILIO AUDIO - wysyłanie do klienta
# ==========================================
async def send_to_twilio(ws: WebSocket, audio: bytes, stream_sid: str):
    """Wyślij audio do Twilio - cały payload na raz"""
    if not audio or not stream_sid:
        return
        
    message = {
        "event": "media",
        "streamSid": stream_sid,
        "media": {
            "payload": base64.b64encode(audio).decode("ascii")
        }
    }
    await ws.send_text(json.dumps(message))
    logger.info(f"📤 Sent to Twilio: {len(audio)} bytes")

# ==========================================
# ENDPOINTS
# ==========================================
@app.get("/health")
async def health():
    return {"status": "ok", "service": "Voice AI"}

@app.post("/twilio/incoming")
async def incoming(request: Request):
    host = request.headers.get("host", "localhost")
    
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://{host}/ws" />
    </Connect>
</Response>"""
    
    logger.info("📞 Incoming call")
    return Response(content=twiml, media_type="application/xml")

@app.websocket("/ws")
async def websocket(ws: WebSocket):
    await ws.accept()
    logger.info("🔌 WebSocket connected")
    
    stream_sid = None
    
    async def on_transcript(text: str):
        if len(text.strip()) < 2:
            return
            
        # 1. Intent detection (GPT → JSON)
        intent = await detect_intent(text)
        logger.info(f"🎯 Intent: {intent}")
        
        # 2. Backend generuje odpowiedź (ZERO halucynacji)
        response_text = generate_response(intent)
        logger.info(f"💬 Response: {response_text}")
        
        # 3. TTS
        audio = await text_to_speech(response_text)
        
        # 4. Wyślij do Twilio
        if audio:
            await send_to_twilio(ws, audio, stream_sid)
    
    stt = DeepgramSTT(on_transcript)
    
    try:
        while True:
            msg = await ws.receive_text()
            data = json.loads(msg)
            event = data.get("event")
            
            if event == "start":
                stream_sid = data.get("streamSid")
                logger.info(f"▶️ Stream started: {stream_sid}")
                
                # Połącz z Deepgram
                await stt.connect()
                
                # Powitanie
                greeting = f"Dzień dobry, tu {BUSINESS_DATA['name']}. W czym mogę pomóc?"
                audio = await text_to_speech(greeting)
                if audio:
                    await send_to_twilio(ws, audio, stream_sid)
                    
            elif event == "media":
                payload = data.get("media", {}).get("payload", "")
                audio = base64.b64decode(payload)
                await stt.send(audio)
                
            elif event == "stop":
                logger.info("⏹️ Stream stopped")
                break
                
    except Exception as e:
        logger.error(f"❌ Error: {e}")
    finally:
        await stt.close()
        logger.info("👋 Closed")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8765))
    uvicorn.run(app, host="0.0.0.0", port=port)