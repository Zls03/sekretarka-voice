"""
VOICE AI v3.1 - HELPERS
=======================
Plik 2/2: helpers.py

Zawiera:
- TursoDB - baza danych
- GPT - wykrywanie intencji
- TTS - ElevenLabs
- Formatowanie po polsku
- State Machine (process_conversation)
- Smart Turn (FAL API)
- Audio/Transcript bufory
"""
from dotenv import load_dotenv
load_dotenv()

import os
import json
import base64
import asyncio
import aiohttp
import httpx
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any
from loguru import logger
import openai

# ==========================================
# KONFIGURACJA
# ==========================================
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "NacdHGUYR1k3M0FAbAia")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")
FAL_KEY = os.getenv("FAL_KEY", "")

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
            logger.warning("DB not configured")
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
# TENANT
# ==========================================
async def get_tenant_by_phone(phone: str) -> Optional[Dict]:
    """Pobierz tenant po numerze telefonu"""
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
    
    # Usługi
    services = await db.execute(
        "SELECT id, name, duration_minutes, price FROM services WHERE tenant_id = ? AND is_active = 1",
        [tenant_id]
    )
    
    # Godziny pracy - jako lista (dla build_business_context)
    hours_rows = await db.execute(
        "SELECT day_of_week, open_time, close_time FROM working_hours WHERE tenant_id = ?",
        [tenant_id]
    )
    working_hours = []
    for h in hours_rows:
        working_hours.append({
            "day_of_week": int(h["day_of_week"]) if h["day_of_week"] else 0,
            "open_time": h["open_time"],
            "close_time": h["close_time"]
        })
    
    # FAQ
    faq_rows = await db.execute(
        "SELECT question, answer FROM tenant_faq WHERE tenant_id = ? ORDER BY sort_order",
        [tenant_id]
    )
    
    # Usługi informacyjne (dla trybu bez rezerwacji)
    info_services = await db.execute(
        "SELECT name, price, description FROM info_services WHERE tenant_id = ? ORDER BY sort_order",
        [tenant_id]
    )
    
    return {
        **tenant,
        "business_name": tenant.get("business_name") or tenant.get("name"),
        "services": services,
        "working_hours": working_hours,
        "faq": faq_rows,
        "is_blocked": int(tenant.get("is_blocked") or 0),
        "minutes_limit": int(tenant.get("minutes_limit") or 100),
        "minutes_used": float(tenant.get("minutes_used") or 0),
        "first_message": tenant.get("first_message") or "Dzień dobry, w czym mogę pomóc?",
        "additional_info": tenant.get("additional_info") or "",
        "industry": tenant.get("industry") or "",
        "booking_enabled": int(tenant.get("booking_enabled") if tenant.get("booking_enabled") is not None else 1),
        "transfer_enabled": int(tenant.get("transfer_enabled") or 0),
        "transfer_number": tenant.get("transfer_number") or "",
        "notification_email": tenant.get("notification_email") or tenant.get("email") or "",
        "info_services": info_services,
    }


# ==========================================
# GPT - INTENCJE
# ==========================================
def get_today_info() -> str:
    today = datetime.now()
    days_pl = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]
    return f"Dziś jest {days_pl[today.weekday()]}, {today.strftime('%Y-%m-%d')}"


INTENT_SYSTEM_PROMPT = """Jesteś parserem języka naturalnego dla asystenta głosowego.

{today_info}

INTENCJE:
- greeting: powitanie
- ask_services: pytanie o usługi/cennik
- ask_hours: pytanie o godziny otwarcia
- book: chęć rezerwacji
- select_service: wybór usługi
- select_date: podanie daty
- select_time: podanie godziny
- select_date_and_time: data I godzina razem
- confirm: potwierdzenie (tak, zgadza się)
- deny: zaprzeczenie (nie)
- cancel: anulowanie
- goodbye: pożegnanie
- other: inne

PARSOWANIE DAT:
- "dziś/dzisiaj" → dzisiejsza data
- "jutro" → +1 dzień
- "pojutrze" → +2 dni
- "w poniedziałek/wtorek/..." → najbliższy taki dzień

PARSOWANIE GODZIN:
- "o dziesiątej" → 10:00
- "o dziewiątej" → 09:00
- "o czternastej" → 14:00
- "rano" → 09:00
- "po południu" → 14:00

ZWRÓĆ TYLKO JSON:
{{
  "intent": "nazwa",
  "service": "usługa lub null",
  "date_raw": "tekst daty lub null",
  "date_parsed": "YYYY-MM-DD lub null",
  "time_raw": "tekst godziny lub null",
  "time_parsed": "HH:MM lub null",
  "name": "imię lub null"
}}"""


async def detect_intent(text: str, context: str = "", services: List[str] = None) -> Dict:
    """GPT parsuje tekst i zwraca intencję + dane"""
    try:
        today = datetime.now()
        tomorrow = (today + timedelta(days=1)).strftime("%Y-%m-%d")
        
        system_prompt = INTENT_SYSTEM_PROMPT.format(today_info=get_today_info())
        
        if services:
            system_prompt += f"\n\nDOSTĘPNE USŁUGI: {', '.join(services)}"
        
        messages = [{"role": "system", "content": system_prompt}]
        
        if context:
            messages.append({"role": "user", "content": f"Kontekst: {context}"})
        
        messages.append({"role": "user", "content": f"Tekst: \"{text}\""})
        
        response = oai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=200,
            temperature=0
        )
        
        result_text = response.choices[0].message.content.strip()
        
        # Wyczyść markdown
        if "```" in result_text:
            result_text = result_text.split("```")[1]
            if result_text.startswith("json"):
                result_text = result_text[4:]
        result_text = result_text.strip()
        
        result = json.loads(result_text)
        logger.info(f"🎯 Intent: {result.get('intent')} | date: {result.get('date_parsed')} | time: {result.get('time_parsed')}")
        return result
        
    except Exception as e:
        logger.error(f"Intent error: {e}")
        return {"intent": "other"}


# ==========================================
# TTS - ELEVENLABS
# ==========================================
async def text_to_speech(text: str) -> bytes:
    """Konwertuj tekst na audio"""
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}/stream?output_format=ulaw_8000&optimize_streaming_latency=3"
    
    try:
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
    except Exception as e:
        logger.error(f"TTS error: {e}")
    return b""


# ==========================================
# FORMATOWANIE PO POLSKU
# ==========================================
def format_hour_polish(time_str: str) -> str:
    """10:00 → dziesiątej, 08:00 → ósmej"""
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
        # Obsługa formatu "08:00" lub "8:00"
        hour = int(time_str.split(":")[0])
        return hour_words.get(hour, f"godzinie {hour}")
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
    """2026-01-27 → poniedziałek"""
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
# DOSTĘPNOŚĆ TERMINÓW
# ==========================================
async def get_available_slots(tenant_id: str, date_str: str, service_duration: int) -> List[str]:
    """Pobierz dostępne sloty"""
    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        day_of_week = date_obj.weekday()
        
        hours = await db.execute(
            "SELECT open_time, close_time FROM working_hours WHERE tenant_id = ? AND day_of_week = ?",
            [tenant_id, day_of_week]
        )
        
        if not hours or not hours[0].get("open_time"):
            return []
        
        open_time = hours[0]["open_time"]
        close_time = hours[0]["close_time"]
        
        slots = []
        current = datetime.strptime(open_time, "%H:%M")
        end = datetime.strptime(close_time, "%H:%M")
        duration = int(service_duration) if service_duration else 30
        
        while current + timedelta(minutes=duration) <= end:
            slots.append(current.strftime("%H:%M"))
            current += timedelta(minutes=30)
        
        logger.info(f"📅 Sloty na {date_str}: {slots}")
        return slots[:6]
        
    except Exception as e:
        logger.error(f"Slots error: {e}")
        return []


async def create_booking(conv) -> bool:
    """Zapisz rezerwację"""
    try:
        booking_id = f"book_{int(datetime.utcnow().timestamp())}"
        
        service_id = conv.selected_service.get("id") if conv.selected_service else None
        duration = conv.selected_service.get("duration_minutes", 30) if conv.selected_service else 30
        
        await db.execute(
            """INSERT INTO bookings 
               (id, tenant_id, service_id, customer_phone, 
                booking_date, booking_time, duration_minutes, status, call_sid, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, datetime('now'))""",
            [
                booking_id,
                conv.tenant["id"],
                service_id,
                conv.caller_phone,
                conv.selected_date,
                conv.selected_time,
                duration,
                conv.call_sid
            ]
        )
        
        logger.info(f"✅ Booking: {booking_id}")
        return True
    except Exception as e:
        logger.error(f"Booking error: {e}")
        return False


async def save_call_log(conv):
    """Zapisz log rozmowy i zaktualizuj zużycie minut"""
    try:
        ended_at = datetime.utcnow()
        duration = int((ended_at - conv.started_at).total_seconds())
        duration_minutes = duration / 60.0
        
        # Zapisz log
        await db.execute(
            """INSERT INTO call_logs 
               (id, tenant_id, call_sid, caller_phone, started_at, ended_at, 
                duration_seconds, transcript, intents_log, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', datetime('now'))""",
            [
                f"call_{int(datetime.utcnow().timestamp())}",
                conv.tenant["id"],
                conv.call_sid,
                conv.caller_phone,
                conv.started_at.isoformat(),
                ended_at.isoformat(),
                duration,
                json.dumps(conv.transcript, ensure_ascii=False),
                json.dumps(conv.intents_log, ensure_ascii=False),
            ]
        )
        
        # Zaktualizuj zużycie minut
        await db.execute(
            "UPDATE tenants SET minutes_used = minutes_used + ? WHERE id = ?",
            [duration_minutes, conv.tenant["id"]]
        )
        
        # Sprawdź czy przekroczono 99% limitu - auto-blokada
        minutes_limit = conv.tenant.get("minutes_limit", 100)
        new_used = conv.tenant.get("minutes_used", 0) + duration_minutes
        
        if new_used >= minutes_limit * 0.99:
            await db.execute(
                "UPDATE tenants SET is_blocked = 1 WHERE id = ?",
                [conv.tenant["id"]]
            )
            logger.warning(f"⚠️ Tenant {conv.tenant['id']} AUTO-BLOCKED - limit reached ({new_used:.1f}/{minutes_limit} min)")
        
        logger.info(f"📊 Call logged: {duration}s ({duration_minutes:.2f} min)")
    except Exception as e:
        logger.error(f"Call log error: {e}")


# ==========================================
# HELPERY
# ==========================================
def find_service(tenant: Dict, text: str) -> Optional[Dict]:
    """Znajdź usługę po nazwie"""
    if not text:
        return None
    
    text_lower = text.lower()
    services = tenant.get("services", [])
    
    for svc in services:
        svc_name_lower = svc['name'].lower()
        if svc_name_lower in text_lower or text_lower in svc_name_lower:
            return svc
        for word in svc_name_lower.split():
            if len(word) > 3 and word in text_lower:
                return svc
    
    return None


# ==========================================
# STATE MACHINE
# ==========================================
async def process_conversation(conv, intent_data: Dict, user_text: str) -> str:
    """Główna logika State Machine"""
    from bot_old import State  # Import tutaj żeby uniknąć circular import
    
    tenant = conv.tenant
    intent = intent_data.get("intent", "other")
    
    # Log intencji
    conv.intents_log.append({
        "text": user_text,
        "intent": intent_data,
        "state": conv.state.value,
        "timestamp": datetime.utcnow().isoformat()
    })
    
    # === GLOBALNE INTENCJE ===
    if intent == "goodbye":
        conv.state = State.END
        return "Dziękuję za telefon. Do usłyszenia!"
    
    if intent == "ask_hours":
        hours = tenant.get("working_hours", {})
        weekday = hours.get(0)
        if weekday:
            open_h = format_hour_polish(weekday['open'])
            close_h = format_hour_polish(weekday['close'])
            return f"Pracujemy od {open_h} do {close_h}."
        return "Przepraszam, nie mam informacji o godzinach."
    
    if intent == "ask_services":
        services = tenant.get("services", [])
        if services:
            svc_list = [f"{s['name']} za {format_price_polish(s['price'])}" for s in services]
            return "Oferujemy: " + ", ".join(svc_list) + ". Chcesz się umówić?"
        return "Przepraszam, nie mam informacji o usługach."
    
    # === STATE: LISTENING ===
    if conv.state in [State.START, State.LISTENING]:
        
        if intent == "greeting":
            return "Dzień dobry! W czym mogę pomóc?"
        
        if intent == "book":
            service_name = intent_data.get("service")
            date_parsed = intent_data.get("date_parsed")
            time_parsed = intent_data.get("time_parsed")
            
            # Szukaj usługi
            if service_name:
                service = find_service(tenant, service_name)
                if service:
                    conv.selected_service = service
            
            # Mamy wszystko → potwierdź
            if conv.selected_service and date_parsed and time_parsed:
                conv.selected_date = date_parsed
                conv.selected_time = time_parsed
                
                duration = conv.selected_service.get("duration_minutes", 30)
                slots = await get_available_slots(tenant["id"], date_parsed, duration)
                
                if time_parsed in slots:
                    conv.state = State.CONFIRM_BOOKING
                    return f"Rezerwuję {conv.selected_service['name']} na {format_date_polish(date_parsed)} o {format_hour_polish(time_parsed)}. Potwierdzasz?"
                else:
                    conv.state = State.ASK_TIME
                    conv.available_slots = slots
                    if slots:
                        slots_text = ", ".join([format_hour_polish(s) for s in slots[:4]])
                        return f"Ta godzina jest zajęta. Dostępne: {slots_text}. Która pasuje?"
                    return "Niestety brak wolnych terminów. Może inny dzień?"
            
            # Mamy usługę i datę → pytaj o godzinę
            if conv.selected_service and date_parsed:
                conv.selected_date = date_parsed
                duration = conv.selected_service.get("duration_minutes", 30)
                slots = await get_available_slots(tenant["id"], date_parsed, duration)
                
                if slots:
                    conv.available_slots = slots
                    conv.state = State.ASK_TIME
                    slots_text = ", ".join([format_hour_polish(s) for s in slots[:4]])
                    return f"Na {format_date_polish(date_parsed)} mam: {slots_text}. Która godzina?"
                return "Niestety brak wolnych terminów. Może inny dzień?"
            
            # Mamy usługę → pytaj o datę
            if conv.selected_service:
                conv.state = State.ASK_DATE
                return f"Świetnie, {conv.selected_service['name']} za {format_price_polish(conv.selected_service['price'])}. Na kiedy?"
            
            # Brak usługi → pytaj
            conv.state = State.ASK_SERVICE
            services = tenant.get("services", [])
            svc_names = [s['name'] for s in services]
            return f"Chętnie umówię wizytę. Na jaką usługę? Mamy: {', '.join(svc_names)}."
        
        return "Jak mogę pomóc? Mogę umówić wizytę lub podać cennik."
    
    # === STATE: ASK_SERVICE ===
    if conv.state == State.ASK_SERVICE:
        service_name = intent_data.get("service") or user_text
        service = find_service(tenant, service_name)
        
        if service:
            conv.selected_service = service
            date_parsed = intent_data.get("date_parsed")
            
            if date_parsed:
                conv.selected_date = date_parsed
                duration = service.get("duration_minutes", 30)
                slots = await get_available_slots(tenant["id"], date_parsed, duration)
                
                time_parsed = intent_data.get("time_parsed")
                if time_parsed and time_parsed in slots:
                    conv.selected_time = time_parsed
                    conv.state = State.CONFIRM_BOOKING
                    return f"Rezerwuję {service['name']} na {format_date_polish(date_parsed)} o {format_hour_polish(time_parsed)}. Potwierdzasz?"
                elif slots:
                    conv.available_slots = slots
                    conv.state = State.ASK_TIME
                    slots_text = ", ".join([format_hour_polish(s) for s in slots[:4]])
                    return f"Na {format_date_polish(date_parsed)} mam: {slots_text}. Która godzina?"
            
            conv.state = State.ASK_DATE
            return f"Świetnie, {service['name']} za {format_price_polish(service['price'])}. Na kiedy?"
        
        services = tenant.get("services", [])
        svc_names = [s['name'] for s in services]
        return f"Nie rozpoznałam usługi. Mamy: {', '.join(svc_names)}."
    
    # === STATE: ASK_DATE ===
    if conv.state == State.ASK_DATE:
        date_parsed = intent_data.get("date_parsed")
        
        if date_parsed:
            conv.selected_date = date_parsed
            duration = conv.selected_service.get("duration_minutes", 30) if conv.selected_service else 30
            slots = await get_available_slots(tenant["id"], date_parsed, duration)
            
            time_parsed = intent_data.get("time_parsed")
            if time_parsed and time_parsed in slots:
                conv.selected_time = time_parsed
                conv.state = State.CONFIRM_BOOKING
                svc_name = conv.selected_service['name'] if conv.selected_service else "wizytę"
                return f"Rezerwuję {svc_name} na {format_date_polish(date_parsed)} o {format_hour_polish(time_parsed)}. Potwierdzasz?"
            elif slots:
                conv.available_slots = slots
                conv.state = State.ASK_TIME
                slots_text = ", ".join([format_hour_polish(s) for s in slots[:4]])
                return f"Na {format_date_polish(date_parsed)} mam: {slots_text}. Która?"
            return "Niestety brak wolnych terminów. Może inny dzień?"
        
        return "Nie zrozumiałam daty. Powiedz: jutro, w piątek, itp."
    
    # === STATE: ASK_TIME ===
    if conv.state == State.ASK_TIME:
        time_parsed = intent_data.get("time_parsed")
        
        if time_parsed:
            if time_parsed in conv.available_slots:
                conv.selected_time = time_parsed
                conv.state = State.CONFIRM_BOOKING
                svc_name = conv.selected_service['name'] if conv.selected_service else "wizytę"
                return f"Rezerwuję {svc_name} na {format_date_polish(conv.selected_date)} o {format_hour_polish(time_parsed)}. Potwierdzasz?"
            else:
                slots_text = ", ".join([format_hour_polish(s) for s in conv.available_slots[:4]])
                return f"Ta godzina zajęta. Dostępne: {slots_text}."
        
        slots_text = ", ".join([format_hour_polish(s) for s in conv.available_slots[:4]])
        return f"Nie zrozumiałam. Dostępne: {slots_text}."
    
    # === STATE: CONFIRM_BOOKING ===
    if conv.state == State.CONFIRM_BOOKING:
        if intent == "confirm":
            success = await create_booking(conv)
            
            if success:
                conv.state = State.BOOKING_DONE
                svc_name = conv.selected_service['name'] if conv.selected_service else "Wizyta"
                return f"Gotowe! {svc_name} zarezerwowana na {format_date_polish(conv.selected_date)} o {format_hour_polish(conv.selected_time)}. Do zobaczenia!"
            return "Przepraszam, wystąpił błąd. Spróbuj ponownie."
        
        if intent == "deny":
            conv.state = State.LISTENING
            conv.selected_service = None
            conv.selected_date = None
            conv.selected_time = None
            conv.available_slots = []
            return "W porządku, anulowano. Jak jeszcze mogę pomóc?"
        
        return "Czy potwierdzasz rezerwację? Powiedz tak lub nie."
    
    # === STATE: BOOKING_DONE ===
    if conv.state == State.BOOKING_DONE:
        if intent == "book":
            conv.state = State.ASK_SERVICE
            conv.selected_service = None
            conv.selected_date = None
            conv.selected_time = None
            services = tenant.get("services", [])
            svc_names = [s['name'] for s in services]
            return f"Chętnie umówię kolejną wizytę. Na jaką usługę? Mamy: {', '.join(svc_names)}."
        
        return "Dziękuję! Czy mogę jeszcze pomóc?"
    
    return "Przepraszam, nie zrozumiałam. Mogę pomóc z rezerwacją lub cennikiem."


# ==========================================
# FAL SMART TURN
# ==========================================
class FalSmartTurn:
    """Smart Turn przez FAL API"""
    
    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold
        self.api_url = "https://fal.run/fal-ai/smart-turn"
        
    async def predict(self, audio_base64: str) -> Dict[str, Any]:
        if not FAL_KEY:
            return {"end_of_turn": True, "probability": 0.5}
            
        if not audio_base64:
            return {"end_of_turn": True, "probability": 0.5}
            
        try:
            audio_url = f"data:audio/wav;base64,{audio_base64}"
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.api_url,
                    headers={
                        "Authorization": f"Key {FAL_KEY}",
                        "Content-Type": "application/json"
                    },
                    json={"audio_url": audio_url},
                    timeout=aiohttp.ClientTimeout(total=2.0)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        probability = data.get("probability", 0.5)
                        return {
                            "end_of_turn": probability >= self.threshold,
                            "probability": probability
                        }
        except Exception as e:
            logger.error(f"FAL error: {e}")
        
        return {"end_of_turn": True, "probability": 0.5}


# ==========================================
# AUDIO BUFFER
# ==========================================
class AudioBuffer:
    """Bufor audio dla Smart Turn"""
    
    def __init__(self, max_seconds: float = 8.0):
        self.max_bytes = int(8000 * max_seconds)
        self.buffer = bytearray()
        
    def add(self, data: bytes):
        self.buffer.extend(data)
        if len(self.buffer) > self.max_bytes:
            self.buffer = self.buffer[-self.max_bytes:]
            
    def get_wav_base64(self) -> str:
        if not self.buffer:
            return ""
            
        try:
            import audioop
            import struct
            import io
            
            pcm_8khz = audioop.ulaw2lin(bytes(self.buffer), 2)
            pcm_16khz = audioop.ratecv(pcm_8khz, 2, 1, 8000, 16000, None)[0]
            
            wav_buffer = io.BytesIO()
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
            wav_buffer.write(struct.pack('<I', 16))
            wav_buffer.write(struct.pack('<H', 1))
            wav_buffer.write(struct.pack('<H', num_channels))
            wav_buffer.write(struct.pack('<I', sample_rate))
            wav_buffer.write(struct.pack('<I', byte_rate))
            wav_buffer.write(struct.pack('<H', block_align))
            wav_buffer.write(struct.pack('<H', bits_per_sample))
            wav_buffer.write(b'data')
            wav_buffer.write(struct.pack('<I', data_size))
            wav_buffer.write(pcm_16khz)
            
            return base64.b64encode(wav_buffer.getvalue()).decode('utf-8')
        except Exception as e:
            logger.error(f"Audio conversion error: {e}")
            return ""
            
    def clear(self):
        self.buffer = bytearray()
        
    def duration_seconds(self) -> float:
        return len(self.buffer) / 8000.0


# ==========================================
# TRANSCRIPT BUFFER
# ==========================================
class TranscriptBuffer:
    """Bufor transkrypcji"""
    
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