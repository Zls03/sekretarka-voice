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
# HELPER: Sprawdzanie frustracji
# ==========================================

def check_frustration(flow_manager: FlowManager, tenant: dict) -> tuple[bool, dict | None]:
    """
    Sprawdza czy osiągnięto limit nieudanych prób.
    Zwraca (True, fallback_node) jeśli tak, (False, None) jeśli nie.
    """
    count = flow_manager.state.get("misunderstanding_count", 0) + 1
    flow_manager.state["misunderstanding_count"] = count
    logger.info(f"⚠️ Misunderstanding #{count}")
    
    if count >= 3:
        logger.warning(f"🚨 Frustration limit reached ({count})")
        return (True, create_fallback_node(tenant))
    
    return (False, None)


def reset_frustration(flow_manager: FlowManager):
    """Resetuje licznik po udanej interakcji"""
    if flow_manager.state.get("misunderstanding_count", 0) > 0:
        flow_manager.state["misunderstanding_count"] = 0
        logger.info("✅ Frustration counter reset")
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
- Na końcu zadaj KRÓTKIE pytanie czy klient potrzebuje czegoś jeszcze
- WAŻNE: Użyj INNEGO zakończenia niż w poprzednich odpowiedziach! Wybierz coś nowego, np: "Masz inne pytania?", "Coś jeszcze?", "Czy to wszystko?", "Mogę jeszcze pomóc?"
- NIE POWTARZAJ tego samego zakończenia dwa razy"""
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
        # Sprawdź frustrację
        is_frustrated, fallback_node = check_frustration(flow_manager, tenant)
        if is_frustrated:
            return (None, fallback_node)
        
        available = ", ".join([s["name"] for s in services])
        return (f"Nie mamy usługi '{service_name}'. Dostępne: {available}.", None)
    
    # Sukces - reset licznika
    reset_frustration(flow_manager)
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
        # Sprawdź frustrację
        is_frustrated, fallback_node = check_frustration(flow_manager, tenant)
        if is_frustrated:
            return (None, fallback_node)
        
        available = ", ".join([s["name"] for s in staff_list])
        return (f"Nie mamy pracownika {staff_name}. U nas pracują: {available}.", None)
    
    # Sukces - reset licznika
    reset_frustration(flow_manager)
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
        # Sprawdź frustrację
        is_frustrated, fallback_node = check_frustration(flow_manager, tenant)
        if is_frustrated:
            return (None, fallback_node)
        
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
    
    # Sukces - reset licznika
    reset_frustration(flow_manager)
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
        # Sprawdź frustrację
        is_frustrated, fallback_node = check_frustration(flow_manager, tenant)
        if is_frustrated:
            return (None, fallback_node)
        
        return (f"Nie rozumiem godziny '{time_str}'.", None)
    
    if hour not in slots:
        # Sprawdź frustrację
        is_frustrated, fallback_node = check_frustration(flow_manager, tenant)
        if is_frustrated:
            return (None, fallback_node)
        
        slots_text = ", ".join([format_hour_polish(h) for h in slots[:5]])
        return (f"Godzina {format_hour_polish(hour)} niedostępna. Mam: {slots_text}.", None)
    
    # Sukces - reset licznika
    reset_frustration(flow_manager)
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
        handler=lambda args, fm: (None, create_end_node()),  # None = pożegnanie w node
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
# NODE: Fallback (bot nie rozumie)
# ==========================================

def create_fallback_node(tenant: dict) -> dict:
    """Node gdy bot nie rozumie po 3 próbach"""
    
    transfer_enabled = tenant.get("transfer_enabled", 0) == 1
    transfer_number = tenant.get("transfer_number", "")
    business_name = tenant.get("name", "salon")
    
    # Buduj tekst w zależności od dostępnych opcji
    if transfer_enabled and transfer_number:
        options_text = "Czy chcesz zostawić wiadomość, żeby właściciel oddzwonił, czy wolisz przekierowanie na jego numer?"
        functions = [
            leave_message_fallback_function(tenant),
            transfer_call_function(tenant),
            try_again_function(tenant),
        ]
    else:
        options_text = "Czy mogę przekazać wiadomość do właściciela? Oddzwoni najszybciej jak to możliwe."
        functions = [
            leave_message_fallback_function(tenant),
            try_again_function(tenant),
        ]
    
    return {
        "name": "fallback",
        "pre_actions": [
            {"type": "tts_say", "text": f"Przepraszam, mam problem ze zrozumieniem. {options_text}"}
        ],
        "respond_immediately": False,
        "role_messages": [{
            "role": "system",
            "content": f"""Jesteś asystentem {business_name}. Miałeś problem ze zrozumieniem klienta.
Zaproponowałeś opcje: wiadomość do właściciela{', przekierowanie' if transfer_enabled else ''}, lub spróbować ponownie.
Czekaj na wybór klienta."""
        }],
        "task_messages": [{
            "role": "system", 
            "content": """Klient wybiera opcję:
- Chce zostawić WIADOMOŚĆ → leave_message
- Chce PRZEKIEROWANIE → transfer_call (jeśli dostępne)
- Chce SPRÓBOWAĆ PONOWNIE → try_again
- Chce się POŻEGNAĆ → end_conversation"""
        }],
        "functions": functions
    }


def leave_message_fallback_function(tenant: dict) -> FlowsFunctionSchema:
    """Klient chce zostawić wiadomość"""
    return FlowsFunctionSchema(
        name="leave_message",
        description="Klient chce zostawić wiadomość dla właściciela",
        properties={},
        required=[],
        handler=lambda args, fm: handle_leave_message_start(args, fm, tenant),
    )


async def handle_leave_message_start(args: dict, flow_manager: FlowManager, tenant: dict):
    """Rozpocznij zbieranie wiadomości"""
    logger.info("📝 Starting message collection")
    flow_manager.state["misunderstanding_count"] = 0  # Reset
    return ("Oczywiście. Powiedz jak masz na imię i jaką wiadomość przekazać.", 
            create_collect_message_node(tenant))


def create_collect_message_node(tenant: dict) -> dict:
    """Node do zbierania wiadomości od klienta"""
    return {
        "name": "collect_message",
        "respond_immediately": False,
        "role_messages": [{
            "role": "system",
            "content": "Zbierasz wiadomość od klienta. Zapisz imię i treść wiadomości."
        }],
        "task_messages": [{
            "role": "system",
            "content": """Klient podaje imię i wiadomość.
Gdy masz dane → save_message
Jeśli klient się rozmyślił → end_conversation"""
        }],
        "functions": [
            save_message_function(tenant),
        ]
    }


def save_message_function(tenant: dict) -> FlowsFunctionSchema:
    """Zapisz wiadomość"""
    return FlowsFunctionSchema(
        name="save_message",
        description="Zapisz wiadomość od klienta (masz imię i treść)",
        properties={
            "customer_name": {"type": "string", "description": "Imię klienta"},
            "message": {"type": "string", "description": "Treść wiadomości"},
        },
        required=["customer_name", "message"],
        handler=lambda args, fm: handle_save_message(args, fm, tenant),
    )


async def handle_save_message(args: dict, flow_manager: FlowManager, tenant: dict):
    """Zapisz wiadomość i wyślij email"""
    name = args.get("customer_name", "Nieznany")
    message = args.get("message", "")
    
    logger.info(f"📧 Message from {name}: {message[:50]}...")
    
    # TODO: Wysłanie emaila do właściciela (zrobimy w kroku 2.4)
    # Na razie tylko logujemy
    
    owner_email = tenant.get("email") or tenant.get("owner_email")
    if owner_email:
        logger.info(f"📧 Would send email to: {owner_email}")
    
    return (f"Dziękuję {name}. Przekażę wiadomość, właściciel oddzwoni najszybciej jak to możliwe. Do widzenia!",
            create_end_node())


def transfer_call_function(tenant: dict) -> FlowsFunctionSchema:
    """Przekierowanie na numer właściciela"""
    return FlowsFunctionSchema(
        name="transfer_call",
        description="Klient chce przekierowanie na numer właściciela",
        properties={},
        required=[],
        handler=lambda args, fm: handle_transfer_call(args, fm, tenant),
    )


async def handle_transfer_call(args: dict, flow_manager: FlowManager, tenant: dict):
    """Przekieruj rozmowę - TODO: implementacja Twilio Dial"""
    transfer_number = tenant.get("transfer_number", "")
    
    if not transfer_number:
        return ("Przepraszam, przekierowanie nie jest teraz dostępne. Czy mogę przekazać wiadomość?", None)
    
    logger.info(f"📞 Transfer requested to: {transfer_number}")
    
    # TODO: Implementacja Twilio <Dial> w kroku 2.7
    # Na razie informujemy że przekierowujemy
    
    flow_manager.state["transfer_requested"] = True
    flow_manager.state["transfer_number"] = transfer_number
    
    return ("Przekierowuję do właściciela. Proszę czekać.", create_end_node())


def try_again_function(tenant: dict) -> FlowsFunctionSchema:
    """Klient chce spróbować ponownie"""
    return FlowsFunctionSchema(
        name="try_again",
        description="Klient chce spróbować ponownie (od początku)",
        properties={},
        required=[],
        handler=lambda args, fm: handle_try_again(args, fm, tenant),
    )


async def handle_try_again(args: dict, flow_manager: FlowManager, tenant: dict):
    """Wróć do początku"""
    logger.info("🔄 Client wants to try again")
    flow_manager.state["misunderstanding_count"] = 0  # Reset
    return ("Dobrze, spróbujmy jeszcze raz. W czym mogę pomóc?", 
            create_continue_conversation_node(tenant))

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
    # WAŻNE: None = nie mów nic tutaj, pożegnanie jest w pre_actions node'a
    return (None, create_end_node())


def create_end_node() -> dict:
    return {
        "name": "end",
        "respond_immediately": False,  # ← TO JEST KLUCZOWE
        "pre_actions": [
            {"type": "tts_say", "text": "Do widzenia, miłego dnia!"}
        ],
        "post_actions": [
            {"type": "end_conversation"}
        ],
        "role_messages": [],
        "task_messages": [],  # Wymagane przez Pipecat
        "functions": []
    }