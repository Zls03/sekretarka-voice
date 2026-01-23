"""
VOICE AI - MULTI-TENANT SYSTEM
==============================
Uniwersalny silnik głosowy z bazą Turso.

Stack:
- Twilio Media Streams (bidirectional)
- Deepgram Nova-2 (STT polski)
- ElevenLabs Flash 2.5 (TTS)
- GPT-4o-mini (tylko intent → JSON)
- Turso/libSQL (baza danych)
- State Machine (kontrola rozmowy)
"""

import os
import json
import base64
import asyncio
import aiohttp
import httpx
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from enum import Enum
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
TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")

app = FastAPI(title="Voice AI Multi-Tenant")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

oai_client = openai.OpenAI(api_key=OPENAI_API_KEY)


# ==========================================
# TURSO DATABASE (HTTP API)
# ==========================================
class TursoDB:
    """Prosty klient Turso przez HTTP API"""
    
    def __init__(self):
        # Konwertuj libsql:// na https://
        self.url = TURSO_DATABASE_URL.replace("libsql://", "https://")
        self.token = TURSO_AUTH_TOKEN
        
    async def execute(self, sql: str, args: List = None) -> List[Dict]:
        """Wykonaj zapytanie SQL"""
        if not self.url or not self.token:
            logger.warning("⚠️ Turso not configured, using fallback data")
            return []
            
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.url}/v2/pipeline",
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "requests": [
                            {
                                "type": "execute",
                                "stmt": {
                                    "sql": sql,
                                    "args": [{"type": "text", "value": str(a)} for a in (args or [])]
                                }
                            },
                            {"type": "close"}
                        ]
                    },
                    timeout=10.0
                )
                
                if response.status_code == 200:
                    data = response.json()
                    results = data.get("results", [])
                    if results and results[0].get("type") == "ok":
                        result = results[0].get("response", {}).get("result", {})
                        cols = [c.get("name") for c in result.get("cols", [])]
                        rows = []
                        for row in result.get("rows", []):
                            row_dict = {}
                            for i, col in enumerate(cols):
                                val = row[i]
                                row_dict[col] = val.get("value") if isinstance(val, dict) else val
                            rows.append(row_dict)
                        return rows
                    return []
                else:
                    logger.error(f"Turso error: {response.status_code} - {response.text}")
                    return []
        except Exception as e:
            logger.error(f"Turso exception: {e}")
            return []

db = TursoDB()


# ==========================================
# FALLBACK DATA (gdy brak Turso)
# ==========================================
FALLBACK_TENANTS = {
    "+48732071272": {
        "id": "tenant_001",
        "name": "Salon Fryzjerski Anna",
        "phone_number": "+48732071272",
        "address": "ul. Kwiatowa 15, Warszawa",
        "working_hours": {
            0: {"open": "09:00", "close": "17:00"},
            1: {"open": "09:00", "close": "17:00"},
            2: {"open": "09:00", "close": "17:00"},
            3: {"open": "09:00", "close": "19:00"},
            4: {"open": "09:00", "close": "17:00"},
            5: {"open": "10:00", "close": "14:00"},
            6: None
        },
        "services": [
            {"id": "svc1", "name": "Strzyżenie damskie", "duration": 60, "price": 80},
            {"id": "svc2", "name": "Strzyżenie męskie", "duration": 30, "price": 50},
            {"id": "svc3", "name": "Koloryzacja", "duration": 120, "price": 200},
            {"id": "svc4", "name": "Modelowanie", "duration": 45, "price": 60},
        ],
        "staff": [
            {"id": "staff1", "name": "Anna Kowalska"},
            {"id": "staff2", "name": "Maria Nowak"},
        ]
    }
}


async def get_tenant_by_phone(phone: str) -> Optional[Dict]:
    """Znajdź firmę po numerze telefonu"""
    
    # Normalizuj numer
    phone_clean = phone.replace(" ", "").replace("-", "")
    phone_suffix = phone_clean[-9:] if len(phone_clean) >= 9 else phone_clean
    
    # Próbuj z bazy Turso
    rows = await db.execute(
        "SELECT * FROM tenants WHERE phone_number LIKE ? AND is_active = 1",
        [f"%{phone_suffix}"]
    )
    
    if rows:
        tenant = rows[0]
        tenant_id = tenant["id"]
        
        # Pobierz usługi
        services = await db.execute(
            "SELECT id, name, duration_minutes as duration, price FROM services WHERE tenant_id = ? AND is_active = 1",
            [tenant_id]
        )
        
        # Pobierz godziny
        hours_rows = await db.execute(
            "SELECT day_of_week, open_time, close_time FROM working_hours WHERE tenant_id = ?",
            [tenant_id]
        )
        working_hours = {}
        for h in hours_rows:
            day = int(h["day_of_week"])
            if h["open_time"]:
                working_hours[day] = {"open": h["open_time"], "close": h["close_time"]}
            else:
                working_hours[day] = None
        
        # Pobierz pracowników
        staff = await db.execute(
            "SELECT id, name, role FROM staff WHERE tenant_id = ? AND is_active = 1",
            [tenant_id]
        )
        
        return {
            **tenant,
            "services": services,
            "working_hours": working_hours,
            "staff": staff
        }
    
    # Fallback na hardcoded dane
    for tenant_phone, tenant_data in FALLBACK_TENANTS.items():
        if phone_clean.endswith(tenant_phone[-9:]):
            logger.info("📦 Using fallback data")
            return tenant_data
    
    return None


# ==========================================
# STATE MACHINE
# ==========================================
class State(Enum):
    START = "start"
    LISTENING = "listening"
    ASK_SERVICE = "ask_service"
    ASK_DATE = "ask_date"
    ASK_TIME = "ask_time"
    CONFIRM = "confirm"
    DONE = "done"
    END = "end"


@dataclass
class Conversation:
    """Kontekst rozmowy"""
    tenant: Dict[str, Any]
    call_sid: str = ""
    caller_phone: str = ""
    state: State = State.START
    
    # Zbierane dane do rezerwacji
    selected_service: Optional[Dict] = None
    selected_date: Optional[str] = None
    selected_time: Optional[str] = None
    customer_name: Optional[str] = None
    
    # Historia
    transcript: List[str] = field(default_factory=list)


# Aktywne rozmowy (w produkcji: Redis)
conversations: Dict[str, Conversation] = {}


# ==========================================
# INTENT DETECTION (GPT → tylko słowo)
# ==========================================
INTENT_PROMPT = """Rozpoznaj intencję użytkownika. Odpowiedz JEDNYM słowem z listy:
GREETING, ASK_HOURS, ASK_SERVICES, ASK_ADDRESS, BOOK, SELECT_SERVICE, SELECT_DATE, SELECT_TIME, CONFIRM, DENY, GOODBYE, OTHER

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
        intent = response.choices[0].message.content.strip().upper()
        return intent.split()[0] if intent else "OTHER"
    except Exception as e:
        logger.error(f"Intent error: {e}")
        return "OTHER"


# ==========================================
# RESPONSE GENERATOR (Backend - ZERO GPT!)
# ==========================================
def generate_response(conv: Conversation, intent: str, user_text: str) -> str:
    """
    Backend generuje odpowiedź z DANYCH firmy.
    GPT NIE generuje tekstu do klienta!
    """
    
    tenant = conv.tenant
    
    # === GREETING ===
    if intent == "GREETING":
        return f"Dzień dobry, tu {tenant['name']}. W czym mogę pomóc?"
    
    # === ASK_HOURS ===
    if intent == "ASK_HOURS":
        hours = tenant.get("working_hours", {})
        parts = []
        
        weekday = hours.get(0)
        if weekday:
            open_h = weekday['open'].replace(':00', '') if weekday['open'] else ''
            close_h = weekday['close'].replace(':00', '') if weekday['close'] else ''
            parts.append(f"od poniedziałku do piątku od {open_h} do {close_h}")
        
        thursday = hours.get(3)
        if thursday and weekday and thursday.get("close") != weekday.get("close"):
            close_h = thursday['close'].replace(':00', '')
            parts.append(f"w czwartki dłużej do {close_h}")
        
        saturday = hours.get(5)
        if saturday:
            open_h = saturday['open'].replace(':00', '')
            close_h = saturday['close'].replace(':00', '')
            parts.append(f"w soboty od {open_h} do {close_h}")
        
        if hours.get(6) is None:
            parts.append("w niedziele zamknięte")
        
        return "Pracujemy " + ", ".join(parts) + "." if parts else "Przepraszam, nie mam informacji o godzinach."
    
    # === ASK_SERVICES ===
    if intent == "ASK_SERVICES":
        services = tenant.get("services", [])
        if not services:
            return "Przepraszam, nie mam informacji o usługach."
        
        svc_list = [f"{s['name']} za {int(s['price'])} złotych" for s in services]
        conv.state = State.LISTENING
        return "Oferujemy: " + ", ".join(svc_list) + ". Czym mogę służyć?"
    
    # === ASK_ADDRESS ===
    if intent == "ASK_ADDRESS":
        address = tenant.get("address", "")
        return f"Znajdujemy się pod adresem {address}." if address else "Przepraszam, nie mam informacji o adresie."
    
    # === BOOK ===
    if intent == "BOOK":
        conv.state = State.ASK_SERVICE
        services = tenant.get("services", [])
        svc_names = [s['name'] for s in services]
        return f"Chętnie umówię wizytę. Na jaką usługę? Mamy: {', '.join(svc_names)}."
    
    # === SELECT_SERVICE ===
    if intent == "SELECT_SERVICE" or conv.state == State.ASK_SERVICE:
        user_lower = user_text.lower()
        services = tenant.get("services", [])
        
        for svc in services:
            svc_name_lower = svc['name'].lower()
            # Sprawdź czy nazwa usługi lub jej część pasuje
            if svc_name_lower in user_lower or any(word in user_lower for word in svc_name_lower.split() if len(word) > 3):
                conv.selected_service = svc
                conv.state = State.ASK_DATE
                return f"Świetnie, {svc['name']} za {int(svc['price'])} złotych. Na kiedy zarezerwować?"
        
        svc_names = [s['name'] for s in services]
        return f"Nie rozpoznałam usługi. Mamy: {', '.join(svc_names)}. Którą wybrać?"
    
    # === SELECT_DATE ===
    if intent == "SELECT_DATE" or conv.state == State.ASK_DATE:
        conv.selected_date = user_text
        conv.state = State.ASK_TIME
        return "Na którą godzinę?"
    
    # === SELECT_TIME ===
    if intent == "SELECT_TIME" or conv.state == State.ASK_TIME:
        conv.selected_time = user_text
        conv.state = State.CONFIRM
        svc_name = conv.selected_service['name'] if conv.selected_service else "usługę"
        return f"Rezerwuję {svc_name} na {conv.selected_date}, godzina {conv.selected_time}. Potwierdzam?"
    
    # === CONFIRM ===
    if intent == "CONFIRM" and conv.state == State.CONFIRM:
        conv.state = State.DONE
        # TODO: Zapisz rezerwację do bazy + Google Calendar
        return "Rezerwacja potwierdzona! Dziękuję i do zobaczenia."
    
    # === DENY ===
    if intent == "DENY":
        conv.state = State.LISTENING
        conv.selected_service = None
        conv.selected_date = None
        conv.selected_time = None
        return "W porządku. Jak mogę pomóc?"
    
    # === GOODBYE ===
    if intent == "GOODBYE":
        conv.state = State.END
        return "Dziękuję za telefon. Do usłyszenia!"
    
    # === OTHER / FALLBACK ===
    return "Przepraszam, nie zrozumiałam. Mogę pomóc z godzinami otwarcia, cennikiem usług lub umówić wizytę."


# ==========================================
# TTS - ElevenLabs (streaming + ulaw_8000)
# ==========================================
async def text_to_speech(text: str) -> bytes:
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}/stream?output_format=ulaw_8000&optimize_streaming_latency=3"
    
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json",
                "accept": "audio/wav"
            },
            json={"text": text, "model_id": "eleven_flash_v2_5"}
        ) as response:
            if response.status == 200:
                audio = await response.read()
                logger.info(f"🔊 TTS: {len(audio)} bytes")
                return audio
            logger.error(f"TTS error: {response.status}")
            return b""


# ==========================================
# DEEPGRAM STT (Nova-2 General, polski)
# ==========================================
class DeepgramSTT:
    def __init__(self, on_transcript):
        self.on_transcript = on_transcript
        self.ws = None
        self.session = None
        
    async def connect(self):
        url = (
            "wss://api.deepgram.com/v1/listen"
            "?model=nova-2-general"
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
                        transcript = data.get("channel", {}).get("alternatives", [{}])[0].get("transcript", "")
                        if transcript and data.get("is_final"):
                            logger.info(f"📝 STT: {transcript}")
                            await self.on_transcript(transcript)
        except Exception as e:
            logger.error(f"Deepgram error: {e}")
            
    async def send(self, audio: bytes):
        if self.ws:
            await self.ws.send_bytes(audio)
            
    async def close(self):
        if self.ws:
            await self.ws.close()
        if self.session:
            await self.session.close()


# ==========================================
# TWILIO AUDIO
# ==========================================
async def send_audio(ws: WebSocket, audio: bytes, stream_sid: str):
    if audio and stream_sid:
        await ws.send_text(json.dumps({
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": base64.b64encode(audio).decode("ascii")}
        }))
        logger.info(f"📤 Sent: {len(audio)} bytes")


# ==========================================
# ENDPOINTS
# ==========================================
@app.get("/health")
async def health():
    return {"status": "ok", "service": "Voice AI Multi-Tenant"}


@app.get("/api/tenants")
async def list_tenants():
    """Lista wszystkich firm (dla panelu admina)"""
    rows = await db.execute("SELECT id, name, phone_number, address FROM tenants WHERE is_active = 1")
    return {"tenants": rows}


@app.post("/twilio/incoming")
async def incoming(request: Request):
    """Webhook dla przychodzących połączeń Twilio"""
    host = request.headers.get("host", "localhost")
    form = await request.form()
    
    called = form.get("Called", "")
    caller = form.get("From", "")
    call_sid = form.get("CallSid", "")
    
    logger.info(f"📞 Call: {caller} → {called} (SID: {call_sid})")
    
    # Znajdź firmę po numerze
    tenant = await get_tenant_by_phone(called)
    
    if not tenant:
        logger.warning(f"⚠️ Unknown number: {called}")
        return Response(
            content='<?xml version="1.0"?><Response><Say language="pl-PL">Przepraszamy, ten numer nie jest aktywny.</Say></Response>',
            media_type="application/xml"
        )
    
    logger.info(f"✅ Tenant: {tenant['name']}")
    
    # Utwórz kontekst rozmowy
    conversations[call_sid] = Conversation(
        tenant=tenant,
        call_sid=call_sid,
        caller_phone=caller
    )
    
    # TwiML - połącz z WebSocket
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://{host}/ws">
            <Parameter name="callSid" value="{call_sid}" />
        </Stream>
    </Connect>
</Response>"""
    
    return Response(content=twiml, media_type="application/xml")


@app.websocket("/ws")
async def websocket_handler(ws: WebSocket):
    """Bidirectional WebSocket: Twilio ↔ Deepgram ↔ GPT ↔ ElevenLabs"""
    await ws.accept()
    logger.info("🔌 WebSocket connected")
    
    stream_sid = None
    call_sid = None
    conv: Optional[Conversation] = None
    
    async def on_transcript(text: str):
        """Obsłuż transkrypcję od Deepgram"""
        if not conv or len(text.strip()) < 2:
            return
        
        # Zapisz do historii
        conv.transcript.append(f"USER: {text}")
        
        # Wykryj intent (GPT → tylko słowo)
        intent = await detect_intent(text)
        logger.info(f"🎯 Intent: {intent}")
        
        # Generuj odpowiedź (Backend z DANYCH, nie GPT!)
        response = generate_response(conv, intent, text)
        conv.transcript.append(f"BOT: {response}")
        logger.info(f"💬 {response}")
        
        # TTS
        audio = await text_to_speech(response)
        if audio:
            await send_audio(ws, audio, stream_sid)
    
    stt = DeepgramSTT(on_transcript)
    
    try:
        while True:
            msg = await ws.receive_text()
            data = json.loads(msg)
            event = data.get("event")
            
            if event == "start":
                stream_sid = data.get("streamSid")
                call_sid = data.get("start", {}).get("customParameters", {}).get("callSid", "")
                
                logger.info(f"▶️ Stream: {stream_sid}, Call: {call_sid}")
                
                # Pobierz kontekst rozmowy
                conv = conversations.get(call_sid)
                if not conv:
                    logger.error(f"❌ No conversation for: {call_sid}")
                    break
                
                # Połącz z Deepgram
                await stt.connect()
                
                # Powitanie
                greeting = f"Dzień dobry, tu {conv.tenant['name']}. W czym mogę pomóc?"
                conv.transcript.append(f"BOT: {greeting}")
                
                audio = await text_to_speech(greeting)
                if audio:
                    await send_audio(ws, audio, stream_sid)
                    conv.state = State.LISTENING
                
            elif event == "media":
                payload = data.get("media", {}).get("payload", "")
                await stt.send(base64.b64decode(payload))
                
            elif event == "stop":
                logger.info("⏹️ Stream stopped")
                # TODO: Zapisz call_log do bazy
                break
                
    except Exception as e:
        logger.error(f"❌ Error: {e}")
    finally:
        await stt.close()
        if call_sid and call_sid in conversations:
            del conversations[call_sid]
        logger.info("👋 Closed")


# ==========================================
# STARTUP - inicjalizacja bazy
# ==========================================
@app.on_event("startup")
async def startup():
    """Inicjalizuj tabele w bazie przy starcie"""
    if not TURSO_DATABASE_URL:
        logger.warning("⚠️ TURSO_DATABASE_URL not set - using fallback data")
        return
    
    # Utwórz tabele jeśli nie istnieją
    schema = """
    CREATE TABLE IF NOT EXISTS tenants (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        phone_number TEXT UNIQUE NOT NULL,
        address TEXT,
        email TEXT,
        google_calendar_id TEXT,
        timezone TEXT DEFAULT 'Europe/Warsaw',
        greeting_text TEXT,
        goodbye_text TEXT,
        is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    
    CREATE TABLE IF NOT EXISTS working_hours (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        day_of_week INTEGER NOT NULL,
        open_time TEXT,
        close_time TEXT,
        FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
    );
    
    CREATE TABLE IF NOT EXISTS services (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        name TEXT NOT NULL,
        duration_minutes INTEGER NOT NULL,
        price REAL NOT NULL,
        currency TEXT DEFAULT 'PLN',
        description TEXT,
        is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
    );
    
    CREATE TABLE IF NOT EXISTS staff (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        name TEXT NOT NULL,
        role TEXT,
        phone TEXT,
        email TEXT,
        google_calendar_id TEXT,
        is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
    );
    
    CREATE TABLE IF NOT EXISTS bookings (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        service_id TEXT NOT NULL,
        staff_id TEXT,
        customer_name TEXT,
        customer_phone TEXT NOT NULL,
        booking_date TEXT NOT NULL,
        booking_time TEXT NOT NULL,
        duration_minutes INTEGER NOT NULL,
        status TEXT DEFAULT 'confirmed',
        notes TEXT,
        call_sid TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
    );
    
    CREATE TABLE IF NOT EXISTS call_logs (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        call_sid TEXT NOT NULL,
        caller_phone TEXT,
        started_at TEXT,
        ended_at TEXT,
        duration_seconds INTEGER,
        transcript TEXT,
        intents_log TEXT,
        booking_id TEXT,
        status TEXT DEFAULT 'completed',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
    );
    """
    
    # Wykonaj każde CREATE TABLE osobno
    for statement in schema.split(";"):
        statement = statement.strip()
        if statement and "CREATE TABLE" in statement:
            await db.execute(statement)
    
    logger.info("✅ Database tables initialized")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8765)))