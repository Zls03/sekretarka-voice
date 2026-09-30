"""Narzędzie book_appointment — wieloetapowa rezerwacja wizyty z walidacją po stronie serwera."""

import asyncio
import re
from datetime import datetime

import dateparser
from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.services.llm_service import FunctionCallParams

from app.booking.availability import (
    _slots_summary,
    format_availability_message,
    get_next_available_days,
    get_opening_hours,
    get_staff_working_hours,
    staff_can_do_service,
    validate_max_days_ahead,
    validate_min_advance_hours,
    validate_slot_available,
)
from app.booking.panel_api import _save_booking_via_api, get_available_slots_from_api
from app.booking.parsing import DATEPARSER_SETTINGS, _normalize_time, _parse_time, preprocess_date_text
from app.booking.replies import _closing_question
from app.booking.sms import increment_sms_count, send_booking_sms, send_booking_sms_vonage
from app.panel_client import save_client_visit
from app.polish.formatting import POLISH_DAYS, format_date_polish, format_hour_polish, natural_list
from app.polish.grammar import detect_gender, odmien_imie
from app.prompt.business_context import _assistant_gender, build_business_context


def _get_next_step(state: dict, staff_list: list) -> str:
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


def _ask(call_state: dict, state: dict, text: str) -> dict:
    """Pośredni krok — model MUSI powiedzieć dokładnie `text`, rozmowa trwa dalej."""
    call_state["booking"] = state
    return {"status": "ask", "say_exactly": text, "done": False}


def _finish(call_state: dict, text: str, status: str) -> dict:
    """Koniec tematu rezerwacji (zapisana/anulowana/nieudana) — model mówi `text`,
    a booking wraca do stanu pustego (kolejne wywołanie zacznie od nowa)."""
    call_state["booking"] = {}
    return {"status": status, "say_exactly": text, "done": True}


async def _answer_general_question(question: str, tenant: dict, context_box: dict) -> str:
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


async def _handle_book_appointment(
    args: dict, tenant: dict, caller_phone: str, call_state: dict, context_box: dict, channel: str = "twilio"
) -> dict:
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
        if not state:
            # Nic nie było w toku w TEJ rozmowie — "rezerwacja anulowana" byłoby fałszywym
            # potwierdzeniem (patrz ZAKAZ FAŁSZYWYCH POTWIERDZEŃ w prompcie). Model mógł tu trafić
            # bo klient chce odwołać wizytę umówioną WCZEŚNIEJ (inna rozmowa) — do tego jest
            # osobne narzędzie manage_booking, nie to.
            return _ask(call_state, {}, "Nie mam żadnej rezerwacji w trakcie tej rozmowy. Chce Pan/Pani umówić nową wizytę, czy odwołać wcześniej umówioną?")
        return _finish(call_state, "Rozumiem, rezerwacja anulowana. Czy mogę w czymś jeszcze pomóc?", "cancelled")

    # === OBSŁUGA ZMIANY ===
    if confirmation == "change":
        change_field = args.get("change_field")
        field_names = {
            "service": "usługę", "staff": "pracownika",
            "date": "datę", "time": "godzinę", "name": "imię",
        }
        # Model czasem ustawia confirmation="change" BEZ change_field gdy klient po prostu SAM
        # PODAJE inny termin niż zaproponowany (typowo odpowiadając na "zapisać, czy wolisz inny
        # termin?" własną datą/godziną) — to NIE jest "popraw pole X", to zwykła kontynuacja z
        # nową wartością. Bez tego cały stan (usługa, pracownik) był kasowany i rozmowa
        # zapętlała się w "zaczynamy od nowa" w kółko — złapane na żywym telefonie. Wnioskujemy
        # change_field z tego CO faktycznie przyszło w tym wywołaniu.
        if not change_field and (date_text or time_text or service_text or staff_text):
            change_field = "date" if date_text else ("time" if time_text else ("service" if service_text else "staff"))
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
    return await _save_booking(state, tenant, caller_phone, call_state, channel)


async def _save_booking(state: dict, tenant: dict, caller_phone: str, call_state: dict, channel: str = "twilio") -> dict:
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
                # tenant.get("phone_number") jest numerem VONAGE dla tras vonage, a Twilio wysyłkę
                # SMS przyjmuje TYLKO z numerów które sam obsługuje ("From" musi być numerem Twilio,
                # patrz błąd 21659 złapany na żywym telefonie: numer Vonage odrzucony) — stąd wybór
                # providera po kanale połączenia, nie jeden zaszyty na sztywno jak w cascade
                # (cascade zawsze ma Twilio, więc tam ten problem nie istnieje).
                sms_func = send_booking_sms_vonage if channel == "vonage" else send_booking_sms
                sms_sent = await sms_func(
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
            f"{notes_confirm}{sms_info} {_closing_question()}"
        )
        return _finish(call_state, final_text, "booked")

    except Exception as e:
        logger.error(f"💾 [BOOKING] SAVE error: {e}")
        return _ask(call_state, state, "Coś poszło nie tak. Przekazać wiadomość?")


def build_book_appointment_tool(tenant: dict, caller_phone: str, call_state: dict, context_box: dict, channel: str = "twilio") -> FunctionSchema:
    """FunctionSchema dla rezerwacji — WARUNKOWO dołączane z bot_gemini_test.py tylko gdy
    tenant.get("booking_enabled")==1 (nazwa pola do potwierdzenia przy podpinaniu).

    call_state: ten sam call_state/gemini_state dict co reszta realtime_tools.py — trzyma
    stan bookingu w call_state["booking"] (dict, pusty gdy nic w toku).

    context_box: {"context": None}, ten sam wzorzec co w build_contact_owner_tool — LLMContext
    powstaje PO zbudowaniu listy tools, więc handler czyta context_box["context"] dopiero
    przy faktycznym wywołaniu (potrzebne do _answer_general_question dla pytań pobocznych)."""
    services = tenant.get("services", [])
    staff_list = tenant.get("staff", [])
    service_names = [s["name"] for s in services]
    staff_names = [s["name"] for s in staff_list] + ["dowolny"]

    async def handle_book_appointment(params: FunctionCallParams):
        result = await _handle_book_appointment(params.arguments, tenant, caller_phone, call_state, context_box, channel)
        await params.result_callback(result)

    return FunctionSchema(
        name="book_appointment",
        description="""Umów NOWĄ wizytę. Użyj gdy klient: chce umówić/zarezerwować wizytę, pyta o wolne
terminy/dostępność, albo chce coś zmienić W TRAKCIE TEJ ROZMOWY zanim rezerwacja została zapisana
(np. poprawia datę/godzinę/usługę przed potwierdzeniem).
⛔ NIE używaj do odwołania/przełożenia wizyty którą klient umówił WCZEŚNIEJ (inna rozmowa/inny dzień)
— do tego jest osobne narzędzie manage_booking. Ten tool zna tylko rezerwację w toku TEJ rozmowy;
jeśli jeszcze nic nie zaczęto (confirmation="no" bez wcześniejszych danych), NIE potwierdzi żadnego
anulowania, bo nie ma czego anulować.
Wywołuj przy KAŻDEJ kolejnej
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
rezygnuje → confirmation="no".
⚠️ confirmation="change" jest TYLKO do POPRAWIANIA pola które klient JUŻ WCZEŚNIEJ ustalił/
potwierdził w tej rozmowie (np. po podsumowaniu mówi "nie, zmieńmy jednak godzinę"). Gdy
klient po prostu ODPOWIADA na propozycję terminu własną datą/godziną (np. na pytanie
"zapisać, czy wolisz inny termin?" mówi "wolę we wtorek o dziesiątej") — to jest ZWYKŁA
kontynuacja: confirmation="none" (albo "yes" jeśli dosłownie akceptuje), wypełnij date_text/
time_text nową wartością, NIE ustawiaj "change". Błędne użycie "change" tutaj resetuje całą
rezerwację (usługę, pracownika) do zera, co jest widoczne dla klienta i frustrujące.
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
