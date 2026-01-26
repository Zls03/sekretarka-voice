# flows.py - Pipecat Flows dla systemu rezerwacji
# WERSJA 5.0 - Podzielony na moduły, naprawione odpowiedzi na pytania
"""
GŁÓWNA LOGIKA:
- Node'y i handlery
- Importuje helpers z flows_helpers.py
"""

from pipecat_flows import FlowManager, FlowsFunctionSchema
from datetime import datetime
from loguru import logger
import random
import string

# Import helperów
from flows_helpers import (
    parse_polish_date, parse_time,
    format_hour_polish, format_date_polish,
    get_opening_hours, validate_date_constraints,
    get_available_slots, save_booking_to_api,
    build_business_context, POLISH_DAYS
)


# ==========================================
# NODE: Powitanie
# ==========================================

def create_initial_node(tenant: dict, greeting_played: bool = False) -> dict:
    business_name = tenant.get("name", "salon")
    first_message = tenant.get("first_message") or f"Dzień dobry, tu {business_name}. W czym mogę pomóc?"
    
    services = tenant.get("services", [])
    services_list = ", ".join([s["name"] for s in services]) if services else "brak usług"
    
    staff = tenant.get("staff", [])
    staff_list = ", ".join([s["name"] for s in staff]) if staff else "brak pracowników"
    
    # Jeśli powitanie już odtworzone przez Twilio <Play> - nie mów znowu
    if greeting_played:
        pre_actions = []
        logger.info("🎵 Greeting already played by Twilio - skipping TTS")
    else:
        pre_actions = [{"type": "tts_say", "text": first_message}]
        logger.info("🔊 Using TTS for greeting")
    
    return {
        "name": "greeting",
        "pre_actions": pre_actions,
        "respond_immediately": False,
        "role_messages": [{
            "role": "system",
            "content": f"""Jesteś asystentem głosowym dla firmy "{business_name}".

ZASADY:
- Mów krótko i naturalnie
- Używaj polskiego języka
- NIE używaj emoji
- Godziny mów słownie (dziesiąta, nie 10:00)

DOSTĘPNE USŁUGI: {services_list}
PRACOWNICY: {staff_list}"""
        }],
        "task_messages": [{
            "role": "system",
            "content": """Klient usłyszał przywitanie. CZEKAJ na odpowiedź.

- Chce się UMÓWIĆ → start_booking
- Ma PYTANIE → answer_question  
- Chce się POŻEGNAĆ → end_conversation"""
        }],
        "functions": [
            start_booking_function(),
            answer_question_function(tenant),
        ]
    }

# ==========================================
# FUNKCJA: Odpowiedź na pytanie (NAPRAWIONA!)
# ==========================================

def answer_question_function(tenant: dict) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="answer_question",
        description="Klient ma pytanie (godziny, ceny, lokalizacja, inne)",
        properties={
            "question": {"type": "string", "description": "Pytanie klienta"}
        },
        required=["question"],
        handler=lambda args, fm: handle_answer_question(args, fm, tenant),
    )


async def handle_answer_question(args: dict, flow_manager: FlowManager, tenant: dict):
    question = args.get("question", "")
    logger.info(f"❓ Question: {question}")
    
    # Buduj kontekst z danych firmy
    context = build_business_context(tenant)
    
    # Przejdź do node'a który ODPOWIE na pytanie
    return (None, create_answer_node(tenant, question, context))

def create_answer_node(tenant: dict, question: str, context: str) -> dict:
    """Node który odpowiada na pytanie klienta"""
    
    question_lower = question.lower()
    is_service_question = any(word in question_lower for word in ["usług", "cen", "ile kosztuje", "cennik"])
    booking_enabled = tenant.get("booking_enabled", 1) == 1
    
    # Losowa końcówka
    endings = [
        "Czy mogę jeszcze w czymś pomóc?",
        "Czy masz jeszcze jakieś pytania?",
        "Czy jest coś jeszcze, w czym mogę pomóc?",
    ]
    
    if is_service_question and booking_enabled:
        endings.append("Jeśli chcesz, mogę umówić wizytę.")
    
    ending = random.choice(endings)
    
    return {
        "name": "answer_question_node",
        "role_messages": [{
            "role": "system",
            "content": f"""Odpowiedz na pytanie klienta.

INFORMACJE O FIRMIE:
{context}

ZASADY:
- Odpowiedz KRÓTKO (1-2 zdania)
- Użyj informacji z FAQ jeśli pasują
- Na końcu powiedz: "{ending}"
- NIE proponuj rezerwacji przy każdym pytaniu"""
        }],
        "task_messages": [{
            "role": "system",
            "content": f"""Pytanie: "{question}"

Po odpowiedzi CZEKAJ na reakcję klienta."""
        }],
        "functions": [
            start_booking_function(),
            answer_question_function(tenant),
        ]
    }

# ==========================================
# FUNKCJA: Rozpocznij rezerwację
# ==========================================

def start_booking_function() -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="start_booking",
        description="Klient chce umówić wizytę",
        properties={},
        required=[],
        handler=handle_start_booking,
    )


async def handle_start_booking(args: dict, flow_manager: FlowManager):
    logger.info("📅 Starting booking flow")
    tenant = flow_manager.state.get("tenant", {})
    
    staff = tenant.get("staff", [])
    if not staff:
        return ("Przepraszam, nie mamy skonfigurowanych pracowników. Czy mogę przekazać wiadomość?", 
                create_take_message_node(tenant))
    
    return ("Świetnie, umówmy wizytę.", create_get_service_node(tenant))


# ==========================================
# NODE: Wybór usługi
# ==========================================

def create_get_service_node(tenant: dict) -> dict:
    services = tenant.get("services", [])
    services_list = ", ".join([s["name"] for s in services]) if services else "brak"
    
    return {
        "name": "get_service",
        "task_messages": [{
            "role": "system",
            "content": f"""Zapytaj jaką usługę klient wybiera.
DOSTĘPNE: {services_list}
Gdy powie usługę → select_service"""
        }],
        "functions": [select_service_function(tenant)]
    }


def select_service_function(tenant: dict) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="select_service",
        description="Klient wybrał usługę",
        properties={"service_name": {"type": "string", "description": "Nazwa usługi"}},
        required=["service_name"],
        handler=lambda args, fm: handle_select_service(args, fm, tenant),
    )


async def handle_select_service(args: dict, flow_manager: FlowManager, tenant: dict):
    service_name = args.get("service_name", "").lower()
    services = tenant.get("services", [])
    
    found = None
    for s in services:
        if service_name in s["name"].lower() or s["name"].lower() in service_name:
            found = s
            break
    
    if not found:
        available = ", ".join([s["name"] for s in services])
        return (f"Nie mamy usługi '{service_name}'. Dostępne: {available}.", None)
    
    flow_manager.state["selected_service"] = found
    logger.info(f"✅ Service: {found['name']}")
    return (f"Świetnie, {found['name']}.", create_get_staff_node(tenant))


# ==========================================
# NODE: Wybór pracownika
# ==========================================

def create_get_staff_node(tenant: dict) -> dict:
    staff = tenant.get("staff", [])
    staff_list = ", ".join([s["name"] for s in staff])
    
    return {
        "name": "get_staff",
        "task_messages": [{
            "role": "system",
            "content": f"""Zapytaj do kogo klient chce się umówić.
PRACOWNICY: {staff_list}
Jeśli bez preferencji → any_available_staff"""
        }],
        "functions": [
            select_staff_function(tenant),
            any_staff_function(tenant),
        ]
    }


def select_staff_function(tenant: dict) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="select_staff",
        description="Klient wybrał pracownika",
        properties={"staff_name": {"type": "string", "description": "Imię"}},
        required=["staff_name"],
        handler=lambda args, fm: handle_select_staff(args, fm, tenant),
    )


def any_staff_function(tenant: dict) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="any_available_staff",
        description="Klient nie ma preferencji",
        properties={},
        required=[],
        handler=lambda args, fm: handle_any_staff(args, fm, tenant),
    )


async def handle_select_staff(args: dict, flow_manager: FlowManager, tenant: dict):
    staff_name = args.get("staff_name", "").lower()
    staff_list = tenant.get("staff", [])
    
    found = None
    for s in staff_list:
        if staff_name in s["name"].lower() or s["name"].lower() in staff_name:
            found = s
            break
    
    if not found:
        available = ", ".join([s["name"] for s in staff_list])
        return (f"Nie mamy pracownika {staff_name}. U nas pracują: {available}.", None)
    
    flow_manager.state["selected_staff"] = found
    logger.info(f"✅ Staff: {found['name']}")
    return (f"Dobrze, do {found['name']}.", create_get_date_node(tenant))


async def handle_any_staff(args: dict, flow_manager: FlowManager, tenant: dict):
    staff_list = tenant.get("staff", [])
    if staff_list:
        first = staff_list[0]
        flow_manager.state["selected_staff"] = first
        return (f"Umówię do {first['name']}.", create_get_date_node(tenant))
    return ("Brak dostępnych pracowników.", create_end_node())


# ==========================================
# NODE: Wybór daty
# ==========================================

def create_get_date_node(tenant: dict) -> dict:
    return {
        "name": "get_date",
        "task_messages": [{
            "role": "system",
            "content": """Zapytaj kiedy klient chce się umówić.
Gdy poda datę (i opcjonalnie godzinę) → check_availability"""
        }],
        "functions": [check_availability_function(tenant)]
    }


def check_availability_function(tenant: dict) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="check_availability",
        description="Sprawdź dostępność",
        properties={
            "date": {"type": "string", "description": "Data (jutro, poniedziałek, 2024-01-15)"},
            "preferred_time": {"type": "string", "description": "Preferowana godzina"}
        },
        required=["date"],
        handler=lambda args, fm: handle_check_availability(args, fm, tenant),
    )


async def handle_check_availability(args: dict, flow_manager: FlowManager, tenant: dict):
    date_str = args.get("date", "")
    preferred_time = args.get("preferred_time", "")
    
    staff = flow_manager.state.get("selected_staff", {})
    service = flow_manager.state.get("selected_service", {})
    
    parsed_date = parse_polish_date(date_str)
    if not parsed_date:
        return (f"Nie rozumiem daty '{date_str}'. Powiedz np. jutro, w poniedziałek.", None)
    
    if parsed_date.date() < datetime.now().date():
        return ("Nie mogę umówić na datę która minęła.", None)
    
    valid, error = validate_date_constraints(parsed_date, tenant, staff)
    if not valid:
        return (error, None)
    
    weekday = parsed_date.weekday()
    if get_opening_hours(tenant, weekday) is None:
        return (f"W {POLISH_DAYS[weekday]} jesteśmy zamknięci.", None)
    
    slots = await get_available_slots(tenant, staff, service, parsed_date)
    if not slots:
        return (f"Na {format_date_polish(parsed_date)} brak wolnych terminów.", None)
    
    flow_manager.state["selected_date"] = parsed_date
    flow_manager.state["available_slots"] = slots
    
    # Jeśli podał preferowaną godzinę
    if preferred_time:
        hour = parse_time(preferred_time)
        if hour and hour in slots:
            flow_manager.state["selected_time"] = hour
            return (f"{format_hour_polish(hour)} {format_date_polish(parsed_date)} jest wolna. Jak się Pan nazywa?",
                    create_get_name_node(tenant))
        elif hour:
            closest = min(slots, key=lambda x: abs(x - hour))
            return (f"{format_hour_polish(hour)} zajęta. Najbliższa wolna: {format_hour_polish(closest)}.", 
                    create_select_time_node(tenant))
    
    slots_text = ", ".join([format_hour_polish(h) for h in slots[:3]])
    return (f"Na {format_date_polish(parsed_date)} mam: {slots_text}. Która pasuje?", 
            create_select_time_node(tenant))


# ==========================================
# NODE: Wybór godziny
# ==========================================

def create_select_time_node(tenant: dict) -> dict:
    return {
        "name": "select_time",
        "task_messages": [{"role": "system", "content": "Klient wybiera godzinę → confirm_time"}],
        "functions": [confirm_time_function(tenant)]
    }


def confirm_time_function(tenant: dict) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="confirm_time",
        description="Klient wybrał godzinę",
        properties={"time": {"type": "string", "description": "Godzina"}},
        required=["time"],
        handler=lambda args, fm: handle_confirm_time(args, fm, tenant),
    )


async def handle_confirm_time(args: dict, flow_manager: FlowManager, tenant: dict):
    time_str = args.get("time", "")
    hour = parse_time(time_str)
    slots = flow_manager.state.get("available_slots", [])
    
    if hour is None:
        return (f"Nie rozumiem godziny '{time_str}'.", None)
    
    if hour not in slots:
        slots_text = ", ".join([format_hour_polish(h) for h in slots[:5]])
        return (f"Godzina {format_hour_polish(hour)} niedostępna. Mam: {slots_text}.", None)
    
    flow_manager.state["selected_time"] = hour
    return (f"Godzina {format_hour_polish(hour)}. Jak się Pan nazywa?", create_get_name_node(tenant))


# ==========================================
# NODE: Imię i zakończenie rezerwacji
# ==========================================

def create_get_name_node(tenant: dict) -> dict:
    return {
        "name": "get_name",
        "task_messages": [{"role": "system", "content": "Zapisz imię → complete_booking"}],
        "functions": [complete_booking_function(tenant)]
    }


def complete_booking_function(tenant: dict) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="complete_booking",
        description="Zapisz rezerwację",
        properties={"customer_name": {"type": "string", "description": "Imię"}},
        required=["customer_name"],
        handler=lambda args, fm: handle_complete_booking(args, fm, tenant),
    )


async def handle_complete_booking(args: dict, flow_manager: FlowManager, tenant: dict):
    name = args.get("customer_name", "")
    flow_manager.state["customer_name"] = name
    
    service = flow_manager.state.get("selected_service", {})
    staff = flow_manager.state.get("selected_staff", {})
    date = flow_manager.state.get("selected_date")
    hour = flow_manager.state.get("selected_time")
    
    logger.info(f"💾 Booking: {name}, {service.get('name')}, {staff.get('name')}, {date}, {hour}")
    
    try:
        await save_booking_to_api(tenant, staff, service, date, hour, name)
    except Exception as e:
        logger.error(f"❌ Save error: {e}")
    
    date_text = format_date_polish(date) if date else "wybrany dzień"
    time_text = format_hour_polish(hour) if hour else "wybraną godzinę"
    
    return (f"Gotowe! {service.get('name')} u {staff.get('name')}, {date_text} o {time_text}. Do zobaczenia!",
            create_anything_else_node(tenant))


# ==========================================
# NODE: Czy coś jeszcze?
# ==========================================

def create_anything_else_node(tenant: dict) -> dict:
    return {
        "name": "anything_else",
        "task_messages": [{"role": "system", "content": "Zapytaj czy możesz jeszcze pomóc."}],
        "functions": [
            need_more_help_function(tenant),
            no_more_help_function(),
        ]
    }


def need_more_help_function(tenant: dict) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="need_more_help",
        description="Klient chce jeszcze pomoc",
        properties={},
        required=[],
        handler=lambda args, fm: (None, create_continue_conversation_node(tenant)),
    )


def no_more_help_function() -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="no_more_help",
        description="Klient kończy",
        properties={},
        required=[],
        handler=lambda args, fm: ("Do widzenia!", create_end_node()),
    )


# ==========================================
# NODE: Kontynuacja rozmowy
# ==========================================

def create_continue_conversation_node(tenant: dict) -> dict:
    services = tenant.get("services", [])
    staff = tenant.get("staff", [])
    
    return {
        "name": "continue_conversation",
        "respond_immediately": False,
        "role_messages": [{
            "role": "system",
            "content": f"""Kontynuuj rozmowę. NIE witaj się ponownie.
USŁUGI: {", ".join([s["name"] for s in services])}
PRACOWNICY: {", ".join([s["name"] for s in staff])}"""
        }],
        "task_messages": [{
            "role": "system",
            "content": "Umówić → start_booking, Pytanie → answer_question, Koniec → end_conversation"
        }],
        "functions": [
            start_booking_function(),
            answer_question_function(tenant),
        ]
    }


# ==========================================
# NODE: Przyjmij wiadomość
# ==========================================

def create_take_message_node(tenant: dict) -> dict:
    return {
        "name": "take_message",
        "task_messages": [{"role": "system", "content": "Zapytaj czy zostawić wiadomość do właściciela."}],
        "functions": [
            leave_message_function(tenant),
            no_message_function(),
        ]
    }


def leave_message_function(tenant: dict) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="leave_message",
        description="Klient zostawia wiadomość",
        properties={
            "name": {"type": "string"},
            "phone": {"type": "string"},
            "message": {"type": "string"}
        },
        required=["name"],
        handler=lambda args, fm: handle_leave_message(args, fm, tenant),
    )


def no_message_function() -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="no_message",
        description="Nie zostawia wiadomości",
        properties={},
        required=[],
        handler=lambda args, fm: ("Rozumiem. Do widzenia!", create_end_node()),
    )


async def handle_leave_message(args: dict, flow_manager: FlowManager, tenant: dict):
    name = args.get("name", "")
    logger.info(f"📝 Message from: {name}")
    # TODO: Wyślij email do właściciela
    return (f"Dziękuję {name}. Przekażę wiadomość, oddzwonimy!", create_end_node())


# ==========================================
# NODE: Zakończenie
# ==========================================

def end_conversation_function() -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="end_conversation",
        description="Klient żegna się lub kończy rozmowę (np. 'do widzenia', 'dziękuję', 'to wszystko')",
        properties={},
        required=[],
        handler=handle_end_conversation,
    )


async def handle_end_conversation(args: dict, flow_manager: FlowManager):
    logger.info("👋 Ending conversation")
    # Zwracamy tekst pożegnania I node końcowy
    return ("Do widzenia, miłego dnia!", create_end_node())


def create_end_node() -> dict:
    return {
        "name": "end",
        "pre_actions": [
            {"type": "tts_say", "text": "Dziękuję za rozmowę, do usłyszenia!"}
        ],
        "post_actions": [
            {"type": "end_conversation"}
        ]
    }