"""
VOICE AI - PRODUCTION ARCHITECTURE v3.0 (FAL Edition)
=====================================================
Smart Turn przez FAL API - bez torch lokalnie!

Stack:
- Deepgram Nova-3 (STT polski)
- FAL Smart Turn API (hosted - bez torch!)
- GPT-4o-mini (intencje + parsowanie)
- ElevenLabs Flash 2.5 (TTS)
- Twilio Media Streams
- Turso/libSQL (baza danych)

Różnica od lokalnej wersji:
- Smart Turn przez API FAL (~50-100ms)
- Nie wymaga torch/torchaudio (~2GB mniej!)
- Działa na Railway bez timeout
"""

import os
import json
import base64
import asyncio
import aiohttp
import httpx
import numpy as np
from datetime import datetime, timedelta
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
FAL_KEY = os.getenv("FAL_KEY", "")  # Klucz do FAL API

# Smart Turn config
SMART_TURN_THRESHOLD = 0.5
SILENCE_THRESHOLD_MS = 600  # Ile ms ciszy żeby sprawdzić Smart Turn
FALLBACK_TIMEOUT_MS = 2000  # Fallback jeśli Smart Turn niedostępny

app = FastAPI(title="Voice AI v3.0 - FAL Smart Turn")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

oai_client = openai.OpenAI(api_key=OPENAI_API_KEY)


# ==========================================
# TURSO DATABASE
# ==========================================
class TursoDB:
    def __init__(self):
        self.url = TURSO_DATABASE_URL.replace("libsql://", "https://")
        self.token = TURSO_AUTH_TOKEN
        
    async def execute(self, sql: str, args: List = None) -> List[Dict]:
        if not self.url or not self.token:
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
                                    "args": [{"type": "text", "value": str(a) if a is not None else None} for a in (args or [])]
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
        except Exception as e:
            logger.error(f"DB error: {e}")
        return []

db = TursoDB()


# ==========================================
# AUDIO BUFFER
# ==========================================
class AudioBuffer:
    """Bufor audio - konwertuje mulaw 8kHz → base64 dla FAL API"""
    
    def __init__(self, max_seconds: float = 8.0):
        self.max_bytes = int(8000 * max_seconds)  # 8kHz mulaw
        self.buffer = bytearray()
        
    def add(self, data: bytes):
        """Dodaj audio"""
        self.buffer.extend(data)
        # Ogranicz do max
        if len(self.buffer) > self.max_bytes:
            self.buffer = self.buffer[-self.max_bytes:]
            
    def get_wav_base64(self) -> str:
        """Konwertuj do WAV base64 dla FAL API"""
        if not self.buffer:
            return ""
            
        try:
            import audioop
            import struct
            import io
            
            # Mulaw → PCM 16-bit
            pcm_8khz = audioop.ulaw2lin(bytes(self.buffer), 2)
            
            # Resample 8kHz → 16kHz (Smart Turn wymaga 16kHz)
            pcm_16khz = audioop.ratecv(pcm_8khz, 2, 1, 8000, 16000, None)[0]
            
            # Zbuduj WAV header
            wav_buffer = io.BytesIO()
            
            # WAV header
            num_channels = 1
            sample_rate = 16000
            bits_per_sample = 16
            byte_rate = sample_rate * num_channels * bits_per_sample // 8
            block_align = num_channels * bits_per_sample // 8
            data_size = len(pcm_16khz)
            
            wav_buffer.write(b'RIFF')
            wav_buffer.write(struct.pack('<I', 36 + data_size))
            wav_buffer.write(b'WAVE')
            wav_buffer.write(b'fmt ')
            wav_buffer.write(struct.pack('<I', 16))  # Subchunk1Size
            wav_buffer.write(struct.pack('<H', 1))   # AudioFormat (PCM)
            wav_buffer.write(struct.pack('<H', num_channels))
            wav_buffer.write(struct.pack('<I', sample_rate))
            wav_buffer.write(struct.pack('<I', byte_rate))
            wav_buffer.write(struct.pack('<H', block_align))
            wav_buffer.write(struct.pack('<H', bits_per_sample))
            wav_buffer.write(b'data')
            wav_buffer.write(struct.pack('<I', data_size))
            wav_buffer.write(pcm_16khz)
            
            # Base64
            return base64.b64encode(wav_buffer.getvalue()).decode('utf-8')
        except Exception as e:
            logger.error(f"Audio conversion error: {e}")
            return ""
            
    def clear(self):
        self.buffer = bytearray()
        
    def duration_seconds(self) -> float:
        return len(self.buffer) / 8000.0


# ==========================================
# FAL SMART TURN API
# ==========================================
class FalSmartTurn:
    """
    Smart Turn przez FAL API.
    Nie wymaga torch lokalnie!
    
    Docs: https://fal.ai/models/fal-ai/smart-turn/api
    """
    
    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold
        self.api_url = "https://fal.run/fal-ai/smart-turn"
        
    async def predict(self, audio_base64: str) -> Dict[str, Any]:
        """
        Sprawdź czy to koniec wypowiedzi.
        
        Args:
            audio_base64: Audio WAV jako base64
            
        Returns:
            {"end_of_turn": bool, "probability": float}
        """
        if not FAL_KEY:
            logger.warning("FAL_KEY not set - using fallback")
            return {"end_of_turn": True, "probability": 0.5}
            
        if not audio_base64:
            return {"end_of_turn": True, "probability": 0.5}
            
        try:
            # Utwórz data URL
            audio_url = f"data:audio/wav;base64,{audio_base64}"
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.api_url,
                    headers={
                        "Authorization": f"Key {FAL_KEY}",
                        "Content-Type": "application/json"
                    },
                    json={"audio_url": audio_url},
                    timeout=aiohttp.ClientTimeout(total=2.0)  # Max 2s
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        probability = data.get("probability", 0.5)
                        
                        logger.debug(f"🧠 FAL Smart Turn: prob={probability:.2f}")
                        
                        return {
                            "end_of_turn": probability >= self.threshold,
                            "probability": probability
                        }
                    else:
                        error = await resp.text()
                        logger.error(f"FAL API error: {resp.status} - {error}")
                        return {"end_of_turn": True, "probability": 0.5}
                        
        except asyncio.TimeoutError:
            logger.warning("FAL API timeout - using fallback")
            return {"end_of_turn": True, "probability": 0.5}
        except Exception as e:
            logger.error(f"FAL Smart Turn error: {e}")
            return {"end_of_turn": True, "probability": 0.5}


# ==========================================
# TRANSCRIPT BUFFER
# ==========================================
class TranscriptBuffer:
    """Buforuje fragmenty transkrypcji"""
    
    def __init__(self):
        self.fragments: List[str] = []
        self.last_update = datetime.now()
        
    def add(self, text: str):
        if text.strip():
            self.fragments.append(text.strip())
            self.last_update = datetime.now()
            
    def get_text(self) -> str:
        return " ".join(self.fragments)
        
    def clear(self):
        self.fragments = []
        
    def age_ms(self) -> float:
        return (datetime.now() - self.last_update).total_seconds() * 1000
        
    def has_content(self) -> bool:
        return len(self.fragments) > 0


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
class ConversationContext:
    state: State = State.LISTENING
    selected_service: Optional[Dict] = None
    selected_date: Optional[str] = None
    selected_time: Optional[str] = None
    customer_name: Optional[str] = None
    transcript: List[Dict] = field(default_factory=list)


# ==========================================
# GPT INTENT DETECTION
# ==========================================
def build_gpt_prompt(tenant: Dict) -> str:
    today = datetime.now()
    day_names = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]
    today_name = day_names[today.weekday()]
    
    services_list = ", ".join([s['name'] for s in tenant.get('services', [])])
    
    return f"""Jesteś parserem języka polskiego dla asystenta głosowego salonu "{tenant['business_name']}".

DZISIAJ: {today_name}, {today.strftime('%Y-%m-%d')}

DOSTĘPNE USŁUGI: {services_list}

Twoim zadaniem jest wyłącznie parsowanie intencji i danych. Odpowiadaj TYLKO w formacie JSON.

INTENCJE:
- "greeting" - przywitanie
- "ask_services" - pytanie o usługi/cennik
- "ask_hours" - pytanie o godziny otwarcia
- "book" - chęć rezerwacji
- "select_service" - wybór usługi
- "select_date" - wybór daty
- "select_time" - wybór godziny
- "select_date_and_time" - wybór daty i godziny razem
- "confirm" - potwierdzenie
- "cancel" - anulowanie
- "other" - inne

PARSOWANIE DAT (względem {today.strftime('%Y-%m-%d')}):
- "dziś/dzisiaj" → {today.strftime('%Y-%m-%d')}
- "jutro" → {(today + timedelta(days=1)).strftime('%Y-%m-%d')}
- "pojutrze" → {(today + timedelta(days=2)).strftime('%Y-%m-%d')}
- dni tygodnia → najbliższa data tego dnia

PARSOWANIE GODZIN:
- "o dziesiątej" → "10:00"
- "o wpół do jedenastej" → "10:30"
- "rano" → "09:00"
- "po południu" → "14:00"

ODPOWIEDŹ (tylko JSON):
{{
  "intent": "nazwa_intencji",
  "service": "nazwa usługi lub null",
  "date_raw": "oryginalne słowa lub null",
  "date_parsed": "YYYY-MM-DD lub null",
  "time_raw": "oryginalne słowa lub null",
  "time_parsed": "HH:MM lub null",
  "name": "imię klienta lub null"
}}"""


async def detect_intent(text: str, tenant: Dict, context: ConversationContext) -> Dict:
    """Wykryj intencję używając GPT"""
    try:
        history = " | ".join([f"{t['role']}: {t['text']}" for t in context.transcript[-6:]])
        
        response = oai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": build_gpt_prompt(tenant)},
                {"role": "user", "content": f"Kontekst: {history}\n\nNowa wypowiedź: \"{text}\""}
            ],
            temperature=0.1,
            max_tokens=200
        )
        
        content = response.choices[0].message.content.strip()
        
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        elif "```" in content:
            content = content.split("```")[1].split("```")[0]
            
        result = json.loads(content)
        logger.info(f"🎯 Intent: {result.get('intent')} | date: {result.get('date_parsed')} | time: {result.get('time_parsed')}")
        return result
    except Exception as e:
        logger.error(f"GPT error: {e}")
        return {"intent": "other"}


# ==========================================
# AVAILABILITY & BOOKING
# ==========================================
async def get_available_slots(tenant_id: str, date: str, duration: int) -> List[str]:
    try:
        date_obj = datetime.strptime(date, "%Y-%m-%d")
        day_of_week = date_obj.weekday()
        
        hours_rows = await db.execute(
            "SELECT open_time, close_time FROM working_hours WHERE tenant_id = ? AND day_of_week = ?",
            [tenant_id, day_of_week]
        )
        
        if not hours_rows or not hours_rows[0].get("open_time"):
            return []
            
        open_time = hours_rows[0]["open_time"]
        close_time = hours_rows[0]["close_time"]
        
        logger.info(f"📅 Godziny pracy: {open_time} - {close_time}")
        
        slots = []
        current = datetime.strptime(open_time, "%H:%M")
        end = datetime.strptime(close_time, "%H:%M") - timedelta(minutes=duration)
        
        while current <= end:
            slots.append(current.strftime("%H:%M"))
            current += timedelta(minutes=30)
            
        logger.info(f"📅 Dostępne sloty: {slots}")
        return slots[:6]
    except Exception as e:
        logger.error(f"Slots error: {e}")
        return []


async def create_booking(tenant_id: str, service: Dict, date: str, time: str, customer_name: str = None) -> str:
    booking_id = f"book_{int(datetime.now().timestamp())}"
    
    await db.execute(
        """INSERT INTO bookings (id, tenant_id, service_id, service_name, date, time, 
           duration_minutes, price, customer_name, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?)""",
        [booking_id, tenant_id, service.get('id'), service.get('name'), date, time,
         service.get('duration_minutes', 30), service.get('price', 0),
         customer_name, datetime.now().isoformat()]
    )
    
    logger.info(f"✅ Booking created: {booking_id}")
    return booking_id


async def save_call_log(tenant_id: str, caller: str, called: str, started_at: datetime, 
                        transcript: List[Dict], booking_id: str = None):
    ended_at = datetime.now()
    duration_seconds = int((ended_at - started_at).total_seconds())
    
    await db.execute(
        """INSERT INTO call_logs (id, tenant_id, caller_number, called_number, 
           started_at, ended_at, duration_seconds, transcript, booking_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [f"call_{int(datetime.now().timestamp())}", tenant_id, caller, called,
         started_at.isoformat(), ended_at.isoformat(), duration_seconds,
         json.dumps(transcript, ensure_ascii=False), booking_id, datetime.now().isoformat()]
    )
    
    logger.info(f"📊 Call logged: {duration_seconds}s")


# ==========================================
# POLISH FORMATTING
# ==========================================
def format_price_polish(price: int) -> str:
    if price == 0:
        return "bezpłatnie"
    
    units = ["", "jeden", "dwa", "trzy", "cztery", "pięć", "sześć", "siedem", "osiem", "dziewięć"]
    teens = ["dziesięć", "jedenaście", "dwanaście", "trzynaście", "czternaście", 
             "piętnaście", "szesnaście", "siedemnaście", "osiemnaście", "dziewiętnaście"]
    tens = ["", "dziesięć", "dwadzieścia", "trzydzieści", "czterdzieści", 
            "pięćdziesiąt", "sześćdziesiąt", "siedemdziesiąt", "osiemdziesiąt", "dziewięćdziesiąt"]
    hundreds = ["", "sto", "dwieście", "trzysta", "czterysta", 
                "pięćset", "sześćset", "siedemset", "osiemset", "dziewięćset"]
    
    if price < 10:
        num = units[price]
    elif price < 20:
        num = teens[price - 10]
    elif price < 100:
        num = tens[price // 10] + (" " + units[price % 10] if price % 10 else "")
    else:
        num = hundreds[price // 100]
        rest = price % 100
        if rest:
            if rest < 10:
                num += " " + units[rest]
            elif rest < 20:
                num += " " + teens[rest - 10]
            else:
                num += " " + tens[rest // 10] + (" " + units[rest % 10] if rest % 10 else "")
    
    if price == 1:
        return f"{num} złoty"
    elif price % 10 in [2, 3, 4] and price not in [12, 13, 14]:
        return f"{num} złote"
    else:
        return f"{num} złotych"


def format_time_polish(time_str: str) -> str:
    hours_words = {
        "08": "ósmej", "09": "dziewiątej", "10": "dziesiątej", "11": "jedenastej",
        "12": "dwunastej", "13": "trzynastej", "14": "czternastej", "15": "piętnastej",
        "16": "szesnastej", "17": "siedemnastej", "18": "osiemnastej"
    }
    
    hour = time_str[:2]
    minute = time_str[3:5] if len(time_str) > 3 else "00"
    
    hour_word = hours_words.get(hour, time_str)
    
    if minute == "00":
        return hour_word
    elif minute == "30":
        return f"wpół do {hour_word}"
    else:
        return time_str


def format_date_polish(date_str: str) -> str:
    today = datetime.now().date()
    date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
    
    diff = (date_obj - today).days
    
    if diff == 0:
        return "dziś"
    elif diff == 1:
        return "jutro"
    elif diff == 2:
        return "pojutrze"
    else:
        days = ["poniedziałek", "wtorek", "środę", "czwartek", "piątek", "sobotę", "niedzielę"]
        return days[date_obj.weekday()]


# ==========================================
# ELEVENLABS TTS
# ==========================================
async def text_to_speech(text: str) -> bytes:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
                headers={
                    "xi-api-key": ELEVENLABS_API_KEY,
                    "Content-Type": "application/json"
                },
                json={
                    "text": text,
                    "model_id": "eleven_flash_v2_5",
                    "output_format": "ulaw_8000",
                    "voice_settings": {
                        "stability": 0.5,
                        "similarity_boost": 0.75
                    }
                }
            ) as resp:
                if resp.status == 200:
                    audio = await resp.read()
                    logger.info(f"🔊 TTS: {len(audio)} bytes")
                    return audio
                else:
                    logger.error(f"TTS error: {resp.status}")
                    return b""
    except Exception as e:
        logger.error(f"TTS error: {e}")
        return b""


# ==========================================
# DEEPGRAM STT
# ==========================================
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
            "&interim_results=false"
            "&endpointing=false"  # Smart Turn zadecyduje
            "&punctuate=true"
            "&utterance_end_ms=1500"
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
            logger.info(f"🎤 Deepgram Nova-3 connected")
            asyncio.create_task(self._listen())
        except Exception as e:
            logger.error(f"Deepgram connect error: {e}")
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
# TENANT
# ==========================================
async def get_tenant_by_phone(phone: str) -> Optional[Dict]:
    phone_clean = phone.replace(" ", "").replace("-", "")
    phone_suffix = phone_clean[-9:] if len(phone_clean) >= 9 else phone_clean
    
    rows = await db.execute(
        "SELECT * FROM tenants WHERE phone_number LIKE ? AND is_active = 1",
        [f"%{phone_suffix}"]
    )
    
    if not rows:
        return None
    
    tenant = rows[0]
    tenant_id = tenant["id"]
    
    services = await db.execute(
        "SELECT id, name, duration_minutes, price FROM services WHERE tenant_id = ? AND is_active = 1",
        [tenant_id]
    )
    
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
    
    return {
        "id": tenant_id,
        "business_name": tenant.get("business_name"),
        "services": services,
        "working_hours": working_hours
    }


# ==========================================
# RESPONSE GENERATOR
# ==========================================
async def generate_response(intent_data: Dict, tenant: Dict, context: ConversationContext) -> str:
    intent = intent_data.get("intent", "other")
    
    # Aktualizuj kontekst
    if intent_data.get("service"):
        service_name = intent_data["service"].lower()
        for s in tenant.get("services", []):
            if service_name in s["name"].lower():
                context.selected_service = s
                break
                
    if intent_data.get("date_parsed"):
        context.selected_date = intent_data["date_parsed"]
        
    if intent_data.get("time_parsed"):
        context.selected_time = intent_data["time_parsed"]
        
    if intent_data.get("name"):
        context.customer_name = intent_data["name"]
    
    # Greeting
    if intent == "greeting":
        return f"Dzień dobry, tu {tenant['business_name']}. W czym mogę pomóc?"
    
    # Pytanie o usługi
    if intent == "ask_services":
        services_text = ", ".join([
            f"{s['name']} za {format_price_polish(s['price'])}" 
            for s in tenant.get("services", [])
        ])
        return f"Oferujemy: {services_text}. Chcesz się umówić?"
    
    # Pytanie o godziny
    if intent == "ask_hours":
        today_day = datetime.now().weekday()
        hours = tenant.get("working_hours", {}).get(today_day)
        if hours:
            return f"Dziś jesteśmy otwarci od {hours['open']} do {hours['close']}."
        else:
            return "Dziś niestety jesteśmy zamknięci."
    
    # Chęć rezerwacji
    if intent == "book":
        if context.selected_service and context.selected_date and context.selected_time:
            context.state = State.CONFIRM_BOOKING
            return await _confirm_booking_response(context, tenant)
        elif context.selected_service and context.selected_date:
            context.state = State.ASK_TIME
            slots = await get_available_slots(tenant["id"], context.selected_date, 
                                             context.selected_service.get("duration_minutes", 30))
            if slots:
                slots_text = ", ".join([format_time_polish(s) for s in slots[:4]])
                return f"O której godzinie? Dostępne: {slots_text}."
            return "O której godzinie?"
        elif context.selected_service:
            context.state = State.ASK_DATE
            return "Na kiedy chcesz się umówić?"
        else:
            context.state = State.ASK_SERVICE
            services = ", ".join([s['name'] for s in tenant.get("services", [])])
            return f"Chętnie umówię wizytę. Na jaką usługę? Mamy: {services}."
    
    # Wybór usługi
    if intent == "select_service":
        if context.selected_service:
            price = format_price_polish(context.selected_service.get("price", 0))
            
            if context.selected_date and context.selected_time:
                context.state = State.CONFIRM_BOOKING
                return await _confirm_booking_response(context, tenant)
            elif context.selected_date:
                context.state = State.ASK_TIME
                return f"Świetnie, {context.selected_service['name']} za {price}. O której godzinie?"
            else:
                context.state = State.ASK_DATE
                return f"Świetnie, {context.selected_service['name']} za {price}. Na kiedy?"
        return "Przepraszam, nie rozpoznałam usługi."
    
    # Wybór daty
    if intent == "select_date":
        if context.selected_date:
            if context.selected_service and context.selected_time:
                context.state = State.CONFIRM_BOOKING
                return await _confirm_booking_response(context, tenant)
            elif context.selected_service:
                context.state = State.ASK_TIME
                slots = await get_available_slots(tenant["id"], context.selected_date,
                                                 context.selected_service.get("duration_minutes", 30))
                if slots:
                    slots_text = ", ".join([format_time_polish(s) for s in slots[:4]])
                    return f"O której? Dostępne: {slots_text}."
                return "O której godzinie?"
        return "Nie zrozumiałam daty. Na kiedy?"
    
    # Wybór godziny
    if intent == "select_time":
        if context.selected_time:
            if context.selected_service and context.selected_date:
                slots = await get_available_slots(tenant["id"], context.selected_date,
                                                 context.selected_service.get("duration_minutes", 30))
                if context.selected_time in slots:
                    context.state = State.CONFIRM_BOOKING
                    return await _confirm_booking_response(context, tenant)
                else:
                    slots_text = ", ".join([format_time_polish(s) for s in slots[:4]])
                    return f"Ta godzina zajęta. Dostępne: {slots_text}."
        return "Nie zrozumiałam godziny."
    
    # Wybór daty i godziny
    if intent == "select_date_and_time":
        if context.selected_date and context.selected_time:
            if context.selected_service:
                slots = await get_available_slots(tenant["id"], context.selected_date,
                                                 context.selected_service.get("duration_minutes", 30))
                if context.selected_time in slots:
                    context.state = State.CONFIRM_BOOKING
                    return await _confirm_booking_response(context, tenant)
                else:
                    slots_text = ", ".join([format_time_polish(s) for s in slots[:4]])
                    return f"Ta godzina zajęta. Dostępne: {slots_text}."
            else:
                context.state = State.ASK_SERVICE
                services = ", ".join([s['name'] for s in tenant.get("services", [])])
                return f"Na jaką usługę? Mamy: {services}."
        return "Podaj datę i godzinę."
    
    # Potwierdzenie
    if intent == "confirm" and context.state == State.CONFIRM_BOOKING:
        if context.selected_service and context.selected_date and context.selected_time:
            booking_id = await create_booking(
                tenant["id"], 
                context.selected_service,
                context.selected_date,
                context.selected_time,
                context.customer_name
            )
            context.state = State.BOOKING_DONE
            
            date_text = format_date_polish(context.selected_date)
            time_text = format_time_polish(context.selected_time)
            
            return f"Gotowe! {context.selected_service['name']} na {date_text} o {time_text}. Do zobaczenia!"
    
    # Anulowanie
    if intent == "cancel":
        context.state = State.LISTENING
        context.selected_service = None
        context.selected_date = None
        context.selected_time = None
        return "Anuluję. W czym mogę pomóc?"
    
    return "Jak mogę pomóc? Mogę umówić wizytę lub podać cennik."


async def _confirm_booking_response(context: ConversationContext, tenant: Dict) -> str:
    date_text = format_date_polish(context.selected_date)
    time_text = format_time_polish(context.selected_time)
    return f"Rezerwuję {context.selected_service['name']} na {date_text} o {time_text}. Potwierdzasz?"


# ==========================================
# TWILIO ENDPOINTS
# ==========================================
@app.post("/twilio/incoming")
async def incoming(request: Request):
    form = await request.form()
    caller = form.get("From", "unknown")
    called = form.get("To", "unknown")
    
    logger.info(f"📞 Call: {caller} → {called}")
    
    tenant = await get_tenant_by_phone(called)
    
    if not tenant:
        logger.warning(f"❌ No tenant for {called}")
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response><Say language="pl-PL">Przepraszam, ten numer nie jest skonfigurowany.</Say><Hangup/></Response>',
            media_type="application/xml"
        )
    
    logger.info(f"✅ Tenant: {tenant['business_name']}")
    
    host = request.headers.get("host", request.url.hostname)
    ws_url = f"wss://{host}/ws?caller={caller}&called={called}"
    
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{ws_url}" />
    </Connect>
</Response>"""
    
    return Response(content=twiml, media_type="application/xml")


@app.websocket("/ws")
async def websocket_handler(websocket: WebSocket):
    """WebSocket handler z FAL Smart Turn"""
    await websocket.accept()
    
    caller = websocket.query_params.get("caller", "unknown")
    called = websocket.query_params.get("called", "unknown")
    
    logger.info(f"🔌 WebSocket connected")
    
    tenant = await get_tenant_by_phone(called)
    if not tenant:
        await websocket.close()
        return
    
    # Kontekst rozmowy
    context = ConversationContext()
    stream_sid = None
    started_at = datetime.now()
    booking_id = None
    
    # Bufory
    audio_buffer = AudioBuffer()
    transcript_buffer = TranscriptBuffer()
    
    # Smart Turn
    smart_turn = FalSmartTurn(threshold=SMART_TURN_THRESHOLD)
    
    # Flagi
    processing = False
    
    async def process_turn():
        """Przetwórz zakończoną turę"""
        nonlocal processing, booking_id
        
        if processing or not transcript_buffer.has_content():
            return
            
        processing = True
        
        try:
            full_text = transcript_buffer.get_text()
            transcript_buffer.clear()
            
            if not full_text.strip():
                return
            
            context.transcript.append({"role": "user", "text": full_text})
            
            # Intent + Response
            intent_data = await detect_intent(full_text, tenant, context)
            response = await generate_response(intent_data, tenant, context)
            
            logger.info(f"💬 {response}")
            context.transcript.append({"role": "assistant", "text": response})
            
            # TTS
            audio = await text_to_speech(response)
            
            if audio and stream_sid:
                chunk_size = 8000
                for i in range(0, len(audio), chunk_size):
                    chunk = audio[i:i+chunk_size]
                    payload = base64.b64encode(chunk).decode("utf-8")
                    await websocket.send_json({
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": payload}
                    })
                    await asyncio.sleep(0.05)
                    
            if context.state == State.BOOKING_DONE:
                booking_id = f"book_{int(datetime.now().timestamp())}"
                
            audio_buffer.clear()
            
        finally:
            processing = False
    
    async def on_transcript(text: str):
        """Callback dla transkrypcji"""
        if text.strip():
            transcript_buffer.add(text.strip())
    
    # Deepgram
    keyterms = [s['name'] for s in tenant.get('services', [])]
    keyterms.extend(["rezerwacja", "umówić", "wizyta", "termin"])
    stt = DeepgramSTT(on_transcript, keyterms)
    await stt.connect()
    
    # Przywitanie
    greeting = f"Dzień dobry, tu {tenant['business_name']}. W czym mogę pomóc?"
    context.transcript.append({"role": "assistant", "text": greeting})
    greeting_audio = await text_to_speech(greeting)
    
    # Task sprawdzający ciszę
    async def turn_checker():
        """Sprawdza czy użytkownik skończył mówić"""
        while True:
            await asyncio.sleep(0.15)  # Co 150ms
            
            if transcript_buffer.has_content() and not processing:
                silence_ms = transcript_buffer.age_ms()
                
                # Jeśli dość ciszy - sprawdź Smart Turn
                if silence_ms >= SILENCE_THRESHOLD_MS:
                    # Użyj FAL Smart Turn
                    if FAL_KEY and audio_buffer.duration_seconds() > 0.5:
                        wav_b64 = audio_buffer.get_wav_base64()
                        if wav_b64:
                            result = await smart_turn.predict(wav_b64)
                            if result["end_of_turn"]:
                                logger.debug(f"🧠 Smart Turn: END (prob={result['probability']:.2f})")
                                await process_turn()
                            else:
                                logger.debug(f"🧠 Smart Turn: CONTINUE (prob={result['probability']:.2f})")
                        else:
                            # Fallback
                            if silence_ms >= FALLBACK_TIMEOUT_MS:
                                await process_turn()
                    else:
                        # Bez FAL - fallback na timeout
                        if silence_ms >= FALLBACK_TIMEOUT_MS:
                            await process_turn()
    
    turn_task = asyncio.create_task(turn_checker())
    
    try:
        async for message in websocket.iter_json():
            event = message.get("event")
            
            if event == "start":
                stream_sid = message.get("streamSid")
                logger.info(f"▶️ Stream started: {stream_sid}")
                
                if greeting_audio and stream_sid:
                    payload = base64.b64encode(greeting_audio).decode("utf-8")
                    await websocket.send_json({
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": payload}
                    })
                    
            elif event == "media":
                payload = message.get("media", {}).get("payload", "")
                if payload:
                    audio = base64.b64decode(payload)
                    await stt.send(audio)
                    audio_buffer.add(audio)
                    
            elif event == "stop":
                logger.info(f"⏹️ Stream stopped")
                break
                
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        turn_task.cancel()
        await stt.close()
        await save_call_log(tenant["id"], caller, called, started_at, context.transcript, booking_id)
        logger.info(f"👋 Closed")


# ==========================================
# HEALTH CHECK
# ==========================================
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "3.0-fal",
        "smart_turn": "fal-api" if FAL_KEY else "fallback",
        "features": ["fal-smart-turn", "deepgram-nova3", "elevenlabs"]
    }


@app.get("/")
async def root():
    return {"message": "Voice AI v3.0 - FAL Smart Turn", "status": "running"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)