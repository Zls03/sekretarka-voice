# realtime_booking.py — Faza 3 planu migracji (CLAUDE.md): rezerwacje jako function-calling
# tool, współdzielony między OpenAI Realtime I Gemini Live (ten sam wzorzec co
# realtime_tools.py/realtime_prompt.py — jeden plik, oba providery).
"""
Port z flows_booking_simple.py (cascade, pipecat_flows) — CAŁA logika walidacji kroków
(usługa → pracownik → data → godzina → imię → uwagi → potwierdzenie → zapis) przeniesiona
~1:1. Zmienione tylko I/O: flow_manager.state["booking"] → call_state["booking"] (ten sam
call_state/gemini_state dict co reszta realtime_tools.py), TTSSpeakFrame+node → zwrot przez
FunctionCallParams.result_callback.

ARCHITEKTURA — JEDNO stanowe narzędzie, NIE dwa. CLAUDE.md nazywa fazę 3
"sprawdz_dostepnosc()/zarezerwuj()" (dwa bezstanowe tools) — świadomie NIE tak zrobione:
ten serwis (bot_gemini_test.py) nie ma FlowManager/przełączania node'ów jak cascade,
wszystkie tools są zawsze widoczne naraz. Gdyby dyscyplinę kolejności kroków (usługa
PRZED datą, data PRZED godziną, itd.) zostawić dwóm luźnym tools + samemu promptowi, to
dokładnie to czego cascade świadomie unika (patrz komentarz przy "confirmation" niżej:
"Handler nie zależy od LLM że wpisze zgodę"). Zamiast tego: JEDNO FunctionSchema
(book_appointment) wywoływane co turę, cała dyscyplina w Pythonie.

MODEL NIE IMPROWIZUJE PRZY DATACH/CENACH/GODZINACH. Każdy wynik niesie pole "say_exactly"
— dokładny, z góry obliczony polski tekst. Opis narzędzia (description) wymusza żeby model
powtórzył go SŁOWO W SŁOWO, bez własnych dodatków. To zastępuje _respond() z cascade
(które wypychało TTSSpeakFrame bezpośrednio, z pominięciem generowania przez LLM).
say_now/gemini_say_now (bot_gemini_test.py) NIE nadają się tutaj — to mechanizm do
jednorazowego zagajenia POZA aktywnym kontekstem rozmowy (LLMMessagesAppendFrame z
run_llm=True na nowo budowanym turn), nie do powtarzanego użycia w środku wieloturowej
rozmowy z już podłączonymi context aggregatorami.

PODWÓJNA WALIDACJA SLOTU zostaje (świeży fetch z get_available_slots_from_api tuż przed
zapisem, dokładnie jak _save_booking() w cascade) — PLUS nowa warstwa: POST
/api/panel/{slug}/bookings może teraz zwrócić 409 {"error": "slot_taken"} (unique index
na bookings(staff_id, booking_date, booking_time) dodany w bizvoice-panel w tej samej
sesji) gdy dwie równoległe rozmowy trafią w dokładnie ten sam termin między walidacją a
zapisem — obsłużone identycznie jak nieudana re-walidacja: klientowi proponowany jest
najbliższy inny wolny termin, nie generyczny błąd.

CO ŚWIADOMIE NIE ZOSTAŁO PRZENIESIONE (i dlaczego):
- start_booking_function_simple/handle_start_booking_simple — osobna funkcja startowa
  cascade do pre-wypełniania stanu z pierwszego zdania klienta. W TEJ architekturze
  WSZYSTKIE pola book_appointment są dostępne od razu przy pierwszym wywołaniu (nie ma
  osobnego "wejścia" do trybu rezerwacji) — pre-fill "z pierwszego zdania" to dokładnie
  ten sam kod co pre-fill przy KAŻDYM innym wywołaniu (już obsłużone niżej: sekcje 1-4
  akceptują dowolne pola niezależnie od tego, które to wywołanie z kolei).
- Flaga "_jak_ostatnio" (scripted propozycja "jak ostatnio" dla powracającego klienta,
  inicjowana w handle_start_booking_simple na podstawie client_profile) — tutaj
  client_profile/CRM (last_service/last_staff) już trafia do system promptu (patrz
  realtime_prompt.py::_build_crm_hint) i model MOŻE naturalnie zaproponować "jak
  ostatnio" własnymi słowami, PRZED wywołaniem book_appointment. Gdy klient się zgodzi,
  model wywoła book_appointment z service/staff już wypełnionymi — ten kod obsłuży to
  identycznie jak każde inne pre-wypełnione wywołanie. Nie wymaga osobnego mechanizmu
  (i nie da się bezpiecznie odtworzyć bez sygnału z handle_start_booking_simple, którego
  tu nie ma).
- "soft_interest" (przekazywanie stanu z osobnej funkcji check_availability z cascade) —
  ta funkcja nie istnieje w architekturze Realtime/Gemini Live (poza zakresem tego
  zadania), więc nie ma skąd tego przekazać.
- play_snippet("checking"/"saving") — dźwiękowe wypełniacze cascade podczas wolniejszych
  wywołań API. Brak odpowiednika w tym serwisie (contact_owner/submit_lead też tego nie
  mają) — pominięte, nie jest to poprawnościowe, tylko kosmetyczne.
- fuzzy_match_service/fuzzy_match_staff — używane w cascade WYŁĄCZNIE w pominiętej wyżej
  funkcji startowej. W handle_book_appointment (tej faktycznie portowanej logice) dopasowanie
  usługi/pracownika jest ZAWSZE dokładne (exact match), bo pole "service"/"staff" ma
  "enum" z listą prawdziwych nazw — API function-calling samo wymusza że model może
  zwrócić TYLKO wartość z listy, więc fuzzy matching po naszej stronie jest zbędny.
"""

import re
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, Tuple, List

import dateparser
import httpx
from loguru import logger

from pipecat.services.llm_service import FunctionCallParams
from pipecat.adapters.schemas.function_schema import FunctionSchema

from flows_helpers import (
    format_hour_polish, format_date_polish,
    get_available_slots, get_available_slots_from_api,
    staff_can_do_service, send_booking_sms, increment_sms_count,
    get_opening_hours, get_staff_working_hours,
    POLISH_DAYS, build_business_context,
    validate_max_days_ahead, validate_min_advance_hours,
    _assistant_gender, PANEL_API_URL, ADMIN_PANEL_API_URL, PANEL_SLUG,
)
from polish_mappings import odmien_imie, detect_gender, natural_list
from helpers import save_client_visit

DATEPARSER_SETTINGS = {
    'PREFER_DATES_FROM': 'future',
    'PREFER_DAY_OF_MONTH': 'first',
    'RETURN_AS_TIMEZONE_AWARE': False,
}


# ============================================================================
# POMOCNICZE — PROPONOWANIE TERMINÓW (1:1 z flows_booking_simple.py)
# ============================================================================

async def get_next_available_days(
    tenant: Dict, staff: Dict, service: Dict, max_days: int = 14, limit: int = 3
) -> List[Dict]:
    """Znajduje najbliższe dni z wolnymi terminami.

    Returns: [{"date": datetime, "slots": ["10:00", ...], "slots_count": N}, ...]
    """
    results = []
    today = datetime.now()

    for day_offset in range(max_days):
        check_date = today + timedelta(days=day_offset)
        try:
            slots = await get_available_slots_from_api(tenant, staff, service, check_date)
            if slots:
                results.append({"date": check_date, "slots": slots, "slots_count": len(slots)})
                if len(results) >= limit:
                    break
        except Exception as e:
            logger.warning(f"⚠️ [BOOKING] Error checking date {check_date}: {e}")
            continue

    return results


def _slots_summary(slots: List[str]) -> str:
    """Podsumowanie slotów: max 2 przykłady (voice-friendly)"""
    if not slots:
        return "brak wolnych terminów"
    if len(slots) == 1:
        return format_hour_polish(slots[0])
    if len(slots) == 2:
        return f"{format_hour_polish(slots[0])} lub {format_hour_polish(slots[1])}"
    first = slots[0]
    mid = slots[len(slots) // 2]
    return f"{format_hour_polish(first)}, {format_hour_polish(mid)} i inne"


def format_availability_message(available_days: List[Dict]) -> str:
    """Formatuje wiadomość o dostępnych terminach — KRÓTKO (voice-friendly)"""
    if not available_days:
        return "Niestety, w najbliższych dniach nie ma wolnych terminów."
    first = available_days[0]
    date_str = format_date_polish(first["date"])
    first_slot = format_hour_polish(first["slots"][0])
    return f"Najbliższy wolny termin to {date_str} o {first_slot}. Zapisać, czy wolisz inny termin?"


# ============================================================================
# PREPROCESSING DAT (1:1 z flows_booking_simple.py)
# ============================================================================

def preprocess_date_text(date_text: str) -> str:
    """Czyści tekst daty przed przekazaniem do dateparser — usuwa polskie przyimki i
    modyfikatory czasowe."""
    if not date_text:
        return date_text

    text = date_text.lower().strip()

    time_modifiers = [
        " po południu", " popołudniu", " popoludniu",
        " rano", " wieczorem", " przed południem",
        " po poludniu",
    ]
    for mod in time_modifiers:
        text = text.replace(mod, "")

    prefixes_to_remove = ["na ", "w dniu ", "dnia ", "w ", "we "]
    for prefix in prefixes_to_remove:
        if text.startswith(prefix):
            text = text[len(prefix):]
            break

    day_mappings = {
        "poniedziałek": "poniedziałek", "wtorek": "wtorek",
        "środę": "środa", "środe": "środa",
        "czwartek": "czwartek", "piątek": "piątek",
        "sobotę": "sobota", "sobote": "sobota",
        "niedzielę": "niedziela", "niedziele": "niedziela",
    }
    for wrong, correct in day_mappings.items():
        if text == wrong or text.startswith(wrong + " "):
            text = text.replace(wrong, correct, 1)
            break

    return text.strip()


# ============================================================================
# WALIDACJA SLOTÓW (1:1 z flows_booking_simple.py)
# ============================================================================

async def validate_slot_available(
    tenant: Dict, staff: Dict, service: Dict, date: datetime, time_str: str
) -> Tuple[bool, List[str]]:
    """Sprawdza czy konkretny slot jest dostępny. Pobiera ŚWIEŻE dane z API (bez cache)."""
    logger.info(f"🔍 [BOOKING] Validating slot: {date.strftime('%Y-%m-%d')} at {time_str}")

    try:
        current_slots = await get_available_slots_from_api(tenant, staff, service, date)
    except Exception as e:
        logger.error(f"❌ [BOOKING] API error during validation: {e}")
        current_slots = await get_available_slots(tenant, staff, service, date)

    time_normalized = _normalize_time(time_str)
    slots_normalized = [_normalize_time(s) for s in current_slots]
    is_available = time_normalized in slots_normalized

    if is_available:
        logger.info(f"✅ [BOOKING] Slot {time_str} is AVAILABLE")
    else:
        logger.warning(f"❌ [BOOKING] Slot {time_str} is NOT available! Available: {current_slots[:5]}")

    return (is_available, current_slots)


# ============================================================================
# PARSOWANIE CZASU (1:1 z flows_booking_simple.py — czyste funkcje tekstowe)
# ============================================================================

def _parse_time(text: str) -> Optional[str]:
    """Parsuje godzinę z tekstu polskiego"""
    if not text:
        return None

    text = text.lower().strip()

    stt_time_fixes = {
        "siedem zer zero": "7:00", "siedem zero zero": "7:00", "siedem zero": "7:00",
        "osiem zer zero": "8:00", "osiem zero zero": "8:00", "osiem zero": "8:00",
        "dziewięć zer zero": "9:00", "dziewięć zero": "9:00",
    }
    for wrong, correct in stt_time_fixes.items():
        if wrong in text:
            return correct

    if "wpół do" in text or "w pół do" in text:
        wpol_mappings = {
            "siódmej": "6:30", "siedmej": "6:30",
            "ósmej": "7:30", "osmej": "7:30",
            "dziewiątej": "8:30", "dziewiatej": "8:30",
            "dziesiątej": "9:30", "dziesiatej": "9:30",
            "jedenastej": "10:30", "dwunastej": "11:30",
            "trzynastej": "12:30", "czternastej": "13:30",
            "piętnastej": "14:30", "pietnastej": "14:30",
            "szesnastej": "15:30", "siedemnastej": "16:30",
            "osiemnastej": "17:30",
        }
        for word, time in wpol_mappings.items():
            if word in text:
                return time

    has_thirty = any(x in text for x in ["trzydzieści", "trzydziesci", "30", ":30"])
    word_to_hour = {
        "dziewiąt": 9, "dziesiąt": 10, "jedenast": 11, "dwunast": 12,
        "trzynast": 13, "czternast": 14, "piętnast": 15, "szesnast": 16,
        "siedemnast": 17, "osiemnast": 18, "dziewiętnast": 19, "dwudziest": 20,
        "ósm": 8, "siódm": 7,
    }
    for word, hour in word_to_hour.items():
        if word in text:
            minutes = "30" if has_thirty else "00"
            return f"{hour}:{minutes}"

    match = re.search(r'(\d{1,2})[:\.](\d{2})', text)
    if match:
        return f"{int(match.group(1))}:{match.group(2)}"

    match = re.search(r'(?:o|na|godzin[aeę]?)\s*(\d{1,2})', text)
    if match:
        return f"{int(match.group(1))}:00"

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
            m = parts[1].zfill(2)
            return f"{h}:{m}"
        return f"{int(time_val)}:00"
    elif isinstance(time_val, int):
        return f"{time_val}:00"
    return str(time_val)


def _get_next_step(state: Dict, staff_list: List) -> str:
    """Określa następny krok w rezerwacji — używane w komunikacie po 'change'."""
    if "service" not in state:
        return "Na jaką usługę?"
    elif "staff" not in state:
        available = [s for s in staff_list if staff_can_do_service(s, state.get("service", {}))]
        names = natural_list([s["name"] for s in available])
        return f"Do kogo? Dostępni: {names}."
    elif "date" not in state:
        staff_name = odmien_imie(state["staff"]["name"])
        return f"Na jaki dzień do {staff_name}?"
    elif "time" not in state:
        slots_text = _slots_summary(state.get("available_slots", []))
        return f"Którą godzinę? Wolne są: {slots_text}."
    elif "name" not in state:
        return "Na jakie imię zapisać wizytę?"
    else:
        return "Czy mogę potwierdzić rezerwację?"


# ============================================================================
# ZAPIS DO API — własny wariant (nie modyfikujemy flows_helpers.py, cascade ma
# działać bez zmian) z rozróżnieniem 409 "slot_taken" od innych błędów.
# ============================================================================

async def _save_booking_via_api(
    tenant: Dict, staff: Dict, service: Dict,
    date: datetime, time_str: str, customer_name: str, customer_phone: str, notes: str = "",
) -> Tuple[str, Dict]:
    """POST /api/panel/{slug}/bookings. 409 (nowy unique index w bizvoice-panel na
    staff_id+booking_date+booking_time, dodany w tej samej sesji) NIE jest retry'owany —
    slot jest definitywnie zajęty, ponawianie nic nie da. Inne błędy retry'owane ×3 z
    0.5s odstępem, tak jak flows_helpers.save_booking_to_api.

    Returns: (outcome, data) — outcome to "ok" | "slot_taken" | "error"."""
    slug = tenant.get("slug") or PANEL_SLUG
    if not slug:
        logger.warning("⚠️ [BOOKING] Brak panel slug — nie mogę zapisać")
        return ("error", {})

    date_str = date.strftime("%Y-%m-%d")
    base_url = ADMIN_PANEL_API_URL if tenant.get("source") == "admin" else PANEL_API_URL
    payload = {
        "staff_id": staff.get("id"),
        "service_id": service.get("id"),
        "date": date_str,
        "time": time_str,
        "client_name": customer_name,
        "client_phone": customer_phone,
    }
    if notes:
        payload["notes"] = notes

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(f"{base_url}/api/panel/{slug}/bookings", json=payload)
                if response.status_code in (200, 201):
                    data = response.json()
                    data["booking_code"] = data.get("visitCode") or data.get("booking_code") or ""
                    logger.info(f"✅ [BOOKING] Zapisano: {data.get('bookingId')} (kod {data['booking_code']})")
                    return ("ok", data)
                if response.status_code == 409:
                    logger.warning(f"⚠️ [BOOKING] 409 slot_taken: {date_str} {time_str}")
                    return ("slot_taken", {})
                logger.warning(f"⚠️ [BOOKING] API error {response.status_code} (próba {attempt + 1}/3)")
        except Exception as e:
            logger.error(f"❌ [BOOKING] API exception (próba {attempt + 1}/3): {e}")
        if attempt < 2:
            await asyncio.sleep(0.5)

    logger.error("❌ [BOOKING] Zapis nie powiódł się po 3 próbach")
    return ("error", {})


# ============================================================================
# WYNIK — kontrakt zwracany przez result_callback (patrz opis narzędzia niżej)
# ============================================================================

def _ask(call_state: Dict, state: Dict, text: str) -> Dict:
    """Pośredni krok — model MUSI powiedzieć dokładnie `text`, rozmowa trwa dalej."""
    call_state["booking"] = state
    return {"status": "ask", "say_exactly": text, "done": False}


def _finish(call_state: Dict, text: str, status: str) -> Dict:
    """Koniec tematu rezerwacji (zapisana/anulowana/nieudana) — model mówi `text`,
    a booking wraca do stanu pustego (kolejne wywołanie zacznie od nowa)."""
    call_state["booking"] = {}
    return {"status": status, "say_exactly": text, "done": True}


async def _answer_general_question(question: str, tenant: Dict, context_box: Dict) -> str:
    """Odpowiada na pytanie klienta niezwiązane bezpośrednio z krokiem rezerwacji — 1:1 z
    _answer_and_continue() w cascade, tylko historia rozmowy czytana z LLMContext
    (context_box["context"], ten sam wzorzec co realtime_tools.py::generate_conversation_summary)
    zamiast flow_manager.task.get_context_messages(). To JEDYNE miejsce w tym pliku gdzie
    tekst do powiedzenia pochodzi z osobnego wywołania LLM, nie z czystej logiki Pythona —
    dokładnie jak w cascade (tam też osobne wywołanie gpt-4.1-mini), więc to nie regresja."""
    import openai
    try:
        client = openai.AsyncOpenAI()
        history = []
        ctx = context_box.get("context")
        if ctx:
            try:
                all_messages = ctx.get_messages()
                user_assistant = [m for m in all_messages if m.get("role") in ("user", "assistant") and m.get("content")]
                history = user_assistant[-6:]
            except Exception:
                pass

        context = build_business_context(tenant)
        response = await client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": f"""Odpowiedz KRÓTKO (1-2 zdania) na pytanie klienta.

INFORMACJE O FIRMIE:
{context}

ZASADY:
- Odpowiedz TYLKO na pytanie
- Użyj DOKŁADNYCH danych z powyższych informacji
- NIE WYMYŚLAJ informacji których nie masz
- Mów {_assistant_gender(tenant.get("assistant_name", "Ania"))["gender_short"]}
- NIGDY nie pisz "Pan/Pani" ze slashem — TTS czyta to dosłownie
- Używaj formy bezpłciowej dopóki nie znasz płci klienta
- Gdy klient poda imię → używaj odpowiednio "Pan" lub "Pani"
- NIGDY nie używaj formy "ty"
- Na końcu NIE pytaj czy mogę w czymś pomóc"""},
                *history,
                {"role": "user", "content": question},
            ],
            max_tokens=150,
            temperature=0.3,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"❌ [BOOKING] GPT error: {e}")
        return "Nie mam tej informacji."


# ============================================================================
# GŁÓWNY HANDLER — 1:1 port handle_book_appointment (flows_booking_simple.py:292-938)
# ============================================================================

async def _handle_book_appointment(
    args: Dict, tenant: Dict, caller_phone: str, call_state: Dict, context_box: Dict
) -> Dict:
    service_text = args.get("service")
    staff_text = args.get("staff")
    date_text = args.get("date_text")
    time_text = args.get("time_text")
    customer_name = args.get("customer_name")
    confirmation = args.get("confirmation", "none")
    question = args.get("question")
    notes = args.get("notes")

    state = call_state.get("booking", {})

    logger.info(f"📥 [BOOKING] service={service_text}, staff={staff_text}, "
                f"date={date_text}, time={time_text}, name={customer_name}, confirm={confirmation}")

    services = tenant.get("services", [])
    staff_list = tenant.get("staff", [])

    # === OBSŁUGA ANULOWANIA ===
    if confirmation == "no":
        return _finish(call_state, "Rozumiem, rezerwacja anulowana. Czy mogę w czymś jeszcze pomóc?", "cancelled")

    # === OBSŁUGA ZMIANY ===
    if confirmation == "change":
        change_field = args.get("change_field")
        field_names = {
            "service": "usługę", "staff": "pracownika",
            "date": "datę", "time": "godzinę", "name": "imię",
        }
        if change_field and change_field in field_names:
            if change_field == "service":
                names = natural_list([s["name"] for s in services[:5]])
                if "service" not in state:
                    return _ask(call_state, state, f"Na jaką usługę? Mamy {names}.")
                saved_name = state.get("name")
                state = {}
                if saved_name:
                    state["name"] = saved_name
                return _ask(call_state, state, f"Dobrze, na jaką usługę? Mamy {names}.")
            elif change_field == "staff":
                for k in ("staff", "date", "time", "available_slots", "_pending_date", "_pending_time"):
                    state.pop(k, None)
            elif change_field == "date":
                for k in ("date", "time", "available_slots", "_pending_date", "_pending_time"):
                    state.pop(k, None)
            elif change_field == "time":
                for k in ("time", "_pending_time"):
                    state.pop(k, None)
            else:
                state.pop(change_field, None)

            if change_field == "time" and time_text:
                pass  # fall through do walidacji godziny
            elif change_field == "date" and date_text:
                pass  # fall through do walidacji daty
            else:
                return _ask(call_state, state, f"Dobrze, zmieniam {field_names[change_field]}. {_get_next_step(state, staff_list)}")
        else:
            return _ask(call_state, {}, "Dobrze, zaczynamy od nowa. Na jaką usługę?")

    # === OBSŁUGA PYTANIA O DOSTĘPNOŚĆ / INNE PYTANIE ===
    if question:
        question_lower = question.lower()
        availability_keywords = [
            "kiedy wolne", "wolny termin", "wolne terminy", "na jaki", "na jaki dzień",
            "kiedy można", "kiedy dostępn", "jaki termin", "najbliższy termin",
            "najszybciej", "jest wolny", "są wolne", "macie wolne",
            "najbliższ", "jakie terminy", "wolne godziny", "kiedy wolna",
        ]
        is_availability_question = any(kw in question_lower for kw in availability_keywords)

        if is_availability_question and "service" in state and "staff" in state:
            available_days = await get_next_available_days(
                tenant, state["staff"], state["service"],
                max_days=int(state["staff"].get("max_booking_days") or 14), limit=2,
            )
            if available_days:
                state["_pending_date"] = available_days[0]["date"].strftime("%Y-%m-%d")
                state["_pending_time"] = available_days[0]["slots"][0]
                return _ask(call_state, state, format_availability_message(available_days))
            else:
                return _ask(call_state, state,
                    f"Niestety, w najbliższych {int(state['staff'].get('max_booking_days') or 14)} dniach "
                    f"nie ma wolnych terminów. Nowe terminy pojawiają się codziennie — proszę spróbować jutro lub za kilka dni.")

        elif is_availability_question and "service" not in state:
            return _ask(call_state, state,
                "Żeby sprawdzić dostępne terminy, muszę wiedzieć na jaką usługę. "
                f"Mamy: {natural_list([s['name'] for s in services[:4]])}. Która usługa?")

        else:
            answer = await _answer_general_question(question, tenant, context_box)
            full_response = f"{answer} {_get_next_step(state, staff_list)}"
            return _ask(call_state, state, full_response)

    # === PRE-FILL: zachowaj date/time z tego wywołania nawet jeśli wyjdziemy wcześniej ===
    if date_text and "date" not in state and "_pending_date" not in state:
        state["_pending_date"] = date_text
    if time_text and "time" not in state and "_pending_time" not in state:
        state["_pending_time"] = time_text

    # === 1. WALIDACJA USŁUGI ===
    _current_service_name = state.get("service", {}).get("name", "").strip().lower()
    _service_changed = service_text and service_text.strip().lower() != _current_service_name
    if service_text and (("service" not in state) or _service_changed):
        found = next((s for s in services if s["name"].strip().lower() == service_text.strip().lower()), None)
        if found:
            if _service_changed:
                for k in ("staff", "date", "time", "available_slots", "_pending_date", "_pending_time", "_last_date_text"):
                    state.pop(k, None)
            state["service"] = found
        else:
            names = ", ".join(s["name"] for s in services)
            return _ask(call_state, state, f"Nie rozpoznałam usługi. Dostępne: {names}.")

    if "service" not in state:
        names = natural_list([s["name"] for s in services[:5]])
        return _ask(call_state, state, f"Na jaką usługę? Mamy {names}.")

    # === 2. WALIDACJA PRACOWNIKA ===
    _current_staff_name = state.get("staff", {}).get("name", "")
    _staff_changed = staff_text and staff_text != _current_staff_name
    if staff_text and (("staff" not in state) or _staff_changed):
        if _staff_changed and "staff" in state:
            for k in ("staff", "date", "time", "available_slots", "_pending_date", "_pending_time", "_last_date_text"):
                state.pop(k, None)
        if staff_text == "dowolny":
            available = [s for s in staff_list if staff_can_do_service(s, state["service"])]
            if available:
                state["staff"] = available[0]
                staff_name = odmien_imie(available[0]["name"])
                has_date = date_text or state.get("_pending_date") or "date" in state
                if not has_date:
                    return _ask(call_state, state, f"Dobrze, zapiszę do {staff_name}. Na jaki dzień?")
        else:
            found = next((s for s in staff_list if s["name"] == staff_text), None)
            if found:
                if staff_can_do_service(found, state["service"]):
                    state["staff"] = found
                else:
                    available = [s for s in staff_list if staff_can_do_service(s, state["service"])]
                    names = ", ".join(s["name"] for s in available)
                    return _ask(call_state, state, f"{found['name']} nie wykonuje {state['service']['name']}. Tę usługę wykonują: {names}.")
            else:
                names = ", ".join(s["name"] for s in staff_list)
                return _ask(call_state, state, f"Nie rozpoznałam pracownika. Dostępni: {names}.")

    if "staff" not in state:
        available = [s for s in staff_list if staff_can_do_service(s, state["service"])]
        if len(available) == 1:
            state["staff"] = available[0]
        elif len(available) == 0:
            return _ask(call_state, state, f"Przepraszam, obecnie nie mamy dostępnych pracowników do {state['service']['name']}.")
        else:
            names = natural_list([s["name"] for s in available])
            return _ask(call_state, state, f"Świetnie. Do kogo? Dostępni: {names}.")

    # === 3. WALIDACJA DATY ===
    _date_from_system = False
    if not date_text:
        pending = state.pop("_pending_date", None)
        if pending:
            date_text = pending
            _date_from_system = True
    elif "_pending_date" in state:
        state.pop("_pending_date")

    if date_text and ("date" not in state or date_text != state.get("_last_date_text")):
        state["_last_date_text"] = date_text
        state.pop("date", None)
        state.pop("time", None)
        state.pop("available_slots", None)

        date_text_clean = preprocess_date_text(date_text)
        _iso = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', date_text_clean)
        if _iso:
            parsed_date = datetime(int(_iso.group(1)), int(_iso.group(2)), int(_iso.group(3)))
        else:
            parsed_date = dateparser.parse(date_text_clean, languages=['pl'], settings=DATEPARSER_SETTINGS)

        if parsed_date:
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            if parsed_date.date() < today.date():
                return _ask(call_state, state, f"Data {format_date_polish(parsed_date)} już minęła. Podaj przyszłą datę.")

            weekday = parsed_date.weekday()
            if get_opening_hours(tenant, weekday) is None:
                date_label = format_date_polish(parsed_date).capitalize()
                return _ask(call_state, state, f"{date_label} to {POLISH_DAYS[weekday]} — jesteśmy zamknięci. Na kiedy?")

            if not _date_from_system:
                is_valid, constraint_msg = validate_max_days_ahead(parsed_date, tenant, state["staff"])
                if not is_valid:
                    staff_name = odmien_imie(state["staff"]["name"])
                    max_days_val = int(state["staff"].get("max_booking_days") or 14)
                    available_days = await get_next_available_days(tenant, state["staff"], state["service"], max_days=max_days_val, limit=1)
                    date_label = format_date_polish(parsed_date)
                    if available_days:
                        first = available_days[0]
                        state["_pending_date"] = first["date"].strftime("%Y-%m-%d")
                        state["_pending_time"] = first["slots"][0]
                        suggestion = (
                            f"{constraint_msg} {date_label.capitalize()} to za daleko. "
                            f"Najbliższy wolny termin u {staff_name} "
                            f"to {format_date_polish(first['date'])} o {format_hour_polish(first['slots'][0])}. Czy zapisać na ten termin?"
                        )
                        return _ask(call_state, state, suggestion)
                    else:
                        return _ask(call_state, state, f"{constraint_msg} {date_label.capitalize()} to za daleko. Niestety w tym oknie nie ma wolnych terminów.")

            slots = await get_available_slots_from_api(tenant, state["staff"], state["service"], parsed_date)

            if not slots:
                available_days = await get_next_available_days(
                    tenant, state["staff"], state["service"],
                    max_days=int(state["staff"].get("max_booking_days") or 14), limit=2,
                )
                staff_name = odmien_imie(state["staff"]["name"])
                if available_days:
                    suggestion = format_availability_message(available_days)
                    return _ask(call_state, state, f"{format_date_polish(parsed_date).capitalize()} u {staff_name} nie ma wolnych terminów. {suggestion}")
                else:
                    max_days = int(state["staff"].get("max_booking_days") or 14)
                    return _ask(call_state, state,
                        f"{format_date_polish(parsed_date).capitalize()} u {staff_name} nie ma wolnych terminów "
                        f"i w najbliższych {max_days} dniach grafik jest pełny. Nowe terminy pojawiają się codziennie — proszę spróbować jutro.")

            state["date"] = parsed_date
            state["available_slots"] = slots
            state.pop("_retry_date", None)
        else:
            state["_retry_date"] = state.get("_retry_date", 0) + 1
            if state["_retry_date"] >= 3:
                for k in ("date", "time", "available_slots", "_pending_date", "_pending_time", "_retry_date", "_last_date_text"):
                    state.pop(k, None)
                return _ask(call_state, state, "Przepraszam za kłopot. Na jaki dzień szukamy terminu? Proszę powiedzieć np. 'jutro' lub '15 maja'.")
            return _ask(call_state, state, "Nie zrozumiałam daty. Proszę powiedzieć np. 'jutro', 'w piątek' lub '15 maja'.")

    if "date" not in state:
        staff_name = odmien_imie(state["staff"]["name"])
        available_days = await get_next_available_days(
            tenant, state["staff"], state["service"],
            max_days=int(state["staff"].get("max_booking_days") or 14), limit=1,
        )
        if available_days:
            first_day = available_days[0]
            first_date_str = format_date_polish(first_day["date"])
            first_slot = format_hour_polish(first_day["slots"][0])
            state["_pending_date"] = first_day["date"].strftime("%Y-%m-%d")
            state["_pending_time"] = first_day["slots"][0]
            return _ask(call_state, state, f"U {staff_name} najbliższy wolny termin to {first_date_str} o {first_slot}. Zapisać, czy wolisz inny termin?")
        else:
            max_days = int(state["staff"].get("max_booking_days") or 14)
            return _ask(call_state, state, f"U {staff_name} w najbliższych {max_days} dniach nie ma wolnych terminów. Nowe terminy pojawiają się codziennie — proszę spróbować jutro lub za kilka dni.")

    # === 4. WALIDACJA GODZINY ===
    if not time_text:
        time_text = state.pop("_pending_time", None)
    elif "_pending_time" in state:
        state.pop("_pending_time")

    time_just_set = False
    if time_text and ("time" not in state or _normalize_time(time_text) != _normalize_time(state.get("time", ""))):
        state.pop("time", None)
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
                return _ask(call_state, state, f"Rozumiem, szukamy terminu {range_name}. Na jaki dzień?")
            if filtered:
                slots_text = _slots_summary(filtered)
                return _ask(call_state, state, f"Tak, {range_name} wolne są: {slots_text}. Którą godzinę wybrać?")
            else:
                all_slots = natural_list([format_hour_polish(s) for s in state.get("available_slots", [])[:6]])
                return _ask(call_state, state, f"{range_name.capitalize()} zajęte. Dostępne: {all_slots}.")

        parsed_time = _parse_time(time_text)

        if parsed_time:
            _h, _m = (int(x) for x in parsed_time.split(":"))
            requested_datetime = state["date"].replace(hour=_h, minute=_m, second=0, microsecond=0)
            is_advance_valid, advance_msg = validate_min_advance_hours(requested_datetime, tenant, state["staff"])
            if not is_advance_valid:
                slots_text = _slots_summary(state.get("available_slots", []))
                return _ask(call_state, state, f"{advance_msg} Wolne są: {slots_text}.")

            is_available, current_slots = await validate_slot_available(tenant, state["staff"], state["service"], state["date"], parsed_time)

            if is_available:
                state["time"] = parsed_time
                state.pop("_retry_time", None)
                time_just_set = True
                state["available_slots"] = current_slots
            else:
                if current_slots:
                    work_day = state["date"].weekday()
                    staff_hours = get_staff_working_hours(state["staff"], work_day)
                    if not staff_hours:
                        staff_hours = get_opening_hours(tenant, work_day)

                    requested_h = int(parsed_time.split(":")[0])
                    requested_m = int(parsed_time.split(":")[1]) if ":" in parsed_time else 0

                    if staff_hours:
                        open_h, close_h = staff_hours
                        if requested_h < open_h or (requested_h == open_h and requested_m < 0):
                            slots_text = _slots_summary(current_slots)
                            return _ask(call_state, state, f"W tym dniu pracujemy od {format_hour_polish(f'{open_h}:00')}. Wolne są: {slots_text}.")
                        elif requested_h >= close_h:
                            slots_text = _slots_summary(current_slots)
                            return _ask(call_state, state, f"W tym dniu pracujemy do {format_hour_polish(f'{close_h}:00')}. Wolne są: {slots_text}.")

                    slots_text = _slots_summary(current_slots)
                    return _ask(call_state, state, f"Godzina {format_hour_polish(parsed_time)} zajęta. Wolne: {slots_text}.")
                else:
                    state.pop("date", None)
                    available_days = await get_next_available_days(
                        tenant, state["staff"], state["service"],
                        max_days=int(state["staff"].get("max_booking_days") or 14), limit=2,
                    )
                    if available_days:
                        suggestion = format_availability_message(available_days)
                        return _ask(call_state, state, f"Na ten dzień nie ma już wolnych terminów. {suggestion}")
                    else:
                        return _ask(call_state, state, "Na ten dzień nie ma już wolnych terminów i w najbliższych dniach też jest pełny grafik.")
        else:
            state["_retry_time"] = state.get("_retry_time", 0) + 1
            if state["_retry_time"] >= 3:
                for k in ("time", "_pending_time", "_retry_time"):
                    state.pop(k, None)
                slots_text = _slots_summary(state.get("available_slots", []))
                return _ask(call_state, state, f"Przepraszam za kłopot. Dostępne godziny: {slots_text}. Którą wybrać?")
            slots_text = natural_list([format_hour_polish(s) for s in state["available_slots"][:6]])
            return _ask(call_state, state, f"Nie rozumiem godziny. Dostępne są: {slots_text}.")

    if "time" not in state:
        slots_text = _slots_summary(state["available_slots"])
        return _ask(call_state, state, f"{format_date_polish(state['date']).capitalize()} wolne są: {slots_text}. Którą godzinę?")

    # === 5. WALIDACJA IMIENIA ===
    name_just_collected = False
    if customer_name and "name" not in state:
        name = customer_name.strip()
        for prefix in ["pan ", "pani ", "na "]:
            if name.lower().startswith(prefix):
                name = name[len(prefix):]

        if len(name) >= 2 and name.lower() not in ["tak", "nie", "halo", "proszę"]:
            state["name"] = name.title()
            name_just_collected = True
        else:
            gender_msg = _assistant_gender(tenant.get("assistant_name", "Ania"))["nie_dosłyszałam"]
            return _ask(call_state, state, f"{gender_msg} imienia. Na jakie imię zapisać wizytę?")

    if "name" not in state:
        return _ask(call_state, state, f"Świetnie, {format_date_polish(state['date'])} o {format_hour_polish(state['time'])}. Na jakie imię zapisać wizytę?")

    # === 5.5 UWAGI ===
    _no_notes = {"brak", "nie", "nie ma", "żadnych", "brak uwag", "nie mam", "nie mam uwag", "żadne", "ok", "dobrze"}
    if notes and "notes" not in state:
        notes_clean = notes.strip().lower().rstrip(".")
        if notes_clean not in _no_notes and not notes_clean.startswith("brak") and not notes_clean.startswith("nie ma"):
            state["notes"] = notes.strip()

    # === 6. POTWIERDZENIE ===
    if "confirmed" not in state:
        is_confirming = (
            confirmation not in ("no", "change")
            and not name_just_collected
            and not time_just_set
            and not question
        )
        if is_confirming:
            state["confirmed"] = True
        else:
            staff_name = odmien_imie(state["staff"]["name"])
            customer_gender = detect_gender(state["name"])
            customer_name_declined = odmien_imie(state["name"])
            notes_part = f" Uwagi: {state['notes']}." if state.get("notes") else ""

            if customer_name and state.get("name") and customer_name.strip().lower() != state["name"].lower() and not name_just_collected:
                new_name = customer_name.strip().title()
                for prefix in ["pan ", "pani ", "na "]:
                    if new_name.lower().startswith(prefix):
                        new_name = new_name[len(prefix):].title()
                state["name"] = new_name
                customer_name_declined = odmien_imie(new_name)
                customer_gender = detect_gender(new_name)
                return _ask(call_state, state, f"Poprawiam — na {customer_gender} {customer_name_declined}. Zgadza się?")

            summary = (
                f"{state['service']['name']} u {staff_name}, "
                f"{format_date_polish(state['date'])} o {format_hour_polish(state['time'])} "
                f"— na {customer_gender} {customer_name_declined}.{notes_part} Zgadza się?"
            )
            return _ask(call_state, state, summary)

    # === 7. ZAPIS REZERWACJI ===
    return await _save_booking(state, tenant, caller_phone, call_state)


async def _save_booking(state: Dict, tenant: Dict, caller_phone: str, call_state: Dict) -> Dict:
    """Zapisuje rezerwację do API — z PODWÓJNĄ walidacją. 1:1 z _save_booking() w cascade,
    plus obsługa 409 slot_taken (patrz _save_booking_via_api)."""
    logger.info("💾 [BOOKING] SAVING BOOKING...")

    try:
        is_available, current_slots = await validate_slot_available(tenant, state["staff"], state["service"], state["date"], state["time"])

        if not is_available:
            logger.warning("❌ [BOOKING] Slot was taken between confirmation and save (re-check)")
            if current_slots:
                state.pop("time", None)
                state["available_slots"] = current_slots
                slots_text = _slots_summary(current_slots)
                return _ask(call_state, state, f"Ta godzina właśnie zniknęła. Zostały: {slots_text}. Którą?")
            else:
                state.pop("date", None)
                state.pop("time", None)
                return _ask(call_state, state, "Ten dzień właśnie się zapełnił. Który inny?")

        outcome, result = await _save_booking_via_api(
            tenant, state["staff"], state["service"], state["date"], state["time"],
            state["name"], caller_phone, notes=state.get("notes", ""),
        )

        if outcome == "slot_taken":
            # Baza złapała race condition którego nasza re-walidacja wyżej nie złapała
            # (dwie równoległe rozmowy trafiły w ten sam termin między naszym sprawdzeniem
            # a zapisem) — dokładnie ta sama ścieżka co nieudana re-walidacja powyżej.
            logger.warning("❌ [BOOKING] 409 z API mimo udanej re-walidacji — prawdziwy race condition")
            fresh_slots = await get_available_slots_from_api(tenant, state["staff"], state["service"], state["date"])
            if fresh_slots:
                state.pop("time", None)
                state["available_slots"] = fresh_slots
                slots_text = _slots_summary(fresh_slots)
                return _ask(call_state, state, f"Ta godzina właśnie została zajęta. Zostały: {slots_text}. Którą?")
            else:
                state.pop("date", None)
                state.pop("time", None)
                return _ask(call_state, state, "Ten dzień właśnie się zapełnił. Który inny?")

        if outcome == "error" or not result:
            return _ask(call_state, state, "Coś poszło nie tak z zapisem. Przekazać wiadomość do właściciela?")

        # Sukces
        booking_code = result.get("booking_code", "")
        sms_info = ""
        if booking_code and caller_phone:
            try:
                sms_sent = await send_booking_sms(
                    tenant=tenant, customer_phone=caller_phone,
                    service_name=state["service"]["name"], staff_name=state["staff"]["name"],
                    date_str=state["date"].strftime("%d.%m"), time_str=state["time"],
                    booking_code=booking_code,
                )
                if sms_sent:
                    await increment_sms_count(tenant.get("id"))
                    sms_info = " Wysłałam esemes z potwierdzeniem."
                else:
                    sms_info = " Niestety esemes nie dotarł, ale rezerwacja jest zapisana."
            except Exception as e:
                logger.error(f"📱 [BOOKING] SMS error: {e}")
                sms_info = " Niestety esemes nie dotarł, ale rezerwacja jest zapisana."

        try:
            time_padded = state["time"].zfill(5)
            scheduled_at = f"{state['date'].strftime('%Y-%m-%d')}T{time_padded}:00"
            asyncio.create_task(save_client_visit(
                firm_id=tenant.get("id", ""), phone=caller_phone, name=state.get("name", ""),
                service=state["service"]["name"], staff=state["staff"]["name"],
                scheduled_at=scheduled_at, notes=state.get("notes", ""),
            ))
        except Exception as e:
            logger.warning(f"[BOOKING] CRM save_client_visit error: {e}")

        staff_name = odmien_imie(state["staff"]["name"])
        notes_confirm = " Uwagi zapisane." if state.get("notes") else ""
        final_text = (
            f"Gotowe. {state['service']['name']} u {staff_name}, "
            f"{format_date_polish(state['date'])} o {format_hour_polish(state['time'])}."
            f"{notes_confirm}{sms_info}"
        )
        return _finish(call_state, final_text, "booked")

    except Exception as e:
        logger.error(f"💾 [BOOKING] SAVE error: {e}")
        return _ask(call_state, state, "Coś poszło nie tak. Przekazać wiadomość?")


# ============================================================================
# FunctionSchema — publiczny interfejs (wzorzec build_X_tool z realtime_tools.py)
# ============================================================================

def build_book_appointment_tool(tenant: Dict, caller_phone: str, call_state: Dict, context_box: Dict) -> FunctionSchema:
    """FunctionSchema dla rezerwacji — WARUNKOWO dołączane z bot_gemini_test.py tylko gdy
    tenant.get("booking_enabled")==1 (nazwa pola do potwierdzenia przy podpinaniu).

    call_state: ten sam call_state/gemini_state dict co reszta realtime_tools.py — trzyma
    stan bookingu w call_state["booking"] (dict, pusty gdy nic w toku).

    context_box: {"context": None}, ten sam wzorzec co w build_submit_lead_tool — LLMContext
    powstaje PO zbudowaniu listy tools, więc handler czyta context_box["context"] dopiero
    przy faktycznym wywołaniu (potrzebne do _answer_general_question dla pytań pobocznych)."""
    services = tenant.get("services", [])
    staff_list = tenant.get("staff", [])
    service_names = [s["name"] for s in services]
    staff_names = [s["name"] for s in staff_list] + ["dowolny"]

    async def handle_book_appointment(params: FunctionCallParams):
        result = await _handle_book_appointment(params.arguments, tenant, caller_phone, call_state, context_box)
        await params.result_callback(result)

    return FunctionSchema(
        name="book_appointment",
        description="""Umów wizytę. Użyj gdy klient: chce umówić/zarezerwować wizytę, pyta o wolne
terminy/dostępność, chce zmienić lub odwołać istniejącą rezerwację. Wywołuj przy KAŻDEJ kolejnej
odpowiedzi klienta dotyczącej tej rezerwacji (aż do "done": true) — przekazuj DOKŁADNIE co klient
powiedział (nie interpretuj, nie zgaduj brakujących pól).

⛔ KRYTYCZNE — WYNIK ZAWIERA POLE "say_exactly": Twoja odpowiedź klientowi MUSI być TĄ
TREŚCIĄ, SŁOWO W SŁOWO — bez zmiany choćby jednego słowa, bez dodawania własnych zdań przed
ani po, bez skracania. To dotyczy dat, godzin, cen i potwierdzeń — nie improwizuj przy nich
pod żadnym pozorem, nawet jeśli treść wydaje Ci się sztywna. Jeśli wynik ma "done": true,
temat rezerwacji jest zamknięty (zapisana/anulowana/nieudana) — po powiedzeniu say_exactly
NIE kontynuuj tematu rezerwacji.

Data: przekaż w formacie YYYY-MM-DD, tłumacząc co klient powiedział na podstawie dzisiejszej
daty z instrukcji systemowych (np. "jutro" → jutrzejsza data ISO, "w piątek" → data najbliższego
piątku, "dwudziestego maja" → "2026-05-20"). Jeśli nie jesteś pewien/pewna dokładnej daty,
możesz też przekazać naturalny tekst klienta ("jutro", "w piątek") — jest też parsowany.
Godzina: format HH:MM, zamień słowa klienta na cyfry ("na trzynastą" → "13:00", "wpół do
dwunastej" → "11:30", "czternasta zero" → "14:00"). Null jeśli klient nie podał.
Potwierdzenie: klient wyraża zgodę ("tak", "oczywiście", "pasuje") → confirmation="yes";
rezygnuje → confirmation="no"; chce coś zmienić → confirmation="change" + change_field.
Wypełniaj WSZYSTKIE pola które klient podał w jednym zdaniu, nie tylko jedno.""",
        properties={
            "service": {
                "type": "string", "enum": service_names,
                "description": "Wybierz usługę z listy która najbardziej pasuje do słów klienta",
            },
            "staff": {
                "type": "string", "enum": staff_names,
                "description": "Wybierz pracownika z listy lub 'dowolny'",
            },
            "date_text": {
                "type": "string",
                "description": "Data w formacie YYYY-MM-DD (patrz opis narzędzia) lub naturalny tekst klienta. Null jeśli klient nie podał daty.",
            },
            "time_text": {
                "type": "string",
                "description": "Godzina w formacie HH:MM (patrz opis narzędzia). Null jeśli klient nie podał godziny.",
            },
            "customer_name": {
                "type": "string", "description": "Imię klienta lub null",
            },
            "confirmation": {
                "type": "string", "enum": ["yes", "no", "change", "none"],
                "description": "yes=potwierdza, no=anuluje, change=chce zmienić coś, none=nic z tych",
            },
            "change_field": {
                "type": "string", "enum": ["service", "staff", "date", "time", "name"],
                "description": "Co klient chce zmienić gdy confirmation='change'",
            },
            "question": {
                "type": "string",
                "description": "Jeśli klient pyta o coś poza samą rezerwacją — wpisz pytanie. Null jeśli kontynuuje rezerwację.",
            },
            "notes": {
                "type": "string",
                "description": "Uwagi klienta do wizyty. Null jeśli brak uwag lub powiedział 'nie'.",
            },
        },
        required=["confirmation"],
        handler=handle_book_appointment,
    )
