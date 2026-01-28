# flows_helpers.py - Funkcje pomocnicze dla Pipecat Flows
"""
Zawiera:
- Parsowanie dat i godzin (polskie)
- Formatowanie po polsku
- Integracja z API panelu (kalendarz, rezerwacje)
- Walidacje
"""

import os
import asyncio
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
    
    # Konwertuj na int (mogą być stringi z bazy lub None)
    try:
        min_advance_hours = int(staff.get("min_advance_hours") or staff.get("min_booking_hours") or 12)
    except (ValueError, TypeError):
        min_advance_hours = 12
    
    min_booking_time = now + timedelta(hours=min_advance_hours)
    
    if date < min_booking_time:
        return (False, f"Rezerwacje przyjmujemy z minimum {min_advance_hours} godzinnym wyprzedzeniem.")
    
    try:
        max_days_ahead = int(staff.get("max_days_ahead") or staff.get("max_booking_days") or 14)
    except (ValueError, TypeError):
        max_days_ahead = 14
    
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
    """Pobiera wolne sloty z API panelu (Google Calendar) - z retry"""
    staff_id = staff.get("id")
    service_id = service.get("id")
    date_str = date.strftime("%Y-%m-%d")
    slug = tenant.get("slug") or PANEL_SLUG
    
    if not slug:
        logger.warning("⚠️ No panel slug configured")
        return []
    
    # Retry logic - 3 próby
    for attempt in range(3):
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
                    logger.warning(f"⚠️ Calendar API returned {response.status_code} (attempt {attempt + 1}/3)")
                    
        except Exception as e:
            logger.error(f"❌ Calendar API error (attempt {attempt + 1}/3): {e}")
        
        # Czekaj przed kolejną próbą (tylko jeśli nie ostatnia)
        if attempt < 2:
            import asyncio
            await asyncio.sleep(0.5)
    
    logger.error(f"❌ Calendar API failed after 3 attempts for {date_str}")
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
    """Zapisuje rezerwację przez API panelu - z retry i kodem wizyty"""
    import random
    
    slug = tenant.get("slug") or PANEL_SLUG
    
    if not slug:
        logger.warning("⚠️ No panel slug configured")
        return {}
    
    # Generuj unikalny 4-cyfrowy kod wizyty
    booking_code = str(random.randint(1000, 9999))
    
    # POPRAWKA: Użyj tylko daty (bez czasu) - czysta data YYYY-MM-DD
    date_only = date.date() if date else None
    date_str = date_only.strftime("%Y-%m-%d") if date_only else None
    time_str = f"{hour:02d}:00" if hour is not None else None
    
    logger.info(f"📅 Booking request: {date_str} at {time_str} for {customer_name}")
    
    # Retry logic - 3 próby
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    f"{PANEL_API_URL}/api/panel/{slug}/bookings",
                    json={
                        "staff_id": staff.get("id"),
                        "service_id": service.get("id"),
                        "date": date_str,
                        "time": time_str,
                        "client_name": customer_name,
                        "client_phone": customer_phone,
                        "booking_code": booking_code,
                    }
                )
                
                if response.status_code in [200, 201]:
                    data = response.json()
                    data["booking_code"] = booking_code
                    logger.info(f"✅ Booking saved: {data.get('bookingId')} (code: {booking_code})")
                    return data
                else:
                    logger.warning(f"⚠️ Booking API error: {response.status_code} (attempt {attempt + 1}/3)")
                    
        except Exception as e:
            logger.error(f"❌ Booking API error (attempt {attempt + 1}/3): {e}")
        
        if attempt < 2:
            await asyncio.sleep(0.5)
    
    logger.error(f"❌ Booking API failed after 3 attempts")
    return {}


async def send_booking_sms(
    tenant: dict, customer_phone: str, service_name: str, 
    staff_name: str, date_str: str, time_str: str, booking_code: str
) -> bool:
    """Wysyła SMS z potwierdzeniem wizyty przez Twilio"""
    import os
    
    twilio_sid = os.getenv("TWILIO_ACCOUNT_SID")
    twilio_token = os.getenv("TWILIO_AUTH_TOKEN")
    
    # Użyj numeru firmy z bazy (tenant), nie globalnego!
    twilio_number = tenant.get("phone_number")
    
    if not twilio_number:
        logger.warning(f"⚠️ No phone_number for tenant {tenant.get('name')}")
        return False
    
    # Upewnij się że numer ma format +48...
    if not twilio_number.startswith("+"):
        twilio_number = f"+48{twilio_number.replace(' ', '').replace('-', '')[-9:]}"
    
    if not all([twilio_sid, twilio_token, twilio_number]):
        logger.warning("⚠️ Twilio SMS not configured")
        return False
    
    if not customer_phone or len(customer_phone) < 9:
        logger.warning(f"⚠️ Invalid phone for SMS: {customer_phone}")
        return False
    
    # Formatuj numer
    phone = customer_phone.replace(" ", "").replace("-", "")
    if not phone.startswith("+"):
        phone = f"+48{phone[-9:]}"
    
    business_name = tenant.get("name", "Salon")
    
    # Krótki SMS (max 160 znaków)
    sms_text = f"{business_name}: {service_name} {date_str} g.{time_str}, {staff_name}. Kod:{booking_code}. Do zobaczenia!"
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{twilio_sid}/Messages.json",
                auth=(twilio_sid, twilio_token),
                data={
                    "From": twilio_number,
                    "To": phone,
                    "Body": sms_text,
                }
            )
            
            if response.status_code in [200, 201]:
                logger.info(f"📱 SMS sent to {phone}: {sms_text[:50]}...")
                return True
            else:
                logger.error(f"📱 SMS error: {response.status_code} - {response.text}")
                return False
                
    except Exception as e:
        logger.error(f"📱 SMS error: {e}")
        return False


async def increment_sms_count(tenant_id: str):
    """Zwiększa licznik SMS dla tenant"""
    from helpers import db
    
    try:
        await db.execute(
            "UPDATE tenants SET sms_count = COALESCE(sms_count, 0) + 1 WHERE id = ?",
            [tenant_id]
        )
        logger.info(f"📱 SMS count incremented for {tenant_id}")
    except Exception as e:
        logger.error(f"📱 SMS count error: {e}")
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
    
    # Adres - formatuj ładnie dla wymowy TTS
    address = tenant.get("address", "")
    if address:
        import re
        
        # Zamień skróty
        address = address.replace("ul.", "ulica").replace("ul ", "ulica ")
        address = address.replace("al.", "aleja").replace("al ", "aleja ")
        address = address.replace("pl.", "plac").replace("pl ", "plac ")
        
        # Dodaj "numer" przed liczbą w adresie (np. "Kwiatowa 15" → "Kwiatowa numer 15")
        # Szuka: spacja + cyfry + (koniec lub przecinek lub spacja)
        address = re.sub(r' (\d+)([,\s]|$)', r' numer \1\2', address)
        
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
    
    # Godziny pracy pracowników (dla trybu z rezerwacjami)
    if booking_enabled:
        staff = tenant.get("staff", [])
        if staff:
            staff_hours = []
            for s in staff:
                wh_json = s.get("working_hours_json", "")
                if wh_json and wh_json != "'{}'":
                    try:
                        import json
                        wh = json.loads(wh_json) if isinstance(wh_json, str) else wh_json
                        days_pl = {"mon": "pon", "tue": "wt", "wed": "śr", "thu": "czw", "fri": "pt", "sat": "sob", "sun": "niedz"}
                        hours_list = []
                        for day_en, day_pl in days_pl.items():
                            day_data = wh.get(day_en, {})
                            if day_data and not day_data.get("closed", False) and day_data.get("open"):
                                hours_list.append(f"{day_pl}: {day_data['open']}-{day_data['close']}")
                        if hours_list:
                            staff_hours.append(f"{s['name']}: {', '.join(hours_list)}")
                    except:
                        pass
            if staff_hours:
                parts.append(f"GODZINY PRACY PRACOWNIKÓW:\n" + "\n".join(staff_hours))
    
    return "\n\n".join(parts)


# ==========================================
# FUZZY MATCHING - Tolerancja na literówki
# ==========================================

from difflib import SequenceMatcher

def fuzzy_match_service(query: str, services: list, threshold: float = 0.5) -> dict | None:
    """
    Dopasuj usługę z tolerancją na literówki.
    Uniwersalne - działa dla każdej branży (fryzjer, kosmetyczka, mechanik...).
    
    Przykłady:
    - "strzyzenie" → "Strzyżenie męskie" ✓
    - "curzenie" → "Strzyżenie męskie" ✓ (błąd Deepgram)
    - "manikur" → "Manicure" ✓
    - "przeglond" → "Przegląd" ✓
    """
    if not query or not services:
        return None
    
    query = query.lower().strip()
    
    best_match = None
    best_score = 0
    
    for service in services:
        name = service["name"].lower()
        
        # 1. Exact match
        if query == name:
            return service
        
        # 2. Zawieranie (query w name lub name w query)
        if query in name or name in query:
            return service
        
        # 3. Początek słowa (np. "strzy" → "strzyżenie")
        min_len = min(len(query), 4)
        if len(query) >= 3 and name.startswith(query[:min_len]):
            return service
        
        # 4. Fuzzy match dla literówek
        score = SequenceMatcher(None, query, name).ratio()
        
        # Bonus za podobny początek
        if len(query) >= 3 and len(name) >= 3:
            if query[:3] == name[:3]:
                score += 0.15
            elif query[:2] == name[:2]:
                score += 0.1
        
        if score > best_score and score >= threshold:
            best_score = score
            best_match = service
    
    return best_match


def fuzzy_match_staff(query: str, staff_list: list, threshold: float = 0.6) -> dict | None:
    """
    Dopasuj pracownika z tolerancją na literówki.
    Obsługuje polskie zdrobnienia imion.
    
    Przykłady:
    - "ania" → "Anna" ✓
    - "kasia" → "Katarzyna" ✓
    - "tomek" → "Tomasz" ✓
    """
    if not query or not staff_list:
        return None
    
    query = query.lower().strip()
    
    # Polskie zdrobnienia - uniwersalne dla wszystkich branż
    name_aliases = {
        "ania": ["anna", "ania", "ani", "aneczka"],
        "kasia": ["katarzyna", "kasia", "kaśka", "kasieńka"],
        "asia": ["joanna", "asia", "joasia", "aśka"],
        "basia": ["barbara", "basia", "baśka"],
        "gosia": ["małgorzata", "gosia", "gośka"],
        "ela": ["elżbieta", "ela", "elka"],
        "ola": ["aleksandra", "ola", "olka"],
        "ewa": ["ewa", "ewka", "ewunia"],
        "magda": ["magdalena", "magda", "magdzia"],
        "tomek": ["tomasz", "tomek"],
        "bartek": ["bartłomiej", "bartosz", "bartek"],
        "krzysiek": ["krzysztof", "krzysiek", "krzyś"],
        "piotrek": ["piotr", "piotrek"],
        "marcin": ["marcin", "marciniek"],
        "michał": ["michał", "michałek"],
        "wiktor": ["wiktor", "wiktoria", "wika"],
        "janek": ["jan", "janek"],
        "maciek": ["maciej", "maciek"],
    }
    
    # Rozszerz query o aliasy
    query_variants = [query]
    for alias, names in name_aliases.items():
        if query in names or query == alias:
            query_variants.extend(names)
            query_variants.append(alias)
    
    # Usuń duplikaty
    query_variants = list(set(query_variants))
    
    # Szukaj exact/contains match
    for staff in staff_list:
        name = staff["name"].lower()
        for variant in query_variants:
            if variant == name or variant in name or name in variant:
                return staff
    
    # Fuzzy match jako fallback
    best_match = None
    best_score = 0
    
    for staff in staff_list:
        name = staff["name"].lower()
        for variant in query_variants:
            score = SequenceMatcher(None, variant, name).ratio()
            if score > best_score and score >= threshold:
                best_score = score
                best_match = staff
    
    return best_match


def staff_can_do_service(staff: dict, service: dict) -> bool:
    """
    Sprawdź czy pracownik wykonuje daną usługę.
    Pusta lista usług = pracownik robi wszystko.
    """
    if not service:
        return True
    
    staff_service_ids = [svc.get("id") for svc in staff.get("services", [])]
    
    # Pusta lista = wszystkie usługi
    if not staff_service_ids:
        return True
    
    return service.get("id") in staff_service_ids