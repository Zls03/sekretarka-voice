"""
VOICE AI v3.1 - PRODUCTION
==========================
Smart Turn + FAL API + Naprawiony WebSocket

Plik 1/2: bot.py - Główna logika, WebSocket, Twilio
Plik 2/2: helpers.py - GPT, TTS, formatowanie, baza danych
"""

import os
import json
import base64
import asyncio
from datetime import datetime
from typing import Optional, Dict, List
from dataclasses import dataclass, field
from enum import Enum
from dotenv import load_dotenv
from loguru import logger
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware

# Import helperów
from helpers import (
    db, get_tenant_by_phone, detect_intent, text_to_speech,
    process_conversation, save_call_log, FalSmartTurn,
    AudioBuffer, TranscriptBuffer
)

load_dotenv()

# ==========================================
# KONFIGURACJA
# ==========================================
FAL_KEY = os.getenv("FAL_KEY", "")
SMART_TURN_THRESHOLD = 0.5
SILENCE_THRESHOLD_MS = 600
FALLBACK_TIMEOUT_MS = 2000

app = FastAPI(title="Voice AI v3.1")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ==========================================
# STATE MACHINE
# ==========================================
class State(Enum):
    START = "start"
    LISTENING = "listening"
    ASK_SERVICE = "ask_service"
    ASK_DATE = "ask_date"
    ASK_TIME = "ask_time"
    CONFIRM_BOOKING = "confirm_booking"
    BOOKING_DONE = "booking_done"
    END = "end"


@dataclass
class Conversation:
    """Kontekst rozmowy"""
    tenant: Dict
    call_sid: str = ""
    caller_phone: str = ""
    state: State = State.START
    started_at: datetime = field(default_factory=datetime.utcnow)
    
    # Rezerwacja
    selected_service: Optional[Dict] = None
    selected_date: Optional[str] = None
    selected_time: Optional[str] = None
    available_slots: List[str] = field(default_factory=list)
    customer_name: Optional[str] = None
    
    # Historia
    transcript: List[Dict] = field(default_factory=list)
    intents_log: List[Dict] = field(default_factory=list)


# Aktywne rozmowy
conversations: Dict[str, Conversation] = {}


# ==========================================
# DEEPGRAM STT
# ==========================================
import aiohttp

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")

class DeepgramSTT:
    def __init__(self, on_transcript, keyterms: List[str] = None):
        self.on_transcript = on_transcript
        self.ws = None
        self.session = None
        self.keyterms = keyterms or []
        
    async def connect(self):
        base_url = (
            "wss://api.deepgram.com/v1/listen"
            "?model=nova-3"
            "&language=pl"
            "&encoding=mulaw"
            "&sample_rate=8000"
            "&endpointing=600"
            "&interim_results=false"
            "&punctuate=true"
        )
        
        if self.keyterms:
            import urllib.parse
            for kt in self.keyterms[:10]:
                encoded = urllib.parse.quote(kt)
                base_url += f"&keyterm={encoded}"
        
        self.session = aiohttp.ClientSession()
        try:
            self.ws = await self.session.ws_connect(
                base_url,
                headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"}
            )
            logger.info("🎤 Deepgram Nova-3 connected")
            asyncio.create_task(self._listen())
        except Exception as e:
            logger.error(f"Deepgram error: {e}")
            await self._connect_fallback()
            
    async def _connect_fallback(self):
        fallback_url = (
            "wss://api.deepgram.com/v1/listen"
            "?model=nova-2-general"
            "&language=pl"
            "&encoding=mulaw"
            "&sample_rate=8000"
            "&endpointing=800"
            "&interim_results=false"
        )
        try:
            if not self.session:
                self.session = aiohttp.ClientSession()
            self.ws = await self.session.ws_connect(
                fallback_url,
                headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"}
            )
            logger.info("🎤 Deepgram Nova-2 fallback connected")
            asyncio.create_task(self._listen())
        except Exception as e:
            logger.error(f"Deepgram fallback error: {e}")
            
    async def _listen(self):
        try:
            async for msg in self.ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get("type") == "Results":
                        transcript = data.get("channel", {}).get("alternatives", [{}])[0].get("transcript", "")
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
# TWILIO ENDPOINT
# ==========================================
@app.post("/twilio/incoming")
async def incoming(request: Request):
    """Połączenie przychodzące z Twilio"""
    host = request.headers.get("host", "localhost")
    form = await request.form()
    
    called = form.get("Called", form.get("To", ""))
    caller = form.get("From", "")
    call_sid = form.get("CallSid", "")
    
    logger.info(f"📞 Call: {caller} → {called}")
    
    tenant = await get_tenant_by_phone(called)
    
    if not tenant:
        logger.warning(f"❌ No tenant for {called}")
        return Response(
            content='<?xml version="1.0"?><Response><Say language="pl-PL">Przepraszamy, ten numer nie jest aktywny.</Say></Response>',
            media_type="application/xml"
        )
    
    logger.info(f"✅ Tenant: {tenant.get('name') or tenant.get('business_name')}")
    
    # Zapisz rozmowę
    conversations[call_sid] = Conversation(
        tenant=tenant,
        call_sid=call_sid,
        caller_phone=caller,
        started_at=datetime.utcnow()
    )
    
    # TwiML z parametrem callSid
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://{host}/ws">
            <Parameter name="callSid" value="{call_sid}" />
        </Stream>
    </Connect>
</Response>"""
    
    return Response(content=twiml, media_type="application/xml")


# ==========================================
# WEBSOCKET HANDLER
# ==========================================
@app.websocket("/ws")
async def websocket_handler(ws: WebSocket):
    """WebSocket dla Twilio Media Stream"""
    await ws.accept()
    logger.info("🔌 WebSocket connected")
    
    stream_sid = None
    call_sid = None
    conv: Optional[Conversation] = None
    stt = None
    
    # Bufory dla Smart Turn
    audio_buffer = AudioBuffer()
    transcript_buffer = TranscriptBuffer()
    smart_turn = FalSmartTurn(threshold=SMART_TURN_THRESHOLD)
    
    processing = False
    
    async def send_audio(audio: bytes):
        """Wyślij audio do Twilio"""
        if audio and stream_sid:
            await ws.send_text(json.dumps({
                "event": "media",
                "streamSid": stream_sid,
                "media": {"payload": base64.b64encode(audio).decode("ascii")}
            }))
    
    async def process_turn():
        """Przetwórz turę użytkownika"""
        nonlocal processing
        
        if processing or not transcript_buffer.has_content() or not conv:
            return
            
        processing = True
        
        try:
            full_text = transcript_buffer.get_text()
            transcript_buffer.clear()
            
            if not full_text.strip():
                return
            
            conv.transcript.append({
                "role": "user", 
                "text": full_text, 
                "time": datetime.utcnow().isoformat()
            })
            
            # Kontekst dla GPT
            context = " | ".join([f"{t['role']}: {t['text']}" for t in conv.transcript[-6:]])
            services = [s['name'] for s in conv.tenant.get("services", [])]
            
            # GPT → intencja
            intent_data = await detect_intent(full_text, context, services)
            
            # State Machine → odpowiedź
            response = await process_conversation(conv, intent_data, full_text)
            
            conv.transcript.append({
                "role": "assistant", 
                "text": response, 
                "time": datetime.utcnow().isoformat()
            })
            logger.info(f"💬 {response}")
            
            # TTS
            audio = await text_to_speech(response)
            if audio:
                await send_audio(audio)
                
            audio_buffer.clear()
            
        finally:
            processing = False
    
    async def on_transcript(text: str):
        """Callback dla transkrypcji Deepgram"""
        if text.strip():
            transcript_buffer.add(text.strip())
    
    # Task sprawdzający ciszę (Smart Turn)
    async def turn_checker():
        while True:
            await asyncio.sleep(0.15)
            
            if transcript_buffer.has_content() and not processing:
                silence_ms = transcript_buffer.age_ms()
                
                if silence_ms >= SILENCE_THRESHOLD_MS:
                    # Użyj FAL Smart Turn jeśli dostępny
                    if FAL_KEY and audio_buffer.duration_seconds() > 0.5:
                        wav_b64 = audio_buffer.get_wav_base64()
                        if wav_b64:
                            result = await smart_turn.predict(wav_b64)
                            if result["end_of_turn"]:
                                logger.debug(f"🧠 Smart Turn: END (prob={result['probability']:.2f})")
                                await process_turn()
                            else:
                                logger.debug(f"🧠 Smart Turn: CONTINUE")
                        elif silence_ms >= FALLBACK_TIMEOUT_MS:
                            await process_turn()
                    elif silence_ms >= FALLBACK_TIMEOUT_MS:
                        # Fallback bez Smart Turn
                        await process_turn()
    
    turn_task = asyncio.create_task(turn_checker())
    
    try:
        while True:
            msg = await ws.receive_text()
            data = json.loads(msg)
            event = data.get("event")
            
            if event == "start":
                stream_sid = data.get("streamSid")
                # Pobierz callSid z parametrów (tak jak w starym kodzie!)
                call_sid = data.get("start", {}).get("customParameters", {}).get("callSid", "")
                
                logger.info(f"▶️ Stream started: {stream_sid}, call: {call_sid}")
                
                conv = conversations.get(call_sid)
                if not conv:
                    logger.error(f"❌ No conversation for {call_sid}")
                    break
                
                # Keyterms z usług
                keyterms = [s['name'] for s in conv.tenant.get("services", [])]
                keyterms.extend(["rezerwacja", "umówić", "wizyta", "termin"])
                
                # Start Deepgram
                stt = DeepgramSTT(on_transcript, keyterms)
                await stt.connect()
                
                # Przywitanie
                tenant_name = conv.tenant.get('name') or conv.tenant.get('business_name', 'salon')
                greeting = f"Dzień dobry, tu {tenant_name}. W czym mogę pomóc?"
                conv.transcript.append({
                    "role": "assistant", 
                    "text": greeting, 
                    "time": datetime.utcnow().isoformat()
                })
                
                audio = await text_to_speech(greeting)
                if audio:
                    await send_audio(audio)
                    conv.state = State.LISTENING
                
            elif event == "media":
                payload = data.get("media", {}).get("payload", "")
                if payload:
                    audio_bytes = base64.b64decode(payload)
                    if stt:
                        await stt.send(audio_bytes)
                    audio_buffer.add(audio_bytes)
                
            elif event == "stop":
                logger.info("⏹️ Stream stopped")
                break
                
    except Exception as e:
        logger.error(f"❌ WebSocket error: {e}")
    finally:
        turn_task.cancel()
        
        if stt:
            await stt.close()
        
        if conv:
            await save_call_log(conv)
        
        if call_sid and call_sid in conversations:
            del conversations[call_sid]
        
        logger.info("👋 Closed")


# ==========================================
# HEALTH CHECK
# ==========================================
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "3.1",
        "smart_turn": "fal-api" if FAL_KEY else "fallback"
    }

@app.get("/")
async def root():
    return {"message": "Voice AI v3.1", "status": "running"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))