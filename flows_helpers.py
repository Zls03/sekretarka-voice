# flows_helpers.py - Funkcje pomocnicze dla Pipecat Flows
"""
Zawiera:
- Parsowanie dat i godzin (polskie)
- Formatowanie po polsku
- Integracja z API panelu (kalendarz, rezerwacje)
- Walidacje
"""

import os
import httpx
from datetime import datetime, timedelta
from typing import Optional, List
from loguru import logger

# URL do panelu Next.js
PANEL_API_URL = os.getenv("PANEL_API_URL", "http://localhost:3000")
PANEL_SLUG = os.getenv("PANEL_SLUG", "")

# ==========================================
# STAŁE - POLSKIE DNI
# ==========================================

POLISH_DAYS = {
    0: "poniedziałek",
    1: "wtorek", 
    2: "środa",
    3: "czwartek",
    4: "piątek",
    5: "sobota",
    6: "niedziela"
}

POLISH_DAYS_REVERSE = {v: k for k, v in POLISH_DAYS.items()}

HOUR_WORDS = {
    8: "ósmej", 9: "dziewiątej", 10: "dziesiątej",
    11: "jedenastej", 12: "dwunastej", 13: "trzynastej",
    14: "czternastej", 15: "piętnastej", 16: "szesnastej",
    17: "siedemnastej", 18: "osiemnastej", 19: "dziewiętnastej",
    20: "dwudziestej"
}

WORD_TO_HOUR = {
    "ósma": 8, "osma": 8,
    "dziewiąta": 9, "dziewiata": 9,
    "dziesiąta": 10, "dziesiata": 10,
    "jedenasta": 11,
    "dwunasta": 12,
    "trzynasta": 13,
    "czternasta": 14,
    "piętnasta": 15, "pietnasta": 15,
    "szesnasta": 16,
    "siedemnasta": 17,
    "osiemnasta": 18,
    "dziewiętnasta": 19, "dziewietnasta": 19,
    "dwudziesta": 20,
}


# ==========================================
# PARSOWANIE
# ==========================================

def parse_polish_date(date_str: str) -> Optional[datetime]:
    """Parsuj polską datę (dziś, jutro, pojutrze, dzień tygodnia)"""
    date_str = date_str.lower().strip()
    today = datetime.now()
    
    if date_str in ["dziś", "dzis", "dzisiaj", "teraz"]:
        return today
    elif date_str in ["jutro"]:
        return today + timedelta(days=1)
    elif date_str in ["pojutrze"]:
        return today + timedelta(days=2)
    elif date_str in POLISH_DAYS_REVERSE:
        target_weekday = POLISH_DAYS_REVERSE[date_str]
        days_ahead = target_weekday - today.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        return today + timedelta(days=days_ahead)
    
    for fmt in ["%Y-%m-%d", "%d.%m.%Y", "%d.%m", "%d-%m-%Y", "%d/%m/%Y"]:
        try:
            parsed = datetime.strptime(date_str, fmt)
            if parsed.year == 1900:
                parsed = parsed.replace(year=today.year)
            return parsed
        except:
            pass
    
    return None


def parse_time(time_str: str) -> Optional[int]:
    """Parsuj godzinę (słownie lub numerycznie) → zwraca godzinę jako int"""
    time_str = time_str.lower().strip()
    
    if time_str in WORD_TO_HOUR:
        return WORD_TO_HOUR[time_str]
    
    import re
    numbers = re.findall(r'\d+', time_str)
    if numbers:
        hour = int(numbers[0])
        if 0 <= hour <= 23:
            return hour
    
    return None


# ==========================================
# FORMATOWANIE
# ==========================================

def format_hour_polish(hour: int) -> str:
    """Formatuj godzinę po polsku słownie"""
    return HOUR_WORDS.get(hour, f"{hour}")


def format_date_polish(date: datetime) -> str:
    """Formatuj datę po polsku"""
    today = datetime.now().date()
    target = date.date()
    
    if target == today:
        return "dziś"
    elif target == today + timedelta(days=1):
        return "jutro"
    elif target == today + timedelta(days=2):
        return "pojutrze"
    else:
        day_name = POLISH_DAYS[target.weekday()]
        return f"w {day_name}, {target.day}.{target.month}"


# ==========================================
# GODZINY PRACY
# ==========================================

def get_opening_hours(tenant: dict, weekday: int) -> tuple[int, int] | None:
    """Pobierz godziny otwarcia dla danego dnia tygodnia"""
    default_hours = {
        0: (9, 18), 1: (9, 18), 2: (9, 18), 3: (9, 18), 4: (9, 18),
        5: (9, 14), 6: None,
    }
    
    working_hours = tenant.get("working_hours", [])
    for wh in working_hours:
        if wh.get("day_of_week") == weekday:
            open_time = wh.get("open_time")
            close_time = wh.get("close_time")
            if open_time and close_time:
                open_hour = int(open_time.split(":")[0])
                close_hour = int(close_time.split(":")[0])
                return (open_hour, close_hour)
            return None
    
    return default_hours.get(weekday)


def validate_date_constraints(date: datetime, tenant: dict, staff: dict) -> tuple[bool, str]:
    """Sprawdza ograniczenia daty (min wyprzedzenie, max dni w przód)"""
    now = datetime.now()
    
    min_advance_hours = staff.get("min_advance_hours") or staff.get("min_booking_hours") or 12
    min_booking_time = now + timedelta(hours=min_advance_hours)
    
    if date < min_booking_time:
        return (False, f"Rezerwacje przyjmujemy z minimum {min_advance_hours} godzinnym wyprzedzeniem.")
    
    max_days_ahead = staff.get("max_days_ahead") or staff.get("max_booking_days") or 14
    max_date = now + timedelta(days=max_days_ahead)
    
    if date > max_date:
        return (False, f"Rezerwacje można składać maksymalnie {max_days_ahead} dni w przód.")
    
    return (True, "")


# ==========================================
# API - KALENDARZ
# ==========================================

async def get_available_slots_from_api(
    tenant: dict, staff: dict, service: dict, date: datetime
) -> List[int]:
    """Pobiera wolne sloty z API panelu (Google Calendar)"""
    staff_id = staff.get("id")
    service_id = service.get("id")
    date_str = date.strftime("%Y-%m-%d")
    slug = tenant.get("slug") or PANEL_SLUG
    
    if not slug:
        logger.warning("⚠️ No panel slug configured")
        return []
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{PANEL_API_URL}/api/panel/{slug}/calendar/slots",
                params={"staffId": staff_id, "serviceId": service_id, "date": date_str}
            )
            
            if response.status_code == 200:
                data = response.json()
                slots = data.get("slots", [])
                hours = []
                for slot in slots:
                    if isinstance(slot, str) and ":" in slot:
                        hours.append(int(slot.split(":")[0]))
                    elif isinstance(slot, int):
                        hours.append(slot)
                logger.info(f"📅 Got {len(hours)} slots from API for {date_str}")
                return hours
            else:
                logger.warning(f"⚠️ Calendar API returned {response.status_code}")
                return []
                
    except Exception as e:
        logger.error(f"❌ Calendar API error: {e}")
        return []


async def get_available_slots_from_working_hours(
    tenant: dict, staff: dict, service: dict, date: datetime
) -> List[int]:
    """Fallback: generuje sloty z godzin pracy"""
    weekday = date.weekday()
    opening_hours = get_opening_hours(tenant, weekday)
    
    if not opening_hours:
        return []
    
    open_hour, close_hour = opening_hours
    service_duration = service.get("duration_minutes", 60)
    
    slots = []
    current_hour = open_hour
    
    while current_hour + (service_duration / 60) <= close_hour:
        slots.append(current_hour)
        current_hour += 1
    
    now = datetime.now()
    if date.date() == now.date():
        min_hour = now.hour + 1
        slots = [h for h in slots if h >= min_hour]
    
    logger.info(f"📅 Generated {len(slots)} slots from working hours")
    return slots


async def get_available_slots(
    tenant: dict, staff: dict, service: dict, date: datetime
) -> List[int]:
    """Główna funkcja - kalendarz lub godziny pracy"""
    calendar_connected = staff.get("google_calendar_id") or staff.get("google_connected")
    
    if calendar_connected:
        logger.info(f"📅 Staff {staff.get('name')} has calendar, using API")
        slots = await get_available_slots_from_api(tenant, staff, service, date)
        if slots:
            return slots
        logger.warning("⚠️ API returned no slots, falling back")
    
    return await get_available_slots_from_working_hours(tenant, staff, service, date)


# ==========================================
# API - REZERWACJE
# ==========================================

async def save_booking_to_api(
    tenant: dict, staff: dict, service: dict, 
    date: datetime, hour: int, customer_name: str, customer_phone: str = ""
) -> dict:
    """Zapisuje rezerwację przez API panelu"""
    slug = tenant.get("slug") or PANEL_SLUG
    
    if not slug:
        logger.warning("⚠️ No panel slug configured")
        return {}
    
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{PANEL_API_URL}/api/panel/{slug}/bookings",
                json={
                    "staff_id": staff.get("id"),
                    "service_id": service.get("id"),
                    "date": date.strftime("%Y-%m-%d") if date else None,
                    "time": f"{hour:02d}:00" if hour else None,
                    "client_name": customer_name,
                    "client_phone": customer_phone,
                }
            )
            
            if response.status_code in [200, 201]:
                data = response.json()
                logger.info(f"✅ Booking saved: {data.get('bookingId')}")
                return data
            else:
                logger.warning(f"⚠️ Booking API error: {response.status_code}")
                return {}
                
    except Exception as e:
        logger.error(f"❌ Booking API error: {e}")
        return {}


# ==========================================
# BUDOWANIE KONTEKSTU DLA ODPOWIEDZI
# ==========================================

def build_business_context(tenant: dict) -> str:
    """Buduje kontekst o firmie dla GPT"""
    parts = []
    booking_enabled = tenant.get("booking_enabled", 1) == 1
    
    # Godziny pracy
    working_hours = tenant.get("working_hours", [])
    if working_hours:
        hours_text = []
        for wh in working_hours:
            day_num = wh.get("day_of_week", 0)
            if wh.get("open_time"):
                day_name = POLISH_DAYS.get(day_num, str(day_num))
                hours_text.append(f"{day_name}: {wh['open_time']}-{wh['close_time']}")
        if hours_text:
            parts.append(f"GODZINY PRACY: {', '.join(hours_text)}")
    
    # Usługi/Cennik - różne źródło w zależności od trybu
    if booking_enabled:
        # Tryb z rezerwacjami - usługi z kalendarza
        services = tenant.get("services", [])
        if services:
            svc_text = []
            for s in services:
                price = s.get('price', 'cena do uzgodnienia')
                duration = s.get('duration_minutes', 30)
                svc_text.append(f"{s['name']} - {price} zł ({duration} min)")
            parts.append(f"CENNIK: {', '.join(svc_text)}")
    else:
        # Tryb informacyjny - usługi z info_services
        info_services = tenant.get("info_services", [])
        if info_services:
            svc_text = []
            for s in info_services:
                name = s.get('name', '')
                price = s.get('price', '')
                description = s.get('description', '')
                
                if price and description:
                    svc_text.append(f"{name} - {price} ({description})")
                elif price:
                    svc_text.append(f"{name} - {price}")
                elif description:
                    svc_text.append(f"{name} ({description})")
                else:
                    svc_text.append(name)
            parts.append(f"CENNIK/USŁUGI: {', '.join(svc_text)}")
        
        # Dodaj informację że rezerwacje są wyłączone
        parts.append("UWAGA: Rezerwacje telefoniczne są WYŁĄCZONE. Jeśli klient pyta o rezerwację, poinformuj że nie jest dostępna przez telefon.")
    
    # Adres - formatuj ładnie
    address = tenant.get("address", "")
    if address:
        address = address.replace("ul.", "ulica").replace("ul ", "ulica ")
        address = address.replace("al.", "aleja").replace("al ", "aleja ")
        address = address.replace("pl.", "plac").replace("pl ", "plac ")
        parts.append(f"ADRES: {address}")
    
    # FAQ
    faq = tenant.get("faq", [])
    if faq:
        faq_text = []
        for f in faq:
            q = f.get("question", "")
            a = f.get("answer", "")
            if q and a:
                faq_text.append(f"Pytanie: {q} → Odpowiedź: {a}")
        if faq_text:
            parts.append(f"FAQ:\n" + "\n".join(faq_text))
    
    # Dodatkowe info
    additional = tenant.get("additional_info", "")
    if additional:
        parts.append(f"DODATKOWE INFO: {additional}")
    
    return "\n\n".join(parts)