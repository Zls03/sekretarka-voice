# flows.py - Pipecat Flows dla systemu rezerwacji
# WERSJA 2.0 - Profesjonalna z pełną walidacją
"""
FUNKCJE:
- Natychmiastowe przywitanie (pre_actions + tts_say)
- Walidacja godzin otwarcia
- Walidacja kalendarza (nie rezerwuj w przeszłości)
- Walidacja usług i pracowników
- Podsumowanie po rezerwacji
- Pytanie "czy coś jeszcze?"
"""

from pipecat_flows import FlowManager, FlowsFunctionSchema
from datetime import datetime, timedelta
from typing import Any, Optional
from loguru import logger
import locale

# Ustawienie polskiego locale dla nazw dni
try:
    locale.setlocale(locale.LC_TIME, 'pl_PL.UTF-8')
except:
    pass

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
        # Znajdź najbliższy taki dzień
        target_weekday = POLISH_DAYS_REVERSE[date_str]
        days_ahead = target_weekday - today.weekday()
        if days_ahead <= 0:  # Już minął ten dzień w tym tygodniu
            days_ahead += 7
        return today + timedelta(days=days_ahead)
    
    # Próbuj parsować jako datę
    for fmt in ["%Y-%m-%d", "%d.%m.%Y", "%d.%m", "%d-%m-%Y", "%d/%m/%Y"]:
        try:
            parsed = datetime.strptime(date_str, fmt)
            if parsed.year == 1900:  # Brak roku
                parsed = parsed.replace(year=today.year)
            return parsed
        except:
            pass
    
    return None


def parse_time(time_str: str) -> Optional[int]:
    """Parsuj godzinę (słownie lub numerycznie) → zwraca godzinę jako int"""
    time_str = time_str.lower().strip()
    
    # Słowne godziny
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
    
    # Próbuj wyciągnąć liczbę
    import re
    numbers = re.findall(r'\d+', time_str)
    if numbers:
        hour = int(numbers[0])
        if 0 <= hour <= 23:
            return hour
    
    return None


def get_opening_hours(tenant: dict, weekday: int) -> tuple[int, int] | None:
    """Pobierz godziny otwarcia dla danego dnia tygodnia"""
    # Domyślne godziny jeśli brak w tenant
    default_hours = {
        0: (9, 18),   # Poniedziałek
        1: (9, 18),   # Wtorek
        2: (9, 18),   # Środa
        3: (9, 18),   # Czwartek
        4: (9, 18),   # Piątek
        5: (9, 14),   # Sobota (krócej)
        6: None,      # Niedziela - zamknięte
    }
    
    # TODO: Pobierz z tenant.opening_hours jeśli dostępne
    opening_hours = tenant.get("opening_hours", {})
    
    day_name = POLISH_DAYS[weekday]
    if day_name in opening_hours:
        hours = opening_hours[day_name]
        if hours is None or hours.get("closed"):
            return None
        return (hours.get("open", 9), hours.get("close", 18))
    
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
# GŁÓWNY NODE: Powitanie
# ==========================================

def create_initial_node(tenant: dict) -> dict:
    """
    Node początkowy z NATYCHMIASTOWYM przywitaniem!
    Używa pre_actions z tts_say żeby od razu się przywitać.
    """
    business_name = tenant.get("name", "salon")
    first_message = tenant.get("first_message") or f"Dzień dobry, tu {business_name}. W czym mogę pomóc?"
    
    services = tenant.get("services", [])
    services_list = ", ".join([s["name"] for s in services]) if services else "brak usług"
    
    staff = tenant.get("staff", [])
    staff_list = ", ".join([s["name"] for s in staff]) if staff else "brak pracowników"
    
    return {
        "name": "greeting",
        
        # ⚡ NATYCHMIASTOWE PRZYWITANIE - przed LLM!
        "pre_actions": [
            {
                "type": "tts_say",
                "text": first_message
            }
        ],
        
        # ⏸️ NIE generuj odpowiedzi LLM od razu - czekaj na klienta
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

ODPOWIADAJ krótko i naturalnie."""
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
    return ("Świetnie, umówmy wizytę.", create_get_service_node(tenant))


# ==========================================
# NODE: Wybór usługi (z walidacją)
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
            "service_name": {
                "type": "string",
                "description": "Nazwa usługi"
            }
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
# NODE: Wybór pracownika (z walidacją)
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
    
    found_staff = None
    for s in staff_list:
        if staff_name in s["name"].lower() or s["name"].lower() in staff_name:
            found_staff = s
            break
    
    if not found_staff:
        available = ", ".join([s["name"] for s in staff_list])
        return (
            f"Przepraszam, nie mamy pracownika {staff_name}. U nas pracują: {available}.",
            None
        )
    
    flow_manager.state["selected_staff"] = found_staff
    logger.info(f"✅ Staff selected: {found_staff['name']}")
    
    return (f"Dobrze, do {found_staff['name']}.", create_get_date_node(tenant))


async def handle_any_staff(args: dict, flow_manager: FlowManager, tenant: dict):
    staff_list = tenant.get("staff", [])
    
    if staff_list:
        first_staff = staff_list[0]
        flow_manager.state["selected_staff"] = first_staff
        return (f"Dobrze, umówię do {first_staff['name']}.", create_get_date_node(tenant))
    else:
        return ("Przepraszam, nie mamy dostępnych pracowników.", create_end_node())


# ==========================================
# NODE: Wybór daty (z WALIDACJĄ godzin otwarcia!)
# ==========================================

def create_get_date_node(tenant: dict) -> dict:
    return {
        "name": "get_date",
        "task_messages": [
            {
                "role": "system",
                "content": """Zapytaj kiedy klient chciałby się umówić.

Proponuj: dziś, jutro, pojutrze, lub konkretny dzień tygodnia.
Gdy klient poda dzień, użyj funkcji check_availability."""
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
                "description": "Preferowana pora (opcjonalnie)"
            }
        },
        required=["date"],
        handler=lambda args, fm: handle_check_availability(args, fm, tenant),
    )


async def handle_check_availability(args: dict, flow_manager: FlowManager, tenant: dict):
    """
    WALIDACJA KALENDARZA:
    1. Czy to nie przeszłość?
    2. Czy firma jest otwarta tego dnia?
    3. Jakie godziny są dostępne?
    """
    date_str = args.get("date", "")
    preferred_time = args.get("preferred_time", "")
    
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
    
    # 3. Sprawdź godziny otwarcia
    weekday = parsed_date.weekday()
    opening_hours = get_opening_hours(tenant, weekday)
    
    if opening_hours is None:
        day_name = POLISH_DAYS[weekday]
        return (
            f"Przepraszam, w {day_name} jesteśmy zamknięci. Zapraszam w inny dzień, od poniedziałku do soboty.",
            None
        )
    
    open_hour, close_hour = opening_hours
    
    # 4. Jeśli to dziś, sprawdź czy nie jest za późno
    if parsed_date.date() == now.date():
        current_hour = now.hour
        if current_hour >= close_hour:
            return (
                f"Przepraszam, na dziś jest już za późno, pracujemy do {format_hour_polish(close_hour)}. Może jutro?",
                None
            )
        # Ogranicz dostępne godziny do tych które jeszcze nie minęły
        open_hour = max(open_hour, current_hour + 1)
    
    # 5. Generuj dostępne sloty
    # TODO: Sprawdź w Google Calendar które sloty są zajęte
    available_slots = []
    for hour in range(open_hour, close_hour):
        available_slots.append(hour)
    
    if not available_slots:
        return (
            f"Przepraszam, na {format_date_polish(parsed_date)} nie mamy już wolnych terminów. Może inny dzień?",
            None
        )
    
    # Zapisz w state
    flow_manager.state["selected_date"] = parsed_date
    flow_manager.state["available_slots"] = available_slots
    
    # Formatuj sloty słownie
    slots_text = ", ".join([format_hour_polish(h) for h in available_slots[:5]])
    if len(available_slots) > 5:
        slots_text += " i inne"
    
    date_formatted = format_date_polish(parsed_date)
    
    return (
        f"Na {date_formatted} mam wolne terminy: {slots_text}. Która godzina pasuje?",
        create_select_time_node(tenant)
    )


# ==========================================
# NODE: Wybór godziny (z WALIDACJĄ!)
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
    
    # TODO: Zapisz do bazy danych i Google Calendar
    
    # Generuj kod wizyty
    import random
    import string
    visit_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    flow_manager.state["visit_code"] = visit_code
    
    # Formatuj podsumowanie
    date_formatted = format_date_polish(date) if date else "wybrany dzień"
    time_formatted = format_hour_polish(hour) if hour else "wybraną godzinę"
    
    summary = f"Rezerwacja potwierdzona! {service.get('name')} u {staff.get('name')}, {date_formatted} o godzinie {time_formatted}. Pana imię: {customer_name}."
    
    # Przejdź do node'a "czy coś jeszcze?"
    return (summary, create_anything_else_node(tenant))


# ==========================================
# NODE: Czy coś jeszcze? (NOWY!)
# ==========================================

def create_anything_else_node(tenant: dict) -> dict:
    """Node pytający czy klient potrzebuje czegoś jeszcze"""
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
        description="Klient nie potrzebuje więcej pomocy, chce zakończyć",
        properties={},
        required=[],
        handler=handle_no_more_help,
    )


async def handle_need_more_help(args: dict, flow_manager: FlowManager, tenant: dict):
    logger.info("🔄 Customer needs more help")
    return ("Oczywiście, w czym mogę pomóc?", create_initial_node(tenant))


async def handle_no_more_help(args: dict, flow_manager: FlowManager):
    logger.info("👋 Customer done, ending conversation")
    return (
        "Dziękuję za rezerwację! Do zobaczenia, miłego dnia!",
        create_end_node()
    )


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
    
    # Przygotuj kontekst z info o firmie
    address = tenant.get("address", "")
    additional_info = tenant.get("additional_info", "")
    
    # TODO: Pobierz FAQ z bazy
    
    # Wróć do greeting po odpowiedzi
    return (None, create_initial_node(tenant))


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
    """Końcowy node - pożegnanie i rozłączenie"""
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