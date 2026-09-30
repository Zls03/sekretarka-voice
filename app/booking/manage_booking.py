"""Narzędzie manage_booking — odwołanie lub przełożenie wcześniej umówionej wizyty."""

import re
from datetime import datetime

import dateparser
from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.services.llm_service import FunctionCallParams

from app.booking.availability import slots_summary
from app.booking.panel_api import cancel_booking_in_panel, get_available_slots_from_api, reschedule_booking_in_panel
from app.booking.parsing import (
    DATEPARSER_SETTINGS,
    normalize_time,
    parse_iso_datetime,
    parse_time,
    preprocess_date_text,
)
from app.booking.replies import closing_question
from app.panel_client import get_client_profile
from app.polish.formatting import format_date_polish, format_hour_polish, natural_list
from app.polish.grammar import detect_gender, odmien_imie


def _describe_booking(b: dict) -> str:
    dt = parse_iso_datetime(b["scheduled_at"])
    staff_part = f" u {odmien_imie(b['staff'])}" if b.get("staff") else ""
    return f"{b.get('service') or 'wizyta'}{staff_part}, {format_date_polish(dt)} o {format_hour_polish(dt.strftime('%H:%M'))}"


def _match_booking_by_text(bookings: list[dict], text: str) -> int | None:
    """Dopasowuje wskazaną przez klienta wizytę po dacie (gdy ma kilka nadchodzących)."""
    if not text:
        return None
    parsed = dateparser.parse(preprocess_date_text(text), languages=["pl"], settings=DATEPARSER_SETTINGS)
    if not parsed:
        return None
    for i, b in enumerate(bookings):
        if parse_iso_datetime(b["scheduled_at"]).date() == parsed.date():
            return i
    return None


def _ask_mgmt(call_state: dict, state: dict, text: str) -> dict:
    call_state["manage_booking"] = state
    return {"status": "ask", "say_exactly": text, "done": False}


def _finish_mgmt(call_state: dict, text: str, status: str) -> dict:
    call_state["manage_booking"] = {}
    return {"status": status, "say_exactly": text, "done": True}


async def manage_booking_step(args: dict, tenant: dict, caller_phone: str, call_state: dict) -> dict:
    action = args.get("action")
    new_date_text = args.get("date_text")
    new_time_text = args.get("time_text")
    confirmation = args.get("confirmation", "none")
    which_text = args.get("which_visit")

    state = call_state.get("manage_booking", {})

    logger.info(
        f"📥 [MANAGE_BOOKING] action={action}, date={new_date_text}, time={new_time_text}, "
        f"confirm={confirmation}, which={which_text}"
    )

    # === 1. ZNAJDŹ WIZYTĘ(Y) PO NUMERZE — tylko raz na rozmowę o zarządzaniu ===
    if "bookings" not in state:
        profile = await get_client_profile(tenant.get("id", ""), caller_phone)
        candidates = [v for v in ((profile or {}).get("upcoming_visits") or []) if v.get("booking_id")]
        if not candidates:
            return _finish_mgmt(
                call_state,
                "Nie widzę żadnej nadchodzącej wizyty przypisanej do tego numeru telefonu. "
                "Mogę przekazać wiadomość właścicielowi — proszę powiedzieć, czego dokładnie potrzeba.",
                "not_found",
            )
        state["bookings"] = candidates

    bookings = state["bookings"]

    # === OBSŁUGA REZYGNACJI Z CAŁEJ OPERACJI (nie mylić z "no" jako odpowiedzią na inne pytanie) ===
    if confirmation == "no" and ("selected" in state or "pending_action" in state):
        return _finish_mgmt(call_state, "Dobrze, zostawiam wizytę bez zmian. W czym jeszcze mogę pomóc?", "no_op")

    # === 2. WYBIERZ KTÓRĄ WIZYTĘ (gdy klient ma kilka nadchodzących) ===
    if "selected" not in state:
        if len(bookings) == 1:
            state["selected"] = 0
        else:
            match_idx = _match_booking_by_text(bookings, which_text) if which_text else None
            if match_idx is not None:
                state["selected"] = match_idx
            else:
                options = natural_list([_describe_booking(b) for b in bookings])
                return _ask_mgmt(
                    call_state, state, f"Widzę kilka nadchodzących wizyt: {options}. Której z nich dotyczy?"
                )

    booking = bookings[state["selected"]]
    booking_desc = _describe_booking(booking)

    # === 3. CO KLIENT CHCE ZROBIĆ ===
    if action not in ("cancel", "reschedule"):
        # Klient mógł tylko zapytać "czy mam wizytę" bez chęci zmiany czegokolwiek — informuj,
        # nie zakładaj z góry akcji. Bez "Pan/Pani" ze slashem (TTS czyta to dosłownie jako
        # "pan ukośnik pani" — złapane na żywym telefonie), zdanie bezpłciowe jak wszędzie
        # indziej w prompcie (patrz FORMA ZWRACANIA SIĘ w realtime_prompt.py).
        # Imię z SAMEJ rezerwacji (customer_name), nie z ogólnego profilu klienta — może się
        # różnić (ktoś dzwoni z domowego numeru i pyta o wizytę innego domownika). Tylko tutaj,
        # NIE w liście do rozróżnienia kilku wizyt (_describe_booking) — tam ten sam dzwoniący
        # więc powtarzanie identycznego imienia przy każdej pozycji byłoby zbędne.
        name_part = ""
        if booking.get("customer_name"):
            name_part = f" — na {detect_gender(booking['customer_name'])} {odmien_imie(booking['customer_name'])}"
        return _ask_mgmt(
            call_state,
            state,
            f"Tak, jest zaplanowana wizyta: {booking_desc}{name_part}. Czy chodzi o zmianę tego terminu?",
        )

    # === 4A. ANULOWANIE ===
    if action == "cancel":
        if confirmation != "yes":
            state["pending_action"] = "cancel"
            return _ask_mgmt(call_state, state, f"Potwierdzam odwołanie wizyty — {booking_desc}. Zgadza się?")
        ok = await cancel_booking_in_panel(tenant, booking["booking_id"])
        if ok:
            return _finish_mgmt(call_state, f"Gotowe, wizyta została odwołana. {closing_question()}", "cancelled")
        return _finish_mgmt(
            call_state,
            "Nie udało się automatycznie odwołać wizyty — przekażę to właścicielowi. Proszę powiedzieć, czego dotyczy sprawa.",
            "error",
        )

    # === 4B. PRZEŁOŻENIE — reużywa walidacji daty/godziny z book_appointment ===
    staff_obj = next((s for s in tenant.get("staff", []) if s["name"] == booking.get("staff")), None)
    service_obj = next((s for s in tenant.get("services", []) if s["name"] == booking.get("service")), None)
    if not staff_obj or not service_obj:
        return _finish_mgmt(
            call_state,
            "Nie mogę automatycznie przełożyć tej wizyty — przekażę wiadomość właścicielowi. Proszę powiedzieć, na kiedy przełożyć.",
            "error",
        )

    if not new_date_text and not new_time_text:
        state["pending_action"] = "reschedule"
        return _ask_mgmt(call_state, state, f"Na jaki termin przełożyć wizytę — {booking_desc}?")

    if not new_date_text:
        # Model nie odsyła pól ustalonych w poprzednich turach (patrz book_appointment) — jeśli data
        # była już podana i sparsowana wcześniej w TEJ operacji przełożenia, użyj jej ponownie.
        # Jeśli w ogóle nie padła (klient powiedział tylko nową godzinę, np. "przełóż na 13:00")
        # zakładamy że chodzi o TEN SAM dzień co obecna wizyta — dopytywanie o dzień gdy z
        # kontekstu jasno wynika o którą wizytę chodzi brzmiało nienaturalnie na żywym telefonie.
        new_date_text = state.get("_new_date") or booking["scheduled_at"][:10]

    date_text_clean = preprocess_date_text(new_date_text)
    _iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", date_text_clean)
    parsed_date = (
        datetime(int(_iso.group(1)), int(_iso.group(2)), int(_iso.group(3)))
        if _iso
        else dateparser.parse(date_text_clean, languages=["pl"], settings=DATEPARSER_SETTINGS)
    )

    if not parsed_date:
        return _ask_mgmt(
            call_state, state, "Nie zrozumiałam daty. Proszę powiedzieć np. 'jutro', 'w piątek' lub '15 maja'."
        )
    if parsed_date.date() < datetime.now().date():
        return _ask_mgmt(call_state, state, f"Data {format_date_polish(parsed_date)} już minęła. Podaj przyszłą datę.")

    slots = await get_available_slots_from_api(tenant, staff_obj, service_obj, parsed_date)
    if not slots:
        return _ask_mgmt(
            call_state,
            state,
            f"{format_date_polish(parsed_date).capitalize()} nie ma wolnych terminów. Na jaki inny dzień?",
        )

    state["_new_date"] = parsed_date.strftime("%Y-%m-%d")
    state["_new_slots"] = slots

    if not new_time_text:
        # Tak samo jak przy dacie — jeśli godzina była już podana wcześniej w tej operacji
        # (np. klient teraz tylko potwierdza "tak"), użyj jej ponownie zamiast pytać od nowa.
        if "_new_time" in state:
            new_time_text = state["_new_time"]
        else:
            return _ask_mgmt(
                call_state,
                state,
                f"{format_date_polish(parsed_date).capitalize()} wolne są: {slots_summary(slots)}. Którą godzinę?",
            )

    parsed_time = parse_time(new_time_text)
    slots_normalized = [normalize_time(s) for s in slots]
    if not parsed_time or normalize_time(parsed_time) not in slots_normalized:
        return _ask_mgmt(call_state, state, f"Ta godzina jest zajęta. Wolne są: {slots_summary(slots)}. Którą wybrać?")

    if confirmation != "yes":
        state["_new_time"] = parsed_time
        new_date_obj = datetime.strptime(state["_new_date"], "%Y-%m-%d")
        # Krótko — pełny opis wizyty (usługa/pracownik) już padł raz przy identyfikacji,
        # powtarzanie go w każdej turze brzmiało sztywno/robotycznie na żywym telefonie.
        return _ask_mgmt(
            call_state,
            state,
            f"Dobrze, przekładamy wizytę na {format_date_polish(new_date_obj)}, na {format_hour_polish(parsed_time)}. Zgadza się?",
        )

    new_date_obj = datetime.strptime(state["_new_date"], "%Y-%m-%d")
    ok = await reschedule_booking_in_panel(tenant, booking["booking_id"], new_date_obj, parsed_time)
    if ok:
        return _finish_mgmt(
            call_state,
            f"Gotowe. Wizyta przełożona na {format_date_polish(new_date_obj)} o {format_hour_polish(parsed_time)}. {closing_question()}",
            "rescheduled",
        )
    return _finish_mgmt(
        call_state,
        "Nie udało się automatycznie przełożyć wizyty — przekażę to właścicielowi. Proszę powiedzieć, na kiedy chce Pan/Pani przełożyć.",
        "error",
    )


def build_manage_booking_tool(tenant: dict, caller_phone: str, call_state: dict) -> FunctionSchema:
    """FunctionSchema do odwoływania/przekładania wizyty umówionej WCZEŚNIEJ (inna rozmowa) —
    warunkowo dołączane tak samo jak book_appointment (ten sam booking_enabled + staff gate,
    patrz bot_gemini_test.py). Nie wymaga osobnego call_state klucza poza "manage_booking"
    (analogicznie do "booking" dla book_appointment) — oba mogą współistnieć w jednej rozmowie."""

    async def handle_manage_booking(params: FunctionCallParams):
        result = await manage_booking_step(params.arguments, tenant, caller_phone, call_state)
        await params.result_callback(result)

    return FunctionSchema(
        name="manage_booking",
        description="""Klient chce ODWOŁAĆ lub PRZEŁOŻYĆ wizytę którą umówił WCZEŚNIEJ (nie w trakcie
tej rozmowy — do nowej rezerwacji lub zmiany PRZED zapisaniem służy book_appointment). Użyj gdy klient
mówi: "chcę odwołać wizytę", "muszę przełożyć termin", "nie mogę przyjść", "zmiana terminu wizyty".
⛔ NIE wywołuj tego narzędzia gdy klient TYLKO pyta "czy mam jakąś wizytę" / "kiedy mam wizytę" bez
chęci czegokolwiek zmieniać — na to odpowiadasz OD RAZU z danych w sekcji INFO O KLIENCIE (CRM) w
promptcie systemowym, bez żadnego wywołania narzędzia. Wywołaj manage_booking dopiero gdy klient
wyraźnie chce ODWOŁAĆ lub PRZEŁOŻYĆ.
⛔ Wizytę znajdujemy PO NUMERZE TELEFONU dzwoniącego automatycznie — NIE pytaj klienta o kod
rezerwacji, żadne ID, ani na jakie IMIĘ jest rezerwacja (to też niepotrzebne, samo "na jakie imię
wizyta?" brzmi jak typowy odruch recepcjonistki, ale tu numer w zupełności wystarczy) — po prostu
wywołaj to narzędzie od razu z tym co klient powiedział, bez żadnego dopytywania na wstępie.
⛔ KRYTYCZNE: wynik niesie pole "say_exactly" — Twoja odpowiedź MUSI być tą treścią słowo w słowo,
bez zmian i dodatków — dotyczy to dat, godzin i potwierdzeń tak samo jak w book_appointment.
Jeśli status="not_found" lub "error" — po powiedzeniu say_exactly, jeśli klient odpowie z treścią
sprawy, wywołaj contact_owner żeby przekazać wiadomość właścicielowi.
Wywołuj przy KAŻDEJ kolejnej odpowiedzi klienta dotyczącej tej sprawy, aż wynik będzie miał "done": true.""",
        properties={
            "action": {
                "type": "string",
                "enum": ["cancel", "reschedule", "none"],
                "description": "cancel=odwołanie wizyty, reschedule=przełożenie na inny termin, none=jeszcze nie wiadomo",
            },
            "date_text": {
                "type": "string",
                "description": "Nowa data (tylko dla reschedule) w formacie YYYY-MM-DD lub naturalny tekst klienta. Null jeśli nie dotyczy.",
            },
            "time_text": {
                "type": "string",
                "description": "Nowa godzina (tylko dla reschedule) w formacie HH:MM. Null jeśli nie dotyczy.",
            },
            "confirmation": {
                "type": "string",
                "enum": ["yes", "no", "none"],
                "description": "yes=klient potwierdza ostatnio zaproponowaną akcję, no=rezygnuje, none=jeszcze nic",
            },
            "which_visit": {
                "type": "string",
                "description": "Jeśli klient ma kilka nadchodzących wizyt i wskazał, o którą chodzi (np. wspomniał dzień) — przekaż to tutaj. Null w innym wypadku.",
            },
        },
        required=["action", "confirmation"],
        handler=handle_manage_booking,
    )
