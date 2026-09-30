"""Scenariusze rozmowy rezerwacyjnej (narzędzia book_appointment i manage_booking).

Każdy scenariusz to seria wywołań narzędzia tak, jak robi to model w trakcie rozmowy.
Podmieniony jest tylko brzeg systemu (API panelu, SMS, CRM, dodatkowe pytanie do GPT) —
cała logika walidacji dat, godzin, pracowników i dostępności działa naprawdę.
"Dziś" to środa 2026-09-30, 10:15 (patrz conftest).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest
from conftest import assert_golden, make_booking_tenant, patch_everywhere

CALLER = "+48600700800"

# Wolne sloty w API panelu wg daty (brak daty w słowniku = DEFAULT_SLOTS).
SCHEDULE = {
    "2026-09-30": ["16:00"],
    "2026-10-01": ["09:00", "10:00", "13:00", "15:30"],
    "2026-10-02": [],
    "2026-10-03": ["10:00", "11:00"],
}
DEFAULT_SLOTS = ["09:00", "12:00"]


def _tenant():
    tenant = make_booking_tenant()
    kasia = tenant["staff"][0]
    kasia["services"].append({"id": "svc_2", "name": "Koloryzacja", "duration_minutes": 120, "price": 250})
    tomek = {
        "id": "staff_2",
        "firm_id": "firm_test_1",
        "name": "Tomek",
        "position": "",
        "description": "",
        "google_connected": 1,
        "working_hours_json": "",
        "min_advance_hours": 2,
        "max_days_ahead": 30,
        "services": [{"id": "svc_1", "name": "Strzyżenie damskie", "duration_minutes": 60, "price": 120}],
    }
    tenant["staff"] = [kasia, tomek]
    return tenant


class Edge:
    """Zamienniki zewnętrznych zależności z zapisem wywołań."""

    def __init__(
        self,
        monkeypatch,
        *,
        schedule=SCHEDULE,
        save_outcome=("ok", {"booking_code": "4321"}),
        slots_disappear_on_save=False,
        bookings=None,
    ):
        import bot_gemini_test  # noqa: F401 — ładuje moduły aplikacji przed podmianą

        self.calls: list = []
        self.schedule = schedule
        self.slot_checks = 0
        self.slots_disappear_on_save = slots_disappear_on_save

        async def slots_from_api(tenant, staff, service, date):
            key = date.strftime("%Y-%m-%d")
            if self.slots_disappear_on_save and self.saving:
                return []
            return list(self.schedule.get(key, DEFAULT_SLOTS)) if self.schedule is not None else []

        async def save_booking(tenant, staff, service, date, time, name, phone, notes=""):
            self.calls.append(["save_booking_in_panel", staff["name"], service["name"], str(date), time, name, notes])
            return save_outcome

        def recorder(name, result=None):
            async def record(*args, **kwargs):
                self.calls.append([name, kwargs or [str(a) for a in args[1:]]])
                return result

            return record

        async def general_answer(question, tenant, context_box):
            self.calls.append(["answer_general_question", question])
            return "Parking jest za budynkiem."

        async def list_bookings(tenant, phone):
            return bookings or []

        self.saving = False
        patch_everywhere(monkeypatch, "get_available_slots_from_api", slots_from_api)
        patch_everywhere(monkeypatch, "save_booking_in_panel", save_booking)
        patch_everywhere(monkeypatch, "send_booking_sms", recorder("send_booking_sms", True))
        patch_everywhere(monkeypatch, "send_booking_sms_vonage", recorder("send_booking_sms_vonage", True))
        patch_everywhere(monkeypatch, "increment_sms_count", recorder("increment_sms_count"))
        patch_everywhere(monkeypatch, "save_client_visit", recorder("save_client_visit"))
        patch_everywhere(monkeypatch, "_answer_general_question", general_answer)
        patch_everywhere(monkeypatch, "closing_question", lambda: "Czy mogę jeszcze w czymś pomóc?")


def _book_step():
    from conftest import _project_modules

    import bot_gemini_test  # noqa: F401

    for module in _project_modules():
        fn = vars(module).get("book_appointment_step")
        if fn is not None and fn.__module__ == module.__name__:
            return fn
    raise AssertionError("book_appointment_step")


def _run(edge: Edge, steps: list[dict], *, channel="twilio", tenant=None) -> dict:
    tenant = tenant or _tenant()
    call_state: dict = {}
    transcript = []

    async def scenario():
        step = _book_step()
        for args in steps:
            edge.saving = args.get("confirmation") == "yes"
            result = await step(args, tenant, CALLER, call_state, {"context": None}, channel)
            await asyncio.sleep(0)  # zadania w tle (zapis wizyty w CRM)
            transcript.append({"args": args, "result": result})

    asyncio.run(scenario())
    state = call_state.get("booking", {})
    return json.loads(
        json.dumps(
            {
                "transcript": transcript,
                "final_state": {k: (v["name"] if isinstance(v, dict) and "name" in v else v) for k, v in state.items()},
                "edge_calls": edge.calls,
            },
            default=str,
        )
    )


N = {"confirmation": "none"}

SCENARIOS = {
    "happy_path_step_by_step": [
        N,
        {**N, "service": "Strzyżenie damskie"},
        {**N, "staff": "Kasia Nowak"},
        {**N, "date_text": "2026-10-01"},
        {**N, "time_text": "10:00"},
        {**N, "customer_name": "pani anna"},
        {"confirmation": "yes"},
    ],
    "all_in_one_then_confirm": [
        {
            **N,
            "service": "Koloryzacja",
            "staff": "Kasia Nowak",
            "date_text": "2026-10-01",
            "time_text": "13:00",
            "customer_name": "Jan",
            "notes": "Proszę o kawę",
        },
        {"confirmation": "yes"},
    ],
    "accept_suggested_first_slot": [
        {**N, "service": "Strzyżenie damskie", "staff": "dowolny"},
        {"confirmation": "yes"},
        {**N, "customer_name": "Ola"},
        {"confirmation": "yes"},
    ],
    "cancel_without_and_with_booking": [
        {"confirmation": "no"},
        {**N, "service": "Strzyżenie damskie"},
        {"confirmation": "no"},
    ],
    "change_fields": [
        {**N, "service": "Strzyżenie damskie", "staff": "Tomek", "date_text": "2026-10-01", "time_text": "09:00"},
        {"confirmation": "change", "change_field": "time", "time_text": "13:00"},
        {"confirmation": "change", "change_field": "date"},
        {"confirmation": "change", "date_text": "2026-10-03"},
        {"confirmation": "change", "change_field": "staff"},
        {"confirmation": "change", "change_field": "service"},
        {"confirmation": "change", "change_field": "name"},
        {"confirmation": "change"},
    ],
    "questions": [
        {**N, "question": "kiedy wolne terminy?"},
        {**N, "question": "Gdzie można zaparkować?"},
        {**N, "service": "Strzyżenie damskie", "staff": "Kasia Nowak"},
        {**N, "question": "jakie terminy są wolne?"},
    ],
    "unknown_service_staff_and_mismatch": [
        {**N, "service": "Masaż"},
        {**N, "service": "Koloryzacja", "staff": "Zbyszek"},
        {**N, "staff": "Tomek"},
        {**N, "staff": "dowolny", "date_text": "jutro"},
    ],
    "date_problems": [
        {**N, "service": "Strzyżenie damskie", "staff": "Kasia Nowak", "date_text": "2026-09-01"},
        {**N, "date_text": "2026-10-04"},
        {**N, "date_text": "2026-12-24"},
        {**N, "date_text": "2026-10-02"},
        {**N, "date_text": "blabla"},
        {**N, "date_text": "coś"},
        {**N, "date_text": "hmm"},
        {**N, "date_text": "w sobotę"},
    ],
    "time_problems": [
        {**N, "service": "Strzyżenie damskie", "staff": "Kasia Nowak", "date_text": "2026-10-01"},
        {**N, "time_text": "po południu"},
        {**N, "time_text": "rano"},
        {**N, "time_text": "07:00"},
        {**N, "time_text": "20:00"},
        {**N, "time_text": "11:00"},
        {**N, "time_text": "xyz"},
        {**N, "time_text": "abc"},
        {**N, "time_text": "qwe"},
        {**N, "time_text": "wpół do dziesiątej"},
    ],
    "same_day_min_advance": [
        {**N, "service": "Strzyżenie damskie", "staff": "Kasia Nowak", "date_text": "dzisiaj", "time_text": "11:00"},
        {**N, "time_text": "16:00"},
    ],
    "name_and_notes": [
        {**N, "service": "Strzyżenie damskie", "staff": "Tomek", "date_text": "2026-10-01", "time_text": "15:30"},
        {**N, "customer_name": "tak"},
        {**N, "customer_name": "Marek", "notes": "brak uwag"},
        {**N, "customer_name": "pan Piotr"},
        {"confirmation": "yes"},
    ],
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_book_appointment_scenario(monkeypatch, name):
    edge = Edge(monkeypatch)
    assert_golden(f"booking/{name}.json", _run(edge, SCENARIOS[name]))


FULL_BOOKING = [
    {
        **N,
        "service": "Strzyżenie damskie",
        "staff": "Tomek",
        "date_text": "2026-10-01",
        "time_text": "10:00",
        "customer_name": "Ewa",
    },
    {"confirmation": "yes"},
]


@pytest.mark.parametrize("variant", ["vonage_sms", "slot_taken_409", "panel_error", "slots_gone_on_recheck"])
def test_book_appointment_saving(monkeypatch, variant):
    kwargs = {
        "vonage_sms": {},
        "slot_taken_409": {"save_outcome": ("slot_taken", None)},
        "panel_error": {"save_outcome": ("error", None)},
        "slots_gone_on_recheck": {"slots_disappear_on_save": True},
    }[variant]
    edge = Edge(monkeypatch, **kwargs)
    channel = "vonage" if variant == "vonage_sms" else "twilio"
    assert_golden(f"booking/save_{variant}.json", _run(edge, FULL_BOOKING, channel=channel))


def test_book_appointment_fully_booked(monkeypatch):
    edge = Edge(monkeypatch, schedule=None)
    steps = [
        {**N, "service": "Strzyżenie damskie", "staff": "Kasia Nowak"},
        {**N, "question": "kiedy wolne terminy?"},
        {**N, "date_text": "2026-10-01"},
    ]
    assert_golden("booking/fully_booked.json", _run(edge, steps))


def test_datetime_is_frozen():
    assert datetime.now().strftime("%Y-%m-%d %H:%M") == "2026-09-30 10:15"
