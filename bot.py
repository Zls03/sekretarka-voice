"""
VOICE AI - PRODUCTION ARCHITECTURE
==================================
Zero halucynacji. 100% kontrola. Polski rynek.

Zasada: GPT = tylko JSON (intencje), Backend = fakty
"""

import os
import json
import base64
import asyncio
import aiohttp
import httpx
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

app = FastAPI(title="Voice AI Production")
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
                return []
        except Exception as e:
            logger.error(f"Turso error: {e}")
            return []

db = TursoDB()


# ==========================================
# STATE MACHINE - PEŁNY FLOW
# ==========================================
class State(Enum):
    START = "start"
    LISTENING = "listening"
    
    # Flow rezerwacji
    ASK_SERVICE = "ask_service"
    ASK_STAFF = "ask_staff"          # Opcjonalnie - wybór pracownika
    ASK_DATE = "ask_date"
    CHECK_AVAILABILITY = "check_availability"
    PROPOSE_SLOTS = "propose_slots"
    ASK_TIME = "ask_time"
    CONFIRM_BOOKING = "confirm_booking"
    ASK_NAME = "ask_name"            # Imię do rezerwacji
    BOOKING_DONE = "booking_done"
    
    # Inne
    ANSWERING = "answering"
    END = "end"


@dataclass
class Conversation:
    """Pełny kontekst rozmowy"""
    tenant: Dict[str, Any]
    call_sid: str = ""
    caller_phone: str = ""
    state: State = State.START
    
    # Timing (do liczenia minut)
    started_at: datetime = field(default_factory=datetime.utcnow)
    ended_at: Optional[datetime] = None
    
    # Zebrane dane do rezerwacji
    selected_service: Optional[Dict] = None
    selected_staff: Optional[Dict] = None
    selected_date: Optional[str] = None      # "2026-01-25"
    selected_time: Optional[str] = None      # "14:00"
    available_slots: List[str] = field(default_factory=list)
    customer_name: Optional[str] = None
    
    # Historia
    transcript: List[Dict] = field(default_factory=list)
    intents_log: List[Dict] = field(default_factory=list)


conversations: Dict[str, Conversation] = {}


# ==========================================
# INTENCJE - GPT ZWRACA TYLKO JSON
# ==========================================
INTENT_SYSTEM_PROMPT = """Jesteś parserem intencji dla asystenta głosowego salonu usługowego.
Twoim JEDYNYM zadaniem jest rozpoznanie intencji i wyekstrahowanie danych.

NIGDY nie generuj tekstu do klienta.
NIGDY nie podawaj godzin, dat, cen.
Zwróć TYLKO poprawny JSON.

Dostępne intencje:
- greeting: powitanie
- ask_services: pytanie o usługi/cennik
- ask_hours: pytanie o godziny otwarcia
- ask_address: pytanie o adres/lokalizację
- ask_price: pytanie o cenę konkretnej usługi
- ask_payment: pytanie o metody płatności
- book: chęć umówienia wizyty
- select_service: wybór usługi (wyekstrahuj nazwę)
- select_staff: wybór pracownika (wyekstrahuj imię)
- select_date: podanie daty (wyekstrahuj: "jutro", "pojutrze", "w piątek", "15 stycznia", itp.)
- select_time: podanie godziny (wyekstrahuj: "o 14", "na 10:30", "rano", "po południu")
- confirm: potwierdzenie (tak, zgadza się, potwierdzam)
- deny: zaprzeczenie (nie, jednak nie, zmieniam zdanie)
- give_name: podanie imienia
- cancel: anulowanie rezerwacji
- reschedule: przełożenie rezerwacji
- goodbye: pożegnanie
- other: coś innego

Odpowiedz TYLKO tym JSON:
{
  "intent": "nazwa_intencji",
  "service": "nazwa usługi jeśli wspomniana",
  "staff": "imię pracownika jeśli wspomniane",
  "date": "data jeśli wspomniana (surowy tekst)",
  "time": "godzina jeśli wspomniana (surowy tekst)",
  "name": "imię klienta jeśli podane"
}"""


async def detect_intent(text: str, context: str = "") -> Dict:
    """GPT zwraca TYLKO JSON z intencją i danymi"""
    try:
        messages = [
            {"role": "system", "content": INTENT_SYSTEM_PROMPT},
        ]
        
        if context:
            messages.append({"role": "user", "content": f"Kontekst rozmowy: {context}"})
        
        messages.append({"role": "user", "content": f"Tekst klienta: {text}"})
        
        response = oai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=150,
            temperature=0,
            response_format={"type": "json_object"}
        )
        
        result = json.loads(response.choices[0].message.content)
        logger.info(f"🎯 Intent JSON: {result}")
        return result
        
    except Exception as e:
        logger.error(f"Intent error: {e}")
        return {"intent": "other"}


# ==========================================
# PARSER DAT I GODZIN
# ==========================================
def parse_polish_date(text: str) -> Optional[str]:
    """Parsuj polskie daty na format YYYY-MM-DD"""
    text = text.lower().strip()
    today = datetime.now()
    
    # Względne daty
    if "dziś" in text or "dzisiaj" in text:
        return today.strftime("%Y-%m-%d")
    if "jutro" in text:
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")
    if "pojutrze" in text:
        return (today + timedelta(days=2)).strftime("%Y-%m-%d")
    
    # Dni tygodnia
    days_pl = {
        "poniedziałek": 0, "poniedzialek": 0,
        "wtorek": 1,
        "środa": 2, "sroda": 2,
        "czwartek": 3,
        "piątek": 4, "piatek": 4,
        "sobota": 5,
        "niedziela": 6, "niedziele": 6
    }
    
    for day_name, day_num in days_pl.items():
        if day_name in text:
            days_ahead = day_num - today.weekday()
            if days_ahead <= 0:
                days_ahead += 7
            return (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    
    # TODO: "15 stycznia", "za tydzień" itp.
    return None


def parse_polish_time(text: str) -> Optional[str]:
    """Parsuj polskie godziny na format HH:MM"""
    import re
    text = text.lower().strip()
    
    # "o 14", "na 14:30", "o czternastej"
    match = re.search(r'(\d{1,2})[:\.]?(\d{2})?', text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2)) if match.group(2) else 0
        if 6 <= hour <= 21:
            return f"{hour:02d}:{minute:02d}"
    
    # Słowne
    time_words = {
        "rano": "09:00",
        "przed południem": "10:00",
        "w południe": "12:00",
        "po południu": "14:00",
        "wieczorem": "17:00"
    }
    
    for word, time_val in time_words.items():
        if word in text:
            return time_val
    
    return None


# ==========================================
# FORMATOWANIE PO POLSKU (TTS)
# ==========================================
def format_hour_polish(time_str: str) -> str:
    """09:00 → dziewiątej"""
    hour_words = {
        6: "szóstej", 7: "siódmej", 8: "ósmej", 9: "dziewiątej",
        10: "dziesiątej", 11: "jedenastej", 12: "dwunastej",
        13: "trzynastej", 14: "czternastej", 15: "piętnastej",
        16: "szesnastej", 17: "siedemnastej", 18: "osiemnastej",
        19: "dziewiętnastej", 20: "dwudziestej"
    }
    if not time_str:
        return ""
    try:
        hour = int(time_str.split(":")[0])
        return hour_words.get(hour, f"godzina {hour}")
    except:
        return time_str


def format_price_polish(price) -> str:
    """50 → pięćdziesiąt złotych"""
    try:
        price = int(float(price))
    except:
        return f"{price} złotych"
    
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


def format_date_polish(date_str: str) -> str:
    """2026-01-25 → sobotę dwudziestego piątego stycznia"""
    try:
        date = datetime.strptime(date_str, "%Y-%m-%d")
        today = datetime.now().date()
        
        if date.date() == today:
            return "dzisiaj"
        if date.date() == today + timedelta(days=1):
            return "jutro"
        if date.date() == today + timedelta(days=2):
            return "pojutrze"
        
        days = ["poniedziałek", "wtorek", "środę", "czwartek", "piątek", "sobotę", "niedzielę"]
        return days[date.weekday()]
    except:
        return date_str


# ==========================================
# DOSTĘPNOŚĆ (MOCK - później Google Calendar)
# ==========================================
async def get_available_slots(tenant_id: str, date: str, service_duration: int, staff_id: str = None) -> List[str]:
    """
    Pobierz dostępne sloty na dany dzień.
    TODO: Integracja z Google Calendar
    """
    # MOCK - zwróć przykładowe sloty
    # W produkcji: sprawdź Google Calendar pracownika
    
    try:
        date_obj = datetime.strptime(date, "%Y-%m-%d")
        day_of_week = date_obj.weekday()
        
        # Pobierz godziny pracy
        hours = await db.execute(
            "SELECT open_time, close_time FROM working_hours WHERE tenant_id = ? AND day_of_week = ?",
            [tenant_id, day_of_week]
        )
        
        if not hours or not hours[0].get("open_time"):
            return []  # Zamknięte
        
        open_time = hours[0]["open_time"]
        close_time = hours[0]["close_time"]
        
        # Generuj sloty co 30 min
        slots = []
        current = datetime.strptime(open_time, "%H:%M")
        end = datetime.strptime(close_time, "%H:%M")
        
        while current + timedelta(minutes=service_duration) <= end:
            slots.append(current.strftime("%H:%M"))
            current += timedelta(minutes=30)
        
        # TODO: Odfiltruj zajęte sloty z Google Calendar
        
        return slots[:5]  # Max 5 propozycji
        
    except Exception as e:
        logger.error(f"Slots error: {e}")
        return ["10:00", "11:00", "14:00", "15:00"]  # Fallback


async def create_booking(conv: Conversation) -> bool:
    """
    Zapisz rezerwację do bazy.
    TODO: Dodaj do Google Calendar
    """
    try:
        booking_id = f"book_{int(datetime.utcnow().timestamp())}"
        
        await db.execute(
            """INSERT INTO bookings 
               (id, tenant_id, service_id, staff_id, customer_name, customer_phone, 
                booking_date, booking_time, duration_minutes, status, call_sid, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, datetime('now'))""",
            [
                booking_id,
                conv.tenant["id"],
                conv.selected_service["id"] if conv.selected_service else None,
                conv.selected_staff["id"] if conv.selected_staff else None,
                conv.customer_name,
                conv.caller_phone,
                conv.selected_date,
                conv.selected_time,
                conv.selected_service.get("duration_minutes", 30) if conv.selected_service else 30,
                conv.call_sid
            ]
        )
        
        logger.info(f"✅ Booking created: {booking_id}")
        return True
        
    except Exception as e:
        logger.error(f"Booking error: {e}")
        return False


# ==========================================
# CALL LOGGING (liczenie minut)
# ==========================================
async def save_call_log(conv: Conversation):
    """Zapisz log rozmowy z czasem trwania"""
    try:
        conv.ended_at = datetime.utcnow()
        duration = int((conv.ended_at - conv.started_at).total_seconds())
        
        log_id = f"call_{int(datetime.utcnow().timestamp())}"
        
        await db.execute(
            """INSERT INTO call_logs 
               (id, tenant_id, call_sid, caller_phone, started_at, ended_at, 
                duration_seconds, transcript, intents_log, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', datetime('now'))""",
            [
                log_id,
                conv.tenant["id"],
                conv.call_sid,
                conv.caller_phone,
                conv.started_at.isoformat(),
                conv.ended_at.isoformat(),
                duration,
                json.dumps(conv.transcript, ensure_ascii=False),
                json.dumps(conv.intents_log, ensure_ascii=False),
            ]
        )
        
        logger.info(f"📊 Call logged: {duration}s ({duration//60}m {duration%60}s)")
        
    except Exception as e:
        logger.error(f"Call log error: {e}")


# ==========================================
# RESPONSE GENERATOR (STATE MACHINE)
# ==========================================
async def process_conversation(conv: Conversation, intent_data: Dict, user_text: str) -> str:
    """
    SERCE SYSTEMU - State Machine
    Backend generuje WSZYSTKIE odpowiedzi z danych.
    GPT NIE generuje tekstu do klienta!
    """
    
    tenant = conv.tenant
    intent = intent_data.get("intent", "other")
    
    # Zapisz intent do logu
    conv.intents_log.append({
        "text": user_text,
        "intent": intent_data,
        "state": conv.state.value,
        "timestamp": datetime.utcnow().isoformat()
    })
    
    # === GLOBALNE INTENCJE (działają w każdym stanie) ===
    
    if intent == "goodbye":
        conv.state = State.END
        return "Dziękuję za telefon. Do usłyszenia!"
    
    if intent == "ask_hours":
        return generate_hours_response(tenant)
    
    if intent == "ask_address":
        address = tenant.get("address", "")
        return f"Znajdujemy się pod adresem {address}." if address else "Przepraszam, nie mam informacji o adresie."
    
    if intent == "ask_services" or intent == "ask_price":
        return generate_services_response(tenant, intent_data.get("service"))
    
    # === STATE MACHINE ===
    
    # Stan: LISTENING (początkowy)
    if conv.state in [State.START, State.LISTENING]:
        
        if intent == "greeting":
            return f"Dzień dobry! W czym mogę pomóc?"
        
        if intent == "book":
            conv.state = State.ASK_SERVICE
            services = tenant.get("services", [])
            svc_names = [s['name'] for s in services]
            return f"Chętnie umówię wizytę. Na jaką usługę? Mamy: {', '.join(svc_names)}."
        
        return "Jak mogę pomóc? Mogę umówić wizytę, podać godziny otwarcia lub cennik usług."
    
    # Stan: ASK_SERVICE
    if conv.state == State.ASK_SERVICE:
        service_name = intent_data.get("service") or user_text
        service = find_service(tenant, service_name)
        
        if service:
            conv.selected_service = service
            conv.state = State.ASK_DATE
            price = format_price_polish(service['price'])
            return f"Świetnie, {service['name']} za {price}. Na kiedy chcesz się umówić?"
        else:
            services = tenant.get("services", [])
            svc_names = [s['name'] for s in services]
            return f"Nie rozpoznałam usługi. Mamy: {', '.join(svc_names)}. Którą wybrać?"
    
    # Stan: ASK_DATE
    if conv.state == State.ASK_DATE:
        date_text = intent_data.get("date") or user_text
        parsed_date = parse_polish_date(date_text)
        
        if parsed_date:
            conv.selected_date = parsed_date
            conv.state = State.CHECK_AVAILABILITY
            
            # Sprawdź dostępność
            duration = conv.selected_service.get("duration_minutes", 30) if conv.selected_service else 30
            slots = await get_available_slots(tenant["id"], parsed_date, duration)
            
            if slots:
                conv.available_slots = slots
                conv.state = State.ASK_TIME
                date_pl = format_date_polish(parsed_date)
                slots_text = ", ".join([format_hour_polish(s) for s in slots[:3]])
                return f"Na {date_pl} mam wolne terminy: {slots_text}. Która godzina pasuje?"
            else:
                conv.state = State.ASK_DATE
                return f"Niestety na {format_date_polish(parsed_date)} nie mamy wolnych terminów. Może inny dzień?"
        else:
            return "Nie zrozumiałam daty. Powiedz na przykład: jutro, w piątek, albo podaj konkretną datę."
    
    # Stan: ASK_TIME
    if conv.state == State.ASK_TIME:
        time_text = intent_data.get("time") or user_text
        parsed_time = parse_polish_time(time_text)
        
        if parsed_time and parsed_time in conv.available_slots:
            conv.selected_time = parsed_time
            conv.state = State.CONFIRM_BOOKING
            
            svc_name = conv.selected_service['name'] if conv.selected_service else "wizytę"
            date_pl = format_date_polish(conv.selected_date)
            time_pl = format_hour_polish(parsed_time)
            
            return f"Podsumowuję: {svc_name} na {date_pl} o {time_pl}. Czy potwierdzasz rezerwację?"
        
        elif parsed_time:
            # Godzina poza dostępnymi
            slots_text = ", ".join([format_hour_polish(s) for s in conv.available_slots[:3]])
            return f"Ta godzina jest zajęta. Dostępne terminy to: {slots_text}. Którą wybrać?"
        else:
            return "Nie zrozumiałam godziny. Powiedz na przykład: o dziesiątej, na czternastą, o 14:30."
    
    # Stan: CONFIRM_BOOKING
    if conv.state == State.CONFIRM_BOOKING:
        if intent == "confirm":
            # Zapisz rezerwację
            success = await create_booking(conv)
            
            if success:
                conv.state = State.BOOKING_DONE
                svc_name = conv.selected_service['name'] if conv.selected_service else "Wizyta"
                date_pl = format_date_polish(conv.selected_date)
                time_pl = format_hour_polish(conv.selected_time)
                return f"Gotowe! {svc_name} zarezerwowana na {date_pl} o {time_pl}. Dziękuję i do zobaczenia!"
            else:
                return "Przepraszam, wystąpił błąd. Spróbuj ponownie lub zadzwoń później."
        
        elif intent == "deny":
            conv.state = State.LISTENING
            conv.selected_service = None
            conv.selected_date = None
            conv.selected_time = None
            return "W porządku, rezerwacja anulowana. Jak jeszcze mogę pomóc?"
        
        else:
            return "Czy potwierdzasz rezerwację? Powiedz tak lub nie."
    
    # Stan: BOOKING_DONE
    if conv.state == State.BOOKING_DONE:
        if intent == "book":
            conv.state = State.ASK_SERVICE
            conv.selected_service = None
            conv.selected_date = None
            conv.selected_time = None
            services = tenant.get("services", [])
            svc_names = [s['name'] for s in services]
            return f"Chętnie umówię kolejną wizytę. Na jaką usługę? Mamy: {', '.join(svc_names)}."
        
        return "Dziękuję! Czy mogę jeszcze w czymś pomóc?"
    
    # Fallback
    return "Przepraszam, nie zrozumiałam. Mogę pomóc z umówieniem wizyty, godzinami otwarcia lub cennikiem."


# ==========================================
# HELPER FUNCTIONS
# ==========================================
def find_service(tenant: Dict, text: str) -> Optional[Dict]:
    """Znajdź usługę po nazwie"""
    if not text:
        return None
    
    text_lower = text.lower()
    services = tenant.get("services", [])
    
    for svc in services:
        svc_name_lower = svc['name'].lower()
        # Dokładne dopasowanie lub częściowe
        if svc_name_lower in text_lower or text_lower in svc_name_lower:
            return svc
        # Sprawdź słowa
        for word in svc_name_lower.split():
            if len(word) > 3 and word in text_lower:
                return svc
    
    return None


def generate_hours_response(tenant: Dict) -> str:
    """Generuj odpowiedź o godzinach"""
    hours = tenant.get("working_hours", {})
    parts = []
    
    weekday = hours.get(0)
    if weekday:
        open_h = format_hour_polish(weekday['open'])
        close_h = format_hour_polish(weekday['close'])
        parts.append(f"od poniedziałku do piątku od {open_h} do {close_h}")
    
    saturday = hours.get(5)
    if saturday:
        open_h = format_hour_polish(saturday['open'])
        close_h = format_hour_polish(saturday['close'])
        parts.append(f"w soboty od {open_h} do {close_h}")
    
    if hours.get(6) is None:
        parts.append("w niedziele zamknięte")
    
    return "Pracujemy " + ", ".join(parts) + "." if parts else "Przepraszam, nie mam informacji o godzinach."


def generate_services_response(tenant: Dict, specific_service: str = None) -> str:
    """Generuj odpowiedź o usługach/cenach"""
    services = tenant.get("services", [])
    
    if not services:
        return "Przepraszam, nie mam informacji o usługach."
    
    # Pytanie o konkretną usługę
    if specific_service:
        svc = find_service(tenant, specific_service)
        if svc:
            price = format_price_polish(svc['price'])
            duration = svc.get('duration_minutes', 30)
            return f"{svc['name']} kosztuje {price}, trwa około {duration} minut. Chcesz się umówić?"
    
    # Lista wszystkich usług
    svc_list = [f"{s['name']} za {format_price_polish(s['price'])}" for s in services]
    return "Oferujemy: " + ", ".join(svc_list) + ". Na którą usługę chcesz się umówić?"


# ==========================================
# TTS & STT (bez zmian)
# ==========================================
async def text_to_speech(text: str) -> bytes:
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}/stream?output_format=ulaw_8000&optimize_streaming_latency=3"
    
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json"
            },
            json={"text": text, "model_id": "eleven_flash_v2_5"}
        ) as response:
            if response.status == 200:
                audio = await response.read()
                logger.info(f"🔊 TTS: {len(audio)} bytes")
                return audio
            logger.error(f"TTS error: {response.status}")
            return b""


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
# TWILIO & TENANT
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


async def send_audio(ws: WebSocket, audio: bytes, stream_sid: str):
    if audio and stream_sid:
        await ws.send_text(json.dumps({
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": base64.b64encode(audio).decode("ascii")}
        }))


# ==========================================
# ENDPOINTS
# ==========================================
@app.get("/health")
async def health():
    return {"status": "ok", "version": "2.0", "service": "Voice AI Production"}


@app.post("/twilio/incoming")
async def incoming(request: Request):
    host = request.headers.get("host", "localhost")
    form = await request.form()
    
    called = form.get("Called", "")
    caller = form.get("From", "")
    call_sid = form.get("CallSid", "")
    
    logger.info(f"📞 Call: {caller} → {called}")
    
    tenant = await get_tenant_by_phone(called)
    
    if not tenant:
        return Response(
            content='<?xml version="1.0"?><Response><Say language="pl-PL">Przepraszamy, ten numer nie jest aktywny.</Say></Response>',
            media_type="application/xml"
        )
    
    logger.info(f"✅ Tenant: {tenant['name']}")
    
    conversations[call_sid] = Conversation(
        tenant=tenant,
        call_sid=call_sid,
        caller_phone=caller,
        started_at=datetime.utcnow()
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
        
        conv.transcript.append({"role": "user", "text": text, "time": datetime.utcnow().isoformat()})
        
        # Kontekst dla GPT (ostatnie 3 wymiany)
        context = " | ".join([f"{t['role']}: {t['text']}" for t in conv.transcript[-6:]])
        
        # GPT → tylko JSON
        intent_data = await detect_intent(text, context)
        
        # Backend → odpowiedź
        response = await process_conversation(conv, intent_data, text)
        conv.transcript.append({"role": "assistant", "text": response, "time": datetime.utcnow().isoformat()})
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
                
                conv = conversations.get(call_sid)
                if not conv:
                    break
                
                await stt.connect()
                
                greeting = f"Dzień dobry, tu {conv.tenant['name']}. W czym mogę pomóc?"
                conv.transcript.append({"role": "assistant", "text": greeting, "time": datetime.utcnow().isoformat()})
                
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
        
        # Zapisz log rozmowy
        if conv:
            await save_call_log(conv)
        
        if call_sid and call_sid in conversations:
            del conversations[call_sid]
        
        logger.info("👋 Closed")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8765)))