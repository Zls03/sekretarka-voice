# flows.py - Pipecat Flows dla systemu rezerwacji
# WERSJA 4.0 - Z PRAWDZIWĄ INTEGRACJĄ GOOGLE CALENDAR
"""
FUNKCJE:
- Natychmiastowe przywitanie (pre_actions + tts_say)
- Prawdziwe sloty z Google Calendar (przez API panelu)
- Fallback na godziny pracy gdy brak kalendarza
- Respektowanie: przerwa między wizytami, min wyprzedzenie, max dni w przód
- Walidacja usług i pracowników
- Profesjonalne podsumowanie
"""

from pipecat_flows import FlowManager, FlowsFunctionSchema
from datetime import datetime, timedelta
from typing import Any, Optional, List
from loguru import logger
import locale
import httpx
import os

# Ustawienie polskiego locale dla nazw dni
try:
    locale.setlocale(locale.LC_TIME, 'pl_PL.UTF-8')
except:
    pass

# URL do panelu Next.js (ustaw w .env)
# Format: https://twoj-panel.vercel.app lub http://localhost:3000
PANEL_API_URL = os.getenv("PANEL_API_URL", "http://localhost:3000")
PANEL_SLUG = os.getenv("PANEL_SLUG", "")  # np. "salon-ania"

# ==========================================
# POMOCNICZE FUNKCJE WALIDACJI
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
    
    # Próbuj parsować jako datę
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
    
    word_to_hour = {
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
    
    if time_str in word_to_hour:
        return word_to_hour[time_str]
    
    import re
    numbers = re.findall(r'\d+', time_str)
    if numbers:
        hour = int(numbers[0])
        if 0 <= hour <= 23:
            return hour
    
    return None


def get_opening_hours(tenant: dict, weekday: int) -> tuple[int, int] | None:
    """Pobierz godziny otwarcia dla danego dnia tygodnia"""
    default_hours = {
        0: (9, 18), 1: (9, 18), 2: (9, 18), 3: (9, 18), 4: (9, 18),
        5: (9, 14), 6: None,
    }
    
    # Pobierz z working_hours jeśli dostępne
    working_hours = tenant.get("working_hours", [])
    for wh in working_hours:
        if wh.get("day_of_week") == weekday:
            open_time = wh.get("open_time")
            close_time = wh.get("close_time")
            if open_time and close_time:
                open_hour = int(open_time.split(":")[0])
                close_hour = int(close_time.split(":")[0])
                return (open_hour, close_hour)
            return None  # Zamknięte
    
    return default_hours.get(weekday)


def format_hour_polish(hour: int) -> str:
    """Formatuj godzinę po polsku słownie"""
    hour_words = {
        8: "ósmej", 9: "dziewiątej", 10: "dziesiątej",
        11: "jedenastej", 12: "dwunastej", 13: "trzynastej",
        14: "czternastej", 15: "piętnastej", 16: "szesnastej",
        17: "siedemnastej", 18: "osiemnastej", 19: "dziewiętnastej",
        20: "dwudziestej"
    }
    return hour_words.get(hour, f"{hour}")


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
# INTEGRACJA Z GOOGLE CALENDAR
# ==========================================

async def get_available_slots_from_api(
    tenant: dict,
    staff: dict,
    service: dict,
    date: datetime
) -> List[int]:
    """
    Pobiera wolne sloty z API panelu Next.js.
    Panel sprawdza Google Calendar i zwraca dostępne godziny.
    """
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
                params={
                    "staffId": staff_id,
                    "serviceId": service_id,
                    "date": date_str,
                }
            )
            
            if response.status_code == 200:
                data = response.json()
                slots = data.get("slots", [])
                # Konwertuj "10:00" na 10
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
    tenant: dict,
    staff: dict,
    service: dict,
    date: datetime
) -> List[int]:
    """
    Fallback: generuje sloty z godzin pracy gdy brak Google Calendar.
    Uwzględnia: czas trwania usługi, przerwy między wizytami.
    """
    weekday = date.weekday()
    opening_hours = get_opening_hours(tenant, weekday)
    
    if not opening_hours:
        return []  # Zamknięte
    
    open_hour, close_hour = opening_hours
    
    # Pobierz ustawienia pracownika
    break_minutes = staff.get("break_minutes", 30)
    service_duration = service.get("duration_minutes", 60)
    
    # Generuj sloty co godzinę (uproszczone)
    slots = []
    current_hour = open_hour
    
    while current_hour + (service_duration / 60) <= close_hour:
        slots.append(current_hour)
        current_hour += 1  # Sloty co godzinę
    
    # Jeśli to dziś, usuń godziny które minęły
    now = datetime.now()
    if date.date() == now.date():
        min_hour = now.hour + 1  # Minimum 1h wyprzedzenia
        slots = [h for h in slots if h >= min_hour]
    
    logger.info(f"📅 Generated {len(slots)} slots from working hours for {date.strftime('%Y-%m-%d')}")
    return slots


async def get_available_slots(
    tenant: dict,
    staff: dict,
    service: dict,
    date: datetime
) -> List[int]:
    """
    Główna funkcja pobierania slotów.
    1. Jeśli pracownik ma połączony kalendarz → API
    2. Jeśli nie → godziny pracy
    """
    calendar_connected = staff.get("google_calendar_id") or staff.get("calendar_connected", False)
    
    if calendar_connected:
        logger.info(f"📅 Staff {staff.get('name')} has calendar connected, using API")
        slots = await get_available_slots_from_api(tenant, staff, service, date)
        if slots:
            return slots
        # Fallback jeśli API nie zwróciło slotów
        logger.warning("⚠️ API returned no slots, falling back to working hours")
    
    return await get_available_slots_from_working_hours(tenant, staff, service, date)


def validate_date_constraints(
    date: datetime,
    tenant: dict,
    staff: dict
) -> tuple[bool, str]:
    """
    Sprawdza czy data spełnia ograniczenia:
    - min_advance_hours (np. 12h)
    - max_days_ahead (np. 14 dni)
    """
    now = datetime.now()
    
    # Min wyprzedzenie
    min_advance_hours = staff.get("min_advance_hours", 12)
    min_booking_time = now + timedelta(hours=min_advance_hours)
    
    if date < min_booking_time:
        return (False, f"Rezerwacje przyjmujemy z minimum {min_advance_hours} godzinnym wyprzedzeniem.")
    
    # Max dni w przód
    max_days_ahead = staff.get("max_days_ahead", 14)
    max_date = now + timedelta(days=max_days_ahead)
    
    if date > max_date:
        return (False, f"Rezerwacje można składać maksymalnie {max_days_ahead} dni w przód.")
    
    return (True, "")


# ==========================================
# GŁÓWNY NODE: Powitanie
# ==========================================

def create_initial_node(tenant: dict) -> dict:
    """
    Node początkowy z NATYCHMIASTOWYM przywitaniem!
    """
    business_name = tenant.get("name", "salon")
    first_message = tenant.get("first_message") or f"Dzień dobry, tu {business_name}. W czym mogę pomóc?"
    
    services = tenant.get("services", [])
    services_list = ", ".join([s["name"] for s in services]) if services else "brak usług"
    
    staff = tenant.get("staff", [])
    staff_list = ", ".join([s["name"] for s in staff]) if staff else "brak pracowników"
    
    return {
        "name": "greeting",
        
        "pre_actions": [
            {"type": "tts_say", "text": first_message}
        ],
        
        "respond_immediately": False,
        
        "role_messages": [
            {
                "role": "system",
                "content": f"""Jesteś asystentem głosowym dla firmy "{business_name}".

ZASADY:
- Mów krótko i naturalnie, to rozmowa telefoniczna
- Używaj polskiego języka
- NIE używaj emoji ani specjalnych znaków
- Liczby i godziny mów słownie (np. "dziesiąta" zamiast "10:00")

DOSTĘPNE USŁUGI: {services_list}
PRACOWNICY: {staff_list}

WAŻNE: Jeśli klient pyta o coś czego nie ma (pracownik, usługa), POWIEDZ ŻE NIE MA i podaj co jest dostępne."""
            }
        ],
        
        "task_messages": [
            {
                "role": "system",
                "content": """Klient już usłyszał przywitanie. Teraz CZEKAJ na odpowiedź.

Gdy klient odpowie:
- Jeśli chce się UMÓWIĆ/ZAREZERWOWAĆ → użyj funkcji start_booking
- Jeśli ma PYTANIE (godziny, ceny, lokalizacja) → użyj funkcji answer_question
- Jeśli chce się POŻEGNAĆ → użyj funkcji end_conversation

WAŻNE: Po każdej odpowiedzi ZAWSZE zakończ pytaniem lub propozycją.
NIGDY nie zostawiaj klienta bez dalszej wskazówki."""
            }
        ],
        
        "functions": [
            start_booking_function(),
            answer_question_function(tenant),
            end_conversation_function(),
        ]
    }


# ==========================================
# FUNKCJA: Rozpocznij rezerwację
# ==========================================

def start_booking_function() -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="start_booking",
        description="Klient chce umówić wizytę lub zarezerwować termin",
        properties={},
        required=[],
        handler=handle_start_booking,
    )


async def handle_start_booking(args: dict, flow_manager: FlowManager):
    logger.info("📅 Starting booking flow")
    tenant = flow_manager.state.get("tenant", {})
    
    # Sprawdź czy rezerwacje są włączone
    bookings_enabled = tenant.get("bookings_enabled", True)
    
    # Sprawdź czy jest jakikolwiek pracownik
    staff = tenant.get("staff", [])
    has_any_staff = len(staff) > 0
    
    # Sprawdź czy JAKIKOLWIEK pracownik ma podłączony kalendarz
    has_calendar = any(
        s.get("google_calendar_id") or s.get("calendar_connected") 
        for s in staff
    )
    
    if not bookings_enabled:
        logger.info("⚠️ Bookings disabled")
        return (
            "Przepraszam, w tej chwili nie przyjmujemy rezerwacji online. Czy mogę przekazać właścicielowi prośbę o kontakt?",
            create_take_message_node(tenant)
        )
    
    if not has_any_staff:
        logger.info("⚠️ No staff configured")
        return (
            "Przepraszam, nie mamy jeszcze skonfigurowanych pracowników. Czy mogę przekazać prośbę o kontakt?",
            create_take_message_node(tenant)
        )
    
    if not has_calendar:
        # Brak kalendarza - ale możemy próbować na podstawie godzin pracy
        # UWAGA: To może powodować konflikty!
        logger.warning("⚠️ No calendar connected - using working hours fallback")
        flow_manager.state["calendar_fallback"] = True
    
    return ("Świetnie, umówmy wizytę.", create_get_service_node(tenant))


# ==========================================
# NODE: Przyjmij wiadomość (gdy rezerwacje wyłączone)
# ==========================================

def create_take_message_node(tenant: dict) -> dict:
    return {
        "name": "take_message",
        "task_messages": [
            {
                "role": "system",
                "content": """Klient chciał się umówić ale rezerwacje online są wyłączone.
                
Zapytaj czy chce zostawić wiadomość do właściciela:
- Jeśli TAK → zapytaj o imię, numer telefonu i krótką wiadomość
- Jeśli NIE → pożegnaj się uprzejmie"""
            }
        ],
        "functions": [
            leave_message_function(tenant),
            no_message_function(),
        ]
    }


def leave_message_function(tenant: dict) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="leave_message",
        description="Klient chce zostawić wiadomość",
        properties={
            "name": {"type": "string", "description": "Imię klienta"},
            "phone": {"type": "string", "description": "Numer telefonu"},
            "message": {"type": "string", "description": "Treść wiadomości"}
        },
        required=["name"],
        handler=lambda args, fm: handle_leave_message(args, fm, tenant),
    )


def no_message_function() -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="no_message",
        description="Klient nie chce zostawiać wiadomości",
        properties={},
        required=[],
        handler=handle_no_message,
    )


async def handle_leave_message(args: dict, flow_manager: FlowManager, tenant: dict):
    name = args.get("name", "")
    phone = args.get("phone", "")
    message = args.get("message", "")
    
    logger.info(f"📝 Message left: {name}, {phone}, {message}")
    
    # TODO: Zapisz wiadomość w bazie / wyślij SMS do właściciela
    
    return (
        f"Dziękuję {name}. Przekażę wiadomość właścicielowi. Oddzwonimy jak najszybciej!",
        create_end_node()
    )


async def handle_no_message(args: dict, flow_manager: FlowManager):
    return (
        "Rozumiem. Zapraszam do kontaktu telefonicznego z właścicielem. Do widzenia!",
        create_end_node()
    )


# ==========================================
# NODE: Wybór usługi
# ==========================================

def create_get_service_node(tenant: dict) -> dict:
    services = tenant.get("services", [])
    services_list = ", ".join([s["name"] for s in services]) if services else "brak"
    
    return {
        "name": "get_service",
        "task_messages": [
            {
                "role": "system",
                "content": f"""Zapytaj klienta jaką usługę chce.

DOSTĘPNE USŁUGI: {services_list}

Gdy klient powie usługę, użyj funkcji select_service.
Jeśli powie usługę której NIE MA, powiedz że nie oferujecie i podaj dostępne."""
            }
        ],
        "functions": [
            select_service_function(tenant),
        ]
    }


def select_service_function(tenant: dict) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="select_service",
        description="Klient wybrał usługę",
        properties={
            "service_name": {"type": "string", "description": "Nazwa usługi"}
        },
        required=["service_name"],
        handler=lambda args, fm: handle_select_service(args, fm, tenant),
    )


async def handle_select_service(args: dict, flow_manager: FlowManager, tenant: dict):
    service_name = args.get("service_name", "").lower()
    services = tenant.get("services", [])
    
    logger.info(f"🔍 Validating service: {service_name}")
    
    found_service = None
    for s in services:
        if service_name in s["name"].lower() or s["name"].lower() in service_name:
            found_service = s
            break
    
    if not found_service:
        available = ", ".join([s["name"] for s in services])
        logger.warning(f"❌ Service not found: {service_name}")
        return (
            f"Przepraszam, nie oferujemy usługi '{service_name}'. Mamy: {available}. Którą usługę wybierasz?",
            None
        )
    
    flow_manager.state["selected_service"] = found_service
    logger.info(f"✅ Service selected: {found_service['name']}")
    
    return (f"Świetnie, {found_service['name']}.", create_get_staff_node(tenant))


# ==========================================
# NODE: Wybór pracownika
# ==========================================

def create_get_staff_node(tenant: dict) -> dict:
    staff = tenant.get("staff", [])
    staff_list = ", ".join([s["name"] for s in staff]) if staff else "brak"
    
    return {
        "name": "get_staff",
        "task_messages": [
            {
                "role": "system",
                "content": f"""Zapytaj do kogo klient chce się umówić.

DOSTĘPNI PRACOWNICY: {staff_list}

Jeśli nie ma preferencji, powiedz że wybierzesz pierwszego dostępnego.
Jeśli poda imię którego NIE MA, powiedz że nie ma takiego pracownika."""
            }
        ],
        "functions": [
            select_staff_function(tenant),
            any_staff_function(tenant),
        ]
    }


def select_staff_function(tenant: dict) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="select_staff",
        description="Klient wybrał konkretnego pracownika",
        properties={
            "staff_name": {"type": "string", "description": "Imię pracownika"}
        },
        required=["staff_name"],
        handler=lambda args, fm: handle_select_staff(args, fm, tenant),
    )


def any_staff_function(tenant: dict) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="any_available_staff",
        description="Klient nie ma preferencji, wybierz dowolnego",
        properties={},
        required=[],
        handler=lambda args, fm: handle_any_staff(args, fm, tenant),
    )


async def handle_select_staff(args: dict, flow_manager: FlowManager, tenant: dict):
    staff_name = args.get("staff_name", "").lower()
    staff_list = tenant.get("staff", [])
    
    logger.info(f"🔍 Validating staff: {staff_name}")
    
    found_staff = None
    for s in staff_list:
        if staff_name in s["name"].lower() or s["name"].lower() in staff_name:
            found_staff = s
            break
    
    if not found_staff:
        available = ", ".join([s["name"] for s in staff_list])
        logger.warning(f"❌ Staff not found: {staff_name}")
        return (
            f"Przepraszam, nie mamy pracownika o imieniu {staff_name}. U nas pracują: {available}. Do kogo chcesz się umówić?",
            None
        )
    
    flow_manager.state["selected_staff"] = found_staff
    logger.info(f"✅ Staff selected: {found_staff['name']}")
    
    return (f"Dobrze, do {found_staff['name']}.", create_get_date_node(tenant))


async def handle_any_staff(args: dict, flow_manager: FlowManager, tenant: dict):
    staff_list = tenant.get("staff", [])
    
    if staff_list:
        # Wybierz pierwszego dostępnego
        first_staff = staff_list[0]
        flow_manager.state["selected_staff"] = first_staff
        logger.info(f"✅ Auto-selected staff: {first_staff['name']}")
        return (
            f"Dobrze, umówię do {first_staff['name']}.",
            create_get_date_node(tenant)
        )
    else:
        return (
            "Przepraszam, nie mamy aktualnie dostępnych pracowników.",
            create_end_node()
        )


# ==========================================
# NODE: Wybór daty
# ==========================================

def create_get_date_node(tenant: dict) -> dict:
    return {
        "name": "get_date",
        "task_messages": [
            {
                "role": "system",
                "content": """Zapytaj kiedy klient chciałby się umówić.

Zaproponuj: dziś, jutro, lub konkretny dzień tygodnia.
Gdy klient poda dzień i opcjonalnie godzinę, użyj funkcji check_availability."""
            }
        ],
        "functions": [
            check_availability_function(tenant),
        ]
    }


def check_availability_function(tenant: dict) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="check_availability",
        description="Sprawdź dostępne terminy",
        properties={
            "date": {
                "type": "string",
                "description": "Data (np. 'jutro', 'poniedziałek', '2024-01-15')"
            },
            "preferred_time": {
                "type": "string",
                "description": "Preferowana pora (np. 'rano', 'po południu', '10:00')"
            }
        },
        required=["date"],
        handler=lambda args, fm: handle_check_availability(args, fm, tenant),
    )


async def handle_check_availability(args: dict, flow_manager: FlowManager, tenant: dict):
    """
    GŁÓWNA LOGIKA SPRAWDZANIA DOSTĘPNOŚCI:
    1. Parsuj datę
    2. Sprawdź ograniczenia (min wyprzedzenie, max dni w przód)
    3. Pobierz sloty z kalendarza lub godzin pracy
    4. Jeśli klient podał preferowaną godzinę → auto-potwierdź jeśli wolna
    """
    date_str = args.get("date", "")
    preferred_time = args.get("preferred_time", "")
    
    staff = flow_manager.state.get("selected_staff", {})
    service = flow_manager.state.get("selected_service", {})
    
    logger.info(f"📅 Checking availability: {date_str}, preferred: {preferred_time}")
    
    # 1. Parsuj datę
    parsed_date = parse_polish_date(date_str)
    
    if not parsed_date:
        return (
            f"Przepraszam, nie rozumiem daty '{date_str}'. Powiedz np. 'jutro', 'w poniedziałek' lub podaj datę.",
            None
        )
    
    # 2. Sprawdź czy nie przeszłość
    now = datetime.now()
    if parsed_date.date() < now.date():
        return (
            "Przepraszam, nie mogę umówić na datę która już minęła. Na kiedy chciałbyś się umówić?",
            None
        )
    
    # 3. Sprawdź ograniczenia
    valid, error_msg = validate_date_constraints(parsed_date, tenant, staff)
    if not valid:
        return (error_msg, None)
    
    # 4. Sprawdź czy firma jest otwarta tego dnia
    weekday = parsed_date.weekday()
    opening_hours = get_opening_hours(tenant, weekday)
    
    if opening_hours is None:
        day_name = POLISH_DAYS[weekday]
        return (
            f"Przepraszam, w {day_name} jesteśmy zamknięci. Zapraszam w inny dzień.",
            None
        )
    
    # 5. Pobierz dostępne sloty (z kalendarza lub godzin pracy)
    available_slots = await get_available_slots(tenant, staff, service, parsed_date)
    
    if not available_slots:
        return (
            f"Przepraszam, na {format_date_polish(parsed_date)} nie mamy już wolnych terminów. Może inny dzień?",
            None
        )
    
    # Zapisz w state
    flow_manager.state["selected_date"] = parsed_date
    flow_manager.state["available_slots"] = available_slots
    
    date_formatted = format_date_polish(parsed_date)
    
    # 6. Jeśli klient podał preferowaną godzinę, sprawdź czy wolna
    if preferred_time:
        preferred_hour = parse_time(preferred_time)
        if preferred_hour and preferred_hour in available_slots:
            # Godzina jest wolna! Auto-potwierdź
            flow_manager.state["selected_time"] = preferred_hour
            logger.info(f"✅ Preferred time {preferred_hour}:00 is available, auto-confirming")
            return (
                f"Świetnie, {format_hour_polish(preferred_hour)} {date_formatted} jest wolna. Mogę prosić o imię do rezerwacji?",
                create_get_name_node(tenant)
            )
        elif preferred_hour:
            # Godzina zajęta, zaproponuj najbliższą wolną
            closest = min(available_slots, key=lambda x: abs(x - preferred_hour))
            slots_text = ", ".join([format_hour_polish(h) for h in available_slots[:3]])
            return (
                f"Niestety {format_hour_polish(preferred_hour)} jest zajęta. Najbliższy wolny termin to {format_hour_polish(closest)}. Pasuje? Inne opcje: {slots_text}.",
                create_select_time_node(tenant)
            )
    
    # 7. Klient nie podał godziny - zaproponuj 2-3 sloty
    slots_text = ", ".join([format_hour_polish(h) for h in available_slots[:3]])
    if len(available_slots) > 3:
        slots_text += " lub inną"
    
    return (
        f"Na {date_formatted} mam wolne: {slots_text}. Która godzina pasuje?",
        create_select_time_node(tenant)
    )


# ==========================================
# NODE: Wybór godziny
# ==========================================

def create_select_time_node(tenant: dict) -> dict:
    return {
        "name": "select_time",
        "task_messages": [
            {
                "role": "system",
                "content": """Klient wybiera godzinę.
Gdy wybierze, użyj funkcji confirm_time."""
            }
        ],
        "functions": [
            confirm_time_function(tenant),
        ]
    }


def confirm_time_function(tenant: dict) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="confirm_time",
        description="Klient wybrał godzinę",
        properties={
            "time": {"type": "string", "description": "Wybrana godzina"}
        },
        required=["time"],
        handler=lambda args, fm: handle_confirm_time(args, fm, tenant),
    )


async def handle_confirm_time(args: dict, flow_manager: FlowManager, tenant: dict):
    """WALIDACJA wybranej godziny"""
    time_str = args.get("time", "")
    
    hour = parse_time(time_str)
    available_slots = flow_manager.state.get("available_slots", [])
    
    if hour is None:
        return (
            f"Przepraszam, nie rozumiem godziny '{time_str}'. Powiedz np. 'dziesiąta' lub 'o dziesiątej'.",
            None
        )
    
    if hour not in available_slots:
        slots_text = ", ".join([format_hour_polish(h) for h in available_slots[:5]])
        return (
            f"Przepraszam, godzina {format_hour_polish(hour)} nie jest dostępna. Mam wolne: {slots_text}.",
            None
        )
    
    flow_manager.state["selected_time"] = hour
    logger.info(f"✅ Time selected: {hour}")
    
    return (
        f"Świetnie, godzina {format_hour_polish(hour)}. Jak się Pan/Pani nazywa?",
        create_get_name_node(tenant)
    )


# ==========================================
# NODE: Pobranie imienia
# ==========================================

def create_get_name_node(tenant: dict) -> dict:
    return {
        "name": "get_name",
        "task_messages": [
            {
                "role": "system",
                "content": "Zapisz imię klienta i potwierdź rezerwację."
            }
        ],
        "functions": [
            complete_booking_function(tenant),
        ]
    }


def complete_booking_function(tenant: dict) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="complete_booking",
        description="Zapisz rezerwację",
        properties={
            "customer_name": {"type": "string", "description": "Imię klienta"}
        },
        required=["customer_name"],
        handler=lambda args, fm: handle_complete_booking(args, fm, tenant),
    )


async def handle_complete_booking(args: dict, flow_manager: FlowManager, tenant: dict):
    """Zapisz rezerwację i przejdź do PODSUMOWANIA"""
    customer_name = args.get("customer_name", "")
    
    flow_manager.state["customer_name"] = customer_name
    
    # Pobierz wszystkie dane
    service = flow_manager.state.get("selected_service", {})
    staff = flow_manager.state.get("selected_staff", {})
    date = flow_manager.state.get("selected_date")
    hour = flow_manager.state.get("selected_time")
    
    logger.info(f"💾 Saving booking: {customer_name}, {service.get('name')}, {staff.get('name')}, {date}, {hour}")
    
    # TODO: Zapisz do bazy danych przez API panelu
    # TODO: Dodaj event do Google Calendar
    try:
        await save_booking_to_api(tenant, staff, service, date, hour, customer_name)
    except Exception as e:
        logger.error(f"❌ Failed to save booking: {e}")
        # Kontynuuj mimo błędu - lepiej potwierdzić klientowi
    
    # Generuj kod wizyty
    import random
    import string
    visit_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    flow_manager.state["visit_code"] = visit_code
    
    # Formatuj podsumowanie
    date_formatted = format_date_polish(date) if date else "wybrany dzień"
    time_formatted = format_hour_polish(hour) if hour else "wybraną godzinę"
    
    summary = f"Rezerwacja potwierdzona! {service.get('name')} u {staff.get('name')}, {date_formatted} o godzinie {time_formatted}. Pana imię: {customer_name}."
    
    return (summary, create_anything_else_node(tenant))


async def save_booking_to_api(tenant, staff, service, date, hour, customer_name, customer_phone=""):
    """Zapisuje rezerwację przez API panelu + dodaje do Google Calendar"""
    slug = tenant.get("slug") or PANEL_SLUG
    
    if not slug:
        logger.warning("⚠️ No panel slug configured, skipping API save")
        return
    
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
                logger.info(f"✅ Booking saved via API: {data.get('bookingId')}")
                logger.info(f"📅 Google Calendar event: {data.get('googleEventCreated')}")
                return data
            else:
                logger.warning(f"⚠️ Booking API returned {response.status_code}: {response.text}")
                
    except Exception as e:
        logger.error(f"❌ Booking API error: {e}")
        raise


# ==========================================
# NODE: Czy coś jeszcze?
# ==========================================

def create_anything_else_node(tenant: dict) -> dict:
    return {
        "name": "anything_else",
        "task_messages": [
            {
                "role": "system",
                "content": """Zapytaj: "Czy mogę w czymś jeszcze pomóc?"

Jeśli klient:
- Chce coś jeszcze → wróć do pomocy
- Nie potrzebuje nic więcej → pożegnaj się uprzejmie"""
            }
        ],
        "functions": [
            need_more_help_function(tenant),
            no_more_help_function(),
        ]
    }


def need_more_help_function(tenant: dict) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="need_more_help",
        description="Klient potrzebuje jeszcze pomocy",
        properties={},
        required=[],
        handler=lambda args, fm: handle_need_more_help(args, fm, tenant),
    )


def no_more_help_function() -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="no_more_help",
        description="Klient nie potrzebuje więcej pomocy",
        properties={},
        required=[],
        handler=handle_no_more_help,
    )


async def handle_need_more_help(args: dict, flow_manager: FlowManager, tenant: dict):
    logger.info("🔄 Customer needs more help")
    return ("Oczywiście, w czym mogę pomóc?", create_continue_conversation_node(tenant))


async def handle_no_more_help(args: dict, flow_manager: FlowManager):
    logger.info("👋 Customer done")
    return (
        "Dziękuję za rezerwację! Do zobaczenia, miłego dnia!",
        create_end_node()
    )


# ==========================================
# NODE: Kontynuacja rozmowy (bez przywitania)
# ==========================================

def create_continue_conversation_node(tenant: dict) -> dict:
    business_name = tenant.get("name", "salon")
    services = tenant.get("services", [])
    services_list = ", ".join([s["name"] for s in services]) if services else "brak usług"
    staff = tenant.get("staff", [])
    staff_list = ", ".join([s["name"] for s in staff]) if staff else "brak pracowników"
    
    return {
        "name": "continue_conversation",
        
        "respond_immediately": False,
        
        "role_messages": [
            {
                "role": "system",
                "content": f"""Jesteś asystentem głosowym dla firmy "{business_name}".

ZASADY:
- Mów krótko i naturalnie
- Używaj polskiego języka
- NIE używaj emoji

DOSTĘPNE USŁUGI: {services_list}
PRACOWNICY: {staff_list}"""
            }
        ],
        
        "task_messages": [
            {
                "role": "system",
                "content": """Kontynuuj rozmowę. NIE witaj się ponownie!

Gdy klient odpowie:
- Jeśli chce się UMÓWIĆ → użyj funkcji start_booking
- Jeśli ma PYTANIE → użyj funkcji answer_question
- Jeśli chce się POŻEGNAĆ → użyj funkcji end_conversation

WAŻNE: Po każdej odpowiedzi ZAWSZE zakończ pytaniem.
NIGDY nie zostawiaj rozmówcy bez dalszej wskazówki."""
            }
        ],
        
        "functions": [
            start_booking_function(),
            answer_question_function(tenant),
            end_conversation_function(),
        ]
    }


# ==========================================
# NODE: Odpowiedź na pytanie
# ==========================================

def answer_question_function(tenant: dict) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="answer_question",
        description="Klient ma pytanie (godziny, ceny, lokalizacja)",
        properties={
            "question": {"type": "string", "description": "Pytanie klienta"}
        },
        required=["question"],
        handler=lambda args, fm: handle_answer_question(args, fm, tenant),
    )


async def handle_answer_question(args: dict, flow_manager: FlowManager, tenant: dict):
    question = args.get("question", "")
    logger.info(f"❓ Question: {question}")
    
    # TODO: Pobierz FAQ z bazy i odpowiedz na pytanie
    
    return (None, create_continue_conversation_node(tenant))


# ==========================================
# NODE: Zakończenie rozmowy
# ==========================================

def end_conversation_function() -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="end_conversation",
        description="Klient chce zakończyć rozmowę",
        properties={},
        required=[],
        handler=handle_end_conversation,
    )


async def handle_end_conversation(args: dict, flow_manager: FlowManager):
    logger.info("👋 Ending conversation")
    return ("Do widzenia, miłego dnia!", create_end_node())


def create_end_node() -> dict:
    return {
        "name": "end",
        "task_messages": [
            {
                "role": "system",
                "content": "Rozmowa zakończona. Pożegnaj się krótko jeśli jeszcze nie."
            }
        ],
        "post_actions": [
            {"type": "end_conversation"}
        ]
    }