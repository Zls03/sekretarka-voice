# flows_booking_simple.py - UPROSZCZONY system rezerwacji
# WERSJA 1.0 - Wzorzec: Tool Calling + dateparser + natychmiastowa walidacja
"""
ARCHITEKTURA (z wzorca):
1. GPT zbiera SUROWE dane (nie parsuje!)
2. KOD parsuje daty (dateparser - nie GPT!)
3. KOD waliduje NATYCHMIAST (sloty sprawdzane od razu)
4. KOD generuje odpowiedź (template, nie LLM)

RÓŻNICE OD STAREGO:
- dateparser zamiast własnego parsera dat
- Walidacja slotów NATYCHMIAST po dacie
- JEDNA funkcja zamiast 6 kroków FSM
- Zero LLM do generowania odpowiedzi
"""

import os
import json
import dateparser
from datetime import datetime, timedelta
from typing import Optional, Dict, Tuple, List
from loguru import logger
from pipecat_flows import FlowManager, FlowsFunctionSchema

from flows_helpers import (
    format_hour_polish, format_date_polish,
    get_available_slots, save_booking_to_api,
    fuzzy_match_service, fuzzy_match_staff, 
    staff_can_do_service, send_booking_sms,
    increment_sms_count, get_opening_hours,
    POLISH_DAYS, build_business_context,
)

# Import funkcji odmiany i formatowania
from polish_mappings import (
    odmien_imie, detect_gender, natural_list,
)


# ============================================================================
# KONFIGURACJA
# ============================================================================

# Dateparser settings dla polskiego
DATEPARSER_SETTINGS = {
    'PREFER_DATES_FROM': 'future',
    'PREFER_DAY_OF_MONTH': 'first',
    'RETURN_AS_TIMEZONE_AWARE': False,
}


# ============================================================================
# GŁÓWNA FUNKCJA - wywoływana przez GPT
# ============================================================================

def book_appointment_function(tenant: Dict) -> FlowsFunctionSchema:
    """
    JEDNA funkcja do rezerwacji.
    GPT MUSI wypełnić pola - kod robi resztę.
    """
    return FlowsFunctionSchema(
        name="book_appointment",
        description="""Umów wizytę. Zbierz dane od klienta i przekaż.
Wywołuj przy KAŻDEJ odpowiedzi klienta dotyczącej rezerwacji.
Przekazuj DOKŁADNIE co klient powiedział - nie interpretuj.""",
        properties={
            "service": {
                "type": "string",
                "description": "Nazwa usługi którą klient chce (np. 'strzyżenie', 'farbowanie') lub null jeśli nie podał"
            },
            "staff": {
                "type": "string",
                "description": "Imię pracownika (np. 'Ania', 'do Ani') lub 'dowolny' jeśli obojętnie, lub null"
            },
            "date_text": {
                "type": "string",
                "description": "Data DOKŁADNIE jak klient powiedział (np. 'jutro', 'w piątek', 'na 15 lutego') lub null"
            },
            "time_text": {
                "type": "string",
                "description": "Godzina DOKŁADNIE jak klient powiedział (np. 'na trzynastą', 'o 14:30') lub null"
            },
            "customer_name": {
                "type": "string",
                "description": "Imię klienta lub null"
            },
            "confirmation": {
                "type": "string",
                "enum": ["yes", "no", "change", "none"],
                "description": "Czy klient potwierdza: 'yes' (tak/dobrze), 'no' (nie/anuluj), 'change' (chce zmienić), 'none' (nie dotyczy)"
            },
            "question": {
                "type": "string",
                "description": "Jeśli klient pyta o coś (cena, adres, godziny, parking, dojazd itp.) ZAMIAST kontynuować rezerwację - wpisz pytanie. Null jeśli kontynuuje rezerwację."
            }
        },
        required=["confirmation"],
        handler=lambda args, fm: handle_book_appointment(args, fm, tenant),
    )


async def handle_book_appointment(args: Dict, flow_manager: FlowManager, tenant: Dict) -> Tuple:
    """
    GŁÓWNY HANDLER - przetwarza WSZYSTKO w jednej funkcji.
    
    Logika:
    1. Parsuj dane (dateparser dla dat)
    2. Waliduj NATYCHMIAST
    3. Jeśli brakuje czegoś - pytaj
    4. Jeśli wszystko OK - zapisz
    """
    
    # Pobierz dane z args
    service_text = args.get("service")
    staff_text = args.get("staff")
    date_text = args.get("date_text")
    time_text = args.get("time_text")
    customer_name = args.get("customer_name")
    confirmation = args.get("confirmation", "none")
    
    # Pobierz stan z flow_manager
    state = flow_manager.state.get("booking", {})
    caller_phone = flow_manager.state.get("caller_phone", "")
    
    logger.info(f"📥 BOOK_APPOINTMENT: service={service_text}, staff={staff_text}, "
                f"date={date_text}, time={time_text}, name={customer_name}, confirm={confirmation}")
    
    # Dane z tenanta
    services = tenant.get("services", [])
    staff_list = tenant.get("staff", [])
    
    # === OBSŁUGA ANULOWANIA ===
    if confirmation == "no":
        flow_manager.state["booking"] = {}
        return await _respond("Rozumiem, rezerwacja anulowana. Czy mogę w czymś jeszcze pomóc?", 
                             flow_manager, tenant, done=False)
    
    # === OBSŁUGA ZMIANY ===
    if confirmation == "change":
        # Reset odpowiednich pól
        state = {}
        flow_manager.state["booking"] = state
        return await _respond("Dobrze, zaczynamy od nowa. Na jaką usługę chce się Pan umówić?",
                             flow_manager, tenant)
    
    # === OBSŁUGA PYTANIA W TRAKCIE REZERWACJI ===
    question = args.get("question")
    if question:
        logger.info(f"❓ Question during booking: {question}")
        context = build_business_context(tenant)
        
        # Znajdź co dalej pytać (wróć do rezerwacji)
        if "service" not in state:
            next_step = "Na jaką usługę chce się Pan umówić?"
        elif "staff" not in state:
            available = [s for s in staff_list if staff_can_do_service(s, state["service"])]
            names = natural_list([s["name"] for s in available])
            next_step = f"Do kogo chce się Pan umówić? Dostępni są {names}."
        elif "date" not in state:
            staff_name = odmien_imie(state['staff']['name'])
            next_step = f"Na jaki dzień chce się Pan umówić do {staff_name}?"
        elif "time" not in state:
            slots_text = natural_list([format_hour_polish(s) for s in state.get("available_slots", [])[:5]])
            next_step = f"Którą godzinę Pan wybiera? Wolne są: {slots_text}."
        elif "name" not in state:
            next_step = "Na jakie imię zapisać wizytę?"
        else:
            next_step = "Czy mogę potwierdzić rezerwację?"
        
        # Odpowiedz na pytanie używając GPT (jednorazowo)
        return await _answer_and_continue(question, context, next_step, flow_manager, tenant, state)
    
    
    # === 1. WALIDACJA USŁUGI ===
    if service_text and "service" not in state:
        found = fuzzy_match_service(service_text, services)
        if found:
            state["service"] = found
            logger.info(f"✅ Service: {found['name']}")
        else:
            names = ", ".join(s["name"] for s in services)
            return await _respond(f"Nie mamy usługi '{service_text}'. Dostępne: {names}.",
                                 flow_manager, tenant, state=state)
    
    if "service" not in state:
        names = natural_list([s["name"] for s in services[:5]])
        return await _respond(f"Na jaką usługę chce się Pan umówić? Mamy {names}.",
                             flow_manager, tenant, state=state)
    
    # === 2. WALIDACJA PRACOWNIKA ===
    if staff_text and "staff" not in state:
        if staff_text.lower() in ["dowolny", "obojętnie", "ktokolwiek", "wszystko jedno"]:
            # Auto-wybór pierwszego dostępnego
            available = [s for s in staff_list if staff_can_do_service(s, state["service"])]
            if available:
                state["staff"] = available[0]
                logger.info(f"✅ Staff (auto): {available[0]['name']}")
                # 🔥 Poinformuj o wyborze i od razu pytaj o datę
                staff_name = odmien_imie(available[0]['name'])
                return await _respond(
                    f"Dobrze, zapiszę do {staff_name}. Na jaki dzień?",
                    flow_manager, tenant, state=state)
        else:
            found = fuzzy_match_staff(staff_text, staff_list)
            if found:
                if staff_can_do_service(found, state["service"]):
                    state["staff"] = found
                    logger.info(f"✅ Staff: {found['name']}")
                else:
                    available = [s for s in staff_list if staff_can_do_service(s, state["service"])]
                    names = ", ".join(s["name"] for s in available)
                    return await _respond(
                        f"{found['name']} nie wykonuje {state['service']['name']}. "
                        f"Tę usługę wykonują: {names}.",
                        flow_manager, tenant, state=state)
            else:
                names = ", ".join(s["name"] for s in staff_list)
                return await _respond(f"Nie mamy pracownika '{staff_text}'. Dostępni: {names}.",
                                     flow_manager, tenant, state=state)
    
    if "staff" not in state:
        available = [s for s in staff_list if staff_can_do_service(s, state["service"])]
        
        # 🔥 Auto-wybór gdy tylko 1 pracownik
        if len(available) == 1:
            state["staff"] = available[0]
            logger.info(f"✅ Staff (auto-single): {available[0]['name']}")
            # Nie pytaj o pracownika - przejdź od razu do daty
        elif len(available) == 0:
            return await _respond(
                f"Przepraszam, obecnie nie mamy dostępnych pracowników do {state['service']['name']}.",
                flow_manager, tenant, state=state)
        else:
            names = natural_list([s["name"] for s in available])
            return await _respond(
                f"Świetnie, {state['service']['name']}. Do kogo chce się Pan umówić? "
                f"Dostępni są {names}. Może być też dowolna osoba.",
                flow_manager, tenant, state=state)
    
    # === 3. WALIDACJA DATY (dateparser!) ===
    if date_text and "date" not in state:
        # 🔥 KLUCZOWE: dateparser zamiast własnego parsera!
        parsed_date = dateparser.parse(
            date_text, 
            languages=['pl'],
            settings=DATEPARSER_SETTINGS
        )
        
        if parsed_date:
            # Sprawdź czy nie przeszła
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            if parsed_date.date() < today.date():
                return await _respond(
                    f"Data {format_date_polish(parsed_date)} już minęła. Podaj przyszłą datę.",
                    flow_manager, tenant, state=state)
            
            # Sprawdź czy salon otwarty
            weekday = parsed_date.weekday()
            if get_opening_hours(tenant, weekday) is None:
                return await _respond(
                    f"W {POLISH_DAYS[weekday]} jesteśmy zamknięci. Proszę wybrać inny dzień.",
                    flow_manager, tenant, state=state)
            
            # 🔥 NATYCHMIASTOWA WALIDACJA SLOTÓW!
            # Feedback dla użytkownika
            try:
                from flows import play_snippet
                await play_snippet(flow_manager, "checking")
            except:
                pass
            
            slots = await get_available_slots(
                tenant, state["staff"], state["service"], parsed_date
            )
            
            if not slots:
                staff_name = odmien_imie(state['staff']['name'])
                return await _respond(
                    f"Na {format_date_polish(parsed_date)} u {staff_name} "
                    f"nie ma wolnych terminów. Proszę wybrać inny dzień.",
                    flow_manager, tenant, state=state)
            
            state["date"] = parsed_date
            state["available_slots"] = slots
            logger.info(f"✅ Date: {parsed_date.strftime('%Y-%m-%d')}, slots: {len(slots)}")
        else:
            return await _respond(
                f"Nie rozumiem daty '{date_text}'. "
                f"Proszę powiedzieć np. 'jutro', 'w piątek', '15 lutego'.",
                flow_manager, tenant, state=state)
    
    if "date" not in state:
        staff_name = odmien_imie(state['staff']['name'])
        return await _respond(
            f"Na jaki dzień chce się Pan umówić do {staff_name}?",
            flow_manager, tenant, state=state)
    
    # === 4. WALIDACJA GODZINY ===
    if time_text and "time" not in state:
        parsed_time = _parse_time(time_text)
        
        if parsed_time:
            # Sprawdź czy slot dostępny
            slots = state.get("available_slots", [])
            
            # Normalizuj do porównania
            time_normalized = _normalize_time(parsed_time)
            slot_found = None
            
            for slot in slots:
                if _normalize_time(slot) == time_normalized:
                    slot_found = slot
                    break
            
            if slot_found:
                state["time"] = slot_found
                logger.info(f"✅ Time: {slot_found}")
            else:
                slots_text = natural_list([format_hour_polish(s) for s in slots[:6]])
                return await _respond(
                    f"Godzina {format_hour_polish(parsed_time)} jest niedostępna. "
                    f"Wolne są: {slots_text}.",
                    flow_manager, tenant, state=state)
        else:
            slots_text = natural_list([format_hour_polish(s) for s in state["available_slots"][:6]])
            return await _respond(
                f"Nie rozumiem godziny '{time_text}'. Wolne są: {slots_text}.",
                flow_manager, tenant, state=state)
    
    if "time" not in state:
        slots_text = natural_list([format_hour_polish(s) for s in state["available_slots"][:6]])
        return await _respond(
            f"Na {format_date_polish(state['date'])} wolne są: {slots_text}. "
            f"Którą godzinę Pan wybiera?",
            flow_manager, tenant, state=state)
    
    # === 5. WALIDACJA IMIENIA ===
    if customer_name and "name" not in state:
        name = customer_name.strip()
        
        # Wyczyść
        for prefix in ["pan ", "pani ", "na "]:
            if name.lower().startswith(prefix):
                name = name[len(prefix):]
        
        if len(name) >= 2 and name.lower() not in ["tak", "nie", "halo", "proszę"]:
            state["name"] = name.title()
            logger.info(f"✅ Name: {state['name']}")
        else:
            return await _respond(
                "Nie dosłyszałam imienia. Na jakie imię zapisać wizytę?",
                flow_manager, tenant, state=state)
    
    if "name" not in state:
        return await _respond(
            f"Świetnie, {format_date_polish(state['date'])} o {format_hour_polish(state['time'])}. "
            f"Na jakie imię zapisać wizytę?",
            flow_manager, tenant, state=state)
    
    # === 6. POTWIERDZENIE ===
    if "confirmed" not in state:
        if confirmation == "yes":
            state["confirmed"] = True
        else:
            # Pokaż podsumowanie
            staff_name = odmien_imie(state['staff']['name'])
            customer_gender = detect_gender(state['name'])  # "Pana" lub "Pani"
            customer_name = odmien_imie(state['name'])
            summary = (
                f"Podsumowuję: {state['service']['name']} u {staff_name}, "
                f"{format_date_polish(state['date'])} o {format_hour_polish(state['time'])}, "
                f"na {customer_gender} {customer_name}. Czy mogę potwierdzić?"
            )
            return await _respond(summary, flow_manager, tenant, state=state)
    
    # === 7. ZAPIS REZERWACJI ===
    return await _save_booking(state, flow_manager, tenant, caller_phone)


# ============================================================================
# FUNKCJE POMOCNICZE
# ============================================================================

def _parse_time(text: str) -> Optional[str]:
    """Parsuje godzinę z tekstu polskiego"""
    if not text:
        return None
    
    text = text.lower().strip()
    
    # Słowne godziny
    word_to_hour = {
        "dziewiąt": 9, "dziesiąt": 10, "jedenast": 11, "dwunast": 12,
        "trzynast": 13, "czternast": 14, "piętnast": 15, "szesnast": 16,
        "siedemnast": 17, "osiemnast": 18, "dziewiętnast": 19, "dwudziest": 20,
        "ósm": 8, "siódm": 7,
    }
    
    for word, hour in word_to_hour.items():
        if word in text:
            return f"{hour}:00"
    
    # Numeryczne
    import re
    
    # "14:30", "14.30"
    match = re.search(r'(\d{1,2})[:\.](\d{2})', text)
    if match:
        return f"{int(match.group(1))}:{match.group(2)}"
    
    # "o 14", "na 15"
    match = re.search(r'(?:o|na|godzin[aeę]?)\s*(\d{1,2})', text)
    if match:
        return f"{int(match.group(1))}:00"
    
    # Sama liczba
    match = re.search(r'\b(\d{1,2})\b', text)
    if match:
        hour = int(match.group(1))
        if 7 <= hour <= 21:
            return f"{hour}:00"
    
    return None


def _normalize_time(time_val) -> str:
    """Normalizuje czas do formatu H:MM"""
    if isinstance(time_val, str):
        if ":" in time_val:
            parts = time_val.split(":")
            return f"{int(parts[0])}:{parts[1]}"
        return f"{int(time_val)}:00"
    elif isinstance(time_val, int):
        return f"{time_val}:00"
    return str(time_val)
async def _answer_and_continue(
    question: str,
    context: str,
    next_step: str,
    flow_manager: FlowManager,
    tenant: Dict,
    state: Dict
) -> Tuple:
    """Odpowiada na pytanie klienta i wraca do rezerwacji"""
    import openai
    
    try:
        client = openai.OpenAI()
        
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": f"""Odpowiedz KRÓTKO (1-2 zdania) na pytanie klienta.

INFORMACJE O FIRMIE:
{context}

ZASADY:
- Odpowiedz TYLKO na pytanie
- Użyj DOKŁADNYCH danych z powyższych informacji
- NIE WYMYŚLAJ informacji których nie masz
- Mów w rodzaju żeńskim (jestem asystentką)
- Używaj formy "Pan/Pani"
- Na końcu NIE pytaj czy mogę w czymś pomóc (wrócisz do rezerwacji)"""},
                {"role": "user", "content": question}
            ],
            max_tokens=150,
            temperature=0.3
        )
        
        answer = response.choices[0].message.content.strip()
        logger.info(f"💬 Answer: {answer}")
        
    except Exception as e:
        logger.error(f"❌ GPT error: {e}")
        answer = "Przepraszam, nie mam tej informacji"
    
    # Połącz odpowiedź z powrotem do rezerwacji
    full_response = f"{answer} Wracając do rezerwacji - {next_step.lower()}"
    
    # Zapisz stan i odpowiedz
    flow_manager.state["booking"] = state
    
    from pipecat.frames.frames import TTSSpeakFrame
    await flow_manager.task.queue_frame(TTSSpeakFrame(text=full_response))
    
    logger.info(f"🎤 RESPONSE: {full_response[:80]}...")
    
    return (None, create_booking_node(tenant))

async def _respond(
    text: str, 
    flow_manager: FlowManager, 
    tenant: Dict,
    state: Dict = None,
    done: bool = False
) -> Tuple:
    """Wysyła odpowiedź przez TTS i zwraca następny node"""
    
    # Zapisz stan
    if state is not None:
        flow_manager.state["booking"] = state
    
    # Wyślij TTS
    from pipecat.frames.frames import TTSSpeakFrame
    await flow_manager.task.queue_frame(TTSSpeakFrame(text=text))
    
    logger.info(f"🎤 RESPONSE: {text[:80]}...")
    
    if done:
        from flows import create_anything_else_node
        return (None, create_anything_else_node(tenant))
    else:
        return (None, create_booking_node(tenant))
async def _answer_and_continue(
    question: str,
    context: str,
    next_step: str,
    flow_manager: FlowManager,
    tenant: Dict,
    state: Dict
) -> Tuple:
    """Odpowiada na pytanie w trakcie rezerwacji i wraca do procesu"""
    import openai
    
    try:
        client = openai.OpenAI()
        
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": f"""Odpowiedz KRÓTKO (1-2 zdania) na pytanie klienta.

INFORMACJE O FIRMIE:
{context}

ZASADY:
- Odpowiedz TYLKO na pytanie
- Mów krótko i konkretnie
- Używaj formy "Pan/Pani"
- NIE wymyślaj informacji których nie masz"""
                },
                {"role": "user", "content": question}
            ],
            max_tokens=150,
            temperature=0.3
        )
        
        answer = response.choices[0].message.content.strip()
        logger.info(f"💬 Answer: {answer[:50]}...")
        
    except Exception as e:
        logger.error(f"❌ GPT error: {e}")
        answer = "Przepraszam, nie mam tej informacji"
    
    # Połącz odpowiedź z powrotem do rezerwacji
    full_response = f"{answer} Wracając do rezerwacji - {next_step.lower()}"
    
    return await _respond(full_response, flow_manager, tenant, state=state)

async def _save_booking(
    state: Dict,
    flow_manager: FlowManager,
    tenant: Dict,
    caller_phone: str
) -> Tuple:
    """Zapisuje rezerwację do API"""
    
    logger.info("💾 SAVING BOOKING...")
    
    # Feedback dla użytkownika
    try:
        from flows import play_snippet
        await play_snippet(flow_manager, "saving")
    except:
        pass
    
    try:
        # Double-check slotów
        current_slots = await get_available_slots(
            tenant, state["staff"], state["service"], state["date"]
        )
        
        time_normalized = _normalize_time(state["time"])
        slots_normalized = [_normalize_time(s) for s in current_slots]
        
        if time_normalized not in slots_normalized:
            # Zajęte!
            if current_slots:
                state.pop("time", None)
                state["available_slots"] = current_slots
                slots_text = natural_list([format_hour_polish(s) for s in current_slots[:5]])
                return await _respond(
                    f"Ta godzina właśnie została zajęta. Wolne są: {slots_text}.",
                    flow_manager, tenant, state=state)
            else:
                state.pop("date", None)
                state.pop("time", None)
                return await _respond(
                    "Na ten dzień nie ma już wolnych terminów. Proszę wybrać inny dzień.",
                    flow_manager, tenant, state=state)
        
        # Zapisz
        result = await save_booking_to_api(
            tenant, state["staff"], state["service"],
            state["date"], state["time"],
            state["name"], caller_phone
        )
        
        if result:
            booking_code = result.get("booking_code", "")
            
            # SMS
            if booking_code and caller_phone:
                try:
                    sms_sent = await send_booking_sms(
                        tenant=tenant,
                        customer_phone=caller_phone,
                        service_name=state["service"]["name"],
                        staff_name=state["staff"]["name"],
                        date_str=state["date"].strftime("%d.%m"),
                        time_str=state["time"],
                        booking_code=booking_code
                    )
                    if sms_sent:
                        await increment_sms_count(tenant.get("id"))
                except Exception as e:
                    logger.error(f"📱 SMS error: {e}")
            
            # Sukces!
            flow_manager.state["booking"] = {}
            flow_manager.state["booking_confirmed"] = True
            
            sms_info = " Wysłałam SMS z potwierdzeniem." if booking_code else ""
            staff_name = odmien_imie(state['staff']['name'])
            
            return await _respond(
                f"Gotowe! {state['service']['name']} u {staff_name}, "
                f"{format_date_polish(state['date'])} o {format_hour_polish(state['time'])}."
                f"{sms_info} Do zobaczenia!",
                flow_manager, tenant, done=True)
        else:
            return await _respond(
                "Wystąpił problem z zapisem. Czy przekazać wiadomość do właściciela?",
                flow_manager, tenant, state=state)
            
    except Exception as e:
        logger.error(f"💾 SAVE error: {e}")
        return await _respond(
            "Wystąpił błąd. Czy przekazać wiadomość do właściciela?",
            flow_manager, tenant, state=state)


# ============================================================================
# NODE CREATOR
# ============================================================================

def create_booking_node(tenant: Dict) -> Dict:
    """Tworzy node dla rezerwacji"""
    
    services = tenant.get("services", [])
    staff_list = tenant.get("staff", [])
    
    services_text = ", ".join(s["name"] for s in services[:5])
    staff_text = ", ".join(s["name"] for s in staff_list)
    
    # Aktualna data
    now = datetime.now()
    today_info = f"DZIŚ: {now.strftime('%d.%m.%Y')} ({POLISH_DAYS[now.weekday()]})"
    
    return {
        "name": "booking_simple",
        "respond_immediately": False,
        
        "role_messages": [{
            "role": "system",
            "content": f"""Jesteś asystentką rezerwacji. {today_info}

USŁUGI: {services_text}
PRACOWNICY: {staff_text}

ZASADY:
- Przy KAŻDEJ odpowiedzi klienta wywołaj book_appointment
- Przekazuj DOKŁADNIE co klient powiedział (nie interpretuj!)
- Dla dat: przekaż słownie ("jutro", "w piątek")
- Dla godzin: przekaż słownie ("na trzynastą", "14:30")
- Używaj formy "Pan/Pani"
- Mów krótko"""
        }],
        
        "task_messages": [{
            "role": "system",
            "content": """ZAWSZE wywołaj book_appointment z tym co klient powiedział.

Przykłady:
- "na strzyżenie do Ani" → book_appointment(service="strzyżenie", staff="Ania")
- "jutro" → book_appointment(date_text="jutro")
- "na trzynastą" → book_appointment(time_text="na trzynastą")
- "tak, potwierdzam" → book_appointment(confirmation="yes")
- "nie, dziękuję" → book_appointment(confirmation="no")"""
        }],
        
        "functions": [
            book_appointment_function(tenant),
        ]
    }


def start_booking_function_simple() -> FlowsFunctionSchema:
    """Funkcja startowa - kompatybilna z obecnym systemem"""
    return FlowsFunctionSchema(
        name="start_booking",
        description="Klient chce umówić wizytę",
        properties={},
        required=[],
        handler=handle_start_booking_simple,
    )


async def handle_start_booking_simple(args: Dict, flow_manager: FlowManager):
    """Handler startowy"""
    tenant = flow_manager.state.get("tenant", {})
    
    logger.info("📅 BOOKING START (simple)")
    
    # Reset stanu
    flow_manager.state["booking"] = {}
    flow_manager.state["booking_confirmed"] = False
    
    # 🔥 OD RAZU powiedz coś - nie czekaj!
    services = tenant.get("services", [])
    names = natural_list([s["name"] for s in services[:4]])
    
    from pipecat.frames.frames import TTSSpeakFrame
    await flow_manager.task.queue_frame(
        TTSSpeakFrame(text=f"Chętnie pomogę umówić wizytę. Na jaką usługę? Mamy {names}.")
    )
    
    return (None, create_booking_node(tenant))


def _extract_initial_message(flow_manager: FlowManager) -> Optional[str]:
    """Wyciąga pierwszą wiadomość klienta"""
    try:
        context = flow_manager.get_current_context()
        
        for msg in reversed(context):
            if msg.get("role") == "user":
                content = msg.get("content", "").strip()
                if len(content) > 5:
                    return content
        return None
    except:
        return None


# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    "start_booking_function_simple",
    "book_appointment_function",
    "create_booking_node",
]