"""Narzędzie book_appointment — wieloetapowa rezerwacja wizyty z walidacją po stronie serwera.

Model przekazuje dosłownie to, co powiedział klient; cała logika (dostępność, reguły
wyprzedzenia, godziny pracy) jest tutaj, a model tylko odczytuje odpowiedź `say_exactly`.
Dzięki temu nie może zmyślić wolnego terminu ani potwierdzić niezapisanej wizyty.
"""

import re
from dataclasses import dataclass
from datetime import datetime

import dateparser
from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.services.llm_service import FunctionCallParams

from app.background import spawn
from app.booking.availability import (
    format_availability_message,
    get_next_available_days,
    get_opening_hours,
    get_staff_working_hours,
    slots_summary,
    staff_can_do_service,
    validate_max_days_ahead,
    validate_min_advance_hours,
    validate_slot_available,
)
from app.booking.panel_api import get_available_slots_from_api, save_booking_in_panel
from app.booking.parsing import DATEPARSER_SETTINGS, normalize_time, parse_time, preprocess_date_text
from app.booking.replies import closing_question
from app.booking.sms import increment_sms_count, send_booking_sms, send_booking_sms_vonage
from app.panel_client import save_client_visit
from app.polish.formatting import POLISH_DAYS, format_date_polish, format_hour_polish, natural_list
from app.polish.grammar import detect_gender, odmien_imie
from app.prompt.business_context import assistant_gender_forms, build_business_context


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
        slots_text = slots_summary(state.get("available_slots", []))
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
                user_assistant = [
                    m for m in all_messages if m.get("role") in ("user", "assistant") and m.get("content")
                ]
                history = user_assistant[-6:]
            except Exception:
                pass

        context = build_business_context(tenant)
        response = await client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {
                    "role": "system",
                    "content": f"""Odpowiedz KRÓTKO (1-2 zdania) na pytanie klienta.

INFORMACJE O FIRMIE:
{context}

ZASADY:
- Odpowiedz TYLKO na pytanie
- Użyj DOKŁADNYCH danych z powyższych informacji
- NIE WYMYŚLAJ informacji których nie masz
- Mów {assistant_gender_forms(tenant.get("assistant_name", "Ania"))["gender_short"]}
- NIGDY nie pisz "Pan/Pani" ze slashem — TTS czyta to dosłownie
- Używaj formy bezpłciowej dopóki nie znasz płci klienta
- Gdy klient poda imię → używaj odpowiednio "Pan" lub "Pani"
- NIGDY nie używaj formy "ty"
- Na końcu NIE pytaj czy mogę w czymś pomóc""",
                },
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


@dataclass
class _Turn:
    """Jedno wywołanie narzędzia: argumenty od modelu + stan rezerwacji w toku.

    date_text/time_text są mutowalne — kroki podstawiają tu wartości zapamiętane
    wcześniej (np. termin zaproponowany klientowi, który ten tylko potwierdza).
    """

    tenant: dict
    call_state: dict
    context_box: dict
    state: dict
    service_text: str | None
    staff_text: str | None
    date_text: str | None
    time_text: str | None
    customer_name: str | None
    confirmation: str
    change_field: str | None
    question: str | None
    notes: str | None
    time_just_set: bool = False
    name_just_collected: bool = False

    @property
    def services(self) -> list[dict]:
        return self.tenant.get("services", [])

    @property
    def staff_list(self) -> list[dict]:
        return self.tenant.get("staff", [])

    def ask(self, text: str, state: dict | None = None) -> dict:
        return _ask(self.call_state, self.state if state is None else state, text)

    def staff_for_service(self) -> list[dict]:
        return [s for s in self.staff_list if staff_can_do_service(s, self.state["service"])]

    def max_booking_days(self) -> int:
        return int(self.state["staff"].get("max_booking_days") or 14)

    async def next_available_days(self, limit: int) -> list[dict]:
        return await get_next_available_days(
            self.tenant, self.state["staff"], self.state["service"], max_days=self.max_booking_days(), limit=limit
        )

    def drop(self, *keys: str) -> None:
        for key in keys:
            self.state.pop(key, None)


# Klucze stanu zależne od wybranego terminu — kasowane przy zmianie wcześniejszego wyboru.
_SLOT_KEYS = ("date", "time", "available_slots", "_pending_date", "_pending_time")

_CHANGE_FIELD_NAMES = {"service": "usługę", "staff": "pracownika", "date": "datę", "time": "godzinę", "name": "imię"}

_AVAILABILITY_KEYWORDS = (
    "kiedy wolne",
    "wolny termin",
    "wolne terminy",
    "na jaki",
    "na jaki dzień",
    "kiedy można",
    "kiedy dostępn",
    "jaki termin",
    "najbliższy termin",
    "najszybciej",
    "jest wolny",
    "są wolne",
    "macie wolne",
    "najbliższ",
    "jakie terminy",
    "wolne godziny",
    "kiedy wolna",
)

_AFTERNOON_PHRASES = ("po południu", "popołudniu", "popoludniu", "po poludniu", "popołudniow", "popoludniow")
_MORNING_PHRASES = ("rano", "z rana", "przed południem", "przedpołudni", "dopołudni")

_NAME_PREFIXES = ("pan ", "pani ", "na ")
_NOT_A_NAME = ("tak", "nie", "halo", "proszę")
_NO_NOTES = {"brak", "nie", "nie ma", "żadnych", "brak uwag", "nie mam", "nie mam uwag", "żadne", "ok", "dobrze"}

_NO_FREE_SLOTS_HINT = "Nowe terminy pojawiają się codziennie — proszę spróbować jutro lub za kilka dni."


async def book_appointment_step(
    args: dict, tenant: dict, caller_phone: str, call_state: dict, context_box: dict, channel: str = "twilio"
) -> dict:
    """Jeden krok rozmowy rezerwacyjnej: przetwarza odpowiedź klienta i zwraca, co powiedzieć.

    Kroki idą po kolei; pierwszy, któremu czegoś brakuje albo coś się nie zgadza,
    zwraca pytanie do klienta (say_exactly). Gdy wszystko jest zebrane i potwierdzone,
    wizyta jest zapisywana w panelu.
    """
    turn = _Turn(
        tenant=tenant,
        call_state=call_state,
        context_box=context_box,
        state=call_state.get("booking", {}),
        service_text=args.get("service"),
        staff_text=args.get("staff"),
        date_text=args.get("date_text"),
        time_text=args.get("time_text"),
        customer_name=args.get("customer_name"),
        confirmation=args.get("confirmation", "none"),
        change_field=args.get("change_field"),
        question=args.get("question"),
        notes=args.get("notes"),
    )
    logger.info(
        f"📥 [BOOKING] service={turn.service_text}, staff={turn.staff_text}, "
        f"date={turn.date_text}, time={turn.time_text}, name={turn.customer_name}, confirm={turn.confirmation}"
    )

    for step in (_handle_cancel, _handle_change, _handle_question):
        if (reply := await step(turn)) is not None:
            return reply

    _remember_offered_date_and_time(turn)
    for step in (_resolve_service, _resolve_staff, _resolve_date, _resolve_time, _resolve_name):
        if (reply := await step(turn)) is not None:
            return reply

    _collect_notes(turn)
    if (reply := _confirm(turn)) is not None:
        return reply
    return await _save_booking(turn.state, tenant, caller_phone, call_state, channel)


async def _handle_cancel(turn: _Turn) -> dict | None:
    if turn.confirmation != "no":
        return None
    if not turn.state:
        # Nic nie jest w toku w TEJ rozmowie — potwierdzenie anulowania byłoby fałszywe.
        # Odwołanie wizyty z wcześniejszej rozmowy obsługuje narzędzie manage_booking.
        return turn.ask(
            "Nie mam żadnej rezerwacji w trakcie tej rozmowy. Chce Pan/Pani umówić nową wizytę, czy odwołać wcześniej umówioną?",
            state={},
        )
    return _finish(turn.call_state, "Rozumiem, rezerwacja anulowana. Czy mogę w czymś jeszcze pomóc?", "cancelled")


async def _handle_change(turn: _Turn) -> dict | None:
    if turn.confirmation != "change":
        return None

    field = turn.change_field
    # Model bywa, że ustawia "change" bez change_field, gdy klient po prostu podaje inny
    # termin niż zaproponowany — to kontynuacja z nową wartością, nie reset rezerwacji.
    if not field and (turn.date_text or turn.time_text or turn.service_text or turn.staff_text):
        field = (
            "date" if turn.date_text else ("time" if turn.time_text else ("service" if turn.service_text else "staff"))
        )
    if not field or field not in _CHANGE_FIELD_NAMES:
        return turn.ask("Dobrze, zaczynamy od nowa. Na jaką usługę?", state={})

    if field == "service":
        names = natural_list([s["name"] for s in turn.services[:5]])
        if "service" not in turn.state:
            return turn.ask(f"Na jaką usługę? Mamy {names}.")
        saved_name = turn.state.get("name")
        turn.state = {"name": saved_name} if saved_name else {}
        return turn.ask(f"Dobrze, na jaką usługę? Mamy {names}.")
    if field == "staff":
        turn.drop("staff", *_SLOT_KEYS)
    elif field == "date":
        turn.drop(*_SLOT_KEYS)
    elif field == "time":
        turn.drop("time", "_pending_time")
    else:
        turn.drop(field)

    # Nowa data/godzina podana od razu — przechodzi dalej do normalnej walidacji.
    if (field == "time" and turn.time_text) or (field == "date" and turn.date_text):
        return None
    return turn.ask(f"Dobrze, zmieniam {_CHANGE_FIELD_NAMES[field]}. {_get_next_step(turn.state, turn.staff_list)}")


async def _handle_question(turn: _Turn) -> dict | None:
    if not turn.question:
        return None

    question_lower = turn.question.lower()
    about_availability = any(keyword in question_lower for keyword in _AVAILABILITY_KEYWORDS)

    if about_availability and "service" in turn.state and "staff" in turn.state:
        available_days = await turn.next_available_days(limit=2)
        if not available_days:
            return turn.ask(
                f"Niestety, w najbliższych {turn.max_booking_days()} dniach "
                f"nie ma wolnych terminów. {_NO_FREE_SLOTS_HINT}"
            )
        _offer_slot(turn, available_days[0])
        return turn.ask(format_availability_message(available_days))

    if about_availability and "service" not in turn.state:
        return turn.ask(
            "Żeby sprawdzić dostępne terminy, muszę wiedzieć na jaką usługę. "
            f"Mamy: {natural_list([s['name'] for s in turn.services[:4]])}. Która usługa?"
        )

    answer = await _answer_general_question(turn.question, turn.tenant, turn.context_box)
    return turn.ask(f"{answer} {_get_next_step(turn.state, turn.staff_list)}")


def _offer_slot(turn: _Turn, day: dict) -> None:
    """Zapamiętuje proponowany termin — samo "tak" klienta wystarczy, by go przyjąć."""
    turn.state["_pending_date"] = day["date"].strftime("%Y-%m-%d")
    turn.state["_pending_time"] = day["slots"][0]


def _remember_offered_date_and_time(turn: _Turn) -> None:
    """Data/godzina z tego wywołania nie może przepaść, gdy wcześniej zabraknie np. usługi."""
    if turn.date_text and "date" not in turn.state and "_pending_date" not in turn.state:
        turn.state["_pending_date"] = turn.date_text
    if turn.time_text and "time" not in turn.state and "_pending_time" not in turn.state:
        turn.state["_pending_time"] = turn.time_text


async def _resolve_service(turn: _Turn) -> dict | None:
    requested = turn.service_text
    current = turn.state.get("service", {}).get("name", "").strip().lower()
    changed = requested and requested.strip().lower() != current
    if requested and ("service" not in turn.state or changed):
        found = next((s for s in turn.services if s["name"].strip().lower() == requested.strip().lower()), None)
        if not found:
            names = ", ".join(s["name"] for s in turn.services)
            return turn.ask(f"Nie rozpoznałam usługi. Dostępne: {names}.")
        if changed:
            turn.drop("staff", *_SLOT_KEYS, "_last_date_text")
        turn.state["service"] = found

    if "service" not in turn.state:
        names = natural_list([s["name"] for s in turn.services[:5]])
        return turn.ask(f"Na jaką usługę? Mamy {names}.")
    return None


async def _resolve_staff(turn: _Turn) -> dict | None:
    requested = turn.staff_text
    changed = requested and requested != turn.state.get("staff", {}).get("name", "")
    if requested and ("staff" not in turn.state or changed):
        if changed and "staff" in turn.state:
            turn.drop("staff", *_SLOT_KEYS, "_last_date_text")
        if requested == "dowolny":
            available = turn.staff_for_service()
            if available:
                turn.state["staff"] = available[0]
                has_date = turn.date_text or turn.state.get("_pending_date") or "date" in turn.state
                if not has_date:
                    return turn.ask(f"Dobrze, zapiszę do {odmien_imie(available[0]['name'])}. Na jaki dzień?")
        else:
            found = next((s for s in turn.staff_list if s["name"] == requested), None)
            if not found:
                names = ", ".join(s["name"] for s in turn.staff_list)
                return turn.ask(f"Nie rozpoznałam pracownika. Dostępni: {names}.")
            if not staff_can_do_service(found, turn.state["service"]):
                names = ", ".join(s["name"] for s in turn.staff_for_service())
                return turn.ask(
                    f"{found['name']} nie wykonuje {turn.state['service']['name']}. Tę usługę wykonują: {names}."
                )
            turn.state["staff"] = found

    if "staff" not in turn.state:
        available = turn.staff_for_service()
        if len(available) == 1:
            turn.state["staff"] = available[0]
        elif not available:
            return turn.ask(f"Przepraszam, obecnie nie mamy dostępnych pracowników do {turn.state['service']['name']}.")
        else:
            return turn.ask(f"Świetnie. Do kogo? Dostępni: {natural_list([s['name'] for s in available])}.")
    return None


def _parse_date(date_text: str) -> datetime | None:
    cleaned = preprocess_date_text(date_text)
    iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", cleaned)
    if iso:
        return datetime(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
    return dateparser.parse(cleaned, languages=["pl"], settings=DATEPARSER_SETTINGS)


async def _resolve_date(turn: _Turn) -> dict | None:
    # Termin zaproponowany przez nas (a nie podany przez klienta) nie przechodzi
    # ponownie kontroli "za daleko w przód" — sami go wybraliśmy z dozwolonego okna.
    date_from_system = False
    if not turn.date_text:
        pending = turn.state.pop("_pending_date", None)
        if pending:
            turn.date_text = pending
            date_from_system = True
    elif "_pending_date" in turn.state:
        turn.state.pop("_pending_date")

    if turn.date_text and ("date" not in turn.state or turn.date_text != turn.state.get("_last_date_text")):
        turn.state["_last_date_text"] = turn.date_text
        turn.drop("date", "time", "available_slots")
        parsed_date = _parse_date(turn.date_text)
        if not parsed_date:
            return _date_not_understood(turn)
        if (reply := await _check_date(turn, parsed_date, date_from_system)) is not None:
            return reply

    if "date" not in turn.state:
        return await _propose_first_free_slot(turn)
    return None


def _date_not_understood(turn: _Turn) -> dict:
    turn.state["_retry_date"] = turn.state.get("_retry_date", 0) + 1
    if turn.state["_retry_date"] >= 3:
        turn.drop(*_SLOT_KEYS, "_retry_date", "_last_date_text")
        return turn.ask(
            "Przepraszam za kłopot. Na jaki dzień szukamy terminu? Proszę powiedzieć np. 'jutro' lub '15 maja'."
        )
    return turn.ask("Nie zrozumiałam daty. Proszę powiedzieć np. 'jutro', 'w piątek' lub '15 maja'.")


async def _check_date(turn: _Turn, parsed_date: datetime, date_from_system: bool) -> dict | None:
    """Waliduje datę i pobiera jej wolne godziny; zwraca pytanie, gdy data nie pasuje."""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if parsed_date.date() < today.date():
        return turn.ask(f"Data {format_date_polish(parsed_date)} już minęła. Podaj przyszłą datę.")

    weekday = parsed_date.weekday()
    if get_opening_hours(turn.tenant, weekday) is None:
        date_label = format_date_polish(parsed_date).capitalize()
        return turn.ask(f"{date_label} to {POLISH_DAYS[weekday]} — jesteśmy zamknięci. Na kiedy?")

    if not date_from_system:
        is_valid, constraint_msg = validate_max_days_ahead(parsed_date, turn.tenant, turn.state["staff"])
        if not is_valid:
            return await _date_too_far(turn, parsed_date, constraint_msg)

    slots = await get_available_slots_from_api(turn.tenant, turn.state["staff"], turn.state["service"], parsed_date)
    if not slots:
        return await _date_fully_booked(turn, parsed_date)

    turn.state["date"] = parsed_date
    turn.state["available_slots"] = slots
    turn.state.pop("_retry_date", None)
    return None


async def _date_too_far(turn: _Turn, parsed_date: datetime, constraint_msg: str) -> dict:
    staff_name = odmien_imie(turn.state["staff"]["name"])
    available_days = await turn.next_available_days(limit=1)
    date_label = format_date_polish(parsed_date)
    if not available_days:
        return turn.ask(
            f"{constraint_msg} {date_label.capitalize()} to za daleko. Niestety w tym oknie nie ma wolnych terminów."
        )
    first = available_days[0]
    _offer_slot(turn, first)
    return turn.ask(
        f"{constraint_msg} {date_label.capitalize()} to za daleko. "
        f"Najbliższy wolny termin u {staff_name} "
        f"to {format_date_polish(first['date'])} o {format_hour_polish(first['slots'][0])}. Czy zapisać na ten termin?"
    )


async def _date_fully_booked(turn: _Turn, parsed_date: datetime) -> dict:
    available_days = await turn.next_available_days(limit=2)
    staff_name = odmien_imie(turn.state["staff"]["name"])
    day_label = format_date_polish(parsed_date).capitalize()
    if available_days:
        suggestion = format_availability_message(available_days)
        return turn.ask(f"{day_label} u {staff_name} nie ma wolnych terminów. {suggestion}")
    return turn.ask(
        f"{day_label} u {staff_name} nie ma wolnych terminów "
        f"i w najbliższych {turn.max_booking_days()} dniach grafik jest pełny. "
        "Nowe terminy pojawiają się codziennie — proszę spróbować jutro."
    )


async def _propose_first_free_slot(turn: _Turn) -> dict:
    staff_name = odmien_imie(turn.state["staff"]["name"])
    available_days = await turn.next_available_days(limit=1)
    if not available_days:
        return turn.ask(
            f"U {staff_name} w najbliższych {turn.max_booking_days()} dniach nie ma wolnych terminów. "
            f"{_NO_FREE_SLOTS_HINT}"
        )
    first_day = available_days[0]
    _offer_slot(turn, first_day)
    return turn.ask(
        f"U {staff_name} najbliższy wolny termin to {format_date_polish(first_day['date'])} "
        f"o {format_hour_polish(first_day['slots'][0])}. Zapisać, czy wolisz inny termin?"
    )


async def _resolve_time(turn: _Turn) -> dict | None:
    if not turn.time_text:
        turn.time_text = turn.state.pop("_pending_time", None)
    elif "_pending_time" in turn.state:
        turn.state.pop("_pending_time")

    time_text = turn.time_text
    if time_text and (
        "time" not in turn.state or normalize_time(time_text) != normalize_time(turn.state.get("time", ""))
    ):
        turn.state.pop("time", None)
        if (reply := _answer_time_of_day(turn, time_text.lower().strip())) is not None:
            return reply
        parsed_time = parse_time(time_text)
        if not parsed_time:
            return _time_not_understood(turn)
        if (reply := await _check_time(turn, parsed_time)) is not None:
            return reply

    if "time" not in turn.state:
        slots_text = slots_summary(turn.state["available_slots"])
        return turn.ask(f"{format_date_polish(turn.state['date']).capitalize()} wolne są: {slots_text}. Którą godzinę?")
    return None


def _answer_time_of_day(turn: _Turn, time_lower: str) -> dict | None:
    """ "Po południu" / "rano" — zawężamy listę wolnych godzin zamiast szukać konkretnej."""
    slots = turn.state.get("available_slots", [])
    if any(p in time_lower for p in _AFTERNOON_PHRASES):
        range_name, filtered = "po południu", [s for s in slots if int(s.split(":")[0]) >= 12]
    elif any(p in time_lower for p in _MORNING_PHRASES):
        range_name, filtered = "rano", [s for s in slots if int(s.split(":")[0]) < 12]
    else:
        return None

    if "date" not in turn.state:
        return turn.ask(f"Rozumiem, szukamy terminu {range_name}. Na jaki dzień?")
    if filtered:
        return turn.ask(f"Tak, {range_name} wolne są: {slots_summary(filtered)}. Którą godzinę wybrać?")
    all_slots = natural_list([format_hour_polish(s) for s in slots[:6]])
    return turn.ask(f"{range_name.capitalize()} zajęte. Dostępne: {all_slots}.")


def _time_not_understood(turn: _Turn) -> dict:
    turn.state["_retry_time"] = turn.state.get("_retry_time", 0) + 1
    if turn.state["_retry_time"] >= 3:
        turn.drop("time", "_pending_time", "_retry_time")
        slots_text = slots_summary(turn.state.get("available_slots", []))
        return turn.ask(f"Przepraszam za kłopot. Dostępne godziny: {slots_text}. Którą wybrać?")
    slots_text = natural_list([format_hour_polish(s) for s in turn.state["available_slots"][:6]])
    return turn.ask(f"Nie rozumiem godziny. Dostępne są: {slots_text}.")


async def _check_time(turn: _Turn, parsed_time: str) -> dict | None:
    """Sprawdza wyprzedzenie i świeżą dostępność godziny; zwraca pytanie, gdy nie pasuje."""
    hour, minute = (int(x) for x in parsed_time.split(":"))
    requested = turn.state["date"].replace(hour=hour, minute=minute, second=0, microsecond=0)
    is_advance_valid, advance_msg = validate_min_advance_hours(requested, turn.tenant, turn.state["staff"])
    if not is_advance_valid:
        return turn.ask(f"{advance_msg} Wolne są: {slots_summary(turn.state.get('available_slots', []))}.")

    is_available, current_slots = await validate_slot_available(
        turn.tenant, turn.state["staff"], turn.state["service"], turn.state["date"], parsed_time
    )
    if is_available:
        turn.state["time"] = parsed_time
        turn.state.pop("_retry_time", None)
        turn.state["available_slots"] = current_slots
        turn.time_just_set = True
        return None
    if current_slots:
        return _time_taken(turn, parsed_time, current_slots)

    turn.state.pop("date", None)
    available_days = await turn.next_available_days(limit=2)
    if available_days:
        return turn.ask(f"Na ten dzień nie ma już wolnych terminów. {format_availability_message(available_days)}")
    return turn.ask("Na ten dzień nie ma już wolnych terminów i w najbliższych dniach też jest pełny grafik.")


def _time_taken(turn: _Turn, parsed_time: str, current_slots: list[str]) -> dict:
    slots_text = slots_summary(current_slots)
    work_day = turn.state["date"].weekday()
    working_hours = get_staff_working_hours(turn.state["staff"], work_day) or get_opening_hours(turn.tenant, work_day)
    if working_hours:
        open_h, close_h = working_hours
        requested_h = int(parsed_time.split(":")[0])
        if requested_h < open_h:
            return turn.ask(f"W tym dniu pracujemy od {format_hour_polish(f'{open_h}:00')}. Wolne są: {slots_text}.")
        if requested_h >= close_h:
            return turn.ask(f"W tym dniu pracujemy do {format_hour_polish(f'{close_h}:00')}. Wolne są: {slots_text}.")
    return turn.ask(f"Godzina {format_hour_polish(parsed_time)} zajęta. Wolne: {slots_text}.")


def _strip_name_prefix(name: str) -> str:
    for prefix in _NAME_PREFIXES:
        if name.lower().startswith(prefix):
            name = name[len(prefix) :]
    return name


async def _resolve_name(turn: _Turn) -> dict | None:
    if turn.customer_name and "name" not in turn.state:
        name = _strip_name_prefix(turn.customer_name.strip())
        if len(name) < 2 or name.lower() in _NOT_A_NAME:
            not_heard = assistant_gender_forms(turn.tenant.get("assistant_name", "Ania"))["nie_dosłyszałam"]
            return turn.ask(f"{not_heard} imienia. Na jakie imię zapisać wizytę?")
        turn.state["name"] = name.title()
        turn.name_just_collected = True

    if "name" not in turn.state:
        return turn.ask(
            f"Świetnie, {format_date_polish(turn.state['date'])} o {format_hour_polish(turn.state['time'])}. "
            "Na jakie imię zapisać wizytę?"
        )
    return None


def _collect_notes(turn: _Turn) -> None:
    if turn.notes and "notes" not in turn.state:
        notes_clean = turn.notes.strip().lower().rstrip(".")
        if notes_clean not in _NO_NOTES and not notes_clean.startswith(("brak", "nie ma")):
            turn.state["notes"] = turn.notes.strip()


def _confirm(turn: _Turn) -> dict | None:
    """Podsumowanie do potwierdzenia; None = klient właśnie potwierdził, można zapisywać.

    Potwierdzeniem nie jest odpowiedź, która właśnie dostarczyła godzinę lub imię —
    klient musi najpierw usłyszeć komplet danych.
    """
    state = turn.state
    if "confirmed" in state:
        return None
    if (
        turn.confirmation not in ("no", "change")
        and not turn.name_just_collected
        and not turn.time_just_set
        and not turn.question
    ):
        state["confirmed"] = True
        return None

    if (
        turn.customer_name
        and state.get("name")
        and turn.customer_name.strip().lower() != state["name"].lower()
        and not turn.name_just_collected
    ):
        new_name = turn.customer_name.strip().title()
        for prefix in _NAME_PREFIXES:
            if new_name.lower().startswith(prefix):
                new_name = new_name[len(prefix) :].title()
        state["name"] = new_name
        return turn.ask(f"Poprawiam — na {detect_gender(new_name)} {odmien_imie(new_name)}. Zgadza się?")

    notes_part = f" Uwagi: {state['notes']}." if state.get("notes") else ""
    return turn.ask(
        f"{state['service']['name']} u {odmien_imie(state['staff']['name'])}, "
        f"{format_date_polish(state['date'])} o {format_hour_polish(state['time'])} "
        f"— na {detect_gender(state['name'])} {odmien_imie(state['name'])}.{notes_part} Zgadza się?"
    )


async def _save_booking(
    state: dict, tenant: dict, caller_phone: str, call_state: dict, channel: str = "twilio"
) -> dict:
    """Zapisuje potwierdzoną wizytę w panelu, wysyła SMS i dopisuje wizytę do CRM.

    Termin sprawdzamy jeszcze raz tuż przed zapisem, a panel dodatkowo odrzuca zajęty
    slot (409) — dwie równoległe rozmowy nie zarezerwują tej samej godziny.
    """
    logger.info("💾 [BOOKING] SAVING BOOKING...")
    try:
        is_available, current_slots = await validate_slot_available(
            tenant, state["staff"], state["service"], state["date"], state["time"]
        )
        if not is_available:
            logger.warning("❌ [BOOKING] Slot was taken between confirmation and save (re-check)")
            return _slot_lost(call_state, state, current_slots, "Ta godzina właśnie zniknęła.")

        outcome, result = await save_booking_in_panel(
            tenant,
            state["staff"],
            state["service"],
            state["date"],
            state["time"],
            state["name"],
            caller_phone,
            notes=state.get("notes", ""),
        )
        if outcome == "slot_taken":
            logger.warning("❌ [BOOKING] 409 z API mimo udanej re-walidacji — prawdziwy race condition")
            fresh_slots = await get_available_slots_from_api(tenant, state["staff"], state["service"], state["date"])
            return _slot_lost(call_state, state, fresh_slots, "Ta godzina właśnie została zajęta.")
        if outcome == "error" or not result:
            return _ask(call_state, state, "Coś poszło nie tak z zapisem. Przekazać wiadomość do właściciela?")

        sms_info = await _send_confirmation_sms(state, tenant, caller_phone, result.get("booking_code", ""), channel)
        _record_visit_in_crm(state, tenant, caller_phone)

        notes_confirm = " Uwagi zapisane." if state.get("notes") else ""
        final_text = (
            f"Gotowe. {state['service']['name']} u {odmien_imie(state['staff']['name'])}, "
            f"{format_date_polish(state['date'])} o {format_hour_polish(state['time'])}."
            f"{notes_confirm}{sms_info} {closing_question()}"
        )
        return _finish(call_state, final_text, "booked")
    except Exception as e:
        logger.error(f"💾 [BOOKING] SAVE error: {e}")
        return _ask(call_state, state, "Coś poszło nie tak. Przekazać wiadomość?")


def _slot_lost(call_state: dict, state: dict, remaining_slots: list[str], intro: str) -> dict:
    """Wybrana godzina przepadła w międzyczasie — proponujemy pozostałe albo inny dzień."""
    if remaining_slots:
        state.pop("time", None)
        state["available_slots"] = remaining_slots
        return _ask(call_state, state, f"{intro} Zostały: {slots_summary(remaining_slots)}. Którą?")
    state.pop("date", None)
    state.pop("time", None)
    return _ask(call_state, state, "Ten dzień właśnie się zapełnił. Który inny?")


async def _send_confirmation_sms(state: dict, tenant: dict, caller_phone: str, booking_code: str, channel: str) -> str:
    """Wysyła SMS z kodem wizyty; zwraca zdanie do dopowiedzenia klientowi.

    Operator SMS musi być ten sam co połączenia: Twilio wysyła tylko ze swoich numerów
    (błąd 21659 dla numeru Vonage).
    """
    if not (booking_code and caller_phone):
        return ""
    try:
        sms_func = send_booking_sms_vonage if channel == "vonage" else send_booking_sms
        sms_sent = await sms_func(
            tenant=tenant,
            customer_phone=caller_phone,
            service_name=state["service"]["name"],
            staff_name=state["staff"]["name"],
            date_str=state["date"].strftime("%d.%m"),
            time_str=state["time"],
            booking_code=booking_code,
        )
        if sms_sent:
            await increment_sms_count(tenant.get("id"))
            return " Wysłałam esemes z potwierdzeniem."
    except Exception as e:
        logger.error(f"📱 [BOOKING] SMS error: {e}")
    return " Niestety esemes nie dotarł, ale rezerwacja jest zapisana."


def _record_visit_in_crm(state: dict, tenant: dict, caller_phone: str) -> None:
    try:
        scheduled_at = f"{state['date'].strftime('%Y-%m-%d')}T{state['time'].zfill(5)}:00"
        spawn(
            save_client_visit(
                firm_id=tenant.get("id", ""),
                phone=caller_phone,
                name=state.get("name", ""),
                service=state["service"]["name"],
                staff=state["staff"]["name"],
                scheduled_at=scheduled_at,
                notes=state.get("notes", ""),
            )
        )
    except Exception as e:
        logger.warning(f"[BOOKING] CRM save_client_visit error: {e}")


def build_book_appointment_tool(
    tenant: dict, caller_phone: str, call_state: dict, context_box: dict, channel: str = "twilio"
) -> FunctionSchema:
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
        result = await book_appointment_step(params.arguments, tenant, caller_phone, call_state, context_box, channel)
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
                "type": "string",
                "enum": service_names,
                "description": "Wybierz usługę z listy która najbardziej pasuje do słów klienta",
            },
            "staff": {
                "type": "string",
                "enum": staff_names,
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
                "type": "string",
                "description": "Imię klienta lub null",
            },
            "confirmation": {
                "type": "string",
                "enum": ["yes", "no", "change", "none"],
                "description": "yes=potwierdza, no=anuluje, change=chce zmienić coś, none=nic z tych",
            },
            "change_field": {
                "type": "string",
                "enum": ["service", "staff", "date", "time", "name"],
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
