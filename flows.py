# flows.py - Pipecat Flows dla systemu rezerwacji
from pipecat_flows import FlowManager, FlowsFunctionSchema
from typing import Any
from loguru import logger


def create_initial_node(tenant: dict) -> dict:
    """
    Pierwszy node - powitanie i rozpoznanie intencji.
    
    Flow:
    greeting → [booking] → get_service
            → [question] → answer_question  
            → [goodbye] → end
    """
    
    business_name = tenant.get("name", "salon")
    first_message = tenant.get("first_message") or f"Dzień dobry, tu {business_name}. W czym mogę pomóc?"
    
    # Formatuj usługi
    services = tenant.get("services", [])
    services_list = ", ".join([s["name"] for s in services]) if services else "brak usług"
    
    # Formatuj pracowników
    staff = tenant.get("staff", [])
    staff_list = ", ".join([s["name"] for s in staff]) if staff else "brak pracowników"
    
    return {
        "name": "greeting",
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
                "content": f"""Przywitaj się: "{first_message}"

Następnie rozpoznaj czego chce klient:
- Jeśli chce się UMÓWIĆ/ZAREZERWOWAĆ → użyj funkcji start_booking
- Jeśli ma PYTANIE (godziny, ceny, lokalizacja) → użyj funkcji answer_question
- Jeśli chce się POŻEGNAĆ → użyj funkcji end_conversation"""
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
    """Funkcja do rozpoczęcia procesu rezerwacji"""
    return FlowsFunctionSchema(
        name="start_booking",
        description="Klient chce umówić wizytę lub zarezerwować termin",
        properties={},
        handler=handle_start_booking,
    )


async def handle_start_booking(args: dict, flow_manager: FlowManager):
    """Handler - przejdź do wyboru usługi"""
    logger.info("📅 Starting booking flow")
    
    tenant = flow_manager.state.get("tenant", {})
    return ("Świetnie, umówmy wizytę.", create_get_service_node(tenant))


# ==========================================
# NODE: Wybór usługi
# ==========================================

def create_get_service_node(tenant: dict) -> dict:
    """Node do wyboru usługi - z WALIDACJĄ!"""
    
    services = tenant.get("services", [])
    services_list = ", ".join([s["name"] for s in services]) if services else "brak"
    
    return {
        "name": "get_service",
        "task_messages": [
            {
                "role": "system",
                "content": f"""Zapytaj klienta jaką usługę chce.

DOSTĘPNE USŁUGI: {services_list}

WAŻNE: Gdy klient powie usługę, użyj funkcji select_service.
Jeśli powie usługę której NIE MA na liście, powiedz że nie oferujecie takiej usługi i podaj dostępne."""
            }
        ],
        "functions": [
            select_service_function(tenant),
        ]
    }


def select_service_function(tenant: dict) -> FlowsFunctionSchema:
    """Funkcja wyboru usługi z walidacją"""
    return FlowsFunctionSchema(
        name="select_service",
        description="Klient wybrał usługę",
        properties={
            "service_name": {
                "type": "string",
                "description": "Nazwa usługi którą klient chce"
            }
        },
        required=["service_name"],
        handler=lambda args, fm: handle_select_service(args, fm, tenant),
    )


async def handle_select_service(args: dict, flow_manager: FlowManager, tenant: dict):
    """Handler - WALIDACJA usługi!"""
    service_name = args.get("service_name", "").lower()
    services = tenant.get("services", [])
    
    logger.info(f"🔍 Validating service: {service_name}")
    
    # Szukaj usługi
    found_service = None
    for s in services:
        if service_name in s["name"].lower() or s["name"].lower() in service_name:
            found_service = s
            break
    
    if not found_service:
        # ❌ NIE MA TAKIEJ USŁUGI!
        available = ", ".join([s["name"] for s in services])
        logger.warning(f"❌ Service not found: {service_name}")
        return (
            f"Przepraszam, nie oferujemy usługi '{service_name}'. Mamy: {available}. Którą usługę wybierasz?",
            None  # Zostań w tym samym node
        )
    
    # ✅ Usługa znaleziona
    flow_manager.state["selected_service"] = found_service
    logger.info(f"✅ Service selected: {found_service['name']}")
    
    return (
        f"Świetnie, {found_service['name']}.",
        create_get_staff_node(tenant)
    )


# ==========================================
# NODE: Wybór pracownika
# ==========================================

def create_get_staff_node(tenant: dict) -> dict:
    """Node do wyboru pracownika - z WALIDACJĄ!"""
    
    staff = tenant.get("staff", [])
    staff_list = ", ".join([s["name"] for s in staff]) if staff else "brak"
    
    return {
        "name": "get_staff",
        "task_messages": [
            {
                "role": "system",
                "content": f"""Zapytaj do kogo klient chce się umówić.

DOSTĘPNI PRACOWNICY: {staff_list}

Jeśli klient nie ma preferencji, powiedz że wybierzesz pierwszego dostępnego.
Jeśli poda imię którego NIE MA, powiedz że nie ma takiego pracownika i podaj dostępnych."""
            }
        ],
        "functions": [
            select_staff_function(tenant),
            any_staff_function(tenant),
        ]
    }


def select_staff_function(tenant: dict) -> FlowsFunctionSchema:
    """Funkcja wyboru konkretnego pracownika"""
    return FlowsFunctionSchema(
        name="select_staff",
        description="Klient wybrał konkretnego pracownika",
        properties={
            "staff_name": {
                "type": "string",
                "description": "Imię pracownika"
            }
        },
        required=["staff_name"],
        handler=lambda args, fm: handle_select_staff(args, fm, tenant),
    )


def any_staff_function(tenant: dict) -> FlowsFunctionSchema:
    """Funkcja gdy klient nie ma preferencji"""
    return FlowsFunctionSchema(
        name="any_available_staff",
        description="Klient nie ma preferencji, wybierz dowolnego dostępnego",
        properties={},
        handler=lambda args, fm: handle_any_staff(args, fm, tenant),
    )


async def handle_select_staff(args: dict, flow_manager: FlowManager, tenant: dict):
    """Handler - WALIDACJA pracownika!"""
    staff_name = args.get("staff_name", "").lower()
    staff_list = tenant.get("staff", [])
    
    logger.info(f"🔍 Validating staff: {staff_name}")
    
    # Szukaj pracownika
    found_staff = None
    for s in staff_list:
        if staff_name in s["name"].lower() or s["name"].lower() in staff_name:
            found_staff = s
            break
    
    if not found_staff:
        # ❌ NIE MA TAKIEGO PRACOWNIKA!
        available = ", ".join([s["name"] for s in staff_list])
        logger.warning(f"❌ Staff not found: {staff_name}")
        return (
            f"Przepraszam, nie mamy pracownika o imieniu {staff_name}. U nas pracują: {available}. Do kogo chcesz się umówić?",
            None  # Zostań w tym samym node
        )
    
    # ✅ Pracownik znaleziony
    flow_manager.state["selected_staff"] = found_staff
    logger.info(f"✅ Staff selected: {found_staff['name']}")
    
    return (
        f"Dobrze, do {found_staff['name']}.",
        create_get_date_node(tenant)
    )


async def handle_any_staff(args: dict, flow_manager: FlowManager, tenant: dict):
    """Handler - wybierz pierwszego dostępnego"""
    staff_list = tenant.get("staff", [])
    
    if staff_list:
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
# NODE: Wybór daty/terminu
# ==========================================

def create_get_date_node(tenant: dict) -> dict:
    """Node do wyboru terminu"""
    
    return {
        "name": "get_date",
        "task_messages": [
            {
                "role": "system",
                "content": """Zapytaj kiedy klient chciałby się umówić.

Zaproponuj najbliższe dni (dziś, jutro, pojutrze, lub konkretny dzień tygodnia).
Gdy klient poda dzień, zapytaj o preferowaną godzinę.

Użyj funkcji check_availability gdy będziesz mieć dzień i przybliżoną godzinę."""
            }
        ],
        "functions": [
            check_availability_function(tenant),
        ]
    }


def check_availability_function(tenant: dict) -> FlowsFunctionSchema:
    """Funkcja sprawdzenia dostępności"""
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
    """Handler - sprawdź PRAWDZIWĄ dostępność"""
    date = args.get("date", "")
    preferred_time = args.get("preferred_time", "")
    
    staff = flow_manager.state.get("selected_staff", {})
    service = flow_manager.state.get("selected_service", {})
    
    logger.info(f"📅 Checking availability: {date}, {preferred_time}, staff: {staff.get('name')}")
    
    # TODO: Tutaj integracja z Google Calendar
    # Na razie symulowane sloty
    available_slots = ["dziesiąta", "dwunasta", "czternasta"]
    
    flow_manager.state["available_slots"] = available_slots
    flow_manager.state["selected_date"] = date
    
    slots_text = ", ".join(available_slots)
    
    return (
        f"Sprawdzam... W {date} mam wolne terminy: {slots_text}. Która godzina pasuje?",
        create_select_time_node(tenant)
    )


# ==========================================
# NODE: Wybór godziny
# ==========================================

def create_select_time_node(tenant: dict) -> dict:
    """Node do wyboru konkretnej godziny"""
    
    return {
        "name": "select_time",
        "task_messages": [
            {
                "role": "system",
                "content": """Klient wybiera godzinę z dostępnych.
Gdy wybierze, potwierdź wszystkie szczegóły i przejdź do potwierdzenia."""
            }
        ],
        "functions": [
            confirm_time_function(tenant),
        ]
    }


def confirm_time_function(tenant: dict) -> FlowsFunctionSchema:
    """Funkcja potwierdzenia godziny"""
    return FlowsFunctionSchema(
        name="confirm_time",
        description="Klient wybrał godzinę",
        properties={
            "time": {
                "type": "string",
                "description": "Wybrana godzina"
            }
        },
        required=["time"],
        handler=lambda args, fm: handle_confirm_time(args, fm, tenant),
    )


async def handle_confirm_time(args: dict, flow_manager: FlowManager, tenant: dict):
    """Handler - zapisz wybraną godzinę"""
    time = args.get("time", "")
    
    flow_manager.state["selected_time"] = time
    
    # Podsumowanie
    service = flow_manager.state.get("selected_service", {})
    staff = flow_manager.state.get("selected_staff", {})
    date = flow_manager.state.get("selected_date", "")
    
    logger.info(f"✅ Time selected: {time}")
    
    return (
        f"Świetnie! Potwierdzam: {service.get('name')} u {staff.get('name')}, {date} o godzinie {time}. Jak się Pan nazywa?",
        create_get_name_node(tenant)
    )


# ==========================================
# NODE: Pobranie imienia
# ==========================================

def create_get_name_node(tenant: dict) -> dict:
    """Node do pobrania imienia klienta"""
    
    return {
        "name": "get_name",
        "task_messages": [
            {
                "role": "system",
                "content": """Zapisz imię klienta i potwierdź rezerwację."""
            }
        ],
        "functions": [
            complete_booking_function(tenant),
        ]
    }


def complete_booking_function(tenant: dict) -> FlowsFunctionSchema:
    """Funkcja zakończenia rezerwacji"""
    return FlowsFunctionSchema(
        name="complete_booking",
        description="Zapisz rezerwację z imieniem klienta",
        properties={
            "customer_name": {
                "type": "string",
                "description": "Imię klienta"
            }
        },
        required=["customer_name"],
        handler=lambda args, fm: handle_complete_booking(args, fm, tenant),
    )


async def handle_complete_booking(args: dict, flow_manager: FlowManager, tenant: dict):
    """Handler - ZAPISZ REZERWACJĘ do bazy!"""
    customer_name = args.get("customer_name", "")
    
    flow_manager.state["customer_name"] = customer_name
    
    # Pobierz wszystkie dane
    service = flow_manager.state.get("selected_service", {})
    staff = flow_manager.state.get("selected_staff", {})
    date = flow_manager.state.get("selected_date", "")
    time = flow_manager.state.get("selected_time", "")
    
    logger.info(f"💾 Saving booking: {customer_name}, {service.get('name')}, {staff.get('name')}, {date} {time}")
    
    # TODO: Zapisz do bazy danych i Google Calendar
    # Generuj kod wizyty
    import random
    import string
    visit_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    
    flow_manager.state["visit_code"] = visit_code
    
    return (
        f"Gotowe! Rezerwacja dla {customer_name}: {service.get('name')} u {staff.get('name')}, {date} o {time}. Kod wizyty: {visit_code}. Do zobaczenia!",
        create_end_node()
    )


# ==========================================
# NODE: Odpowiedź na pytanie
# ==========================================

def answer_question_function(tenant: dict) -> FlowsFunctionSchema:
    """Funkcja do odpowiedzi na pytania"""
    
    # Przygotuj info o firmie
    address = tenant.get("address", "brak adresu")
    additional_info = tenant.get("additional_info", "")
    
    return FlowsFunctionSchema(
        name="answer_question",
        description="Klient ma pytanie (godziny, ceny, lokalizacja, etc.)",
        properties={
            "question": {
                "type": "string",
                "description": "Pytanie klienta"
            }
        },
        required=["question"],
        handler=lambda args, fm: handle_answer_question(args, fm, tenant),
    )


async def handle_answer_question(args: dict, flow_manager: FlowManager, tenant: dict):
    """Handler - odpowiedz na pytanie i wróć do głównego menu"""
    question = args.get("question", "")
    
    logger.info(f"❓ Question: {question}")
    
    # TODO: Tutaj możemy użyć FAQ z bazy
    
    # Wróć do greeting po odpowiedzi
    return (
        None,  # LLM sam odpowie na pytanie
        create_initial_node(tenant)  # Wróć do początku
    )


# ==========================================
# NODE: Zakończenie rozmowy
# ==========================================

def end_conversation_function() -> FlowsFunctionSchema:
    """Funkcja zakończenia rozmowy"""
    return FlowsFunctionSchema(
        name="end_conversation",
        description="Klient chce zakończyć rozmowę",
        properties={},
        handler=handle_end_conversation,
    )


async def handle_end_conversation(args: dict, flow_manager: FlowManager):
    """Handler - zakończ rozmowę"""
    logger.info("👋 Ending conversation")
    return (
        "Do widzenia, miłego dnia!",
        create_end_node()
    )


def create_end_node() -> dict:
    """Końcowy node"""
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