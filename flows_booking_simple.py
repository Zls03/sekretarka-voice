# flows_booking_simple.py - UPROSZCZONY system rezerwacji
# WERSJA 1.1 - POPRAWKI: walidacja slotów, proponowanie terminów
"""
ZMIANY W 1.1:
- Lepsze logowanie przy walidacji slotów
- Fresh fetch przed zapisem (bez cache)
- Preprocessing dat (usuwanie "na ")
- Funkcja proponowania najbliższych wolnych terminów
- Walidacja slotu PRZED i PO potwierdzeniu
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
    get_available_slots_from_api,  # Direct API call (no cache)
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
# POMOCNICZE - PROPONOWANIE TERMINÓW
# ============================================================================

async def get_next_available_days(
    tenant: Dict, 
    staff: Dict, 
    service: Dict, 
    max_days: int = 14,
    limit: int = 3
) -> List[Dict]:
    """
    Znajduje najbliższe dni z wolnymi terminami.
    
    Returns:
        Lista słowników: [{"date": datetime, "slots": ["10:00", "11:00", ...], "slots_count": 5}, ...]
    """
    results = []
    today = datetime.now()
    
    for day_offset in range(max_days):
        check_date = today + timedelta(days=day_offset)
        
        # Pomiń jeśli zamknięte
        weekday = check_date.weekday()
        if get_opening_hours(tenant, weekday) is None:
            continue
        
        # Pobierz sloty (używamy API, nie cache)
        try:
            slots = await get_available_slots_from_api(tenant, staff, service, check_date)
            
            if slots and len(slots) > 0:
                results.append({
                    "date": check_date,
                    "slots": slots,  # _slots_summary sama wybierze reprezentatywne
                    "slots_count": len(slots)
                })
                
                if len(results) >= limit:
                    break
                    
        except Exception as e:
            logger.warning(f"⚠️ Error checking date {check_date}: {e}")
            continue
    
    return results

def _slots_summary(slots: List[str]) -> str:
    """Podsumowanie slotów: max 2 przykłady (voice-friendly)"""
    if not slots:
        return "brak wolnych terminów"
    if len(slots) == 1:
        return format_hour_polish(slots[0])
    if len(slots) == 2:
        return f"{format_hour_polish(slots[0])} i {format_hour_polish(slots[1])}"
    
    # Weź 2: początek i środek (rozłożone w czasie)
    first = slots[0]
    mid = slots[len(slots) // 2]
    
    return f"{format_hour_polish(first)} i {format_hour_polish(mid)} i inne"

def format_availability_message(available_days: List[Dict]) -> str:
    """Formatuje wiadomość o dostępnych terminach - KRÓTKO (voice-friendly)"""
    if not available_days:
        return "Niestety, w najbliższych dniach nie ma wolnych terminów."
    
    # Tylko pierwszy dzień ze slotami
    first = available_days[0]
    date_str = format_date_polish(first["date"])
    slots_text = _slots_summary(first["slots"])
    
    if len(available_days) > 1:
        other_dates = natural_list([format_date_polish(d["date"]) for d in available_days[1:]])
        return f"Najbliższy wolny termin to {date_str}: {slots_text}. Wolne też {other_dates}. Który dzień?"
    else:
        return f"Najbliższy wolny termin to {date_str}: {slots_text}. Pasuje?"


# ============================================================================
# PREPROCESSING DAT
# ============================================================================

def preprocess_date_text(date_text: str) -> str:
    """
    Czyści tekst daty przed przekazaniem do dateparser.
    Usuwa polskie przyimki i modyfikatory czasowe.
    """
    if not date_text:
        return date_text
    
    text = date_text.lower().strip()
    
    # 🔥 NOWE: Usuń modyfikatory czasowe (PRZED usunięciem przyimków!)
    time_modifiers = [
        " po południu", " popołudniu", " popoludniu",
        " rano", " wieczorem", " przed południem",
        " po poludniu",  # bez polskich znaków
    ]
    for mod in time_modifiers:
        text = text.replace(mod, "")
    
    # Usuń przyimki z początku
    prefixes_to_remove = [
        "na ", "w dniu ", "dnia ", "w ", "we ", "za "
    ]
    
    for prefix in prefixes_to_remove:
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    
    # Mapowanie dni tygodnia na formy rozumiane przez dateparser
    day_mappings = {
        "poniedziałek": "poniedziałek",
        "wtorek": "wtorek", 
        "środę": "środa",
        "środe": "środa",
        "czwartek": "czwartek",
        "piątek": "piątek",
        "sobotę": "sobota",
        "sobote": "sobota",
        "niedzielę": "niedziela",
        "niedziele": "niedziela",
    }
    
    for wrong, correct in day_mappings.items():
        if text == wrong or text.startswith(wrong + " "):
            text = text.replace(wrong, correct, 1)
            break
    
    return text.strip()

# ============================================================================
# WALIDACJA SLOTÓW
# ============================================================================

async def validate_slot_available(
    tenant: Dict,
    staff: Dict, 
    service: Dict,
    date: datetime,
    time_str: str
) -> Tuple[bool, List[str]]:
    """
    Sprawdza czy konkretny slot jest dostępny.
    Pobiera ŚWIEŻE dane z API (bez cache).
    
    Returns:
        (is_available, current_slots)
    """
    logger.info(f"🔍 Validating slot: {date.strftime('%Y-%m-%d')} at {time_str}")
    
    # Pobierz świeże sloty z API (bypass cache)
    try:
        current_slots = await get_available_slots_from_api(tenant, staff, service, date)
    except Exception as e:
        logger.error(f"❌ API error during validation: {e}")
        # Fallback - użyj cached
        current_slots = await get_available_slots(tenant, staff, service, date)
    
    logger.info(f"📅 Fresh slots from API: {current_slots}")
    
    # Normalizuj do porównania
    time_normalized = _normalize_time(time_str)
    slots_normalized = [_normalize_time(s) for s in current_slots]
    
    logger.info(f"🔍 Comparing: '{time_normalized}' in {slots_normalized[:10]}...")
    
    is_available = time_normalized in slots_normalized
    
    if is_available:
        logger.info(f"✅ Slot {time_str} is AVAILABLE")
    else:
        logger.warning(f"❌ Slot {time_str} is NOT available! Available: {current_slots[:5]}")
    
    return (is_available, current_slots)


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
                "description": "Jeśli klient pyta o coś (cena, dostępność, godziny, kiedy wolne) - wpisz pytanie. Null jeśli kontynuuje rezerwację."
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
    question = args.get("question")
    
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
        state = {}
        flow_manager.state["booking"] = state
        return await _respond("Dobrze, zaczynamy od nowa. Na jaką usługę chce się Pan umówić?",
                             flow_manager, tenant)
    
    # === OBSŁUGA PYTANIA O DOSTĘPNOŚĆ ===
    if question:
        question_lower = question.lower()
        
        # Czy to pytanie o wolne terminy?
        availability_keywords = [
            "kiedy wolne", "wolny termin", "wolne terminy", "na jaki", "na jaki dzień",
            "kiedy można", "kiedy dostępn", "jaki termin", "najbliższy termin",
            "najszybciej", "jest wolny", "są wolne", "macie wolne"
        ]
        
        is_availability_question = any(kw in question_lower for kw in availability_keywords)
        
        if is_availability_question and "service" in state and "staff" in state:
            # Mamy usługę i pracownika - możemy sprawdzić dostępność
            logger.info(f"🔍 Checking availability for: {state['service']['name']} with {state['staff']['name']}")
            
            try:
                from flows import play_snippet
                await play_snippet(flow_manager, "checking")
            except:
                pass
            
            available_days = await get_next_available_days(
                tenant, state["staff"], state["service"], max_days=14, limit=3
            )
            
            if available_days:
                message = format_availability_message(available_days)
                return await _respond(message, flow_manager, tenant, state=state)
            else:
                return await _respond(
                    "Niestety, w najbliższych dwóch tygodniach nie ma wolnych terminów. "
                    "Czy chce Pan zostawić kontakt, a oddzwonimy gdy się coś zwolni?",
                    flow_manager, tenant, state=state)
        
        elif is_availability_question and "service" not in state:
            # Nie mamy usługi - zapytaj najpierw
            return await _respond(
                "Żeby sprawdzić dostępne terminy, muszę wiedzieć na jaką usługę. "
                f"Mamy: {natural_list([s['name'] for s in services[:4]])}. Która Pana interesuje?",
                flow_manager, tenant, state=state)
        
        else:
            # Inne pytanie - użyj GPT
            logger.info(f"❓ General question during booking: {question}")
            context = build_business_context(tenant)
            return await _answer_and_continue(question, context, _get_next_step(state, staff_list), 
                                             flow_manager, tenant, state)
    
    
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
        # 🔥 FIX: Ignoruj przyimki jako imię (STT czasem rozpoznaje "do" osobno)
        if staff_text.lower().strip() in ["do", "na", "u", "od", "dla", "z"]:
            available = [s for s in staff_list if staff_can_do_service(s, state.get("service", {}))]
            names = natural_list([s["name"] for s in available])
            return await _respond(
                f"Do kogo chce się Pan umówić? Dostępni są {names}.",
                flow_manager, tenant, state=state)
        
        if staff_text.lower() in ["dowolny", "obojętnie", "ktokolwiek", "wszystko jedno"]:
            available = [s for s in staff_list if staff_can_do_service(s, state["service"])]
            if available:
                state["staff"] = available[0]
                logger.info(f"✅ Staff (auto): {available[0]['name']}")
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
        
        if len(available) == 1:
            state["staff"] = available[0]
            logger.info(f"✅ Staff (auto-single): {available[0]['name']}")
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
        # 🔥 PREPROCESSING - usuń "na ", "w " itp.
        date_text_clean = preprocess_date_text(date_text)
        logger.info(f"📅 Date preprocessing: '{date_text}' → '{date_text_clean}'")
        
        parsed_date = dateparser.parse(
            date_text_clean, 
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
            
            # 🔥 WALIDACJA SLOTÓW - świeże dane!
            try:
                from flows import play_snippet
                await play_snippet(flow_manager, "checking")
            except:
                pass
            
            slots = await get_available_slots_from_api(
                tenant, state["staff"], state["service"], parsed_date
            )
            
            logger.info(f"📅 Slots for {parsed_date.strftime('%Y-%m-%d')}: {slots}")
            
            if not slots:
                # Zaproponuj inne dni
                available_days = await get_next_available_days(
                    tenant, state["staff"], state["service"], max_days=14, limit=3
                )
                
                staff_name = odmien_imie(state['staff']['name'])
                
                if available_days:
                    suggestion = format_availability_message(available_days)
                    return await _respond(
                        f"Na {format_date_polish(parsed_date)} u {staff_name} nie ma wolnych terminów. "
                        f"{suggestion}",
                        flow_manager, tenant, state=state)
                else:
                    return await _respond(
                        f"Na {format_date_polish(parsed_date)} u {staff_name} nie ma wolnych terminów "
                        f"i w najbliższych dniach też jest pełny grafik. Przepraszam.",
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
        
        # 🔥 PROPONUJ TERMINY od razu!
        available_days = await get_next_available_days(
            tenant, state["staff"], state["service"], max_days=14, limit=3
        )
        
        if available_days:
            first_day = available_days[0]
            first_date_str = format_date_polish(first_day["date"])
            first_slots = _slots_summary(first_day["slots"])
            
            if len(available_days) > 1:
                other_dates = natural_list([format_date_polish(d["date"]) for d in available_days[1:]])
                return await _respond(
                    f"U {staff_name} najbliższy termin to {first_date_str}: {first_slots}. "
                    f"Wolne też {other_dates}. Który dzień?",
                    flow_manager, tenant, state=state)
            else:
                return await _respond(
                    f"U {staff_name} najbliższy termin to {first_date_str}: {first_slots}. Pasuje?",
                    flow_manager, tenant, state=state)
        else:
            return await _respond(
                f"U {staff_name} w najbliższych dwóch tygodniach nie ma wolnych terminów. "
                f"Przepraszam.",
                flow_manager, tenant, state=state)
    
    # === 4. WALIDACJA GODZINY ===
    if time_text and "time" not in state:
        # 🔥 NOWE: Obsługa pory dnia ("po południu", "rano")
        time_lower = time_text.lower().strip()
        
        afternoon_phrases = ["po południu", "popołudniu", "popoludniu", "po poludniu", "popołudniow", "popoludniow"]
        morning_phrases = ["rano", "z rana", "przed południem", "przedpołudni", "dopołudni"]
        
        is_time_range = False
        filtered = []
        range_name = ""
        
        if any(p in time_lower for p in afternoon_phrases):
            filtered = [s for s in state.get("available_slots", []) if int(s.split(":")[0]) >= 12]
            is_time_range = True
            range_name = "po południu"
        elif any(p in time_lower for p in morning_phrases):
            filtered = [s for s in state.get("available_slots", []) if int(s.split(":")[0]) < 12]
            is_time_range = True
            range_name = "rano"
        
        if is_time_range:
            if "date" not in state:
                # Nie mamy jeszcze daty - nie możemy filtrować slotów
                return await _respond(
                    f"Rozumiem, szukamy terminu {range_name}. Na jaki dzień?",
                    flow_manager, tenant, state=state)
            
            if filtered:
                slots_text = _slots_summary(filtered)
                return await _respond(
                    f"Tak, {range_name} wolne są: {slots_text}. Którą godzinę wybrać?",
                    flow_manager, tenant, state=state)
            else:
                all_slots = natural_list([format_hour_polish(s) for s in state.get("available_slots", [])[:6]])
                return await _respond(
                    f"Niestety {range_name} nie ma wolnych terminów. "
                    f"Dostępne godziny to: {all_slots}.",
                    flow_manager, tenant, state=state)
        
        parsed_time = _parse_time(time_text)
        
        if parsed_time:
            # 🔥 WALIDACJA - sprawdź czy slot nadal wolny!
            is_available, current_slots = await validate_slot_available(
                tenant, state["staff"], state["service"], state["date"], parsed_time
            )
            
            if is_available:
                state["time"] = parsed_time
                state["available_slots"] = current_slots  # Odśwież listę
                logger.info(f"✅ Time: {parsed_time}")
            else:
                # Slot zajęty - pokaż aktualne wolne
                if current_slots:
                    slots_text = _slots_summary(current_slots)
                    return await _respond(
                        f"Niestety godzina {format_hour_polish(parsed_time)} jest już zajęta. "
                        f"Wolne są: {slots_text}.",
                        flow_manager, tenant, state=state)
                else:
                    # Cały dzień zajęty
                    state.pop("date", None)
                    available_days = await get_next_available_days(
                        tenant, state["staff"], state["service"], max_days=14, limit=3
                    )
                    if available_days:
                        suggestion = format_availability_message(available_days)
                        return await _respond(
                            f"Na ten dzień nie ma już wolnych terminów. {suggestion}",
                            flow_manager, tenant, state=state)
                    else:
                        return await _respond(
                            "Na ten dzień nie ma już wolnych terminów i w najbliższych dniach też jest pełny grafik.",
                            flow_manager, tenant, state=state)
        else:
            slots_text = natural_list([format_hour_polish(s) for s in state["available_slots"][:6]])
            return await _respond(
                f"Nie rozumiem godziny '{time_text}'. Wolne są: {slots_text}.",
                flow_manager, tenant, state=state)
    
    if "time" not in state:
        slots_text = _slots_summary(state["available_slots"])
        return await _respond(
            f"Na {format_date_polish(state['date'])} wolne są: {slots_text}. "
            f"Którą godzinę Pan wybiera?",
            flow_manager, tenant, state=state)
    
    # === 5. WALIDACJA IMIENIA ===
    if customer_name and "name" not in state:
        name = customer_name.strip()
        
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
            staff_name = odmien_imie(state['staff']['name'])
            customer_gender = detect_gender(state['name'])
            customer_name_declined = odmien_imie(state['name'])
            summary = (
                f"Podsumowuję: {state['service']['name']} u {staff_name}, "
                f"{format_date_polish(state['date'])} o {format_hour_polish(state['time'])}, "
                f"na {customer_gender} {customer_name_declined}. Czy mogę potwierdzić?"
            )
            return await _respond(summary, flow_manager, tenant, state=state)
    
    # === 7. ZAPIS REZERWACJI ===
    return await _save_booking(state, flow_manager, tenant, caller_phone)


# ============================================================================
# FUNKCJE POMOCNICZE
# ============================================================================

def _get_next_step(state: Dict, staff_list: List) -> str:
    """Określa następny krok w rezerwacji"""
    if "service" not in state:
        return "Na jaką usługę chce się Pan umówić?"
    elif "staff" not in state:
        available = [s for s in staff_list if staff_can_do_service(s, state.get("service", {}))]
        names = natural_list([s["name"] for s in available])
        return f"Do kogo chce się Pan umówić? Dostępni są {names}."
    elif "date" not in state:
        staff_name = odmien_imie(state['staff']['name'])
        return f"Na jaki dzień chce się Pan umówić do {staff_name}?"
    elif "time" not in state:
        slots_text = _slots_summary(state.get("available_slots", []))
        return f"Którą godzinę Pan wybiera? Wolne są: {slots_text}."
    elif "name" not in state:
        return "Na jakie imię zapisać wizytę?"
    else:
        return "Czy mogę potwierdzić rezerwację?"


def _parse_time(text: str) -> Optional[str]:
    """Parsuje godzinę z tekstu polskiego"""
    if not text:
        return None
    
    text = text.lower().strip()
    
    # 🔥 KOREKTY STT
    stt_time_fixes = {
        "siedem zer zero": "7:00",
        "siedem zero zero": "7:00", 
        "siedem zero": "7:00",
        "osiem zer zero": "8:00",
        "osiem zero zero": "8:00",
        "osiem zero": "8:00",
        "dziewięć zer zero": "9:00",
        "dziewięć zero": "9:00",
    }
    for wrong, correct in stt_time_fixes.items():
        if wrong in text:
            return correct
    
    # 🔥 NOWE: "wpół do X" = X-1:30
    if "wpół do" in text or "w pół do" in text:
        wpol_mappings = {
            "siódmej": "6:30", "siedmej": "6:30",
            "ósmej": "7:30", "osmej": "7:30",
            "dziewiątej": "8:30", "dziewiatej": "8:30",
            "dziesiątej": "9:30", "dziesiatej": "9:30",
            "jedenastej": "10:30",
            "dwunastej": "11:30",
            "trzynastej": "12:30",
            "czternastej": "13:30",
            "piętnastej": "14:30", "pietnastej": "14:30",
            "szesnastej": "15:30",
            "siedemnastej": "16:30",
            "osiemnastej": "17:30",
        }
        for word, time in wpol_mappings.items():
            if word in text:
                return time
    
    
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
    """Normalizuje czas do formatu H:MM dla porównań"""
    if isinstance(time_val, str):
        if ":" in time_val:
            parts = time_val.split(":")
            h = int(parts[0])
            m = parts[1].zfill(2)  # "0" → "00"
            return f"{h}:{m}"
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
- Na końcu NIE pytaj czy mogę w czymś pomóc"""},
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
    
    full_response = f"{answer} Wracając do rezerwacji - {next_step.lower()}"
    
    return await _respond(full_response, flow_manager, tenant, state=state)


async def _respond(
    text: str, 
    flow_manager: FlowManager, 
    tenant: Dict,
    state: Dict = None,
    done: bool = False
) -> Tuple:
    """Wysyła odpowiedź przez TTS i zwraca następny node"""
    
    if state is not None:
        flow_manager.state["booking"] = state
    
    from pipecat.frames.frames import TTSSpeakFrame
    await flow_manager.task.queue_frame(TTSSpeakFrame(text=text))
    
    logger.info(f"🎤 RESPONSE: {text[:80]}...")
    
    if done:
        from flows import create_anything_else_node
        return (None, create_anything_else_node(tenant))
    else:
        return (None, create_booking_node(tenant))


async def _save_booking(
    state: Dict,
    flow_manager: FlowManager,
    tenant: Dict,
    caller_phone: str
) -> Tuple:
    """Zapisuje rezerwację do API - z PODWÓJNĄ walidacją"""
    
    logger.info("💾 SAVING BOOKING...")
    
    try:
        from flows import play_snippet
        await play_snippet(flow_manager, "saving")
    except:
        pass
    
    try:
        # 🔥 KLUCZOWE: Jeszcze raz sprawdź czy slot jest wolny!
        is_available, current_slots = await validate_slot_available(
            tenant, state["staff"], state["service"], state["date"], state["time"]
        )
        
        if not is_available:
            logger.warning(f"❌ Slot was taken between confirmation and save!")
            
            if current_slots:
                state.pop("time", None)
                state["available_slots"] = current_slots
                slots_text = _slots_summary(current_slots)
                return await _respond(
                    f"Przepraszam, ta godzina właśnie została zajęta przez kogoś innego. "
                    f"Pozostały wolne: {slots_text}. Którą wybrać?",
                    flow_manager, tenant, state=state)
            else:
                state.pop("date", None)
                state.pop("time", None)
                return await _respond(
                    "Przepraszam, ten dzień właśnie się zapełnił. Proszę wybrać inny termin.",
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
            
            from pipecat.frames.frames import TTSSpeakFrame
            await flow_manager.task.queue_frame(TTSSpeakFrame(
                text=f"Gotowe! {state['service']['name']} u {staff_name}, "
                     f"{format_date_polish(state['date'])} o {format_hour_polish(state['time'])}."
                     f"{sms_info} Czy mogę jeszcze w czymś pomóc?"
            ))
            
            from flows import create_anything_else_node
            return (None, create_anything_else_node(tenant))
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
            "content": """ZAWSZE wywołuj book_appointment z tym co klient powiedział.

Przykłady:
- "na strzyżenie do Ani" → book_appointment(service="strzyżenie", staff="Ania")
- "jutro" → book_appointment(date_text="jutro")
- "na trzynastą" → book_appointment(time_text="na trzynastą")
- "po południu" → book_appointment(time_text="po południu")
- "rano" → book_appointment(time_text="rano")
- "przed południem" → book_appointment(time_text="rano")
- "coś popołudniowego" → book_appointment(time_text="po południu")
- "tak, potwierdzam" → book_appointment(confirmation="yes")
- "nie, dziękuję" → book_appointment(confirmation="no")
- "kiedy macie wolne?" → book_appointment(question="kiedy macie wolne?")
- "na jaki dzień jest wolne?" → book_appointment(question="na jaki dzień jest wolne?")"""
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
    
    # NOWE: Sprawdź czy mamy soft_interest z check_availability
    soft_interest = flow_manager.state.get("soft_interest")
    
    if soft_interest:
        # Użyj danych z poprzedniego sprawdzenia
        flow_manager.state["booking"] = {
            "service": soft_interest["service"],
            "staff": soft_interest["staff"],
        }
        del flow_manager.state["soft_interest"]
        
        from polish_mappings import odmien_imie
        staff_name = odmien_imie(soft_interest["staff"]["name"])
        
        from pipecat.frames.frames import TTSSpeakFrame
        await flow_manager.task.queue_frame(
            TTSSpeakFrame(text=f"Świetnie! Na jaki dzień chce się Pan zapisać do {staff_name}?")
        )
        
        return (None, create_booking_node(tenant))
    
    # Standardowy flow - brak soft_interest
    flow_manager.state["booking"] = {}
    flow_manager.state["booking_confirmed"] = False
    
    services = tenant.get("services", [])
    names = natural_list([s["name"] for s in services[:4]])
    
    from pipecat.frames.frames import TTSSpeakFrame
    await flow_manager.task.queue_frame(
        TTSSpeakFrame(text=f"Chętnie pomogę umówić wizytę. Na jaką usługę? Mamy {names}.")
    )
    
    return (None, create_booking_node(tenant))


# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    "start_booking_function_simple",
    "book_appointment_function",
    "create_booking_node",
]