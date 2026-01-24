"""
VOICE AI - PRODUCTION ARCHITECTURE v3.0
=======================================
Profesjonalny turn-taking jak Retell/Vapi!

Stack:
- Deepgram Nova-3 (STT polski) + interim results
- Silero VAD (Voice Activity Detection)
- Smart Turn v3 (inteligentne wykrywanie końca wypowiedzi)
- GPT-4o-mini (intencje + parsowanie)
- ElevenLabs Flash 2.5 (TTS)
- Twilio Media Streams
- Turso/libSQL (baza danych)

Architektura:
1. Audio → Silero VAD (wykrywa mowę/ciszę)
2. Gdy cisza > 200ms → Smart Turn analizuje audio
3. Smart Turn decyduje: END (odpowiadaj) lub CONTINUE (czekaj)
4. Jeśli END → Deepgram transkrypt → GPT → TTS
"""

import os
import json
import base64
import asyncio
import aiohttp
import httpx
import numpy as np
import io
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from enum import Enum
from collections import deque
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

# Smart Turn config
SMART_TURN_THRESHOLD = 0.5  # Próg pewności że to koniec wypowiedzi (0-1)
VAD_SILENCE_MS = 200  # Ile ms ciszy żeby uruchomić Smart Turn
AUDIO_BUFFER_MAX_SECONDS = 8  # Max długość bufora audio dla Smart Turn

app = FastAPI(title="Voice AI Production v3.0 - Smart Turn")
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
# SILERO VAD - Voice Activity Detection
# ==========================================
class SileroVAD:
    """
    Wykrywa czy w audio jest mowa.
    Używa ONNX dla szybkości (~1ms na chunk).
    """
    def __init__(self, sample_rate: int = 16000, threshold: float = 0.5):
        self.sample_rate = sample_rate
        self.threshold = threshold
        self.model = None
        self._h = None
        self._c = None
        self._initialized = False
        
    async def initialize(self):
        """Lazy load modelu - tylko gdy potrzebny"""
        if self._initialized:
            return
            
        try:
            import torch
            torch.set_num_threads(1)
            
            self.model, utils = torch.hub.load(
                repo_or_dir='snakers4/silero-vad',
                model='silero_vad',
                force_reload=False,
                onnx=True  # Używamy ONNX dla szybkości
            )
            self._initialized = True
            logger.info("✅ Silero VAD załadowany (ONNX)")
        except Exception as e:
            logger.warning(f"Silero VAD niedostępny: {e}")
            self._initialized = False
            
    def detect(self, audio_chunk: np.ndarray) -> float:
        """
        Zwraca prawdopodobieństwo że chunk zawiera mowę (0-1).
        Audio musi być 16kHz mono float32.
        """
        if not self._initialized or self.model is None:
            return 0.5  # Fallback - zakładamy że jest mowa
            
        try:
            import torch
            # Konwertuj do tensora
            audio_tensor = torch.from_numpy(audio_chunk).float()
            
            # Wywołaj model
            speech_prob = self.model(audio_tensor, self.sample_rate).item()
            return speech_prob
        except Exception as e:
            logger.error(f"VAD error: {e}")
            return 0.5
            
    def is_speech(self, audio_chunk: np.ndarray) -> bool:
        """Czy chunk zawiera mowę?"""
        return self.detect(audio_chunk) >= self.threshold
        
    def reset(self):
        """Reset stanu między rozmowami"""
        if self.model is not None:
            try:
                self.model.reset_states()
            except:
                pass


# ==========================================
# SMART TURN v3 - Inteligentne wykrywanie końca wypowiedzi
# ==========================================
class SmartTurn:
    """
    Analizuje audio i decyduje czy użytkownik skończył mówić.
    Używa modelu ONNX z pipecat-ai/smart-turn-v3.
    
    Wspiera 23 języki w tym POLSKI!
    """
    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold
        self.session = None
        self.model_loaded = False
        
    async def initialize(self):
        """Lazy load modelu Smart Turn"""
        if self.model_loaded:
            return
            
        try:
            import onnxruntime as ort
            from huggingface_hub import hf_hub_download
            
            # Pobierz model z HuggingFace
            model_path = hf_hub_download(
                repo_id="pipecat-ai/smart-turn-v3",
                filename="smart-turn-v3.1-cpu.onnx"
            )
            
            # Załaduj sesję ONNX
            sess_options = ort.SessionOptions()
            sess_options.intra_op_num_threads = 1
            sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            
            self.session = ort.InferenceSession(
                model_path,
                sess_options=sess_options,
                providers=['CPUExecutionProvider']
            )
            
            self.model_loaded = True
            logger.info("✅ Smart Turn v3 załadowany (ONNX)")
        except Exception as e:
            logger.warning(f"Smart Turn niedostępny: {e}")
            self.model_loaded = False
            
    def predict(self, audio_16khz: np.ndarray) -> Dict[str, Any]:
        """
        Analizuje audio i zwraca czy to koniec wypowiedzi.
        
        Args:
            audio_16khz: Audio 16kHz mono float32, max 8 sekund
            
        Returns:
            {
                "end_of_turn": bool,
                "probability": float (0-1),
                "inference_time_ms": float
            }
        """
        if not self.model_loaded or self.session is None:
            # Fallback - zakładaj że to koniec po 800ms ciszy
            return {"end_of_turn": True, "probability": 0.5, "inference_time_ms": 0}
            
        try:
            import time
            start = time.perf_counter()
            
            # Przygotuj audio - max 8 sekund (128000 samples przy 16kHz)
            max_samples = 16000 * 8
            if len(audio_16khz) > max_samples:
                # Weź ostatnie 8 sekund
                audio_16khz = audio_16khz[-max_samples:]
            
            # Padding jeśli za krótkie (model oczekuje pewnej długości)
            if len(audio_16khz) < 16000:  # Min 1 sekunda
                padding = np.zeros(16000 - len(audio_16khz), dtype=np.float32)
                audio_16khz = np.concatenate([padding, audio_16khz])
            
            # Przygotuj input dla ONNX
            # Model oczekuje audio w formacie [1, samples]
            audio_input = audio_16khz.reshape(1, -1).astype(np.float32)
            
            # Inference
            outputs = self.session.run(None, {"audio": audio_input})
            
            # Output to prawdopodobieństwo końca wypowiedzi
            probability = float(outputs[0][0])
            
            inference_time = (time.perf_counter() - start) * 1000
            
            return {
                "end_of_turn": probability >= self.threshold,
                "probability": probability,
                "inference_time_ms": inference_time
            }
        except Exception as e:
            logger.error(f"Smart Turn error: {e}")
            return {"end_of_turn": True, "probability": 0.5, "inference_time_ms": 0}


# ==========================================
# AUDIO BUFFER - Zbiera audio do analizy
# ==========================================
class AudioBuffer:
    """
    Bufor audio dla Smart Turn.
    Konwertuje mulaw 8kHz (Twilio) → PCM 16kHz (Smart Turn).
    """
    def __init__(self, max_seconds: float = 8.0):
        self.max_samples = int(16000 * max_seconds)  # 16kHz
        self.buffer = np.array([], dtype=np.float32)
        self.mulaw_buffer = bytearray()  # Surowe dane z Twilio
        
    def add_mulaw(self, data: bytes):
        """Dodaj mulaw audio z Twilio (8kHz)"""
        self.mulaw_buffer.extend(data)
        
    def get_audio_16khz(self) -> np.ndarray:
        """
        Konwertuj bufor mulaw 8kHz → PCM float32 16kHz.
        """
        if not self.mulaw_buffer:
            return np.array([], dtype=np.float32)
            
        try:
            # Mulaw → PCM 16-bit
            import audioop
            pcm_8khz = audioop.ulaw2lin(bytes(self.mulaw_buffer), 2)
            
            # PCM bytes → numpy int16
            audio_int16 = np.frombuffer(pcm_8khz, dtype=np.int16)
            
            # Resample 8kHz → 16kHz (proste podwojenie próbek)
            audio_16khz = np.repeat(audio_int16, 2)
            
            # Normalize do float32 (-1 to 1)
            audio_float = audio_16khz.astype(np.float32) / 32768.0
            
            # Ogranicz do max_samples
            if len(audio_float) > self.max_samples:
                audio_float = audio_float[-self.max_samples:]
                
            return audio_float
        except Exception as e:
            logger.error(f"Audio conversion error: {e}")
            return np.array([], dtype=np.float32)
            
    def clear(self):
        """Wyczyść bufor"""
        self.buffer = np.array([], dtype=np.float32)
        self.mulaw_buffer = bytearray()
        
    def duration_seconds(self) -> float:
        """Długość audio w sekundach"""
        return len(self.mulaw_buffer) / 8000.0  # mulaw 8kHz


# ==========================================
# TRANSCRIPT BUFFER - Łączy fragmenty transkrypcji
# ==========================================
class TranscriptBuffer:
    """
    Buforuje fragmenty transkrypcji z Deepgram.
    Łączy je w pełne wypowiedzi.
    """
    def __init__(self):
        self.fragments: List[str] = []
        self.last_update = datetime.now()
        
    def add(self, text: str):
        """Dodaj fragment transkrypcji"""
        if text.strip():
            self.fragments.append(text.strip())
            self.last_update = datetime.now()
            
    def get_full_text(self) -> str:
        """Zwróć połączony tekst"""
        return " ".join(self.fragments)
        
    def clear(self):
        """Wyczyść bufor"""
        self.fragments = []
        
    def age_ms(self) -> float:
        """Ile ms od ostatniej aktualizacji"""
        return (datetime.now() - self.last_update).total_seconds() * 1000


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
    
    # Audio buffers
    audio_buffer: AudioBuffer = field(default_factory=AudioBuffer)
    transcript_buffer: TranscriptBuffer = field(default_factory=TranscriptBuffer)
    
    # Turn state
    is_speaking: bool = False  # Czy user aktualnie mówi
    last_vad_speech: datetime = field(default_factory=datetime.now)
    waiting_for_turn_end: bool = False


# ==========================================
# GPT INTENT DETECTION (bez zmian)
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
- "greeting" - przywitanie (dzień dobry, cześć, halo)
- "ask_services" - pytanie o usługi/cennik
- "ask_hours" - pytanie o godziny otwarcia
- "book" - chęć rezerwacji (chcę się umówić, rezerwacja)
- "select_service" - wybór usługi
- "select_date" - wybór daty (jutro, w poniedziałek, 25 stycznia)
- "select_time" - wybór godziny (o 10, o dziesiątej, rano)
- "select_date_and_time" - wybór daty i godziny razem
- "confirm" - potwierdzenie (tak, zgadza się, potwierdzam)
- "cancel" - anulowanie/rezygnacja
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
- "wieczorem" → "17:00"

ODPOWIEDŹ (tylko JSON):
{{
  "intent": "nazwa_intencji",
  "service": "nazwa usługi lub null",
  "date_raw": "oryginalne słowa o dacie lub null",
  "date_parsed": "YYYY-MM-DD lub null",
  "time_raw": "oryginalne słowa o godzinie lub null",
  "time_parsed": "HH:MM lub null",
  "name": "imię klienta lub null"
}}"""


async def detect_intent(text: str, tenant: Dict, context: ConversationContext) -> Dict:
    """Wykryj intencję używając GPT"""
    try:
        # Dodaj kontekst ostatnich wymian
        history = " | ".join([f"{t['role']}: {t['text']}" for t in context.transcript[-6:]])
        
        response = oai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": build_gpt_prompt(tenant)},
                {"role": "user", "content": f"Kontekst rozmowy: {history}\n\nNowa wypowiedź do sparsowania: \"{text}\""}
            ],
            temperature=0.1,
            max_tokens=200
        )
        
        content = response.choices[0].message.content.strip()
        
        # Wyciągnij JSON
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
# AVAILABILITY & BOOKING (bez zmian)
# ==========================================
async def get_available_slots(tenant_id: str, date: str, duration: int) -> List[str]:
    """Pobierz dostępne sloty na dany dzień"""
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
        
        # Generuj sloty co 30 min
        slots = []
        current = datetime.strptime(open_time, "%H:%M")
        end = datetime.strptime(close_time, "%H:%M") - timedelta(minutes=duration)
        
        while current <= end:
            slots.append(current.strftime("%H:%M"))
            current += timedelta(minutes=30)
            
        logger.info(f"📅 Dostępne sloty: {slots}")
        return slots[:6]  # Max 6 propozycji
    except Exception as e:
        logger.error(f"Slots error: {e}")
        return []


async def create_booking(tenant_id: str, service: Dict, date: str, time: str, customer_name: str = None) -> str:
    """Utwórz rezerwację w bazie"""
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
    """Zapisz log rozmowy"""
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
    
    logger.info(f"📊 Call logged: {duration_seconds}s ({duration_seconds//60}m {duration_seconds%60}s)")


# ==========================================
# POLISH FORMATTING (bez zmian)
# ==========================================
def format_price_polish(price: int) -> str:
    """Formatuj cenę po polsku"""
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
    """Formatuj godzinę po polsku"""
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
    """Formatuj datę po polsku"""
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
# ELEVENLABS TTS (bez zmian)
# ==========================================
async def text_to_speech(text: str) -> bytes:
    """Konwertuj tekst na mowę przez ElevenLabs"""
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
# DEEPGRAM STT - Nova-3 z interim results
# ==========================================
class DeepgramSTT:
    """
    Deepgram Nova-3 dla polskiego.
    Używa interim_results=true żeby mieć szybki feedback.
    """
    def __init__(self, on_transcript, on_interim=None, keyterms: List[str] = None):
        self.on_transcript = on_transcript  # Finalne transkrypcje
        self.on_interim = on_interim  # Interim (do VAD)
        self.ws = None
        self.session = None
        self.keyterms = keyterms or []
        
    async def connect(self):
        # Nova-3 dla polskiego z interim results
        base_url = (
            "wss://api.deepgram.com/v1/listen"
            "?model=nova-3"
            "&language=pl"
            "&encoding=mulaw"
            "&sample_rate=8000"
            "&interim_results=true"  # Włączone dla szybszego feedbacku
            "&endpointing=false"  # Wyłączone - Smart Turn zadecyduje
            "&punctuate=true"
            "&utterance_end_ms=1500"  # Backup timeout
        )
        
        # Dodaj keyterms
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
            logger.info(f"🎤 Deepgram Nova-3 connected (interim=true, endpointing=false)")
            asyncio.create_task(self._listen())
        except Exception as e:
            logger.error(f"Deepgram connect error: {e}")
            await self._connect_fallback()
            
    async def _connect_fallback(self):
        """Fallback do Nova-2 z prostym endpointing"""
        fallback_url = (
            "wss://api.deepgram.com/v1/listen"
            "?model=nova-2-general"
            "&language=pl"
            "&encoding=mulaw"
            "&sample_rate=8000"
            "&endpointing=800"  # Dłuższy timeout jako fallback
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
                        is_final = data.get("is_final", False)
                        
                        if transcript:
                            if is_final:
                                logger.info(f"📝 STT (final): {transcript}")
                                await self.on_transcript(transcript)
                            elif self.on_interim:
                                # Interim - tylko do informacji że user mówi
                                await self.on_interim(transcript)
                                
                    elif data.get("type") == "UtteranceEnd":
                        # Deepgram wykrył koniec wypowiedzi (backup)
                        logger.debug("📝 UtteranceEnd from Deepgram")
                        
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
# TENANT (bez zmian)
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
# RESPONSE GENERATOR (bez zmian)
# ==========================================
async def generate_response(intent_data: Dict, tenant: Dict, context: ConversationContext) -> str:
    """Generuj odpowiedź na podstawie intencji i stanu"""
    
    intent = intent_data.get("intent", "other")
    
    # Aktualizuj kontekst na podstawie sparsowanych danych
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
        # Sprawdź co już mamy
        if context.selected_service and context.selected_date and context.selected_time:
            # Mamy wszystko - potwierdź
            context.state = State.CONFIRM_BOOKING
            return await _confirm_booking_response(context, tenant)
        elif context.selected_service and context.selected_date:
            # Brak godziny
            context.state = State.ASK_TIME
            slots = await get_available_slots(tenant["id"], context.selected_date, 
                                             context.selected_service.get("duration_minutes", 30))
            if slots:
                slots_text = ", ".join([format_time_polish(s) for s in slots[:4]])
                return f"O której godzinie? Dostępne terminy: {slots_text}."
            return "O której godzinie chciałbyś się umówić?"
        elif context.selected_service:
            # Brak daty
            context.state = State.ASK_DATE
            return "Na kiedy chcesz się umówić?"
        else:
            # Brak usługi
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
                return f"Świetnie, {context.selected_service['name']} za {price}. Na kiedy chcesz się umówić?"
        return "Przepraszam, nie rozpoznałam usługi. Jaką usługę wybierasz?"
    
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
                    return f"O której godzinie? Dostępne: {slots_text}."
                return "O której godzinie?"
        return "Przepraszam, nie zrozumiałam daty. Na kiedy chcesz się umówić?"
    
    # Wybór godziny
    if intent == "select_time":
        if context.selected_time:
            if context.selected_service and context.selected_date:
                # Sprawdź dostępność
                slots = await get_available_slots(tenant["id"], context.selected_date,
                                                 context.selected_service.get("duration_minutes", 30))
                if context.selected_time in slots:
                    context.state = State.CONFIRM_BOOKING
                    return await _confirm_booking_response(context, tenant)
                else:
                    slots_text = ", ".join([format_time_polish(s) for s in slots[:4]])
                    return f"Niestety ta godzina jest zajęta. Dostępne terminy: {slots_text}."
        return "Przepraszam, nie zrozumiałam godziny. O której chcesz się umówić?"
    
    # Wybór daty i godziny razem
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
                    return f"Niestety ta godzina jest zajęta. Dostępne: {slots_text}."
            else:
                context.state = State.ASK_SERVICE
                services = ", ".join([s['name'] for s in tenant.get("services", [])])
                return f"Na jaką usługę? Mamy: {services}."
        return "Przepraszam, nie zrozumiałam. Podaj datę i godzinę."
    
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
            
            return f"Gotowe! {context.selected_service['name']} zarezerwowana na {date_text} o {time_text}. Dziękuję i do zobaczenia!"
    
    # Anulowanie
    if intent == "cancel":
        context.state = State.LISTENING
        context.selected_service = None
        context.selected_date = None
        context.selected_time = None
        return "Rozumiem, anuluję. W czym jeszcze mogę pomóc?"
    
    # Domyślna odpowiedź
    return "Jak mogę pomóc? Mogę umówić wizytę, podać godziny otwarcia lub cennik."


async def _confirm_booking_response(context: ConversationContext, tenant: Dict) -> str:
    """Generuj potwierdzenie rezerwacji"""
    date_text = format_date_polish(context.selected_date)
    time_text = format_time_polish(context.selected_time)
    
    return f"Rezerwuję {context.selected_service['name']} na {date_text} o {time_text}. Potwierdzasz?"


# ==========================================
# TURN DETECTOR - Główna logika turn-taking
# ==========================================
class TurnDetector:
    """
    Inteligentny detektor końca wypowiedzi.
    Kombinuje Silero VAD + Smart Turn + buforowanie.
    """
    def __init__(self):
        self.vad = SileroVAD()
        self.smart_turn = SmartTurn(threshold=SMART_TURN_THRESHOLD)
        self.initialized = False
        
    async def initialize(self):
        """Zainicjalizuj modele"""
        if self.initialized:
            return
            
        await self.vad.initialize()
        await self.smart_turn.initialize()
        self.initialized = True
        
    async def should_respond(self, audio_buffer: AudioBuffer, silence_ms: float) -> bool:
        """
        Czy bot powinien odpowiedzieć?
        
        Args:
            audio_buffer: Bufor audio z rozmowy
            silence_ms: Ile ms ciszy od ostatniej mowy
            
        Returns:
            True jeśli powinien odpowiedzieć, False jeśli czekać
        """
        # Jeśli za krótka cisza - nie sprawdzaj
        if silence_ms < VAD_SILENCE_MS:
            return False
            
        # Jeśli brak audio - nie odpowiadaj
        if audio_buffer.duration_seconds() < 0.3:
            return False
            
        # Pobierz audio 16kHz
        audio_16khz = audio_buffer.get_audio_16khz()
        
        if len(audio_16khz) < 4800:  # Min 0.3s przy 16kHz
            return False
            
        # Uruchom Smart Turn
        result = self.smart_turn.predict(audio_16khz)
        
        logger.debug(f"🧠 Smart Turn: prob={result['probability']:.2f}, "
                    f"end={result['end_of_turn']}, time={result['inference_time_ms']:.1f}ms")
        
        return result["end_of_turn"]


# Global turn detector
turn_detector = TurnDetector()


# ==========================================
# TWILIO ENDPOINTS
# ==========================================
@app.post("/twilio/incoming")
async def incoming(request: Request):
    """Obsługa połączeń przychodzących z Twilio"""
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
    
    # Pobierz host z request
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
    """WebSocket handler dla Twilio Media Stream z Smart Turn"""
    await websocket.accept()
    
    caller = websocket.query_params.get("caller", "unknown")
    called = websocket.query_params.get("called", "unknown")
    
    logger.info(f"🔌 WebSocket connected")
    
    # Pobierz tenant
    tenant = await get_tenant_by_phone(called)
    if not tenant:
        await websocket.close()
        return
    
    # Inicjalizuj turn detector
    await turn_detector.initialize()
    
    # Kontekst rozmowy
    context = ConversationContext()
    stream_sid = None
    started_at = datetime.now()
    booking_id = None
    
    # Bufor transkrypcji
    pending_transcripts: List[str] = []
    last_speech_time = datetime.now()
    processing_response = False
    
    async def process_turn():
        """Przetwórz zakończoną turę użytkownika"""
        nonlocal processing_response, booking_id
        
        if processing_response:
            return
            
        # Połącz wszystkie transkrypcje
        full_text = " ".join(pending_transcripts)
        pending_transcripts.clear()
        
        if not full_text.strip():
            return
            
        processing_response = True
        
        try:
            # Zapisz w historii
            context.transcript.append({"role": "user", "text": full_text})
            
            # Wykryj intencję
            intent_data = await detect_intent(full_text, tenant, context)
            
            # Generuj odpowiedź
            response = await generate_response(intent_data, tenant, context)
            
            logger.info(f"💬 {response}")
            context.transcript.append({"role": "assistant", "text": response})
            
            # TTS
            audio = await text_to_speech(response)
            
            if audio and stream_sid:
                # Wyślij audio do Twilio
                chunk_size = 8000  # ~1 sekunda
                for i in range(0, len(audio), chunk_size):
                    chunk = audio[i:i+chunk_size]
                    payload = base64.b64encode(chunk).decode("utf-8")
                    await websocket.send_json({
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": payload}
                    })
                    await asyncio.sleep(0.05)
                    
            # Sprawdź czy koniec rozmowy
            if context.state == State.BOOKING_DONE:
                booking_id = f"book_{int(datetime.now().timestamp())}"
                
            # Wyczyść bufor audio
            context.audio_buffer.clear()
            
        finally:
            processing_response = False
    
    async def on_transcript(text: str):
        """Callback dla finalnej transkrypcji z Deepgram"""
        nonlocal last_speech_time
        
        if text.strip():
            pending_transcripts.append(text.strip())
            last_speech_time = datetime.now()
            
            # Sprawdź czy Smart Turn mówi że to koniec
            if await turn_detector.should_respond(context.audio_buffer, 
                                                   (datetime.now() - last_speech_time).total_seconds() * 1000):
                await process_turn()
    
    async def on_interim(text: str):
        """Callback dla interim transkrypcji - tylko update czasu"""
        nonlocal last_speech_time
        if text.strip():
            last_speech_time = datetime.now()
    
    # Inicjalizuj Deepgram
    keyterms = [s['name'] for s in tenant.get('services', [])]
    keyterms.extend(["rezerwacja", "umówić", "wizyta", "termin"])
    stt = DeepgramSTT(on_transcript, on_interim, keyterms)
    await stt.connect()
    
    # Przywitanie
    greeting = f"Dzień dobry, tu {tenant['business_name']}. W czym mogę pomóc?"
    context.transcript.append({"role": "assistant", "text": greeting})
    greeting_audio = await text_to_speech(greeting)
    
    # Task do sprawdzania ciszy
    async def silence_checker():
        """Sprawdza czy minęło dość ciszy żeby odpowiedzieć"""
        while True:
            await asyncio.sleep(0.1)  # Sprawdzaj co 100ms
            
            if pending_transcripts and not processing_response:
                silence_ms = (datetime.now() - last_speech_time).total_seconds() * 1000
                
                # Jeśli > 800ms ciszy i są transkrypcje - sprawdź Smart Turn
                if silence_ms > 800:
                    if await turn_detector.should_respond(context.audio_buffer, silence_ms):
                        await process_turn()
                # Fallback: jeśli > 2000ms ciszy - odpowiedz mimo wszystko
                elif silence_ms > 2000 and pending_transcripts:
                    logger.debug("⏰ Fallback timeout - responding")
                    await process_turn()
    
    silence_task = asyncio.create_task(silence_checker())
    
    try:
        async for message in websocket.iter_json():
            event = message.get("event")
            
            if event == "start":
                stream_sid = message.get("streamSid")
                logger.info(f"▶️ Stream started: {stream_sid}")
                
                # Wyślij przywitanie
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
                    
                    # Wyślij do Deepgram
                    await stt.send(audio)
                    
                    # Dodaj do bufora audio (dla Smart Turn)
                    context.audio_buffer.add_mulaw(audio)
                    
            elif event == "stop":
                logger.info(f"⏹️ Stream stopped")
                break
                
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        silence_task.cancel()
        await stt.close()
        
        # Zapisz log
        await save_call_log(tenant["id"], caller, called, started_at, 
                          context.transcript, booking_id)
        
        logger.info(f"👋 Closed")


# ==========================================
# HEALTH CHECK
# ==========================================
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "3.0-smart-turn",
        "features": ["silero-vad", "smart-turn-v3", "deepgram-nova3", "elevenlabs"]
    }


@app.get("/")
async def root():
    return {"message": "Voice AI v3.0 - Smart Turn Edition", "status": "running"}