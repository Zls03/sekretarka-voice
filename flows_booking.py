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
import uuid
from helpers import db
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

# ==========================================
# ERROR LOGGING
# ==========================================
async def log_error(flow_manager, error_type: str, error_message: str, context: str = None):
    """Loguje błąd do bazy"""
    try:
        tenant = flow_manager.state.get("tenant", {})
        call_sid = flow_manager.state.get("call_sid", "")
        
        if not tenant.get("id"):
            return
            
        error_id = f"err_{uuid.uuid4().hex[:12]}"
        await db.execute(
            """INSERT INTO error_logs 
               (id, tenant_id, call_sid, error_type, error_message, context, created_at)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
            [error_id, tenant.get("id"), call_sid, error_type, error_message, context]
        )
        logger.info(f"📝 Error logged: {error_type}")
    except Exception as e:
        logger.error(f"Failed to log error: {e}")

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
        # Nie możemy użyć await tutaj bo funkcja nie jest async
        # Błąd zostanie zalogowany w create_booking_failed_node
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
    """Start rezerwacji - KOD resetuje state i przechodzi do wyboru usługi
    
    Zgodnie z Pipecat Flows best practices - wykrywamy pre-wybory z kontekstu
    i zapisujemy do flow_manager.state dla późniejszego użycia.
    """
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
    flow_manager.state["pre_selected_staff"] = None  # Pre-wybór z kontekstu
    
    # Walidacja
    services = tenant.get("services", [])
    staff_list = tenant.get("staff", [])
    
    if not services:
        logger.error("❌ No services configured")
        return ({"error": "Brak skonfigurowanych usług"}, create_collect_contact_name_node(tenant))
    
    if not staff_list:
        logger.error("❌ No staff configured")
        return ({"error": "Brak skonfigurowanych pracowników"}, create_collect_contact_name_node(tenant))
    
    # Wykryj pre-wybranego pracownika z kontekstu rozmowy
    try:
        context = flow_manager.get_current_context()
        staff_names_lower = {s["name"].lower(): s for s in staff_list}
        
        # Sprawdź ostatnie wiadomości użytkownika
        for msg in reversed(context[-10:] if len(context) > 10 else context):
            if msg.get("role") == "user":
                content = msg.get("content", "").lower()
                for name_lower, staff_obj in staff_names_lower.items():
                    # Sprawdź różne formy imienia (mianownik, dopełniacz, biernik)
                    name_variants = [name_lower]
                    
                    # Dodaj formy odmienione
                    if name_lower.endswith("ia"):
                        # Ania → Ani, Anię, Anią
                        name_variants.append(name_lower[:-1] + "i")
                        name_variants.append(name_lower[:-1] + "ę")
                        name_variants.append(name_lower[:-1] + "ą")
                    elif name_lower.endswith("a"):
                        # Kasia → Kasi, Kasię, Kasią
                        name_variants.append(name_lower[:-1] + "i")
                        name_variants.append(name_lower[:-1] + "ę")
                        name_variants.append(name_lower[:-1] + "ą")
                    elif name_lower.endswith("ek"):
                        # Witek → Witka, Witkiem
                        name_variants.append(name_lower[:-2] + "ka")
                        name_variants.append(name_lower[:-2] + "kiem")
                    else:
                        # Wiktor → Wiktora, Wiktorem
                        name_variants.append(name_lower + "a")
                        name_variants.append(name_lower + "em")
                    
                    if any(variant in content for variant in name_variants):
                        flow_manager.state["pre_selected_staff"] = staff_obj
                        logger.info(f"📝 Pre-selected staff from context: {staff_obj['name']}")
                        break
            if flow_manager.state.get("pre_selected_staff"):
                break
    except Exception as e:
        logger.debug(f"Could not parse context for pre-selection: {e}")
    
    # Jeśli mamy pre-wybranego pracownika, filtruj usługi
    pre_staff = flow_manager.state.get("pre_selected_staff")
    if pre_staff:
        return ({"status": "started"}, create_get_service_node(tenant, filter_by_staff=pre_staff))
    
    # Przejdź do wyboru usługi
    return ({"status": "started"}, create_get_service_node(tenant))


# ============================================================================
# KROK 1/6: WYBÓR USŁUGI
# ============================================================================

def create_get_service_node(tenant: dict, filter_by_staff: dict = None) -> dict:
    """NODE: Wybór usługi - GPT generuje naturalny tekst
    
    Args:
        filter_by_staff: Jeśli podany, pokazuj tylko usługi tego pracownika
    """
    services = tenant.get("services", [])
    
    # Filtruj usługi jeśli pracownik pre-wybrany
    staff_info = ""
    if filter_by_staff:
        staff_service_ids = set(s.get("id") for s in filter_by_staff.get("services", []))
        if staff_service_ids:
            services = [s for s in services if s.get("id") in staff_service_ids]
        staff_info = f" u {filter_by_staff.get('name', '')}"
        logger.info(f"📝 Filtering services for {filter_by_staff.get('name')}: {[s['name'] for s in services]}")
    
    service_names = [s["name"] for s in services]
    services_list = ", ".join(service_names)
    
    # Cennik dla GPT (żeby mógł odpowiedzieć na pytania)
    price_info = []
    for s in services:
        info = s["name"]
        if s.get("price"):
            info += f" - {s['price']} zł"
        if s.get("duration_minutes"):
            info += f" ({s['duration_minutes']} min)"
        price_info.append(info)
    price_list = ", ".join(price_info)
    
    role_content = f"""Jesteś w trakcie umawiania wizyty.
KROK 1 z 6: Wybór usługi.

Dostępne usługi{staff_info}: {services_list}
CENNIK: {price_list}

Powiedz naturalnie że chętnie umówisz wizytę{staff_info} i zapytaj na jaką usługę."""

    return {
        "name": "get_service",
        "respond_immediately": True,  # GPT od razu generuje odpowiedź
        "role_messages": [{
            "role": "system",
            "content": role_content
        }],
        "task_messages": [{
            "role": "system",
            "content": f"""Zapytaj klienta na jaką usługę chce się umówić.
Dostępne: {services_list}

Once they choose a service, use select_service with the service name.
If they ask about price/duration, answer briefly and ask again about the service."""
        }],
        "functions": [
            select_service_function(tenant, service_names),
        ]
    }


def select_service_function(tenant: dict, available_services: list) -> FlowsFunctionSchema:
    services_hint = ", ".join(available_services) if available_services else ""
    
    return FlowsFunctionSchema(
        name="select_service",
        description="Klient wybrał usługę",
        properties={
            "service_name": {
                "type": "string", 
                "description": f"Nazwa usługi którą klient powiedział. Dostępne: {services_hint}"
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
        return ({"error": f"Nie mamy takiej usługi. Dostępne: {available}"}, 
                create_get_service_node(tenant))  # ← RETRY ten sam node!
    
    reset_retry_count(flow_manager, "service")
    flow_manager.state["selected_service"] = found
    logger.info(f"✅ [1/6] Service: {found['name']}")
    
    # Sprawdź czy pracownik był pre-wybrany
    pre_staff = flow_manager.state.get("pre_selected_staff")
    if pre_staff:
        # Walidacja: czy ten pracownik wykonuje wybraną usługę?
        staff_service_ids = [svc.get("id") for svc in pre_staff.get("services", [])]
        if staff_service_ids and found.get("id") not in staff_service_ids:
            # Pracownik nie wykonuje tej usługi - wyczyść pre-wybór
            logger.info(f"⚠️ Pre-selected {pre_staff['name']} doesn't do {found['name']}, clearing")
            flow_manager.state["pre_selected_staff"] = None
        else:
            # OK - użyj pre-wybranego pracownika i pomiń krok wyboru
            flow_manager.state["selected_staff"] = pre_staff
            logger.info(f"✅ [2/6] Staff (pre-selected): {pre_staff['name']}")
            return ({"success": True, "service": found["name"], "staff": pre_staff["name"]}, 
                    create_get_date_node(tenant, pre_staff))
    
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

Once they choose a staff member, use select_staff with the name.
If they say "obojętnie"/"ktokolwiek" → choose: {staff_names[0] if staff_names else ''}

⚠️ NIE pytaj jeszcze o datę - to następny krok!"""
        }],
        "functions": [
            select_staff_function(tenant, staff_names),
        ]
    }


def select_staff_function(tenant: dict, available_names: list) -> FlowsFunctionSchema:
    staff_hint = ", ".join(available_names) if available_names else ""
    
    return FlowsFunctionSchema(
        name="select_staff",
        description=f"""Klient wybrał pracownika. Dostępni: {staff_hint}
UWAGA: "Nie, do X" lub "Nie, chcę X" znaczy że klient WYBIERA X (nie zgadza się z Twoją propozycją).""",
        properties={
            "staff_name": {
                "type": "string", 
                "description": f"Imię pracownika które klient powiedział. Dostępni: {staff_hint}"
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
        selected_service = flow_manager.state.get("selected_service")
        return ({"error": f"Nie mamy takiego pracownika. Dostępni: {available}"}, 
                create_get_staff_node(tenant, selected_service))  # ← RETRY!
    
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
            return ({"error": f"{found['name']} nie wykonuje {selected_service['name']}. Dostępni: {', '.join(available_for_service)}"}, 
                    create_get_staff_node(tenant, selected_service))  # ← RETRY!
    
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
            "content": """Zapytaj klienta na jaki dzień chce się umówić, potem CZEKAJ na odpowiedź.

Once they provide a date (e.g. "jutro", "w piątek", "3 lutego"), use check_availability with EXACTLY what they said.

⚠️ ZASADY:
- NIE wywołuj funkcji dopóki klient nie poda daty
- NIE przeliczaj dat - przekaż dosłownie to co klient powiedział
- Jeśli funkcja zwróci error → poproś o INNY dzień"""
        }],
        "functions": [
            check_availability_function(tenant),
        ]
    }


def check_availability_function(tenant: dict) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="check_availability",
        description="Klient podał datę - sprawdź dostępność. ZAWSZE przekaż DOKŁADNIE co klient powiedział.",
        properties={
            "date": {
                "type": "string", 
                "description": "DOKŁADNIE co klient powiedział o dacie - np. 'sobota', 'jutro', 'w piątek', 'piętnastego'. NIE przeliczaj na format daty!"
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
        staff = flow_manager.state.get("selected_staff")
        return ({"error": "Nie rozumiem daty. Powiedz np. jutro, w piątek, 15 lutego."}, 
                create_get_date_node(tenant, staff))  # ← RETRY!
    
    # Walidacja daty
    today = datetime.now()
    if parsed_date.date() < today.date():
        try:
            parsed_date = parsed_date.replace(year=parsed_date.year + 1)
            if parsed_date.date() < today.date():
                staff = flow_manager.state.get("selected_staff")
                return ({"error": "Ta data już minęła. Wybierz przyszłą datę."}, 
                        create_get_date_node(tenant, staff))
        except:
            staff = flow_manager.state.get("selected_staff")
            return ({"error": "Ta data już minęła."}, 
                    create_get_date_node(tenant, staff))
    
    max_days = tenant.get("max_booking_days", 30)
    max_date = today + timedelta(days=max_days)
    if parsed_date.date() > max_date.date():
        staff = flow_manager.state.get("selected_staff")
        return ({"error": f"Mogę umówić maksymalnie {max_days} dni do przodu."}, 
                create_get_date_node(tenant, staff))
    
    valid, error = validate_date_constraints(parsed_date, tenant, staff)
    if not valid:
        staff = flow_manager.state.get("selected_staff")
        return ({"error": error}, create_get_date_node(tenant, staff))
    
    weekday = parsed_date.weekday()
    if get_opening_hours(tenant, weekday) is None:
        staff = flow_manager.state.get("selected_staff")
        return ({"error": f"W {POLISH_DAYS[weekday]} jesteśmy zamknięci. Wybierz inny dzień."}, 
                create_get_date_node(tenant, staff))
    
    slots = await get_available_slots(tenant, staff, service, parsed_date)
    if not slots:
        if not check_retry_limit(flow_manager, "date"):
            return (None, create_booking_failed_node(tenant, "brak wolnych terminów"))
        date_text = format_date_polish(parsed_date)
        staff = flow_manager.state.get("selected_staff")
        return ({"error": f"Na {date_text} brak wolnych terminów. Wybierz inny dzień."}, 
                create_get_date_node(tenant, staff))
    
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

Once they choose a time, use select_time with the hour they selected.

⚠️ ZASADY:
- NIE mów że "zapisałam" bez wywołania select_time
- Jeśli select_time zwróci error → podaj inne wolne godziny"""
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
        date_text = format_date_polish(flow_manager.state.get("selected_date"))
        logger.warning(f"⚠️ Invalid time '{hour_str}' (parsed: {hour}), available: {slots}")
        return ({"error": f"Ta godzina jest zajęta. Wolne: {slots_text}"}, 
                create_get_time_node(tenant, slots, date_text, slots_text))  # ← RETRY!
    
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

Once they provide their name, use set_customer_name."""
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
        return ({"error": "Nie dosłyszałam. Jak mogę zapisać?"}, 
                create_get_name_node(tenant))  # ← RETRY!
    
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

Once they respond:
- If they confirm (tak/potwierdzam/dobrze/zgadza się) → use confirm_booking_yes
- If they want changes (nie/zmień/popraw) → use confirm_booking_no

⚠️ ZASADY:
- NIE mów że "zarezerwowałam" bez wywołania confirm_booking_yes
- Jeśli klient podaje INNE imię → użyj confirm_booking_no z what_to_change="imię" """
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
        await log_error(
            flow_manager,
            "slot_taken",
            f"Slot {hour}:00 was taken before confirmation",
            f"date={date}, available_now={current_slots}"
        )
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
        await log_error(
            flow_manager, 
            "booking_failed", 
            str(e),
            f"service={service.get('name')}, staff={staff.get('name')}, date={date}, hour={hour}"
        )
    
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
        # 🛡️ Ustaw flagę że rezerwacja zakończona sukcesem
        flow_manager.state["booking_confirmed"] = True
        
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