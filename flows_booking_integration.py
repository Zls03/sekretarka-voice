# flows_booking_integration.py - Integracja FSM z Pipecat Flows
"""
Ten plik łączy nowy system FSM (flows_booking_fsm.py) z istniejącym botem.

UŻYCIE:
1. W flows.py zamień import:
   - STARE: from flows_booking import start_booking_function
   - NOWE: from flows_booking_integration import start_booking_function

2. Reszta kodu zostaje BEZ ZMIAN!

ARCHITEKTURA:
- start_booking_function() → startuje proces FSM
- Każda odpowiedź klienta → handle_booking_message() w FSM
- FSM zwraca gotową odpowiedź + następny node
"""

from pipecat_flows import FlowManager, FlowsFunctionSchema
from loguru import logger
from typing import Dict, Optional
import asyncio

from flows_booking_fsm import (
    BookingState, BookingStep, ParsedInput, Intent,
    handle_booking_message, parse_user_input,
)


# ============================================================================
# START BOOKING - Główna funkcja wywoływana przez GPT
# ============================================================================

def start_booking_function() -> FlowsFunctionSchema:
    """
    Funkcja startowa rezerwacji - kompatybilna z obecnym systemem.
    GPT wywołuje ją gdy klient chce się umówić.
    """
    return FlowsFunctionSchema(
        name="start_booking",
        description="Klient chce umówić wizytę. Użyj gdy klient mówi że chce się umówić/zarezerwować/zapisać.",
        properties={},
        required=[],
        handler=handle_start_booking,
    )


async def handle_start_booking(args: dict, flow_manager: FlowManager):
    """
    Handler startowy - inicjalizuje FSM i zwraca pierwszy node.
    """
    tenant = flow_manager.state.get("tenant", {})
    caller_phone = flow_manager.state.get("caller_phone", "unknown")
    
    logger.info(f"📅 BOOKING START (FSM) | phone: {caller_phone}")
    
    # Walidacja podstawowa
    services = tenant.get("services", [])
    staff_list = tenant.get("staff", [])
    
    if not services:
        logger.error("❌ No services configured")
        from flows_contact import create_collect_contact_name_node
        return ({"error": "Brak skonfigurowanych usług"}, create_collect_contact_name_node(tenant))
    
    if not staff_list:
        logger.error("❌ No staff configured")
        from flows_contact import create_collect_contact_name_node
        return ({"error": "Brak skonfigurowanych pracowników"}, create_collect_contact_name_node(tenant))
    
    # Inicjalizuj nowy stan FSM
    state = BookingState()
    state.current_step = BookingStep.SERVICE
    
    # Wykryj pre-wybranego pracownika z kontekstu rozmowy
    state = detect_preselection_from_context(flow_manager, state, tenant)
    
    # Zapisz stan w flow_manager
    flow_manager.state["booking_state"] = state
    flow_manager.state["booking_confirmed"] = False
    flow_manager.state["current_step"] = state.current_step.value
    
    # Zwróć node który będzie zbierał odpowiedzi
    return ({"status": "started"}, create_booking_conversation_node(tenant, state))


def detect_preselection_from_context(
    flow_manager: FlowManager, 
    state: BookingState, 
    tenant: Dict
) -> BookingState:
    """
    Wykrywa pre-wybranego pracownika z kontekstu rozmowy.
    Np. "Chcę umówić się do Ani" → pre_selected_staff = Ania
    """
    try:
        context = flow_manager.get_current_context()
        staff_list = tenant.get("staff", [])
        staff_names_lower = {s["name"].lower(): s for s in staff_list}
        
        for msg in reversed(context[-10:] if len(context) > 10 else context):
            if msg.get("role") == "user":
                content = msg.get("content", "").lower()
                
                for name_lower, staff_obj in staff_names_lower.items():
                    # Sprawdź różne formy imienia
                    name_variants = [name_lower]
                    
                    if name_lower.endswith("ia"):
                        name_variants.extend([name_lower[:-1] + "i", name_lower[:-1] + "ę"])
                    elif name_lower.endswith("a"):
                        name_variants.extend([name_lower[:-1] + "i", name_lower[:-1] + "ę"])
                    elif name_lower.endswith("ek"):
                        name_variants.extend([name_lower[:-2] + "ka"])
                    else:
                        name_variants.extend([name_lower + "a", name_lower + "em"])
                    
                    if any(variant in content for variant in name_variants):
                        state.pre_selected_staff = staff_obj
                        logger.info(f"📝 Pre-selected staff from context: {staff_obj['name']}")
                        return state
                        
    except Exception as e:
        logger.debug(f"Could not parse context for pre-selection: {e}")
    
    return state


# ============================================================================
# BOOKING CONVERSATION NODE - Główny node rozmowy
# ============================================================================

def create_booking_conversation_node(tenant: Dict, state: BookingState) -> Dict:
    """
    Tworzy node który zbiera odpowiedzi klienta i przekazuje do FSM.
    """
    
    # Buduj prompt na podstawie aktualnego kroku
    step_prompts = {
        BookingStep.SERVICE: "Zapytaj klienta na jaką usługę chce się umówić.",
        BookingStep.STAFF: "Zapytaj do którego pracownika.",
        BookingStep.DATE: "Zapytaj na jaki dzień.",
        BookingStep.TIME: "Podaj wolne godziny i zapytaj którą wybrać.",
        BookingStep.NAME: "Zapytaj na jakie imię zapisać.",
        BookingStep.CONFIRM: "Powtórz podsumowanie i poproś o potwierdzenie.",
    }
    
    current_prompt = step_prompts.get(state.current_step, "Kontynuuj rozmowę.")
    
    # Buduj kontekst
    context_parts = []
    if state.selected_service:
        context_parts.append(f"Usługa: {state.selected_service['name']}")
    if state.selected_staff:
        context_parts.append(f"Pracownik: {state.selected_staff['name']}")
    if state.selected_date:
        from flows_helpers import format_date_polish
        context_parts.append(f"Data: {format_date_polish(state.selected_date)}")
    if state.selected_time:
        from flows_helpers import format_hour_polish
        context_parts.append(f"Godzina: {format_hour_polish(state.selected_time)}")
    
    context_text = "\n".join(context_parts) if context_parts else "Brak wybranych danych."
    
    # Lista dostępnych opcji
    services = tenant.get("services", [])
    staff_list = tenant.get("staff", [])
    
    if state.current_step == BookingStep.SERVICE:
        options = [s["name"] for s in services]
    elif state.current_step == BookingStep.STAFF:
        if state.selected_service:
            from flows_helpers import staff_can_do_service
            options = [s["name"] for s in staff_list if staff_can_do_service(s, state.selected_service)]
        else:
            options = [s["name"] for s in staff_list]
    elif state.current_step == BookingStep.TIME:
        from flows_helpers import format_hour_polish
        options = [format_hour_polish(s) for s in state.available_slots[:6]]
    else:
        options = []
    
    options_text = ", ".join(options) if options else ""
    
    return {
        "name": f"booking_step_{state.current_step.value.lower()}",
        "respond_immediately": True,
        "role_messages": [{
            "role": "system",
            "content": f"""Jesteś asystentką salonu "{tenant.get('name', 'salon')}".
Mów KRÓTKO (1-2 zdania), naturalnie, w rodzaju żeńskim.
Używaj formy "Pan/Pani". Godziny mów słownie.

AKTUALNY STAN REZERWACJI:
{context_text}

KROK: {state.current_step.value}
ZADANIE: {current_prompt}
{f'DOSTĘPNE OPCJE: {options_text}' if options_text else ''}"""
        }],
        "task_messages": [{
            "role": "system",
            "content": f"""Po każdej odpowiedzi klienta wywołaj process_booking_input z DOKŁADNIE tym co klient powiedział.

NIE interpretuj, NIE przetwarzaj - przekaż dosłownie.

Przykłady:
- Klient: "do Ani na piątek" → process_booking_input(user_input="do Ani na piątek")
- Klient: "tak, potwierdzam" → process_booking_input(user_input="tak, potwierdzam")
- Klient: "na trzynastą" → process_booking_input(user_input="na trzynastą")"""
        }],
        "functions": [
            process_booking_input_function(tenant, state),
        ]
    }


def process_booking_input_function(tenant: Dict, state: BookingState) -> FlowsFunctionSchema:
    """
    Funkcja przetwarzająca input klienta przez FSM.
    """
    return FlowsFunctionSchema(
        name="process_booking_input",
        description="Przetwórz odpowiedź klienta. Przekaż DOKŁADNIE co klient powiedział.",
        properties={
            "user_input": {
                "type": "string",
                "description": "DOKŁADNIE to co klient powiedział - nie interpretuj!"
            }
        },
        required=["user_input"],
        handler=lambda args, fm: handle_process_booking_input(args, fm, tenant, state),
    )


async def handle_process_booking_input(
    args: dict, 
    flow_manager: FlowManager, 
    tenant: Dict, 
    state: BookingState
):
    """
    Handler - przekazuje input do FSM i zwraca odpowiedź.
    """
    user_input = args.get("user_input", "")
    caller_phone = flow_manager.state.get("caller_phone", "")
    
    logger.info(f"📥 BOOKING INPUT: '{user_input}' | step: {state.current_step.value}")
    
    # Wywołaj FSM
    response, new_state, is_done = await handle_booking_message(
        user_input, state, tenant, caller_phone
    )
    
    # Aktualizuj stan w flow_manager
    flow_manager.state["booking_state"] = new_state
    flow_manager.state["booking_confirmed"] = new_state.booking_confirmed
    flow_manager.state["current_step"] = new_state.current_step.value
    
    # Wybierz następny node
    if is_done and new_state.booking_confirmed:
        # Sukces - rezerwacja zapisana
        logger.info("✅ BOOKING CONFIRMED!")
        from flows import create_anything_else_node
        return ({"success": True, "message": response}, create_anything_else_node(tenant))
    
    elif is_done:
        # Zakończenie bez rezerwacji (np. goodbye, cancel)
        logger.info("👋 BOOKING ENDED (no confirmation)")
        from flows import create_end_node
        return ({"message": response}, create_end_node())
    
    elif new_state.current_step == BookingStep.FAILED:
        # Błąd - przekieruj do właściciela
        logger.warning("❌ BOOKING FAILED - escalating")
        from flows_contact import create_collect_contact_name_node
        return ({"error": response}, create_collect_contact_name_node(tenant))
    
    else:
        # Kontynuuj rezerwację
        return ({"message": response}, create_booking_conversation_node(tenant, new_state))


# ============================================================================
# HELPER: Snippet dla "sprawdzam..."
# ============================================================================

async def play_checking_snippet(flow_manager: FlowManager):
    """Puszcza snippet 'Sprawdzam...' przez TTS"""
    try:
        from pipecat.frames.frames import TTSSpeakFrame
        import random
        
        phrases = ["Sprawdzam...", "Moment, sprawdzam...", "Już patrzę..."]
        phrase = random.choice(phrases)
        
        await flow_manager.task.queue_frame(TTSSpeakFrame(text=phrase))
        logger.info(f"🔊 TTS snippet: {phrase}")
        
    except Exception as e:
        logger.warning(f"🔊 Snippet error: {e}")


# ============================================================================
# EXPORTS - Kompatybilne z obecnym systemem
# ============================================================================

__all__ = [
    "start_booking_function",
    "create_booking_conversation_node",
    "process_booking_input_function",
]