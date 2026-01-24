"""
VOICE AI - MULTI-TENANT SYSTEM
==============================
Uniwersalny silnik głosowy z bazą Turso.
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
        self.url = TURSO_DATABASE_URL.replace("libsql://", "https://")
        self.token = TURSO_AUTH_TOKEN
        
    async def execute(self, sql: str, args: List = None) -> List[Dict]:
        """Wykonaj zapytanie SQL"""
        if not self.url or not self.token:
            logger.warning("⚠️ Turso not configured")
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
# POBIERANIE DANYCH FIRMY Z BAZY
# ==========================================
async def get_tenant_by_phone(phone: str) -> Optional[Dict]:
    """Znajdź firmę po numerze telefonu - TYLKO z bazy Turso"""
    
    phone_clean = phone.replace(" ", "").replace("-", "")
    phone_suffix = phone_clean[-9:] if len(phone_clean) >= 9 else phone_clean
    
    logger.info(f"🔍 Searching tenant for phone: {phone_suffix}")
    
    # Pobierz firmę z bazy
    rows = await db.execute(
        "SELECT * FROM tenants WHERE phone_number LIKE ? AND is_active = 1",
        [f"%{phone_suffix}"]
    )
    
    if not rows:
        logger.warning(f"❌ No tenant found for: {phone}")
        return None
    
    tenant = rows[0]
    tenant_id = tenant["id"]
    logger.info(f"✅ Found tenant: {tenant['name']} (ID: {tenant_id})")
    
    # Pobierz usługi
    services = await db.execute(
        "SELECT id, name, duration_minutes, price FROM services WHERE tenant_id = ? AND is_active = 1",
        [tenant_id]
    )
    logger.info(f"📦 Services: {len(services)}")
    
    # Pobierz godziny pracy
    hours_rows = await db.execute(
        "SELECT day_of_week, open_time, close_time FROM working_hours WHERE tenant_id = ?",
        [tenant_id]
    )
    working_hours = {}
    for h in hours_rows:
        day = int(h["day_of_week"]) if h["day_of_week"] else 0
        if h["open_time"]:
            working_hours[day] = {"open": h["open_time"], "close": h["close_time"]}
        else:
            working_hours[day] = None
    logger.info(f"🕐 Working hours: {len(hours_rows)} days")
    
    # Pobierz pracowników
    staff = await db.execute(
        "SELECT id, name, role FROM staff WHERE tenant_id = ? AND is_active = 1",
        [tenant_id]
    )
    logger.info(f"👤 Staff: {len(staff)}")
    
    return {
        **tenant,
        "services": services,
        "working_hours": working_hours,
        "staff": staff
    }


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
    
    selected_service: Optional[Dict] = None
    selected_date: Optional[str] = None
    selected_time: Optional[str] = None
    customer_name: Optional[str] = None
    
    transcript: List[str] = field(default_factory=list)


conversations: Dict[str, Conversation] = {}


# ==========================================
# INTENT DETECTION
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
# HELPERS - FORMATOWANIE PO POLSKU
# ==========================================
def format_hour_polish(time_str: str) -> str:
    """09:00 → dziewiątej"""
    hour_words = {
        6: "szóstej", 7: "siódmej", 8: "ósmej", 9: "dziewiątej",
        10: "dziesiątej", 11: "jedenastej", 12: "dwunastej",
        13: "trzynastej", 14: "czternastej", 15: "piętnastej",
        16: "szesnastej", 17: "siedemnastej", 18: "osiemnastej",
        19: "dziewiętnastej", 20: "dwudziestej", 21: "dwudziestej pierwszej",
        22: "dwudziestej drugiej", 23: "dwudziestej trzeciej"
    }
    if not time_str:
        return ""
    try:
        hour = int(time_str.split(":")[0])
        return hour_words.get(hour, time_str)
    except:
        return time_str


def format_price_polish(price) -> str:
    """50 → pięćdziesiąt złotych"""
    try:
        price = int(float(price))
    except:
        return str(price) + " złotych"
    
    ones = ["", "jeden", "dwa", "trzy", "cztery", "pięć", "sześć", "siedem", "osiem", "dziewięć"]
    teens = ["dziesięć", "jedenaście", "dwanaście", "trzynaście", "czternaście", 
             "piętnaście", "szesnaście", "siedemnaście", "osiemnaście", "dziewiętnaście"]
    tens = ["", "dziesięć", "dwadzieścia", "trzydzieści", "czterdzieści", 
            "pięćdziesiąt", "sześćdziesiąt", "siedemdziesiąt", "osiemdziesiąt", "dziewięćdziesiąt"]
    hundreds = ["", "sto", "dwieście", "trzysta", "czterysta", 
                "pięćset", "sześćset", "siedemset", "osiemset", "dziewięćset"]
    
    if price == 0:
        return "zero złotych"
    
    result = []
    
    if price >= 100:
        result.append(hundreds[price // 100])
        price %= 100
    
    if price >= 20:
        result.append(tens[price // 10])
        if price % 10 > 0:
            result.append(ones[price % 10])
    elif price >= 10:
        result.append(teens[price - 10])
    elif price > 0:
        result.append(ones[price])
    
    return " ".join(result) + " złotych"


# ==========================================
# RESPONSE GENERATOR
# ==========================================
def generate_response(conv: Conversation, intent: str, user_text: str) -> str:
    """Backend generuje odpowiedź z DANYCH firmy - ZERO GPT!"""
    
    tenant = conv.tenant
    
    if intent == "GREETING":
        return f"Dzień dobry, tu {tenant['name']}. W czym mogę pomóc?"
    
    if intent == "ASK_HOURS":
        hours = tenant.get("working_hours", {})
        parts = []
        
        weekday = hours.get(0)
        if weekday:
            open_h = format_hour_polish(weekday['open'])
            close_h = format_hour_polish(weekday['close'])
            parts.append(f"od poniedziałku do piątku od {open_h} do {close_h}")
        
        thursday = hours.get(3)
        if thursday and weekday and thursday.get("close") != weekday.get("close"):
            close_h = format_hour_polish(thursday['close'])
            parts.append(f"w czwartki dłużej do {close_h}")
        
        saturday = hours.get(5)
        if saturday:
            open_h = format_hour_polish(saturday['open'])
            close_h = format_hour_polish(saturday['close'])
            parts.append(f"w soboty od {open_h} do {close_h}")
        
        if hours.get(6) is None:
            parts.append("w niedziele zamknięte")
        
        return "Pracujemy " + ", ".join(parts) + "." if parts else "Przepraszam, nie mam informacji o godzinach."
    
    if intent == "ASK_SERVICES":
        services = tenant.get("services", [])
        if not services:
            return "Przepraszam, nie mam informacji o usługach."
        
        svc_list = [f"{s['name']} za {format_price_polish(s['price'])}" for s in services]
        conv.state = State.LISTENING
        return "Oferujemy: " + ", ".join(svc_list) + ". Czym mogę służyć?"
    
    if intent == "ASK_ADDRESS":
        address = tenant.get("address", "")
        return f"Znajdujemy się pod adresem {address}." if address else "Przepraszam, nie mam informacji o adresie."
    
    if intent == "BOOK":
        conv.state = State.ASK_SERVICE
        services = tenant.get("services", [])
        svc_names = [s['name'] for s in services]
        return f"Chętnie umówię wizytę. Na jaką usługę? Mamy: {', '.join(svc_names)}."
    
    if intent == "SELECT_SERVICE" or conv.state == State.ASK_SERVICE:
        user_lower = user_text.lower()
        services = tenant.get("services", [])
        
        for svc in services:
            svc_name_lower = svc['name'].lower()
            if svc_name_lower in user_lower or any(word in user_lower for word in svc_name_lower.split() if len(word) > 3):
                conv.selected_service = svc
                conv.state = State.ASK_DATE
                return f"Świetnie, {svc['name']} za {format_price_polish(svc['price'])}. Na kiedy zarezerwować?"
        
        svc_names = [s['name'] for s in services]
        return f"Nie rozpoznałam usługi. Mamy: {', '.join(svc_names)}. Którą wybrać?"
    
    if intent == "SELECT_DATE" or conv.state == State.ASK_DATE:
        conv.selected_date = user_text
        conv.state = State.ASK_TIME
        return "Na którą godzinę?"
    
    if intent == "SELECT_TIME" or conv.state == State.ASK_TIME:
        conv.selected_time = user_text
        conv.state = State.CONFIRM
        svc_name = conv.selected_service['name'] if conv.selected_service else "usługę"
        return f"Rezerwuję {svc_name} na {conv.selected_date}, godzina {conv.selected_time}. Potwierdzam?"
    
    if intent == "CONFIRM" and conv.state == State.CONFIRM:
        conv.state = State.DONE
        return "Rezerwacja potwierdzona! Dziękuję i do zobaczenia."
    
    if intent == "DENY":
        conv.state = State.LISTENING
        conv.selected_service = None
        conv.selected_date = None
        conv.selected_time = None
        return "W porządku. Jak mogę pomóc?"
    
    if intent == "GOODBYE":
        conv.state = State.END
        return "Dziękuję za telefon. Do usłyszenia!"
    
    return "Przepraszam, nie zrozumiałam. Mogę pomóc z godzinami otwarcia, cennikiem usług lub umówić wizytę."


# ==========================================
# TTS - ElevenLabs
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
# DEEPGRAM STT
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
# TWILIO
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


@app.post("/twilio/incoming")
async def incoming(request: Request):
    host = request.headers.get("host", "localhost")
    form = await request.form()
    
    called = form.get("Called", "")
    caller = form.get("From", "")
    call_sid = form.get("CallSid", "")
    
    logger.info(f"📞 Call: {caller} → {called} (SID: {call_sid})")
    
    tenant = await get_tenant_by_phone(called)
    
    if not tenant:
        logger.warning(f"⚠️ Unknown number: {called}")
        return Response(
            content='<?xml version="1.0"?><Response><Say language="pl-PL">Przepraszamy, ten numer nie jest aktywny.</Say></Response>',
            media_type="application/xml"
        )
    
    logger.info(f"✅ Tenant: {tenant['name']}")
    
    conversations[call_sid] = Conversation(
        tenant=tenant,
        call_sid=call_sid,
        caller_phone=caller
    )
    
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
    await ws.accept()
    logger.info("🔌 WebSocket connected")
    
    stream_sid = None
    call_sid = None
    conv: Optional[Conversation] = None
    
    async def on_transcript(text: str):
        if not conv or len(text.strip()) < 2:
            return
        
        conv.transcript.append(f"USER: {text}")
        
        intent = await detect_intent(text)
        logger.info(f"🎯 Intent: {intent}")
        
        response = generate_response(conv, intent, text)
        conv.transcript.append(f"BOT: {response}")
        logger.info(f"💬 {response}")
        
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
                
                conv = conversations.get(call_sid)
                if not conv:
                    logger.error(f"❌ No conversation for: {call_sid}")
                    break
                
                await stt.connect()
                
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
                break
                
    except Exception as e:
        logger.error(f"❌ Error: {e}")
    finally:
        await stt.close()
        if call_sid and call_sid in conversations:
            del conversations[call_sid]
        logger.info("👋 Closed")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8765)))
