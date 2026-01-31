# flows_booking_integration.py - Integracja FSM z HARD GUARD
# WERSJA 2.0 - LLM NIE MOŻE ominąć FSM podczas rezerwacji
"""
KLUCZOWA ZMIANA:
- respond_immediately = False (LLM NIE MOŻE mówić sam)
- Tylko JEDNA funkcja: process_booking_input
- GPT MUSI ją wywołać - nie ma innej opcji

ARCHITEKTURA ENTERPRISE (USA/Chiny):
- LLM nigdy nie decyduje o flow
- FSM jest JEDYNYM źródłem prawdy
- Pytania typu "jaki to dzień?" → FSM obsługuje jako clarification
"""

from pipecat_flows import FlowManager, FlowsFunctionSchema
from loguru import logger
from typing import Dict, Optional, Tuple
import asyncio

from flows_booking_fsm import (
    BookingState, BookingStep, ParsedInput, Intent,
    handle_booking_message, parse_user_input, generate_response,
    build_generator_context, validate_and_merge, get_next_step,
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
    Handler startowy - inicjalizuje FSM i PRZETWARZA PIERWSZĄ WIADOMOŚĆ.
    🔥 FIX: Pobiera tekst klienta z kontekstu i od razu go przetwarza!
    """
    tenant = flow_manager.state.get("tenant", {})
    caller_phone = flow_manager.state.get("caller_phone", "unknown")
    
    logger.info(f"📅 BOOKING START (FSM v2) | phone: {caller_phone}")
    
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
    
    # Wykryj płeć klienta z kontekstu
    client_gender = detect_client_gender(flow_manager)
    flow_manager.state["client_gender"] = client_gender
    
    # 🔥 WAŻNE: Ustaw flagę że booking jest aktywny
    flow_manager.state["booking_active"] = True
    flow_manager.state["booking_state"] = state
    flow_manager.state["booking_confirmed"] = False
    flow_manager.state["current_step"] = state.current_step.value
    
    # 🔥 FIX: Pobierz PIERWSZĄ wiadomość klienta z kontekstu i przetwórz ją!
    initial_message = extract_initial_booking_message(flow_manager)
    
    if initial_message:
        logger.info(f"📥 INITIAL MESSAGE FOUND: '{initial_message}'")
        
        # Przetwórz od razu przez FSM!
        response, new_state, is_done = await handle_booking_message_v2(
            initial_message, state, tenant, caller_phone, client_gender
        )
        
        # Aktualizuj stan
        flow_manager.state["booking_state"] = new_state
        flow_manager.state["booking_confirmed"] = new_state.booking_confirmed
        flow_manager.state["current_step"] = new_state.current_step.value
        
        if is_done and new_state.booking_confirmed:
            logger.info("✅ BOOKING CONFIRMED on first message!")
            flow_manager.state["booking_active"] = False
            from flows import create_anything_else_node
            # 🔥 FIX: Użyj TTSSpeakFrame bezpośrednio!
            from pipecat.frames.frames import TTSSpeakFrame
            await flow_manager.task.queue_frame(TTSSpeakFrame(text=response))
            return (None, create_anything_else_node(tenant))
        
        # 🔥 FIX: Zapisz odpowiedź w state i zwróć None!
        new_state.last_response = response
        return (None, create_booking_hard_node(tenant, new_state, client_gender))
    
    # Brak initial message - standardowy start (zapytaj o usługę)
    from flows_booking_fsm import generate_response, build_generator_context
    ctx = build_generator_context(BookingStep.SERVICE, state, tenant)
    initial_response = await generate_response(ctx)
    
    # 🔥 FIX: Zapisz odpowiedź w state!
    state.last_response = initial_response
    return (None, create_booking_hard_node(tenant, state, client_gender))


def extract_initial_booking_message(flow_manager: FlowManager) -> Optional[str]:
    """
    🔥 Wyciąga pierwszą wiadomość klienta która zawiera prośbę o rezerwację.
    Szuka w kontekście ostatniej wiadomości user przed wywołaniem start_booking.
    """
    try:
        context = flow_manager.get_current_context()
        
        # 🔥 DEBUG: Pokaż co jest w kontekście
        logger.info(f"📝 CONTEXT DEBUG: {len(context)} messages")
        for i, msg in enumerate(context[-5:]):  # Ostatnie 5
            role = msg.get("role", "?")
            content = msg.get("content", "")[:80]
            logger.info(f"📝 [{i}] {role}: {content}...")
        
        # Szukaj ostatniej wiadomości user (to ta która wywołała start_booking)
        for msg in reversed(context):
            if msg.get("role") == "user":
                content = msg.get("content", "").strip()
                
                # Sprawdź czy to nie jest tylko "tak", "halo" itp.
                ignore_phrases = ["tak", "halo", "słucham", "proszę", "ok", "dobrze", "no"]
                if content.lower() in ignore_phrases:
                    logger.info(f"📝 Skipping ignored phrase: '{content}'")
                    continue
                
                # Sprawdź czy zawiera słowa kluczowe rezerwacji
                booking_keywords = ["umówić", "zapisać", "rezerwac", "wizyt", "termin", 
                                   "strzyżenie", "farbowanie", "manicure", "usług"]
                
                content_lower = content.lower()
                if any(kw in content_lower for kw in booking_keywords):
                    logger.info(f"📝 Found initial booking message: '{content[:80]}...'")
                    return content
                
                # Jeśli to pierwsza sensowna wiadomość - zwróć ją
                if len(content) > 10:
                    logger.info(f"📝 Using first substantial message: '{content[:80]}...'")
                    return content
        
        logger.warning("📝 No initial booking message found in context")
        return None
        
    except Exception as e:
        logger.error(f"📝 Error extracting initial message: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None


def detect_client_gender(flow_manager: FlowManager) -> str:
    """
    Wykrywa płeć klienta z kontekstu rozmowy.
    Zwraca: "male", "female", lub "unknown"
    """
    try:
        context = flow_manager.get_current_context()
        
        male_markers = ["chciałbym", "chciałem", "zapisałbym", "umówiłbym", "jestem zainteresowany", "pan"]
        female_markers = ["chciałabym", "chciałam", "zapisałabym", "umówiłabym", "jestem zainteresowana", "pani"]
        
        for msg in context:
            if msg.get("role") == "user":
                content = msg.get("content", "").lower()
                
                for marker in male_markers:
                    if marker in content:
                        logger.info(f"👤 Detected gender: MALE (marker: '{marker}')")
                        return "male"
                
                for marker in female_markers:
                    if marker in content:
                        logger.info(f"👤 Detected gender: FEMALE (marker: '{marker}')")
                        return "female"
                        
    except Exception as e:
        logger.debug(f"Could not detect gender: {e}")
    
    return "unknown"


# ============================================================================
# HARD NODE - LLM NIE MA WYBORU, MUSI WYWOŁAĆ process_booking_input
# ============================================================================

def create_booking_hard_node(tenant: Dict, state: BookingState, client_gender: str = "unknown") -> Dict:
    """
    🔥 HARD NODE - LLM nie może odpowiedzieć sam!
    
    Kluczowe:
    - respond_immediately = False
    - Tylko JEDNA funkcja do wyboru
    - Instrukcja: "ZAWSZE wywołaj process_booking_input"
    """
    
    # Buduj kontekst stanu
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
    if state.customer_name:
        context_parts.append(f"Imię: {state.customer_name}")
    
    context_text = "\n".join(context_parts) if context_parts else "Brak danych."
    
    # Forma grzecznościowa
    if client_gender == "male":
        gender_instruction = "Klient to PAN - używaj formy męskiej (Pan, Pana, Panu)."
    elif client_gender == "female":
        gender_instruction = "Klient to PANI - używaj formy żeńskiej (Pani, Panią)."
    else:
        gender_instruction = "Nieznana płeć - używaj neutralnie lub 'Pan/Pani'."
    
    # 🔥 Pobierz ostatnią odpowiedź FSM do wypowiedzenia
    last_response = state.last_response if hasattr(state, 'last_response') and state.last_response else None
    
    # 🔥 FIX: Użyj pre_actions żeby bot POWIEDZIAŁ odpowiedź!
    if last_response:
        pre_actions = [{"type": "tts_say", "text": last_response}]
    else:
        pre_actions = []
    
    return {
        "name": f"booking_fsm_{state.current_step.value.lower()}",
        
        # 🔥 KLUCZOWE: pre_actions mówi odpowiedź!
        "pre_actions": pre_actions,
        
        # 🔥 KLUCZOWE: respond_immediately = False
        # LLM NIE MOŻE odpowiedzieć tekstem bez wywołania funkcji!
        "respond_immediately": False,
        
        "role_messages": [{
            "role": "system",
            "content": f"""Jesteś w trybie REZERWACJI. {gender_instruction}

AKTUALNY STAN:
{context_text}
Krok: {state.current_step.value}

⛔ ZAKAZ: NIE odpowiadaj tekstem. TYLKO wywołaj process_booking_input."""
        }],
        
        "task_messages": [{
            "role": "system", 
            "content": """⛔ BEZWZGLĘDNY ZAKAZ ODPOWIADANIA TEKSTEM!

KAŻDA odpowiedź klienta → NATYCHMIAST wywołaj:
process_booking_input(user_input="dokładnie co klient powiedział")

Dotyczy WSZYSTKIEGO:
✓ "na farbowanie" → process_booking_input
✓ "do Ani" → process_booking_input
✓ "tak" → process_booking_input
✓ "jaki to dzień?" → process_booking_input
✓ "nie wiem" → process_booking_input
✓ DOSŁOWNIE WSZYSTKO → process_booking_input

NIE WOLNO Ci:
✗ Odpowiadać na pytania
✗ Wyjaśniać
✗ Dopytywać
✗ Robić COKOLWIEK innego niż process_booking_input

NATYCHMIAST wywołaj process_booking_input!"""
        }],
        
        # 🔥 TYLKO JEDNA FUNKCJA - zero wyboru dla GPT
        "functions": [
            process_booking_input_function(tenant, state, client_gender),
        ]
    }


def process_booking_input_function(tenant: Dict, state: BookingState, client_gender: str) -> FlowsFunctionSchema:
    """
    JEDYNA funkcja dostępna podczas rezerwacji.
    """
    return FlowsFunctionSchema(
        name="process_booking_input",
        description="ZAWSZE wywołaj tę funkcję. Przekaż DOKŁADNIE co klient powiedział.",
        properties={
            "user_input": {
                "type": "string",
                "description": "DOKŁADNIE to co klient powiedział - słowo w słowo!"
            }
        },
        required=["user_input"],
        # 🔥 FIX 1: Przekazujemy tylko tenant - state czytamy z flow_manager.state
        handler=lambda args, fm: handle_booking_input_v2(args, fm, tenant),
    )


# ============================================================================
# HANDLER V2 - Z obsługą clarification i hard control
# ============================================================================

async def handle_booking_input_v2(
    args: dict, 
    flow_manager: FlowManager, 
    tenant: Dict,
):
    """
    Handler v2 - pełna kontrola FSM, obsługa clarification.
    🔥 FIX 1: State ZAWSZE z flow_manager.state (source of truth)
    """
    user_input = args.get("user_input", "").strip()
    caller_phone = flow_manager.state.get("caller_phone", "")
    
    # 🔥 FIX 1: Czytaj state z flow_manager (nie z closure!)
    state = flow_manager.state.get("booking_state")
    if state is None:
        state = BookingState()
        state.current_step = BookingStep.SERVICE
    
    client_gender = flow_manager.state.get("client_gender", "unknown")
    
    logger.info(f"📥 FSM INPUT: '{user_input}' | step: {state.current_step.value}")
    
    # 🔥 FIX 1: Normalizacja STT (np. "Napaweł" → "Na Paweł" → "Paweł")
    user_input = normalize_stt_input(user_input, state.current_step)
    
    # Wywołaj główną logikę FSM
    response, new_state, is_done = await handle_booking_message_v2(
        user_input, state, tenant, caller_phone, client_gender
    )
    
    # Aktualizuj stan w flow_manager
    flow_manager.state["booking_state"] = new_state
    flow_manager.state["booking_confirmed"] = new_state.booking_confirmed
    flow_manager.state["current_step"] = new_state.current_step.value
    
    # Wybierz następny node
    if is_done and new_state.booking_confirmed:
        # ✅ Sukces - rezerwacja zapisana
        logger.info("✅ BOOKING CONFIRMED!")
        flow_manager.state["booking_active"] = False
        from flows import create_anything_else_node
        # 🔥 FIX: Użyj TTSSpeakFrame bezpośrednio!
        from pipecat.frames.frames import TTSSpeakFrame
        await flow_manager.task.queue_frame(TTSSpeakFrame(text=response))
        return (None, create_anything_else_node(tenant))
    
    elif is_done:
        # 👋 Zakończenie (cancel, goodbye)
        logger.info("👋 BOOKING ENDED")
        flow_manager.state["booking_active"] = False
        from flows import create_end_node
        # 🔥 FIX: Użyj TTSSpeakFrame bezpośrednio!
        from pipecat.frames.frames import TTSSpeakFrame
        await flow_manager.task.queue_frame(TTSSpeakFrame(text=response))
        return (None, create_end_node())
    
    elif new_state.current_step == BookingStep.FAILED:
        # ❌ Błąd - przekieruj do właściciela
        logger.warning("❌ BOOKING FAILED - escalating")
        flow_manager.state["booking_active"] = False
        from flows_contact import create_collect_contact_name_node
        # 🔥 FIX: Użyj TTSSpeakFrame bezpośrednio!
        from pipecat.frames.frames import TTSSpeakFrame
        await flow_manager.task.queue_frame(TTSSpeakFrame(text=response))
        return (None, create_collect_contact_name_node(tenant))
    
    else:
        # 🔄 Kontynuuj rezerwację - wróć do HARD NODE
        # 🔥 FIX: Zapisz odpowiedź w state żeby node mógł ją wypowiedzieć!
        new_state.last_response = response
        # 🔥 FIX: Zwróć None jako pierwszy element - odpowiedź jest w pre_actions node'a!
        return (None, create_booking_hard_node(tenant, new_state, client_gender))


def normalize_stt_input(text: str, current_step: BookingStep) -> str:
    """
    Normalizuje błędy STT, szczególnie dla imion.
    """
    if not text:
        return text
    
    original = text
    
    # 🔥 FIX: "Napaweł" → "Paweł" (STT złącza "Na Paweł")
    if current_step == BookingStep.NAME:
        # Usuń prefix "Na" jeśli wygląda na błąd STT
        prefixes_to_strip = ["na", "to", "tu"]
        text_lower = text.lower()
        
        for prefix in prefixes_to_strip:
            if text_lower.startswith(prefix) and len(text) > len(prefix) + 2:
                rest = text[len(prefix):]
                # Sprawdź czy reszta zaczyna się od wielkiej litery (imię)
                if rest and rest[0].isupper():
                    text = rest
                    logger.info(f"🔧 STT FIX: '{original}' → '{text}'")
                    break
                # Lub czy po usunięciu spacji zaczyna się od wielkiej
                rest_stripped = rest.lstrip()
                if rest_stripped and rest_stripped[0].isupper():
                    text = rest_stripped
                    logger.info(f"🔧 STT FIX: '{original}' → '{text}'")
                    break
    
    # Dodatkowe poprawki STT
    from polish_mappings import apply_stt_corrections
    text = apply_stt_corrections(text)
    
    if text != original:
        logger.info(f"🔧 STT normalized: '{original}' → '{text}'")
    
    return text


# ============================================================================
# HANDLE BOOKING MESSAGE V2 - Z obsługą clarification
# ============================================================================

async def handle_booking_message_v2(
    user_text: str,
    state: BookingState,
    tenant: Dict,
    caller_phone: str,
    client_gender: str
) -> Tuple[str, BookingState, bool]:
    """
    Główna logika FSM v2 - z obsługą clarification i pytań.
    """
    
    logger.info(f"📥 BOOKING MESSAGE V2: '{user_text}' | step: {state.current_step.value}")
    
    # 1. PARSE
    parsed = await parse_user_input(user_text, state.current_step)
    
    # 🔥 FIX 2: Obsługa pytań jako CLARIFICATION (nie wyjście z FSM!)
    if parsed.intent == Intent.QUESTION:
        return await handle_clarification(user_text, state, tenant, client_gender)
    
    # 2. Obsługa goodbye/cancel
    if parsed.intent == Intent.GOODBYE:
        # 🔥 FIX 3: Jeśli mamy wszystkie dane - zapytaj o potwierdzenie zamiast kończyć!
        if is_booking_complete(state):
            state.current_step = BookingStep.CONFIRM
            response = await generate_confirm_prompt(state, tenant, client_gender)
            return (response, state, False)
        return ("Do widzenia!", state, True)
    
    if parsed.intent == Intent.CANCEL:
        return ("Rozumiem, rezerwacja anulowana. Do widzenia!", BookingState(), True)
    
    # 3. VALIDATE & MERGE
    state = await validate_and_merge(parsed, state, tenant)
    
    # 4. FSM - następny krok
    next_step = get_next_step(state, parsed)
    
    # 🔥 FIX 4: W kroku CONFIRM - obsłuż specjalnie
    if state.current_step == BookingStep.CONFIRM:
        if parsed.intent == Intent.CONFIRM_YES:
            # Zapisz rezerwację
            from flows_booking_fsm import save_booking
            success, response, state = await save_booking(state, tenant, caller_phone)
            return (response, state, success)
        
        elif parsed.intent == Intent.CONFIRM_NO or parsed.intent == Intent.CHANGE:
            # Zmiana - cofnij
            from flows_booking_fsm import reset_state_for_step
            state = reset_state_for_step(state, next_step)
    
    # 5. Generuj odpowiedź
    state.current_step = next_step
    ctx = build_generator_context_v2(next_step, state, tenant, client_gender)
    response = await generate_response(ctx)
    
    # Wyczyść błędy po zakomunikowaniu
    state.errors = []
    
    return (response, state, False)


async def handle_clarification(
    question: str, 
    state: BookingState, 
    tenant: Dict,
    client_gender: str
) -> Tuple[str, BookingState, bool]:
    """
    🔥 FIX: Obsługuje pytania klienta BEZ wychodzenia z FSM.
    Np. "jaki to dzień?" → odpowiada i ZOSTAJE w tym samym kroku.
    """
    logger.info(f"❓ CLARIFICATION: '{question}' (staying in {state.current_step.value})")
    
    # Buduj kontekst dla odpowiedzi na pytanie
    from flows_helpers import format_date_polish, format_hour_polish
    
    # Odpowiedz na typowe pytania
    question_lower = question.lower()
    
    clarification_response = ""
    
    if "jaki" in question_lower and ("dzień" in question_lower or "data" in question_lower):
        if state.selected_date:
            date_text = format_date_polish(state.selected_date)
            clarification_response = f"To będzie {date_text}."
        else:
            clarification_response = "Jeszcze nie wybraliśmy daty."
    
    elif "która" in question_lower and "godzin" in question_lower:
        if state.selected_time:
            time_text = format_hour_polish(state.selected_time)
            clarification_response = f"Godzina {time_text}."
        else:
            clarification_response = "Jeszcze nie wybraliśmy godziny."
    
    elif "ile" in question_lower and ("koszt" in question_lower or "cen" in question_lower):
        if state.selected_service:
            price = state.selected_service.get("price", "nieznana")
            clarification_response = f"{state.selected_service['name']} kosztuje {price} zł."
        else:
            clarification_response = "Najpierw wybierzmy usługę, potem powiem o cenie."
    
    elif "kto" in question_lower or "pracownik" in question_lower:
        if state.selected_staff:
            clarification_response = f"Wybraliśmy {state.selected_staff['name']}."
        else:
            staff_list = tenant.get("staff", [])
            names = ", ".join(s["name"] for s in staff_list)
            clarification_response = f"Dostępni są: {names}."
    
    else:
        # Generyczna odpowiedź - naturalne przejście z powrotem
        clarification_response = "Rozumiem."
    
    # 🔥 KLUCZOWE: Po odpowiedzi na pytanie - powtórz pytanie z aktualnego kroku
    step_follow_up = get_step_follow_up(state, client_gender)
    
    full_response = f"{clarification_response} {step_follow_up}"
    
    logger.info(f"❓ CLARIFICATION response: '{full_response}'")
    
    # Nie zmieniamy kroku!
    return (full_response, state, False)


def get_step_follow_up(state: BookingState, client_gender: str) -> str:
    """Generuje follow-up pytanie dla aktualnego kroku."""
    
    pan_pani = "Pan" if client_gender == "male" else "Pani" if client_gender == "female" else "Pan/Pani"
    
    follow_ups = {
        BookingStep.SERVICE: f"Na jaką usługę chce się {pan_pani} umówić?",
        BookingStep.STAFF: f"Do kogo chce się {pan_pani} umówić?",
        BookingStep.DATE: f"Na jaki dzień?",
        BookingStep.TIME: f"Na którą godzinę?",
        BookingStep.NAME: f"Na jakie imię zapisać?",
        BookingStep.CONFIRM: f"Czy {pan_pani} potwierdza rezerwację?",
    }
    
    return follow_ups.get(state.current_step, "Czy mogę kontynuować?")


def is_booking_complete(state: BookingState) -> bool:
    """Sprawdza czy mamy wszystkie dane do rezerwacji."""
    return all([
        state.selected_service,
        state.selected_staff,
        state.selected_date,
        state.selected_time,
        state.customer_name,
    ])


async def generate_confirm_prompt(state: BookingState, tenant: Dict, client_gender: str) -> str:
    """Generuje prompt potwierdzenia."""
    from flows_helpers import format_date_polish, format_hour_polish
    
    # 🔥 FIX 3: Forma pytająca dla klienta (nie "czy potwierdzam")
    if client_gender == "male":
        pan_pani = "Pan"
        forma = "Panie"
    elif client_gender == "female":
        pan_pani = "Pani"
        forma = "Pani"
    else:
        pan_pani = "Pan/Pani"
        forma = "Panie/Pani"
    
    return (
        f"Podsumowuję: {state.selected_service['name']} "
        f"u {state.selected_staff['name']}, "
        f"{format_date_polish(state.selected_date)} "
        f"o {format_hour_polish(state.selected_time)}, "
        f"na {forma} {state.customer_name}. "
        f"Czy {pan_pani} potwierdza rezerwację?"
    )


def build_generator_context_v2(step: BookingStep, state: BookingState, tenant: Dict, client_gender: str):
    """Buduje kontekst dla generatora z uwzględnieniem płci."""
    
    from flows_booking_fsm import GeneratorContext
    from flows_helpers import format_hour_polish, staff_can_do_service
    
    services = tenant.get("services", [])
    staff_list = tenant.get("staff", [])
    
    pan_pani = "Pan" if client_gender == "male" else "Pani" if client_gender == "female" else "Pan/Pani"
    
    if step == BookingStep.SERVICE:
        return GeneratorContext(
            step=step,
            state=state,
            errors=state.errors,
            tenant=tenant,
            what_to_say=f"Zapytaj na jaką usługę {pan_pani} chce się umówić.",
            available_options=[s["name"] for s in services],
        )
    
    elif step == BookingStep.STAFF:
        if state.selected_service:
            available = [s for s in staff_list if staff_can_do_service(s, state.selected_service)]
        else:
            available = staff_list
        
        return GeneratorContext(
            step=step,
            state=state,
            errors=state.errors,
            tenant=tenant,
            what_to_say=f"Potwierdź usługę i zapytaj do kogo {pan_pani} chce się umówić.",
            available_options=[s["name"] for s in available],
        )
    
    elif step == BookingStep.DATE:
        return GeneratorContext(
            step=step,
            state=state,
            errors=state.errors,
            tenant=tenant,
            what_to_say=f"Potwierdź pracownika i zapytaj na jaki dzień.",
            available_options=[],
        )
    
    elif step == BookingStep.TIME:
        slots_text = [format_hour_polish(s) for s in state.available_slots[:6]]
        return GeneratorContext(
            step=step,
            state=state,
            errors=state.errors,
            tenant=tenant,
            what_to_say=f"Podaj wolne godziny i zapytaj którą {pan_pani} wybiera.",
            available_options=slots_text,
        )
    
    elif step == BookingStep.NAME:
        return GeneratorContext(
            step=step,
            state=state,
            errors=state.errors,
            tenant=tenant,
            what_to_say=f"Potwierdź termin i zapytaj na jakie imię zapisać.",
            available_options=[],
        )
    
    elif step == BookingStep.CONFIRM:
        return GeneratorContext(
            step=step,
            state=state,
            errors=state.errors,
            tenant=tenant,
            what_to_say=f"Powtórz CAŁE podsumowanie i poproś {pan_pani} o potwierdzenie. Użyj formy: 'Panie/Pani [imię]'.",
            available_options=[],
        )
    
    else:
        return GeneratorContext(
            step=step,
            state=state,
            errors=state.errors,
            tenant=tenant,
            what_to_say="Kontynuuj rozmowę.",
            available_options=[],
        )


# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    "start_booking_function",
    "create_booking_hard_node",
    "process_booking_input_function",
]