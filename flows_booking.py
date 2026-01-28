# flows_booking.py - Logika rezerwacji (kalendarz)
"""
Moduł rezerwacji - 6 kroków:
1. Wybór usługi
2. Wybór pracownika
3. Wybór daty
4. Wybór godziny
5. Imię klienta
6. Potwierdzenie
"""

from pipecat_flows import FlowManager, FlowsFunctionSchema
from datetime import datetime, timedelta
from loguru import logger
from typing import Optional
import asyncio

# Import z flows_helpers.py
from flows_helpers import (
    parse_polish_date, parse_time,
    format_hour_polish, format_date_polish,
    get_opening_hours, validate_date_constraints,
    get_available_slots, save_booking_to_api,
    build_business_context, POLISH_DAYS,
    fuzzy_match_service, fuzzy_match_staff,
    send_booking_sms, increment_sms_count,
)

# ============================================================================
# LIMIT RETRY'ÓW - max 3 błędne odpowiedzi na krok
# ============================================================================

MAX_RETRIES_PER_STEP = 3

def check_retry_limit(flow_manager, step: str) -> bool:
    """
    Sprawdź czy nie przekroczono limitu prób dla danego kroku.
    Zwraca True jeśli OK (można kontynuować), False jeśli limit przekroczony.
    """
    key = f"retry_{step}"
    count = flow_manager.state.get(key, 0) + 1
    flow_manager.state[key] = count
    
    if count > MAX_RETRIES_PER_STEP:
        logger.warning(f"⚠️ Retry limit exceeded for {step}: {count}")
        return False
    
    logger.info(f"🔄 Retry {count}/{MAX_RETRIES_PER_STEP} for {step}")
    return True

def reset_retry_count(flow_manager, step: str):
    """Reset licznika po sukcesie"""
    flow_manager.state[f"retry_{step}"] = 0


# ============================================================================
# WALIDACJA W KODZIE - nie w prompcie!
# ============================================================================

def format_slots_natural(slots: list, format_func) -> str:
    """Formatuj sloty naturalnie - max 3 przykłady z różnych przedziałów"""
    if not slots:
        return ""
    
    if len(slots) == 1:
        return f"tylko {format_func(slots[0])}"
    
    if len(slots) == 2:
        return f"{format_func(slots[0])} albo {format_func(slots[1])}"
    
    if len(slots) <= 3:
        parts = [format_func(h) for h in slots]
        return f"{', '.join(parts[:-1])} albo {parts[-1]}"
    
    # 4+ slotów - wybierz 3 reprezentatywne
    example_slots = [slots[0], slots[len(slots)//2], slots[-1]]
    parts = [format_func(h) for h in example_slots]
    return f"na przykład {parts[0]}, {parts[1]} albo {parts[2]}"


def odmien_imie_dopelniacz(imie: str) -> str:
    """Odmienia imię do dopełniacza (do kogo? - Ani, Kasi, Wiktora)"""
    if not imie:
        return imie
    
    imie = imie.strip()
    
    # Żeńskie -a
    if imie.endswith("ia"):  # Ania, Kasia, Zosia
        return imie[:-1] + "i"
    elif imie.endswith("ja"):  # Maja, Alicja
        return imie[:-1] + "i"
    elif imie.endswith("a"):  # Ewa, Anna, Marta
        return imie[:-1] + "y"
    
    # Męskie
    elif imie.endswith("ek"):  # Tomek, Jacek
        return imie[:-2] + "ka"
    elif imie.endswith("eł"):  # Paweł
        return imie[:-2] + "ła"
    elif imie.endswith(("r", "n", "sz", "ł", "j")):  # Wiktor, Jan, Tomasz
        return imie + "a"
    
    return imie + "a"  # domyślnie


def validate_customer_name(name: str) -> Optional[str]:
    """Waliduj imię - zwraca None jeśli to śmieć."""
    if not name:
        return None
    name = name.strip()
    
    # Lista śmieciowych "imion" które GPT może wymyślić
    invalid = [
        "pan", "pani", "tak", "nie", "halo", "cześć", "dziękuję", 
        "proszę", "dobrze", "ok", "okej", "słucham", "przepraszam",
        "yes", "no", "moment", "chwila", "sekunda", "jasne"
    ]
    
    if name.lower() in invalid or len(name) < 2:
        logger.warning(f"⚠️ Invalid name rejected: '{name}'")
        return None
    
    # Usuń "pan/pani" z początku
    for prefix in ["pan ", "pani "]:
        if name.lower().startswith(prefix):
            name = name[len(prefix):]
    
    return name.strip().title()


# ==========================================
# FUNKCJA: Pytanie W TRAKCIE rezerwacji
# ==========================================

def answer_question_in_booking_function(tenant: dict) -> FlowsFunctionSchema:
    """Odpowiedz na pytanie ALE zostań w aktualnym kroku rezerwacji"""
    return FlowsFunctionSchema(
        name="answer_question_in_booking",
        description="""Klient zadał pytanie w trakcie rezerwacji (np. "ile kosztuje?", "gdzie jesteście?").
Odpowiedz KRÓTKO i wróć do aktualnego kroku. NIE zmieniaj tematu.""",
        properties={
            "question": {"type": "string", "description": "Pytanie klienta"}
        },
        required=["question"],
        handler=lambda args, fm: handle_answer_in_booking(args, fm, tenant),
    )


async def handle_answer_in_booking(args: dict, flow_manager: FlowManager, tenant: dict):
    """Odpowiedz na pytanie ale NIE zmieniaj node'a - zostań gdzie jesteś"""
    question = args.get("question", "")
    context = build_business_context(tenant)
    
    logger.info(f"❓ Question during booking: {question}")
    
    # Zwróć hint dla GPT ale None jako node = zostań w aktualnym ustawieniu
    return ({
        "status": "answered_in_booking",
        "instruction": f"Odpowiedz KRÓTKO na pytanie: '{question}'. Potem wróć do rezerwacji i powtórz aktualne pytanie (np. 'A wracając do rezerwacji - na jaką usługę?'). Kontekst firmy: {context[:500]}"
    }, None)


def create_booking_failed_node(tenant: dict, reason: str = "") -> dict:
    """Node: rezerwacja nie udała się - eskaluj do człowieka"""
    # Lazy import - unikamy circular import
    from flows import collect_message_function, end_conversation_function
    
    return {
        "name": "booking_failed",
        "pre_actions": [
            {"type": "tts_say", "text": "Przepraszam, mam problem ze zrozumieniem. Przekażę do właściciela, który pomoże z rezerwacją."}
        ],
        "respond_immediately": False,
        "role_messages": [{
            "role": "system",
            "content": f"Rezerwacja nie udała się: {reason}. Zaproponuj zostawienie wiadomości."
        }],
        "task_messages": [{
            "role": "system", 
            "content": "Zapytaj czy klient chce zostawić wiadomość do właściciela."
        }],
        "functions": [
            collect_message_function(tenant),
            end_conversation_function(),
        ]
    }


# ==========================================
# FUNKCJA: Rozpocznij rezerwację (SMART)
# ==========================================

def start_booking_function() -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="start_booking",
        description="Klient chce umówić wizytę. Użyj gdy klient mówi że chce się umówić/zarezerwować/zapisać.",
        properties={},
        required=[],
        handler=handle_start_booking_simple,
    )


async def handle_start_booking_simple(args: dict, flow_manager: FlowManager):
    """
    PROSTY BOOKING - zawsze zaczyna od usługi, krok po kroku.
    KOD decyduje o flow, GPT tylko mówi naturalnie.
    """
    # Lazy import - unikamy circular import
    from flows import create_take_message_node
    
    tenant = flow_manager.state.get("tenant", {})
    caller_phone = flow_manager.state.get("caller_phone", "unknown")
    
    logger.info(f"📅 Simple booking START | phone: {caller_phone}")
    
    # Reset state - czysta karta
    flow_manager.state["selected_service"] = None
    flow_manager.state["selected_staff"] = None
    flow_manager.state["selected_date"] = None
    flow_manager.state["selected_time"] = None
    flow_manager.state["customer_name"] = None
    flow_manager.state["available_slots"] = []
    
    # Walidacja podstawowa
    services = tenant.get("services", [])
    staff_list = tenant.get("staff", [])
    
    if not services:
        return ({"status": "error", "reason": "no_services"}, create_take_message_node(tenant))
    
    if not staff_list:
        return ({"status": "error", "reason": "no_staff"}, create_take_message_node(tenant))
    
    # ZAWSZE zacznij od wyboru usługi - pre_actions w ustawią tekst
    return ({"status": "started"}, create_get_service_node(tenant))


def create_get_service_node(tenant: dict) -> dict:
    """NODE: Wybór usługi - STRICT (krok 1/6)"""
    services = tenant.get("services", [])
    service_names = [s["name"] for s in services]
    services_list = ", ".join(service_names) if service_names else "brak"
    
    return {
        "name": "get_service",
        "pre_actions": [
            {"type": "tts_say", "text": f"Jasne, umówimy wizytę. Na jaką usługę? Mamy {services_list}."}
        ],
        "respond_immediately": False,
        "role_messages": [{
            "role": "system",
            "content": f"""Jesteś w trybie ZBIERANIA DANYCH do rezerwacji.
Aktualny krok: WYBÓR USŁUGI.

DOSTĘPNE USŁUGI: {services_list}

TWOJE JEDYNE ZADANIE: Zapytaj o usługę i wywołaj select_service."""
        }],
        "task_messages": [{
            "role": "system",
            "content": f"""KROK 1/6: Wybór usługi

INSTRUKCJA:
1. Zapytaj KRÓTKO: "Na jaką usługę? Mamy: {services_list}"
2. Gdy klient powie usługę → NATYCHMIAST wywołaj select_service
3. Jeśli klient pyta o coś innego → powiedz: "Jasne, odpowiem na to — ale najpierw wybierzmy usługę, żeby dobrze umówić. Którą?"
4. Jeśli cisza/niezrozumienie → powiedz: "Nic nie szkodzi, proszę tylko powiedzieć którą usługę: {services_list}"

Gdy klient powie usługę → wywołaj select_service."""
        }],
        "functions": [
            select_service_function(tenant, service_names),
            answer_question_in_booking_function(tenant),
        ]
    }


def select_service_function(tenant: dict, available_services: list = None) -> FlowsFunctionSchema:
    # Pobierz listę usług jeśli nie podano
    if available_services is None:
        available_services = [s["name"] for s in tenant.get("services", [])]
    
    properties = {
        "service_name": {
            "type": "string", 
            "description": "Nazwa wybranej usługi"
        }
    }
    
    # Dodaj enum tylko jeśli mamy usługi
    if available_services:
        properties["service_name"]["enum"] = available_services
    
    return FlowsFunctionSchema(
        name="select_service",
        description="Klient wybrał usługę z dostępnej listy",
        properties=properties,
        required=["service_name"],
        handler=lambda args, fm: handle_select_service(args, fm, tenant),
    )


async def handle_select_service(args: dict, flow_manager: FlowManager, tenant: dict):
    """Handler wyboru usługi - walidacja W KODZIE z fuzzy matching"""
    service_name = args.get("service_name", "")
    services = tenant.get("services", [])
    
    # Użyj fuzzy matching z helpers
    found = fuzzy_match_service(service_name, services)
    
    if not found:
        # Sprawdź limit retry'ów
        if not check_retry_limit(flow_manager, "service"):
            return (None, create_booking_failed_node(tenant, "nie udało się wybrać usługi"))
        
        available = ", ".join([s["name"] for s in services])
        return ({"status": "error", "message": f"Nie mamy takiej usługi. Dostępne: {available}."}, None)
    
    # Sukces - reset licznika
    reset_retry_count(flow_manager, "service")
    
    # Zapisz i przejdź do wyboru pracownika
    flow_manager.state["selected_service"] = found
    logger.info(f"✅ [1/6] Service selected: {found['name']}")
    
    # Dane dla kontekstu + node z pre_actions
    next_node = create_get_staff_node(tenant, found)
    return ({"status": "success", "service": found["name"]}, next_node)


# ==========================================
# NODE: Wybór pracownika
# ==========================================

def create_get_staff_node(tenant: dict, selected_service: dict = None) -> dict:
    """NODE: Wybór pracownika - STRICT (krok 2/6), filtrowany po usłudze"""
    all_staff = tenant.get("staff", [])
    service_name = selected_service.get("name", "") if selected_service else ""
    
    # Filtruj pracowników którzy wykonują wybraną usługę
    if selected_service:
        service_id = selected_service.get("id")
        available_staff = []
        for s in all_staff:
            staff_service_ids = [svc.get("id") for svc in s.get("services", [])]
            if not staff_service_ids or service_id in staff_service_ids:
                available_staff.append(s)
        
        if not available_staff:
            available_staff = all_staff
            logger.warning(f"⚠️ No staff for service {selected_service.get('name')}, showing all")
    else:
        available_staff = all_staff
    
    staff_names = [s["name"] for s in available_staff]
    staff_list = ", ".join(staff_names)
    
    return {
        "name": "get_staff",
        "pre_actions": [
            {"type": "tts_say", "text": f"{service_name}, świetnie. Do kogo? Tę usługę wykonuje {staff_list}."}
        ],
        "respond_immediately": False,
        "role_messages": [{
            "role": "system",
            "content": f"""Jesteś w trybie ZBIERANIA DANYCH do rezerwacji.
Aktualny krok: WYBÓR PRACOWNIKA.

DOSTĘPNI PRACOWNICY dla tej usługi: {staff_list}

TWOJE JEDYNE ZADANIE: Zapytaj do kogo i wywołaj select_staff."""
        }],
        "task_messages": [{
            "role": "system",
            "content": f"""KROK 2/6: Wybór pracownika

INSTRUKCJA:
1. Zapytaj KRÓTKO: "Do kogo? Dostępni: {staff_list}"
2. Gdy klient powie imię → NATYCHMIAST wywołaj select_staff
3. Jeśli "obojętnie"/"ktokolwiek" → wywołaj select_staff z pierwszym: "{staff_names[0] if staff_names else ''}"
4. Jeśli klient pyta o coś innego → powiedz: "Jasne, zaraz do tego wrócimy — tylko krok drugi z sześciu. Do kogo?"
5. Jeśli cisza/niezrozumienie → powiedz: "Proszę tylko powiedzieć imię: {staff_list}"

MUSISZ użyć funkcji select_staff. Nie możesz odpowiedzieć bez niej."""
        }],
        "functions": [
            select_staff_function(tenant, staff_names),
            answer_question_in_booking_function(tenant),
        ]
    }


def select_staff_function(tenant: dict, available_names: list = None) -> FlowsFunctionSchema:
    # Pobierz listę pracowników jeśli nie podano
    if available_names is None:
        available_names = [s["name"] for s in tenant.get("staff", [])]
    
    properties = {
        "staff_name": {
            "type": "string", 
            "description": "Imię pracownika"
        }
    }
    
    # Dodaj enum tylko jeśli mamy pracowników
    if available_names:
        properties["staff_name"]["enum"] = available_names
    
    return FlowsFunctionSchema(
        name="select_staff",
        description="Klient wybrał pracownika z dostępnej listy",
        properties=properties,
        required=["staff_name"],
        handler=lambda args, fm: handle_select_staff(args, fm, tenant),
    )


async def handle_select_staff(args: dict, flow_manager: FlowManager, tenant: dict):
    """Handler wyboru pracownika - walidacja W KODZIE z fuzzy matching"""
    staff_name = args.get("staff_name", "")
    staff_list = tenant.get("staff", [])
    selected_service = flow_manager.state.get("selected_service", {})
    
    # Użyj fuzzy matching z helpers
    found = fuzzy_match_staff(staff_name, staff_list)
    
    if not found:
        # Sprawdź limit retry'ów
        if not check_retry_limit(flow_manager, "staff"):
            return (None, create_booking_failed_node(tenant, "nie udało się wybrać pracownika"))
        
        available = ", ".join([s["name"] for s in staff_list])
        return ({"status": "error", "message": f"Nie mamy takiego pracownika. Dostępni: {available}."}, None)
    
    # Sukces - reset licznika  
    reset_retry_count(flow_manager, "staff")
    
    # Walidacja: czy pracownik wykonuje tę usługę?
    if selected_service:
        staff_service_ids = [svc.get("id") for svc in found.get("services", [])]
        if staff_service_ids and selected_service.get("id") not in staff_service_ids:
            available_for_service = []
            for st in staff_list:
                st_service_ids = [svc.get("id") for svc in st.get("services", [])]
                if not st_service_ids or selected_service.get("id") in st_service_ids:
                    available_for_service.append(st["name"])
            
            # Sprawdź limit retry'ów
            if not check_retry_limit(flow_manager, "staff"):
                return (None, create_booking_failed_node(tenant, "nie udało się wybrać pracownika"))
            
            return ({"status": "error", "message": f"Niestety {found['name']} nie wykonuje {selected_service['name']}. Tę usługę wykonuje: {', '.join(available_for_service)}."}, None)
    
    # Zapisz i przejdź ZAWSZE do wyboru daty
    flow_manager.state["selected_staff"] = found
    logger.info(f"✅ [2/6] Staff selected: {found['name']}")
    
    # Dane dla kontekstu + node z pre_actions
    next_node = create_get_date_node(tenant, found)
    return ({"status": "success", "staff": found["name"]}, next_node)


# ==========================================
# NODE: Wybór daty
# ==========================================

def create_get_date_node(tenant: dict, selected_staff: dict = None) -> dict:
    """NODE: Wybór daty - STRICT (krok 3/6)"""
    now = datetime.now()
    today_str = f"{now.strftime('%d.%m.%Y')} ({POLISH_DAYS[now.weekday()]})"
    max_days = tenant.get("max_booking_days", 30)
    max_date = now + timedelta(days=max_days)
    max_date_str = max_date.strftime('%d.%m.%Y')
    staff_name = selected_staff.get("name", "") if selected_staff else ""
    
    return {
        "name": "get_date",
        "pre_actions": [
            {"type": "tts_say", "text": f"Do {odmien_imie_dopelniacz(staff_name)}, dobrze. Na kiedy?"}
        ],
        "respond_immediately": False,
        "role_messages": [{
            "role": "system",
            "content": f"""Jesteś w trybie ZBIERANIA DANYCH do rezerwacji.
Aktualny krok: WYBÓR DATY.

DZIŚ: {today_str}
LIMIT: do {max_date_str}

TWOJE JEDYNE ZADANIE: Zapytaj o datę i wywołaj check_availability."""
        }],
        "task_messages": [{
            "role": "system",
            "content": f"""KROK 3/6: Wybór daty

INSTRUKCJA:
1. Zapytaj KRÓTKO: "Na kiedy chciałby Pan umówić wizytę?"
2. Gdy klient poda datę → NATYCHMIAST wywołaj check_availability
3. Akceptuj: "jutro", "pojutrze", dzień tygodnia, datę
4. NIE ZGADUJ godzin - powiedz: "System za chwilę pokaże dokładne wolne godziny"
5. Jeśli klient pyta o coś innego → powiedz: "Już połowa! Tylko data i zaraz pokażę dostępne terminy."
6. Jeśli cisza → powiedz: "Proszę powiedzieć dzień, np. jutro, w piątek..."

⛔ ZAKAZ: NIE WYMYŚLAJ daty! NIE wywołuj check_availability jeśli klient nie podał daty!
⛔ Jeśli klient milczy lub mówi "halo" - odpowiedz TYLKO tekstem, NIE wywołuj funkcji!

MUSISZ użyć funkcji check_availability. Nie możesz odpowiedzieć bez niej."""
        }],
        "functions": [
            check_availability_function(tenant),
            answer_question_in_booking_function(tenant),
        ]
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
    """Handler sprawdzania dostępności - walidacja W KODZIE"""
    # Lazy import
    from flows import play_snippet
    
    date_str = args.get("date", "")
    
    # Daj feedback użytkownikowi że sprawdzamy
    await play_snippet(flow_manager, "checking")
    
    staff = flow_manager.state.get("selected_staff", {})
    service = flow_manager.state.get("selected_service", {})
    
    # Parsuj datę
    parsed_date = parse_polish_date(date_str)
    if not parsed_date:
        # Sprawdź limit retry'ów
        if not check_retry_limit(flow_manager, "date"):
            return (None, create_booking_failed_node(tenant, "nie udało się wybrać daty"))
        
        return ({"status": "error", "message": "Nie rozumiem daty. Proszę powiedzieć np. jutro, w poniedziałek."}, None)
    
    # Popraw rok jeśli data w przeszłości
    today = datetime.now()
    if parsed_date.date() < today.date():
        try:
            parsed_date = parsed_date.replace(year=parsed_date.year + 1)
            if parsed_date.date() < today.date():
                return ("Ta data już minęła. Proszę wybrać przyszłą datę.", None)
        except:
            return ("Ta data już minęła. Proszę wybrać przyszłą datę.", None)
    
    # Limit dni do przodu
    max_days = tenant.get("max_booking_days", 30)
    max_date = today + timedelta(days=max_days)
    if parsed_date.date() > max_date.date():
        return (f"Mogę umówić maksymalnie {max_days} dni do przodu.", None)
    
    # Walidacja constraintów
    valid, error = validate_date_constraints(parsed_date, tenant, staff)
    if not valid:
        return (error, None)
    
    # Sprawdź czy otwarci
    weekday = parsed_date.weekday()
    if get_opening_hours(tenant, weekday) is None:
        return (f"W {POLISH_DAYS[weekday]} jesteśmy zamknięci. Proszę wybrać inny dzień.", None)
    
    # Pobierz sloty z API/kalendarza
    slots = await get_available_slots(tenant, staff, service, parsed_date)
    if not slots:
        # Sprawdź limit retry'ów
        if not check_retry_limit(flow_manager, "date"):
            return (None, create_booking_failed_node(tenant, "brak wolnych terminów"))
        
        return ({"status": "error", "message": f"Na {format_date_polish(parsed_date)} brak wolnych terminów. Proszę wybrać inny dzień."}, None)
    
    # Sukces - reset licznika
    reset_retry_count(flow_manager, "date")
    
    # Zapisz i przejdź ZAWSZE do wyboru godziny (z ENUM!)
    flow_manager.state["selected_date"] = parsed_date
    flow_manager.state["available_slots"] = slots
    
    logger.info(f"✅ [3/6] Date selected: {parsed_date.strftime('%Y-%m-%d')}, available slots: {slots}")
    
    # Formatuj sloty słownie
    slots_text = ", ".join([format_hour_polish(h) for h in slots[:5]])
    date_text = format_date_polish(parsed_date)
    
    # Dane dla kontekstu + node z pre_actions
    next_node = create_get_time_node(tenant, slots, date_text, slots_text)
    return ({"status": "success", "date": parsed_date.strftime("%Y-%m-%d"), "slots": slots[:5]}, next_node)


# ==========================================
# NODE: Wybór godziny
# ==========================================

def create_get_time_node(tenant: dict, available_slots: list, date_text: str = "", slots_text: str = "") -> dict:
    """NODE: Wybór godziny (krok 4/6)"""
    # Formatuj godziny naturalnie - max 3 przykłady
    if not slots_text:
        slots_text = format_slots_natural(available_slots, format_hour_polish)
    
    return {
        "name": "get_time",
        "pre_actions": [
            {"type": "tts_say", "text": f"Na {date_text} mam wolne {slots_text}. Która pasuje?"}
        ],
        "respond_immediately": False,
        "role_messages": [{
            "role": "system",
            "content": f"""Jesteś w trybie ZBIERANIA DANYCH do rezerwacji.
Aktualny krok: WYBÓR GODZINY.

DOSTĘPNE GODZINY: {slots_text}

TWOJE JEDYNE ZADANIE: Zapytaj o godzinę i wywołaj select_time."""
        }],
        "task_messages": [{
            "role": "system",
            "content": f"""KROK 4/6: Wybór godziny

Klient usłyszał dostępne godziny. CZEKAJ na jego wybór.

Gdy klient powie godzinę → wywołaj select_time z TĄ godziną którą powiedział.
Jeśli powie zajętą → powiedz: "Ta jest zajęta. Mam: {slots_text}"
Jeśli cisza → powiedz: "Która godzina Panu pasuje?"

⛔ NIE wybieraj za klienta! Przekaż dokładnie to co powiedział."""
        }],
        "functions": [
            select_time_function(tenant, available_slots),
            answer_question_in_booking_function(tenant),
        ]
    }


def select_time_function(tenant: dict, available_slots: list) -> FlowsFunctionSchema:
    """Funkcja wyboru godziny - BEZ ENUM, walidacja w kodzie"""
    return FlowsFunctionSchema(
        name="select_time",
        description="Klient WYRAŹNIE wybrał konkretną godzinę. NIE wywołuj jeśli klient nie podał godziny!",
        properties={
            "hour": {
                "type": "string",
                "description": "Godzina którą klient WYRAŹNIE powiedział (np. '10', 'dziesiąta')"
            }
        },
        required=["hour"],
        handler=lambda args, fm: handle_select_time(args, fm, tenant),
    )


# Backward compatibility - stara nazwa funkcji
def create_select_time_node(tenant: dict) -> dict:
    """DEPRECATED: Użyj create_get_time_node z listą slotów"""
    logger.warning("⚠️ create_select_time_node called without slots - using empty list")
    return create_get_time_node(tenant, [9, 10, 11, 12, 13, 14, 15, 16])


async def handle_select_time(args: dict, flow_manager: FlowManager, tenant: dict):
    """Handler wyboru godziny - walidacja W KODZIE"""
    hour_str = args.get("hour", "")
    slots = flow_manager.state.get("available_slots", [])
    
    # Próbuj sparsować godzinę
    hour = None
    try:
        hour = int(hour_str)
    except (ValueError, TypeError):
        hour = parse_time(hour_str)
    
    # Walidacja - czy godzina jest na liście dostępnych?
    if hour is None or hour not in slots:
        # Sprawdź limit retry'ów
        if not check_retry_limit(flow_manager, "time"):
            return (None, create_booking_failed_node(tenant, "nie udało się wybrać godziny"))
        
        slots_text = ", ".join([format_hour_polish(h) for h in slots[:5]])
        logger.warning(f"⚠️ Invalid time '{hour_str}' (parsed: {hour}), available: {slots[:5]}")
        return ({"status": "error", "message": f"Ta godzina jest niedostępna. Wolne mam: {slots_text}."}, None)
    
    # Sukces - reset licznika
    reset_retry_count(flow_manager, "time")
    
    # Zapisz i przejdź ZAWSZE do imienia
    flow_manager.state["selected_time"] = hour
    logger.info(f"✅ [4/6] Time selected: {hour}:00")
    
    # Dane dla kontekstu + node z pre_actions
    hour_text = format_hour_polish(hour)
    next_node = create_get_name_node(tenant, hour_text)
    return ({"status": "success", "time": f"{hour}:00"}, next_node)


# ==========================================
# NODE: Imię i zakończenie rezerwacji
# ==========================================

def create_get_name_node(tenant: dict, hour_text: str = "") -> dict:
    """NODE: Imię klienta (krok 5/6)"""
    return {
        "name": "get_name",
        "pre_actions": [
            {"type": "tts_say", "text": f"O {hour_text}, dobrze. Na jakie nazwisko?"}
        ],
        "respond_immediately": False,
        "role_messages": [{
            "role": "system",
            "content": """Jesteś w trybie ZBIERANIA DANYCH do rezerwacji.
Aktualny krok: IMIĘ KLIENTA.

TWOJE JEDYNE ZADANIE: Zapytaj o imię i wywołaj set_customer_name."""
        }],
        "task_messages": [{
            "role": "system",
            "content": """KROK 5/6: Imię klienta

INSTRUKCJA:
1. Zapytaj: "Ostatni krok przed potwierdzeniem - jak mogę zapisać? Imię lub nazwisko."
2. Gdy klient powie imię → NATYCHMIAST wywołaj set_customer_name
3. Jeśli cisza/niezrozumienie → powiedz: "Proszę tylko powiedzieć imię lub nazwisko do rezerwacji."

MUSISZ użyć funkcji set_customer_name. Nie możesz odpowiedzieć bez niej."""
        }],
        "functions": [
            set_customer_name_function(tenant),
            answer_question_in_booking_function(tenant),
        ]
    }


def set_customer_name_function(tenant: dict) -> FlowsFunctionSchema:
    """Funkcja zapisu imienia - przechodzi do CONFIRM"""
    return FlowsFunctionSchema(
        name="set_customer_name",
        description="Zapisz imię klienta",
        properties={
            "customer_name": {"type": "string", "description": "Imię/nazwisko klienta"}
        },
        required=["customer_name"],
        handler=lambda args, fm: handle_set_customer_name(args, fm, tenant),
    )


async def handle_set_customer_name(args: dict, flow_manager: FlowManager, tenant: dict):
    """Handler imienia - walidacja W KODZIE"""
    validated = validate_customer_name(args.get("customer_name", ""))
    
    if not validated:
        # Sprawdź limit retry'ów
        if not check_retry_limit(flow_manager, "name"):
            return (None, create_booking_failed_node(tenant, "nie udało się zapisać imienia"))
        
        return ({"status": "error", "message": "Nie dosłyszałam imienia. Proszę powtórzyć."}, None)
    
    # Sukces - reset licznika
    reset_retry_count(flow_manager, "name")
    
    # Zapisz i przejdź do potwierdzenia
    flow_manager.state["customer_name"] = validated
    logger.info(f"✅ [5/6] Customer name: {validated}")
    
    # Pobierz dane do podsumowania
    service = flow_manager.state.get("selected_service", {})
    staff = flow_manager.state.get("selected_staff", {})
    date = flow_manager.state.get("selected_date")
    hour = flow_manager.state.get("selected_time")
    
    date_text = format_date_polish(date) if date else "wybrany dzień"
    time_text = format_hour_polish(hour) if hour else "wybraną godzinę"
    
    staff_name = staff.get('name', 'pracownika')
    summary = f"{service.get('name', 'wizyta')} u {odmien_imie_dopelniacz(staff_name)}, {date_text} o {time_text}, na {validated}"
    
    # Dane dla kontekstu + node z pre_actions
    next_node = create_confirm_booking_node(tenant, summary)
    return ({"status": "success", "name": validated, "summary": summary}, next_node)


# ==========================================
# NODE: Potwierdzenie rezerwacji
# ==========================================

def create_confirm_booking_node(tenant: dict, summary: str = "") -> dict:
    """NODE: Potwierdzenie (krok 6/6)"""
    return {
        "name": "confirm_booking",
        "pre_actions": [
            {"type": "tts_say", "text": f"Czyli {summary}. Potwierdzam?"}
        ],
        "respond_immediately": False,
        "role_messages": [{
            "role": "system",
            "content": """Jesteś w trybie ZBIERANIA DANYCH do rezerwacji.
Aktualny krok: POTWIERDZENIE.

Klient usłyszał podsumowanie. Czekaj na odpowiedź TAK lub NIE."""
        }],
        "task_messages": [{
            "role": "system",
            "content": """KROK 6/6: Potwierdzenie

Klient usłyszał podsumowanie. Czekaj na odpowiedź:
- TAK/potwierdzam/zgadza się → confirm_booking_yes
- NIE/zmień/inaczej → confirm_booking_no"""
        }],
        "functions": [
            confirm_booking_yes_function(tenant),
            confirm_booking_no_function(tenant),
        ]
    }


def confirm_booking_yes_function(tenant: dict) -> FlowsFunctionSchema:
    """Klient potwierdza - zapisz rezerwację"""
    return FlowsFunctionSchema(
        name="confirm_booking_yes",
        description="Klient POTWIERDZA rezerwację (tak, potwierdzam, zgadza się)",
        properties={},
        required=[],
        handler=lambda args, fm: handle_confirm_booking_yes(args, fm, tenant),
    )


def confirm_booking_no_function(tenant: dict) -> FlowsFunctionSchema:
    """Klient nie potwierdza - wróć do początku"""
    return FlowsFunctionSchema(
        name="confirm_booking_no",
        description="Klient NIE potwierdza lub chce ZMIENIĆ coś (nie, zmień, inaczej)",
        properties={
            "what_to_change": {
                "type": "string",
                "enum": ["usługa", "pracownik", "data", "godzina", "imię", "wszystko"],
                "description": "Co klient chce zmienić"
            }
        },
        required=[],
        handler=lambda args, fm: handle_confirm_booking_no(args, fm, tenant),
    )


async def handle_confirm_booking_yes(args: dict, flow_manager: FlowManager, tenant: dict):
    """Klient potwierdził - TERAZ zapisz rezerwację"""
    # Lazy import
    from flows import play_snippet, create_take_message_node, create_anything_else_node
    
    logger.info("✅ [6/6] Booking CONFIRMED by customer")
    
    # Daj feedback że zapisujemy (będzie API call)
    await play_snippet(flow_manager, "saving")
    
    # Pobierz wszystkie dane
    service = flow_manager.state.get("selected_service", {})
    staff = flow_manager.state.get("selected_staff", {})
    date = flow_manager.state.get("selected_date")
    hour = flow_manager.state.get("selected_time")
    name = flow_manager.state.get("customer_name", "")
    caller_phone = flow_manager.state.get("caller_phone", "")
    
    logger.info(f"💾 Saving booking: {name}, {service.get('name')}, {staff.get('name')}, {date}, {hour}:00")
    
    # Double-check: czy slot nadal wolny?
    try:
        current_slots = await asyncio.wait_for(
            get_available_slots(tenant, staff, service, date),
            timeout=5.0
        )
    except asyncio.TimeoutError:
        logger.error("⚠️ Double-check timeout - cannot verify slot!")
        return (
            "Przepraszam, system chwilowo nie odpowiada. Proszę spróbować za chwilę lub zostawić wiadomość.",
            create_take_message_node(tenant)
        )
    
    if hour not in current_slots:
        logger.warning(f"⚠️ Slot {hour}:00 no longer available!")
        flow_manager.state["available_slots"] = current_slots
        if current_slots:
            slots_text = ", ".join([format_hour_polish(h) for h in current_slots[:3]])
            return (
                f"Przepraszam, godzina {format_hour_polish(hour)} właśnie została zajęta. "
                f"Mam jeszcze: {slots_text}. Która pasuje?",
                create_get_time_node(tenant, current_slots)
            )
        else:
            return (
                f"Przepraszam, na {format_date_polish(date)} nie ma już wolnych terminów. "
                f"Proszę wybrać inny dzień.",
                create_get_date_node(tenant)
            )
    
    booking_code = None
    booking_saved = False
    
    try:
        result = await save_booking_to_api(tenant, staff, service, date, hour, name, caller_phone)
        if result:
            booking_saved = True
            booking_code = result.get("booking_code")
            logger.info(f"✅ BOOKING SAVED!")
            logger.info(f"   📋 Code: {booking_code}")
            logger.info(f"   👤 Customer: {name} ({caller_phone})")
            logger.info(f"   💇 Service: {service.get('name')} @ {staff.get('name')}")
            logger.info(f"   📅 When: {date.strftime('%Y-%m-%d')} {hour}:00")
    except Exception as e:
        logger.error(f"❌ Save error: {e}")
        logger.exception("Full traceback:")
    
    # Wyślij SMS jeśli zapisano
    if booking_saved and booking_code and caller_phone:
        try:
            date_str = date.strftime("%d.%m") if date else ""
            time_str = f"{hour}:00" if hour else ""
            
            sms_sent = await send_booking_sms(
                tenant=tenant,
                customer_phone=caller_phone,
                service_name=service.get("name", "Wizyta"),
                staff_name=staff.get("name", ""),
                date_str=date_str,
                time_str=time_str,
                booking_code=booking_code
            )
            
            if sms_sent:
                await increment_sms_count(tenant.get("id"))
        except Exception as e:
            logger.error(f"📱 SMS error: {e}")
    
    # Komunikat końcowy
    date_text = format_date_polish(date) if date else "wybrany dzień"
    time_text = format_hour_polish(hour) if hour else "wybraną godzinę"
    
    if booking_saved and booking_code:
        return (f"Gotowe! {service.get('name')} u {staff.get('name')}, {date_text} o {time_text}. "
                f"Wysłałam SMS z potwierdzeniem. Do zobaczenia!",
                create_anything_else_node(tenant))
    elif booking_saved:
        return (f"Gotowe! {service.get('name')} u {staff.get('name')}, {date_text} o {time_text}. Do zobaczenia!",
                create_anything_else_node(tenant))
    else:
        return ("Przepraszam, wystąpił problem z zapisem. Czy mogę przekazać wiadomość do właściciela?",
                create_take_message_node(tenant))


async def handle_confirm_booking_no(args: dict, flow_manager: FlowManager, tenant: dict):
    """Klient chce zmienić - wróć do odpowiedniego kroku"""
    what_to_change = args.get("what_to_change", "wszystko")
    
    logger.info(f"🔄 Customer wants to change: {what_to_change}")
    
    if what_to_change == "usługa":
        flow_manager.state["selected_service"] = None
        flow_manager.state["selected_staff"] = None  # Reset też pracownika
        return ("Dobrze, zmieńmy usługę.", create_get_service_node(tenant))
    
    elif what_to_change == "pracownik":
        flow_manager.state["selected_staff"] = None
        selected_service = flow_manager.state.get("selected_service")
        return ("Dobrze, zmieńmy pracownika.", create_get_staff_node(tenant, selected_service))
    
    elif what_to_change == "data":
        flow_manager.state["selected_date"] = None
        flow_manager.state["selected_time"] = None  # Reset też godziny
        return ("Dobrze, zmieńmy datę.", create_get_date_node(tenant))
    
    elif what_to_change == "godzina":
        flow_manager.state["selected_time"] = None
        slots = flow_manager.state.get("available_slots", [])
        if slots:
            return ("Dobrze, zmieńmy godzinę.", create_get_time_node(tenant, slots))
        else:
            return ("Muszę najpierw sprawdzić dostępność.", create_get_date_node(tenant))
    
    elif what_to_change == "imię":
        flow_manager.state["customer_name"] = None
        return ("Dobrze, zmieńmy imię.", create_get_name_node(tenant))
    
    else:  # "wszystko" lub nieznane
        # Reset wszystkiego i zacznij od nowa
        flow_manager.state["selected_service"] = None
        flow_manager.state["selected_staff"] = None
        flow_manager.state["selected_date"] = None
        flow_manager.state["selected_time"] = None
        flow_manager.state["customer_name"] = None
        return ("Dobrze, zacznijmy od nowa.", create_get_service_node(tenant))


# ==========================================
# EXPORTED FUNCTIONS (dla flows.py)
# ==========================================

__all__ = [
    # Główna funkcja startu rezerwacji
    "start_booking_function",
    
    # Node creators (jeśli potrzebne w flows.py)
    "create_get_service_node",
    "create_get_staff_node", 
    "create_get_date_node",
    "create_get_time_node",
    "create_get_name_node",
    "create_confirm_booking_node",
    
    # Helpers
    "validate_customer_name",
    "odmien_imie_dopelniacz",
    "format_slots_natural",
]