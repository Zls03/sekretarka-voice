# flows_helpers.py - Funkcje pomocnicze dla Pipecat Flows
"""
Zawiera:
- Parsowanie dat i godzin (polskie)
- Formatowanie po polsku
- Integracja z API panelu (kalendarz, rezerwacje)
- Walidacje
"""
import random
import os
import asyncio
import httpx
from datetime import datetime, timedelta
from typing import Optional, List
from loguru import logger

from polish_mappings import (
    HOUR_TO_NUMBER, NUMBER_TO_HOUR_WORD,
    NAME_ALIASES, FULL_NAME_TO_ALIASES,
    DAY_TO_NUMBER, NUMBER_TO_DAY,
    POLISH_DAYS, POLISH_DAYS_REVERSE,
    parse_hour_from_text, match_staff_name,
    apply_stt_corrections, normalize_polish_text
)
# URL do panelu Next.js
PANEL_API_URL = os.getenv("PANEL_API_URL", "http://localhost:3000")
PANEL_SLUG = os.getenv("PANEL_SLUG", "")



def parse_polish_date(date_str: str) -> Optional[datetime]:
    """Parsuj polską datę (dziś, jutro, pojutrze, dzień tygodnia, data)
    
    Obsługuje:
    - "dziś", "dzisiaj", "teraz"
    - "jutro", "pojutrze"
    - "sobota", "w sobotę", "sobotę" (wszystkie formy gramatyczne)
    - "15.02", "15 lutego", "2024-02-15"
    """
    import re
    
    if not date_str:
        return None
    
    date_str = date_str.lower().strip()
    date_str = apply_stt_corrections(date_str)
    today = datetime.now()
    
    # 1. Dziś/jutro/pojutrze
    if date_str in ["dziś", "dzis", "dzisiaj", "teraz", "na dziś", "na dzis", "na dzisiaj"]:
        return today
    elif date_str in ["jutro", "na jutro"]:
        return today + timedelta(days=1)
    elif date_str in ["pojutrze", "na pojutrze"]:
        return today + timedelta(days=2)
    
    # 2. Dzień tygodnia - użyj DAY_TO_NUMBER (ma wszystkie formy!)
    if date_str in DAY_TO_NUMBER:
        target_weekday = DAY_TO_NUMBER[date_str]
        days_ahead = target_weekday - today.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        return today + timedelta(days=days_ahead)
    
    # 2b. Sprawdź czy dzień tygodnia jest CZĘŚCIĄ tekstu (np. "na sobotę rano")
    for day_text, weekday_num in sorted(DAY_TO_NUMBER.items(), key=lambda x: -len(x[0])):
        if day_text in date_str:
            days_ahead = weekday_num - today.weekday()
            if days_ahead <= 0:
                days_ahead += 7
            return today + timedelta(days=days_ahead)
    
    # 3. Data z numerem dnia i miesiącem słownie (np. "15 lutego", "piętnastego marca")
    from polish_mappings import MONTH_TO_NUMBER
    
    for month_name, month_num in MONTH_TO_NUMBER.items():
        if month_name in date_str:
            # Wyciągnij dzień (liczbę)
            numbers = re.findall(r'\d+', date_str)
            if numbers:
                day = int(numbers[0])
                if 1 <= day <= 31:
                    year = today.year
                    try:
                        result = datetime(year, month_num, day)
                        # Jeśli data w przeszłości - następny rok
                        if result.date() < today.date():
                            result = datetime(year + 1, month_num, day)
                        return result
                    except ValueError:
                        pass  # Nieprawidłowy dzień dla miesiąca
    
    # 4. Standardowe formaty daty
    for fmt in ["%Y-%m-%d", "%d.%m.%Y", "%d.%m", "%d-%m-%Y", "%d/%m/%Y", "%d/%m"]:
        try:
            parsed = datetime.strptime(date_str, fmt)
            if parsed.year == 1900:
                parsed = parsed.replace(year=today.year)
            # Jeśli data w przeszłości - następny rok
            if parsed.date() < today.date():
                parsed = parsed.replace(year=today.year + 1)
            return parsed
        except:
            pass
    
    # 5. Tylko numer dnia (np. "15", "piętnastego") - zakładamy bieżący/następny miesiąc
    numbers = re.findall(r'\d+', date_str)
    if numbers:
        day = int(numbers[0])
        if 1 <= day <= 31:
            try:
                # Spróbuj bieżący miesiąc
                result = datetime(today.year, today.month, day)
                if result.date() < today.date():
                    # Następny miesiąc
                    if today.month == 12:
                        result = datetime(today.year + 1, 1, day)
                    else:
                        result = datetime(today.year, today.month + 1, day)
                return result
            except ValueError:
                pass
    
    logger.warning(f"⚠️ Could not parse date: '{date_str}'")
    return None


def parse_time(time_str: str) -> Optional[int]:
    """Parsuj godzinę - wrapper na polish_mappings.parse_hour_from_text()"""
    return parse_hour_from_text(time_str)


# ==========================================
# FORMATOWANIE
# ==========================================

def format_hour_polish(hour: int) -> str:
    """Formatuj godzinę po polsku słownie"""
    return NUMBER_TO_HOUR_WORD.get(hour, f"{hour}")


def format_date_polish(date: datetime) -> str:
    """Formatuj datę po polsku - naturalnie słownie"""
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
        
        # Miesiące po polsku w dopełniaczu
        POLISH_MONTHS = {
            1: "stycznia", 2: "lutego", 3: "marca", 4: "kwietnia",
            5: "maja", 6: "czerwca", 7: "lipca", 8: "sierpnia",
            9: "września", 10: "października", 11: "listopada", 12: "grudnia"
        }
        
        month_name = POLISH_MONTHS.get(target.month, str(target.month))
        
        return f"w {day_name}, {target.day} {month_name}"


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
    """Pobiera wolne sloty z API panelu (Google Calendar) - SZYBKA WERSJA"""
    staff_id = staff.get("id")
    service_id = service.get("id")
    date_str = date.strftime("%Y-%m-%d")
    slug = tenant.get("slug") or PANEL_SLUG
    
    if not slug:
        logger.warning("⚠️ No panel slug configured")
        return []
    
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
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
                
    except Exception as e:
        logger.error(f"❌ Calendar API error: {e}")
    
    return []

def get_staff_working_hours(staff: dict, weekday: int) -> tuple[int, int] | None:
    """Pobierz godziny pracy pracownika dla danego dnia"""
    import json
    
    wh_json = staff.get("working_hours_json", "")
    if not wh_json or wh_json == "'{}'":
        return None
    
    try:
        wh = json.loads(wh_json) if isinstance(wh_json, str) else wh_json
        
        # Mapowanie weekday (0=pon) na klucze JSON
        day_keys = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}
        day_key = day_keys.get(weekday)
        
        if not day_key or day_key not in wh:
            return None
        
        day_data = wh[day_key]
        
        # Sprawdź czy pracownik pracuje tego dnia
        if day_data.get("closed", False):
            return None
        
        open_time = day_data.get("open", "")
        close_time = day_data.get("close", "")
        
        if open_time and close_time:
            open_hour = int(open_time.split(":")[0])
            close_hour = int(close_time.split(":")[0])
            return (open_hour, close_hour)
        
    except Exception as e:
        logger.warning(f"⚠️ Error parsing staff working hours: {e}")
    
    return None
async def get_available_slots_from_working_hours(
    tenant: dict, staff: dict, service: dict, date: datetime
) -> List[int]:
    """Fallback: generuje sloty z godzin pracy PRACOWNIKA (lub firmy jako fallback)"""
    weekday = date.weekday()
    
    # Najpierw sprawdź godziny pracownika
    staff_hours = get_staff_working_hours(staff, weekday)
    
    if staff_hours:
        open_hour, close_hour = staff_hours
    else:
        # Fallback do godzin firmy
        opening_hours = get_opening_hours(tenant, weekday)
        if not opening_hours:
            return []
        open_hour, close_hour = opening_hours
    service_duration = service.get("duration_minutes", 60)
    
    slots = []
    current_hour = open_hour
    
    # Przerwa między wizytami - sprawdź różne nazwy pól
    break_between = (
        staff.get("buffer_minutes") or 
        staff.get("break_between_minutes") or 
        staff.get("break_minutes") or 
        tenant.get("buffer_minutes") or 
        0
    )

    while current_hour + (service_duration / 60) <= close_hour:
        slots.append(current_hour)
        # Następny slot = czas usługi + przerwa
        slot_duration_hours = (service_duration + break_between) / 60
        current_hour += max(1, int(slot_duration_hours))
    
    now = datetime.now()
    if date.date() == now.date():
        min_hour = now.hour + 1
        slots = [h for h in slots if h >= min_hour]
    
    logger.info(f"📅 Generated {len(slots)} slots from working hours")
    return slots


# Cache dla slotów (żeby nie odpytywać API wielokrotnie)
_slots_cache = {}

async def get_available_slots(
    tenant: dict, staff: dict, service: dict, date: datetime
) -> List[int]:
    """Główna funkcja - z cache 60s"""
    # Cache key
    cache_key = f"{staff.get('id')}_{date.strftime('%Y-%m-%d')}"
    
    # Sprawdź cache (ważny 60 sekund)
    if cache_key in _slots_cache:
        cached_time, cached_slots = _slots_cache[cache_key]
        if (datetime.now() - cached_time).seconds < 60:
            logger.info(f"📅 Cache hit for {cache_key}: {len(cached_slots)} slots")
            return cached_slots
    
    # Pobierz z API lub working hours
    calendar_connected = staff.get("google_calendar_id") or staff.get("google_connected")
    
    if calendar_connected:
        logger.info(f"📅 Staff {staff.get('name')} has calendar, using API")
        slots = await get_available_slots_from_api(tenant, staff, service, date)
        if slots:
            _slots_cache[cache_key] = (datetime.now(), slots)
            return slots
        logger.warning("⚠️ API returned no slots, falling back")
    
    slots = await get_available_slots_from_working_hours(tenant, staff, service, date)
    _slots_cache[cache_key] = (datetime.now(), slots)
    return slots

# ==========================================
# API - REZERWACJE
# ==========================================

async def save_booking_to_api(
    tenant: dict, staff: dict, service: dict, 
    date: datetime, hour: int, customer_name: str, customer_phone: str = ""
) -> dict:
    """Zapisuje rezerwację przez API panelu - z retry i kodem wizyty"""
    
    slug = tenant.get("slug") or PANEL_SLUG
    
    if not slug:
        logger.warning("⚠️ No panel slug configured")
        return {}
    
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
                    }
                )
                
                if response.status_code in [200, 201]:
                    data = response.json()
                    # Użyj kodu z API (jeśli jest), albo wygeneruj fallback
                    booking_code = data.get("visitCode") or data.get("booking_code") or str(random.randint(1000, 9999))
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
                parts.append(f"GODZINY PRACY PRACOWNIKÓW:\n" + "\n".join(staff_hours))  # 🔥 POPRAWIONE!
    
    # Ostrzeżenie na końcu
    parts.append("⚠️ Jeśli powyżej NIE MA jakiejś informacji - powiedz że nie masz tej informacji. NIE WYMYŚLAJ.")
    
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
    query = apply_stt_corrections(query)
    
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


def fuzzy_match_staff(query: str, staff_list: list, threshold: float = 0.85) -> dict | None:
    """
    Dopasuj pracownika - używa polish_mappings dla zdrobnień i błędów STT.
    Zwraca None jeśli nie ma pewności (bot dopyta).
    """
    # Użyj funkcji z polish_mappings
    result = match_staff_name(query, staff_list)
    
    if result:
        return result
    
    # Fallback - stara logika dla edge cases
    if not query or not staff_list:
        return None
    
    query = query.lower().strip()
    query = apply_stt_corrections(query)
    
    # Exact match
    for staff in staff_list:
        name = staff["name"].lower().strip()
        if query == name or query == name.split()[0]:
            return staff
    
    logger.warning(f"⚠️ Staff not found: '{query}'. Bot will ask.")
    return None

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
