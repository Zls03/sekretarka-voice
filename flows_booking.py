# flows_booking.py - Logika rezerwacji (kalendarz)
# WERSJA 6.0 - HYBRID zgodny z best practices Pipecat Flows (Daily.co)
"""
ARCHITEKTURA (zgodna z zaleceniami twórców Pipecat):
- GPT generuje naturalny tekst (respond_immediately=True, brak pre_actions)
- KOD kontroluje flow (handlery decydują o przejściach)
- KOD waliduje dane (fuzzy matching, sprawdzanie dostępności)

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
    """Sprawdź czy nie przekroczono limitu prób dla danego kroku."""
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
# HELPERS
# ============================================================================

def format_slots_natural(slots: list, format_func) -> str:
    """Formatuj sloty naturalnie - max 3 przykłady"""
    if not slots:
        return ""
    
    if len(slots) == 1:
        return f"tylko {format_func(slots[0])}"
    
    if len(slots) == 2:
        return f"{format_func(slots[0])} i {format_func(slots[1])}"
    
    if len(slots) <= 4:
        parts = [format_func(h) for h in slots]
        return f"{', '.join(parts[:-1])} i {parts[-1]}"
    
    # 5+ slotów - wybierz reprezentatywne
    example_slots = [slots[0], slots[len(slots)//2], slots[-1]]
    parts = [format_func(h) for h in example_slots]
    return f"{parts[0]}, {parts[1]}, {parts[2]} i inne"


def odmien_imie_dopelniacz(imie: str) -> str:
    """Odmienia imię do dopełniacza (do kogo? - Ani, Wiktora)"""
    if not imie:
        return imie
    
    imie = imie.strip()
    
    if imie.endswith("ia"):
        return imie[:-1] + "i"
    elif imie.endswith("ja"):
        return imie[:-1] + "i"
    elif imie.endswith("a"):
        return imie[:-1] + "y"
    elif imie.endswith("ek"):
        return imie[:-2] + "ka"
    elif imie.endswith("eł"):
        return imie[:-2] + "ła"
    elif imie.endswith(("r", "n", "sz", "ł", "j")):
        return imie + "a"
    
    return imie + "a"


def validate_customer_name(name: str) -> Optional[str]:
    """Waliduj imię - zwraca None jeśli to śmieć."""
    if not name:
        return None
    name = name.strip()
    
    invalid = [
        "pan", "pani", "tak", "nie", "halo", "cześć", "dziękuję", 
        "proszę", "dobrze", "ok", "okej", "słucham", "przepraszam",
        "yes", "no", "moment", "chwila", "sekunda", "jasne"
    ]
    
    if name.lower() in invalid or len(name) < 2:
        logger.warning(f"⚠️ Invalid name rejected: '{name}'")
        return None
    
    for prefix in ["pan ", "pani "]:
        if name.lower().startswith(prefix):
            name = name[len(prefix):]
    
    return name.strip().title()


# ============================================================================
# BOOKING FAILED NODE - eskalacja gdy rezerwacja się nie udała
# ============================================================================

def create_booking_failed_node(tenant: dict, reason: str = "") -> dict:
    """Node: rezerwacja nie udała się - przekaż do właściciela"""
    from flows import end_conversation_function
    from flows_contact import contact_owner_function
    
    return {
        "name": "booking_failed",
        "respond_immediately": True,
        "role_messages": [{
            "role": "system",
            "content": f"""Rezerwacja nie udała się: {reason}

Przeproś klienta i zaproponuj przekazanie wiadomości do właściciela, który pomoże z rezerwacją."""
        }],
        "task_messages": [{
            "role": "system",
            "content": "Przeproś KRÓTKO i zapytaj czy przekazać wiadomość do właściciela."
        }],
        "functions": [
            contact_owner_function(tenant),
            end_conversation_function(),
        ]
    }


# ============================================================================
# START BOOKING - główna funkcja rozpoczęcia rezerwacji
# ============================================================================

def start_booking_function() -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="start_booking",
        description="Klient chce umówić wizytę. Użyj gdy klient mówi że chce się umówić/zarezerwować/zapisać.",
        properties={},
        required=[],
        handler=handle_start_booking,
    )


async def handle_start_booking(args: dict, flow_manager: FlowManager):
    """Start rezerwacji - KOD resetuje state i przechodzi do wyboru usługi"""
    from flows_contact import create_collect_contact_name_node
    
    tenant = flow_manager.state.get("tenant", {})
    caller_phone = flow_manager.state.get("caller_phone", "unknown")
    
    logger.info(f"📅 Booking START | phone: {caller_phone}")
    
    # Reset state
    flow_manager.state["selected_service"] = None
    flow_manager.state["selected_staff"] = None
    flow_manager.state["selected_date"] = None
    flow_manager.state["selected_time"] = None
    flow_manager.state["customer_name"] = None
    flow_manager.state["available_slots"] = []
    
    # Walidacja
    services = tenant.get("services", [])
    staff_list = tenant.get("staff", [])
    
    if not services:
        logger.error("❌ No services configured")
        return ({"error": "Brak skonfigurowanych usług"}, create_collect_contact_name_node(tenant))
    
    if not staff_list:
        logger.error("❌ No staff configured")
        return ({"error": "Brak skonfigurowanych pracowników"}, create_collect_contact_name_node(tenant))
    
    # Przejdź do wyboru usługi
    return ({"status": "started"}, create_get_service_node(tenant))


# ============================================================================
# KROK 1/6: WYBÓR USŁUGI
# ============================================================================

def create_get_service_node(tenant: dict) -> dict:
    """NODE: Wybór usługi - GPT generuje naturalny tekst"""
    services = tenant.get("services", [])
    service_names = [s["name"] for s in services]
    services_list = ", ".join(service_names)
    
    return {
        "name": "get_service",
        "respond_immediately": True,  # GPT od razu generuje odpowiedź
        "role_messages": [{
            "role": "system",
            "content": f"""Jesteś w trakcie umawiania wizyty.
KROK 1 z 6: Wybór usługi.

Dostępne usługi: {services_list}

Powiedz naturalnie że chętnie umówisz wizytę i zapytaj na jaką usługę."""
        }],
        "task_messages": [{
            "role": "system",
            "content": f"""Zapytaj klienta na jaką usługę chce się umówić.
Dostępne: {services_list}

Gdy klient powie usługę → wywołaj select_service
Jeśli pyta o cenę/czas → odpowiedz KRÓTKO i wróć do pytania o usługę"""
        }],
        "functions": [
            select_service_function(tenant, service_names),
        ]
    }


def select_service_function(tenant: dict, available_services: list) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="select_service",
        description="Klient wybrał usługę",
        properties={
            "service_name": {
                "type": "string", 
                "description": "Nazwa usługi którą klient wybrał",
                "enum": available_services if available_services else None
            }
        },
        required=["service_name"],
        handler=lambda args, fm: handle_select_service(args, fm, tenant),
    )


async def handle_select_service(args: dict, flow_manager: FlowManager, tenant: dict):
    """Handler: walidacja usługi w KODZIE, przejście do pracownika"""
    service_name = args.get("service_name", "")
    services = tenant.get("services", [])
    
    # Fuzzy matching w kodzie
    found = fuzzy_match_service(service_name, services)
    
    if not found:
        if not check_retry_limit(flow_manager, "service"):
            return (None, create_booking_failed_node(tenant, "nie udało się wybrać usługi"))
        
        available = ", ".join([s["name"] for s in services])
        return ({"error": f"Nie mamy takiej usługi. Dostępne: {available}"}, None)
    
    reset_retry_count(flow_manager, "service")
    flow_manager.state["selected_service"] = found
    logger.info(f"✅ [1/6] Service: {found['name']}")
    
    # KOD decyduje o przejściu do pracownika
    return ({"success": True, "service": found["name"]}, create_get_staff_node(tenant, found))


# ============================================================================
# KROK 2/6: WYBÓR PRACOWNIKA
# ============================================================================

def create_get_staff_node(tenant: dict, selected_service: dict = None) -> dict:
    """NODE: Wybór pracownika - GPT generuje naturalny tekst"""
    all_staff = tenant.get("staff", [])
    service_name = selected_service.get("name", "") if selected_service else ""
    
    # Filtruj pracowników dla tej usługi
    if selected_service:
        service_id = selected_service.get("id")
        available_staff = []
        for s in all_staff:
            staff_service_ids = [svc.get("id") for svc in s.get("services", [])]
            if not staff_service_ids or service_id in staff_service_ids:
                available_staff.append(s)
        if not available_staff:
            available_staff = all_staff
    else:
        available_staff = all_staff
    
    staff_names = [s["name"] for s in available_staff]
    staff_list = ", ".join(staff_names)
    
    return {
        "name": "get_staff",
        "respond_immediately": True,
        "role_messages": [{
            "role": "system",
            "content": f"""Klient wybrał: {service_name}
KROK 2 z 6: Wybór pracownika.

Pracownicy wykonujący tę usługę: {staff_list}

Potwierdź wybór usługi i zapytaj do kogo chce się umówić."""
        }],
        "task_messages": [{
            "role": "system",
            "content": f"""Zapytaj do którego pracownika. Dostępni: {staff_list}

Gdy klient powie imię → wywołaj select_staff
Jeśli "obojętnie"/"ktokolwiek" → wybierz pierwszego: {staff_names[0] if staff_names else ''}

⚠️ NIE pytaj jeszcze o datę - to następny krok!"""
        }],
        "functions": [
            select_staff_function(tenant, staff_names),
        ]
    }


def select_staff_function(tenant: dict, available_names: list) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="select_staff",
        description="Klient wybrał pracownika",
        properties={
            "staff_name": {
                "type": "string", 
                "description": "Imię pracownika",
                "enum": available_names if available_names else None
            }
        },
        required=["staff_name"],
        handler=lambda args, fm: handle_select_staff(args, fm, tenant),
    )


async def handle_select_staff(args: dict, flow_manager: FlowManager, tenant: dict):
    """Handler: walidacja pracownika w KODZIE"""
    staff_name = args.get("staff_name", "")
    staff_list = tenant.get("staff", [])
    selected_service = flow_manager.state.get("selected_service", {})
    
    found = fuzzy_match_staff(staff_name, staff_list)
    
    if not found:
        if not check_retry_limit(flow_manager, "staff"):
            return (None, create_booking_failed_node(tenant, "nie udało się wybrać pracownika"))
        
        available = ", ".join([s["name"] for s in staff_list])
        return ({"error": f"Nie mamy takiego pracownika. Dostępni: {available}"}, None)
    
    # Walidacja: czy pracownik wykonuje tę usługę?
    if selected_service:
        staff_service_ids = [svc.get("id") for svc in found.get("services", [])]
        if staff_service_ids and selected_service.get("id") not in staff_service_ids:
            available_for_service = [
                st["name"] for st in staff_list
                if not [svc.get("id") for svc in st.get("services", [])] 
                or selected_service.get("id") in [svc.get("id") for svc in st.get("services", [])]
            ]
            if not check_retry_limit(flow_manager, "staff"):
                return (None, create_booking_failed_node(tenant, "pracownik nie wykonuje tej usługi"))
            return ({"error": f"{found['name']} nie wykonuje {selected_service['name']}. Dostępni: {', '.join(available_for_service)}"}, None)
    
    reset_retry_count(flow_manager, "staff")
    flow_manager.state["selected_staff"] = found
    logger.info(f"✅ [2/6] Staff: {found['name']}")
    
    return ({"success": True, "staff": found["name"]}, create_get_date_node(tenant, found))


# ============================================================================
# KROK 3/6: WYBÓR DATY
# ============================================================================

def create_get_date_node(tenant: dict, selected_staff: dict = None) -> dict:
    """NODE: Wybór daty - GPT generuje naturalny tekst"""
    now = datetime.now()
    today_str = f"{now.strftime('%d.%m.%Y')} ({POLISH_DAYS[now.weekday()]})"
    staff_name = selected_staff.get("name", "") if selected_staff else ""
    staff_name_dop = odmien_imie_dopelniacz(staff_name)
    
    return {
        "name": "get_date",
        "respond_immediately": True,
        "role_messages": [{
            "role": "system",
            "content": f"""Klient umawia się do {staff_name_dop}.
KROK 3 z 6: Wybór daty.

Dziś jest: {today_str}

Potwierdź wybór pracownika i zapytaj na kiedy."""
        }],
        "task_messages": [{
            "role": "system",
            "content": """Zapytaj na jaki dzień umówić wizytę.

Gdy klient poda datę (jutro, poniedziałek, konkretna data) → wywołaj check_availability

⚠️ NIE zgaduj godzin - system sprawdzi dostępność i poda wolne terminy."""
        }],
        "functions": [
            check_availability_function(tenant),
        ]
    }


def check_availability_function(tenant: dict) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="check_availability",
        description="Klient podał datę - sprawdź dostępność",
        properties={
            "date": {
                "type": "string", 
                "description": "Data podana przez klienta (np. jutro, poniedziałek, 15.02)"
            }
        },
        required=["date"],
        handler=lambda args, fm: handle_check_availability(args, fm, tenant),
    )


async def handle_check_availability(args: dict, flow_manager: FlowManager, tenant: dict):
    """Handler: sprawdzenie dostępności w KODZIE"""
    from flows import play_snippet
    
    date_str = args.get("date", "")
    
    # Feedback dla użytkownika
    await play_snippet(flow_manager, "checking")
    
    staff = flow_manager.state.get("selected_staff", {})
    service = flow_manager.state.get("selected_service", {})
    
    # Parsowanie daty w kodzie
    parsed_date = parse_polish_date(date_str)
    if not parsed_date:
        if not check_retry_limit(flow_manager, "date"):
            return (None, create_booking_failed_node(tenant, "nie udało się wybrać daty"))
        return ({"error": "Nie rozumiem daty. Powiedz np. jutro, w piątek, 15 lutego."}, None)
    
    # Walidacja daty
    today = datetime.now()
    if parsed_date.date() < today.date():
        try:
            parsed_date = parsed_date.replace(year=parsed_date.year + 1)
            if parsed_date.date() < today.date():
                return ({"error": "Ta data już minęła. Wybierz przyszłą datę."}, None)
        except:
            return ({"error": "Ta data już minęła."}, None)
    
    max_days = tenant.get("max_booking_days", 30)
    max_date = today + timedelta(days=max_days)
    if parsed_date.date() > max_date.date():
        return ({"error": f"Mogę umówić maksymalnie {max_days} dni do przodu."}, None)
    
    valid, error = validate_date_constraints(parsed_date, tenant, staff)
    if not valid:
        return ({"error": error}, None)
    
    weekday = parsed_date.weekday()
    if get_opening_hours(tenant, weekday) is None:
        return ({"error": f"W {POLISH_DAYS[weekday]} jesteśmy zamknięci. Wybierz inny dzień."}, None)
    
    # Sprawdź dostępność w API
    slots = await get_available_slots(tenant, staff, service, parsed_date)
    if not slots:
        if not check_retry_limit(flow_manager, "date"):
            return (None, create_booking_failed_node(tenant, "brak wolnych terminów"))
        date_text = format_date_polish(parsed_date)
        return ({"error": f"Na {date_text} brak wolnych terminów. Wybierz inny dzień."}, None)
    
    reset_retry_count(flow_manager, "date")
    flow_manager.state["selected_date"] = parsed_date
    flow_manager.state["available_slots"] = slots
    
    logger.info(f"✅ [3/6] Date: {parsed_date.strftime('%Y-%m-%d')}, slots: {slots}")
    
    date_text = format_date_polish(parsed_date)
    slots_text = format_slots_natural(slots, format_hour_polish)
    
    return (
        {"success": True, "date": date_text, "available_slots": slots_text},
        create_get_time_node(tenant, slots, date_text, slots_text)
    )


# ============================================================================
# KROK 4/6: WYBÓR GODZINY
# ============================================================================

def create_get_time_node(tenant: dict, available_slots: list, date_text: str = "", slots_text: str = "") -> dict:
    """NODE: Wybór godziny - GPT generuje naturalny tekst"""
    if not slots_text:
        slots_text = format_slots_natural(available_slots, format_hour_polish)
    
    return {
        "name": "get_time",
        "respond_immediately": True,
        "role_messages": [{
            "role": "system",
            "content": f"""Data wybrana: {date_text}
KROK 4 z 6: Wybór godziny.

Wolne terminy: {slots_text}

Podaj dostępne godziny i zapytaj która pasuje."""
        }],
        "task_messages": [{
            "role": "system",
            "content": f"""Powiedz jakie godziny są wolne i zapytaj którą wybrać.
Wolne: {slots_text}

Gdy klient powie godzinę → wywołaj select_time"""
        }],
        "functions": [
            select_time_function(tenant, available_slots),
        ]
    }


def select_time_function(tenant: dict, available_slots: list) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="select_time",
        description="Klient wybrał godzinę",
        properties={
            "hour": {
                "type": "string",
                "description": "Godzina którą klient wybrał (np. 10, dziesiąta)"
            }
        },
        required=["hour"],
        handler=lambda args, fm: handle_select_time(args, fm, tenant),
    )


async def handle_select_time(args: dict, flow_manager: FlowManager, tenant: dict):
    """Handler: walidacja godziny w KODZIE"""
    hour_str = args.get("hour", "")
    slots = flow_manager.state.get("available_slots", [])
    
    # Parsowanie godziny w kodzie
    hour = None
    try:
        hour = int(hour_str)
    except (ValueError, TypeError):
        hour = parse_time(hour_str)
    
    if hour is None or hour not in slots:
        if not check_retry_limit(flow_manager, "time"):
            return (None, create_booking_failed_node(tenant, "nie udało się wybrać godziny"))
        
        slots_text = format_slots_natural(slots, format_hour_polish)
        logger.warning(f"⚠️ Invalid time '{hour_str}' (parsed: {hour}), available: {slots}")
        return ({"error": f"Ta godzina jest zajęta. Wolne: {slots_text}"}, None)
    
    reset_retry_count(flow_manager, "time")
    flow_manager.state["selected_time"] = hour
    logger.info(f"✅ [4/6] Time: {hour}:00")
    
    hour_text = format_hour_polish(hour)
    return ({"success": True, "time": hour_text}, create_get_name_node(tenant, hour_text))


# ============================================================================
# KROK 5/6: IMIĘ KLIENTA
# ============================================================================

def create_get_name_node(tenant: dict, hour_text: str = "") -> dict:
    """NODE: Imię klienta - GPT generuje naturalny tekst"""
    return {
        "name": "get_name",
        "respond_immediately": True,
        "role_messages": [{
            "role": "system",
            "content": f"""Godzina wybrana: {hour_text}
KROK 5 z 6: Imię do rezerwacji.

Potwierdź godzinę i zapytaj na jakie imię/nazwisko zapisać."""
        }],
        "task_messages": [{
            "role": "system",
            "content": """Zapytaj jak zapisać rezerwację - imię lub nazwisko.

Gdy klient poda imię → wywołaj set_customer_name"""
        }],
        "functions": [
            set_customer_name_function(tenant),
        ]
    }


def set_customer_name_function(tenant: dict) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="set_customer_name",
        description="Klient podał imię/nazwisko",
        properties={
            "customer_name": {
                "type": "string", 
                "description": "Imię lub nazwisko klienta"
            }
        },
        required=["customer_name"],
        handler=lambda args, fm: handle_set_customer_name(args, fm, tenant),
    )


async def handle_set_customer_name(args: dict, flow_manager: FlowManager, tenant: dict):
    """Handler: walidacja imienia w KODZIE"""
    validated = validate_customer_name(args.get("customer_name", ""))
    
    if not validated:
        if not check_retry_limit(flow_manager, "name"):
            return (None, create_booking_failed_node(tenant, "nie udało się zapisać imienia"))
        return ({"error": "Nie dosłyszałam. Jak mogę zapisać?"}, None)
    
    reset_retry_count(flow_manager, "name")
    flow_manager.state["customer_name"] = validated
    logger.info(f"✅ [5/6] Name: {validated}")
    
    # Buduj podsumowanie
    service = flow_manager.state.get("selected_service", {})
    staff = flow_manager.state.get("selected_staff", {})
    date = flow_manager.state.get("selected_date")
    hour = flow_manager.state.get("selected_time")
    
    date_text = format_date_polish(date) if date else ""
    time_text = format_hour_polish(hour) if hour else ""
    staff_name_dop = odmien_imie_dopelniacz(staff.get('name', ''))
    
    summary = f"{service.get('name', '')} u {staff_name_dop}, {date_text} o {time_text}, na {validated}"
    
    return ({"success": True, "name": validated, "summary": summary}, create_confirm_booking_node(tenant, summary))


# ============================================================================
# KROK 6/6: POTWIERDZENIE
# ============================================================================

def create_confirm_booking_node(tenant: dict, summary: str = "") -> dict:
    """NODE: Potwierdzenie - GPT generuje naturalny tekst"""
    return {
        "name": "confirm_booking",
        "respond_immediately": True,
        "role_messages": [{
            "role": "system",
            "content": f"""KROK 6 z 6: Potwierdzenie rezerwacji.

Podsumowanie: {summary}

Powtórz podsumowanie i poproś o potwierdzenie."""
        }],
        "task_messages": [{
            "role": "system",
            "content": """Powiedz podsumowanie i zapytaj czy potwierdzić.

TAK/potwierdzam → wywołaj confirm_booking_yes
NIE/zmień → wywołaj confirm_booking_no"""
        }],
        "functions": [
            confirm_booking_yes_function(tenant),
            confirm_booking_no_function(tenant),
        ]
    }


def confirm_booking_yes_function(tenant: dict) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="confirm_booking_yes",
        description="Klient POTWIERDZA rezerwację",
        properties={},
        required=[],
        handler=lambda args, fm: handle_confirm_booking_yes(args, fm, tenant),
    )


def confirm_booking_no_function(tenant: dict) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="confirm_booking_no",
        description="Klient chce ZMIENIĆ coś w rezerwacji",
        properties={
            "what_to_change": {
                "type": "string",
                "enum": ["usługa", "pracownik", "data", "godzina", "imię", "wszystko"],
                "description": "Co zmienić"
            }
        },
        required=[],
        handler=lambda args, fm: handle_confirm_booking_no(args, fm, tenant),
    )


async def handle_confirm_booking_yes(args: dict, flow_manager: FlowManager, tenant: dict):
    """Handler: zapis rezerwacji w KODZIE"""
    from flows import play_snippet, create_anything_else_node
    from flows_contact import create_collect_contact_name_node
    
    logger.info("✅ [6/6] CONFIRMED - saving booking")
    
    await play_snippet(flow_manager, "saving")
    
    service = flow_manager.state.get("selected_service", {})
    staff = flow_manager.state.get("selected_staff", {})
    date = flow_manager.state.get("selected_date")
    hour = flow_manager.state.get("selected_time")
    name = flow_manager.state.get("customer_name", "")
    caller_phone = flow_manager.state.get("caller_phone", "")
    
    # Double-check dostępności
    try:
        current_slots = await asyncio.wait_for(
            get_available_slots(tenant, staff, service, date),
            timeout=5.0
        )
    except asyncio.TimeoutError:
        logger.error("⚠️ Timeout checking availability")
        return ({"error": "System nie odpowiada. Spróbuj ponownie lub zostaw wiadomość."}, 
                create_collect_contact_name_node(tenant))
    
    if hour not in current_slots:
        logger.warning(f"⚠️ Slot {hour}:00 no longer available!")
        flow_manager.state["available_slots"] = current_slots
        if current_slots:
            slots_text = format_slots_natural(current_slots, format_hour_polish)
            return ({"error": f"Godzina {format_hour_polish(hour)} właśnie została zajęta. Wolne: {slots_text}"},
                    create_get_time_node(tenant, current_slots))
        else:
            return ({"error": f"Na {format_date_polish(date)} nie ma już wolnych terminów."},
                    create_get_date_node(tenant))
    
    # Zapisz rezerwację
    booking_code = None
    booking_saved = False
    
    try:
        result = await save_booking_to_api(tenant, staff, service, date, hour, name, caller_phone)
        if result:
            booking_saved = True
            booking_code = result.get("booking_code")
            logger.info(f"✅ BOOKING SAVED! Code: {booking_code}")
    except Exception as e:
        logger.error(f"❌ Save error: {e}")
    
    # Wyślij SMS
    if booking_saved and booking_code and caller_phone:
        try:
            sms_sent = await send_booking_sms(
                tenant=tenant,
                customer_phone=caller_phone,
                service_name=service.get("name", "Wizyta"),
                staff_name=staff.get("name", ""),
                date_str=date.strftime("%d.%m") if date else "",
                time_str=f"{hour}:00" if hour else "",
                booking_code=booking_code
            )
            if sms_sent:
                await increment_sms_count(tenant.get("id"))
        except Exception as e:
            logger.error(f"📱 SMS error: {e}")
    
    # Odpowiedź
    date_text = format_date_polish(date) if date else ""
    time_text = format_hour_polish(hour) if hour else ""
    
    if booking_saved:
        sms_info = " Wysłałam SMS z potwierdzeniem." if booking_code else ""
        return (
            {"success": True, "message": f"Gotowe! {service.get('name')} u {staff.get('name')}, {date_text} o {time_text}.{sms_info} Do zobaczenia!"},
            create_anything_else_node(tenant)
        )
    else:
        return (
            {"error": "Wystąpił problem z zapisem. Czy przekazać wiadomość do właściciela?"},
            create_collect_contact_name_node(tenant)
        )


async def handle_confirm_booking_no(args: dict, flow_manager: FlowManager, tenant: dict):
    """Handler: zmiana danych rezerwacji"""
    what = args.get("what_to_change", "wszystko")
    
    logger.info(f"🔄 Change requested: {what}")
    
    if what == "usługa":
        flow_manager.state["selected_service"] = None
        flow_manager.state["selected_staff"] = None
        return ({"info": "Zmieńmy usługę."}, create_get_service_node(tenant))
    
    elif what == "pracownik":
        flow_manager.state["selected_staff"] = None
        service = flow_manager.state.get("selected_service")
        return ({"info": "Zmieńmy pracownika."}, create_get_staff_node(tenant, service))
    
    elif what == "data":
        flow_manager.state["selected_date"] = None
        flow_manager.state["selected_time"] = None
        staff = flow_manager.state.get("selected_staff")
        return ({"info": "Zmieńmy datę."}, create_get_date_node(tenant, staff))
    
    elif what == "godzina":
        flow_manager.state["selected_time"] = None
        slots = flow_manager.state.get("available_slots", [])
        if slots:
            return ({"info": "Zmieńmy godzinę."}, create_get_time_node(tenant, slots))
        else:
            staff = flow_manager.state.get("selected_staff")
            return ({"info": "Sprawdzę dostępność."}, create_get_date_node(tenant, staff))
    
    elif what == "imię":
        flow_manager.state["customer_name"] = None
        return ({"info": "Zmieńmy imię."}, create_get_name_node(tenant))
    
    else:
        # Reset wszystkiego
        flow_manager.state["selected_service"] = None
        flow_manager.state["selected_staff"] = None
        flow_manager.state["selected_date"] = None
        flow_manager.state["selected_time"] = None
        flow_manager.state["customer_name"] = None
        return ({"info": "Zacznijmy od nowa."}, create_get_service_node(tenant))


# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    "start_booking_function",
    "create_get_service_node",
    "create_get_staff_node", 
    "create_get_date_node",
    "create_get_time_node",
    "create_get_name_node",
    "create_confirm_booking_node",
    "validate_customer_name",
    "odmien_imie_dopelniacz",
    "format_slots_natural",
]