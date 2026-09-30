"""Wspólne fixtures testów.

Testy są "charakteryzujące" (golden master): zapisują obecne zachowanie aplikacji
(odpowiedzi webhooków, treść promptu, schematy narzędzi, zapytania SQL) do plików w
tests/golden/ i przy każdym kolejnym uruchomieniu sprawdzają, że nic się nie zmieniło.
Świeży zapis wzorców: `UPDATE_GOLDEN=1 pytest`.

Izolacja od świata zewnętrznego jest twarda:
  * zmienne środowiskowe z sekretami są nadpisywane PRZED importem aplikacji, więc
    lokalny .env (z prawdziwymi kluczami) nigdy nie jest używany w testach;
  * każde połączenie sieciowe (socket.connect) kończy się wyjątkiem;
  * baza Turso jest podmieniona na FakeDB, która tylko zapisuje zapytania.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = Path(__file__).resolve().parent / "golden"

_TEST_ENV = {
    "OPENAI_API_KEY": "test-openai",
    "GOOGLE_API_KEY": "test-google",
    "ELEVENLABS_API_KEY": "test-elevenlabs",
    "ELEVENLABS_AGENT_ID": "agent_env_default",
    "ELEVENLABS_SHARED_SECRET": "",
    "DEEPGRAM_API_KEY": "",
    "RESEND_API_KEY": "",
    "TWILIO_ACCOUNT_SID": "",
    "TWILIO_AUTH_TOKEN": "",
    "VONAGE_API_KEY": "",
    "VONAGE_API_SECRET": "",
    "VONAGE_APPLICATION_ID": "",
    "VONAGE_PRIVATE_KEY": "",
    "VAPID_PRIVATE_KEY": "",
    "GOOGLE_CLIENT_ID": "",
    "GOOGLE_CLIENT_SECRET": "",
    "GOOGLE_APPLICATION_CREDENTIALS_JSON": "",
    "TURSO_DATABASE_URL": "libsql://admin.test",
    "TURSO_AUTH_TOKEN": "test",
    "SAAS_TURSO_DATABASE_URL": "libsql://saas.test",
    "SAAS_TURSO_AUTH_TOKEN": "test",
    "ENCRYPTION_KEY": "",
    "PANEL_URL": "",
    "PANEL_API_URL": "http://panel.test",
    "INTERNAL_API_SECRET": "",
    "TEST_TENANT_ID": "",
    "N8N_CRM_WEBHOOK_URL": "",
}
os.environ.update(_TEST_ENV)
sys.path.insert(0, str(ROOT))


# --------------------------------------------------------------------------
# Sieć
# --------------------------------------------------------------------------

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Blokuje wszystko poza loopbackiem (asyncio na Windows sam łączy się z 127.0.0.1)."""
    for method in ("connect", "connect_ex"):
        original = getattr(socket.socket, method)

        def guarded(self, address, _original=original):
            host = address[0] if isinstance(address, tuple) else address
            if host not in _LOOPBACK:
                raise RuntimeError(f"Połączenie sieciowe zablokowane w testach: {address}")
            return _original(self, address)

        monkeypatch.setattr(socket.socket, method, guarded)


# --------------------------------------------------------------------------
# Podmiana nazw we wszystkich modułach projektu
# --------------------------------------------------------------------------

_ROOT_PREFIX = os.path.normcase(str(ROOT)) + os.sep


def _project_modules():
    for module in list(sys.modules.values()):
        path = os.path.normcase(getattr(module, "__file__", None) or "")
        if path.startswith(_ROOT_PREFIX) and ".venv" not in path and os.sep + "tests" + os.sep not in path:
            yield module


def patch_everywhere(monkeypatch, name: str, value: Any) -> int:
    """Podmienia `name` w KAŻDYM module projektu, który ma taki atrybut.

    Dzięki temu testy nie zależą od tego, w którym pliku funkcja jest zdefiniowana
    ani skąd jest importowana — przeżywają przenoszenie kodu między modułami.
    """
    count = 0
    for module in _project_modules():
        if name in vars(module):
            monkeypatch.setattr(module, name, value)
            count += 1
    assert count, f"Nie znaleziono '{name}' w żadnym module projektu"
    return count


# --------------------------------------------------------------------------
# Baza danych
# --------------------------------------------------------------------------

_RANDOM_ID = re.compile(r"^(tr|contact_\d+)_[0-9a-f]{6,12}$")


def _stable(value: Any) -> Any:
    """Losowe sufiksy identyfikatorów (uuid4/urandom) zastępuje stałym znacznikiem."""
    if isinstance(value, str) and _RANDOM_ID.match(value):
        return value.rsplit("_", 1)[0] + "_<random>"
    return value


class FakeDB:
    """Zapisuje zapytania i zwraca wiersze wg prostych reguł (fragment SQL -> wiersze)."""

    def __init__(self):
        self.calls: list[dict] = []
        self.rules: list[tuple[str, str | None, list[dict]]] = []

    def on(self, sql_fragment: str, rows: list[dict], label: str | None = None) -> None:
        self.rules.append((sql_fragment, label, rows))

    def make_execute(self, label: str):
        async def execute(sql: str, args: list | None = None) -> list[dict]:
            normalized = re.sub(r"\s+", " ", sql).strip()
            self.calls.append({"db": label, "sql": normalized, "args": [_stable(a) for a in args or []]})
            for fragment, rule_label, rows in self.rules:
                if fragment in normalized and rule_label in (None, label):
                    return [dict(r) for r in rows]
            return []

        return execute


@pytest.fixture
def fake_db(monkeypatch):
    import bot_gemini_test  # noqa: F401  — ładuje całą aplikację, żeby instancje DB istniały

    fake = FakeDB()
    seen = set()
    for module in _project_modules():
        for obj in vars(module).values():
            if type(obj).__name__ == "TursoDB" and id(obj) not in seen:
                seen.add(id(obj))
                monkeypatch.setattr(obj, "execute", fake.make_execute(obj.label))
    assert seen, "Nie znaleziono instancji TursoDB"
    return fake


# --------------------------------------------------------------------------
# Czas
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _frozen_time():
    from freezegun import freeze_time

    with freeze_time("2026-09-30 10:15:00", tz_offset=0):
        yield


# --------------------------------------------------------------------------
# Tenanci
# --------------------------------------------------------------------------


def make_tenant(**overrides) -> dict:
    """Tenant w kształcie zwracanym przez helpers._get_tenant_from_saas()."""
    tenant = {
        "id": "firm_test_1",
        "slug": "firm_test_1",
        "source": "saas",
        "name": "Salon Testowy",
        "business_name": "Salon Testowy",
        "industry": "salon fryzjerski",
        "address": "ul. Testowa 1, Kraków",
        "email": "owner@example.com",
        "phone_number": "+48111222333",
        "user_id": "user_1",
        "assistant_name": "Ania",
        "first_message": "Dzień dobry, tu Salon Testowy. W czym mogę pomóc?",
        "additional_info": "Parking za budynkiem.",
        "tts_provider": "google",
        "azure_voice_id": "pl-PL-Chirp3-HD-Aoede",
        "elevenlabs_voice_id": None,
        "elevenlabs_agent_voice_id": "",
        "elevenlabs_tts_stability": None,
        "elevenlabs_tts_speed": None,
        "elevenlabs_tts_similarity_boost": None,
        "speaking_rate": 1.06,
        "realtime_voice": "",
        "gemini_voice": "",
        "gemini_native_voice_enabled": 0,
        "realtime_engine": "gemini",
        "is_active": 1,
        "is_blocked": 0,
        "minutes_used": 0.0,
        "minutes_limit": 100,
        "booking_enabled": 0,
        "transfer_enabled": 0,
        "transfer_number": "",
        "human_first_enabled": 0,
        "human_first_timeout_seconds": 15,
        "siperb_sip_username": "",
        "contact_owner_enabled": 1,
        "custom_report_format": 0,
        "contact_owner_closing_line": "",
        "report_empty_calls": 0,
        "transcript_email_enabled": 0,
        "notification_email": "owner@example.com",
        "lead_email_enabled": 0,
        "lead_email": "",
        "lead_mode": 0,
        "lead_triggers": "",
        "lead_collection": "",
        "lead_urgency_mode": 0,
        "lead_urgency_text": "",
        "recording_enabled": 0,
        "llm_provider": "groq",
        "llm_model": "",
        "crm_enabled": 0,
        "crm_provider": "",
        "crm_domain": "",
        "crm_api_key": "",
        "services": [
            {
                "id": "svc_1",
                "name": "Strzyżenie damskie",
                "duration_minutes": 60,
                "price": 120,
                "description": "Mycie, strzyżenie, modelowanie",
                "price_text": "",
                "duration_text": "",
            },
            {
                "id": "svc_2",
                "name": "Koloryzacja",
                "duration_minutes": 120,
                "price": 250,
                "description": "",
                "price_text": "od 250 zł",
                "duration_text": "ok. 2h",
            },
        ],
        "working_hours": [{"day_of_week": d, "open_time": "09:00", "close_time": "18:00"} for d in range(0, 5)]
        + [{"day_of_week": 5, "open_time": "10:00", "close_time": "14:00"}],
        "faq": [{"question": "Czy mogę zapłacić kartą?", "answer": "Tak, akceptujemy karty."}],
        "info_services": [],
        "staff": [],
    }
    tenant["info_services"] = tenant["services"]
    tenant.update(overrides)
    return tenant


def make_booking_tenant(**overrides) -> dict:
    staff = [
        {
            "id": "staff_1",
            "firm_id": "firm_test_1",
            "name": "Kasia Nowak",
            "position": "Fryzjerka",
            "description": "",
            "google_connected": 1,
            "google_calendar_id": "cal_1",
            "working_hours_json": json.dumps({"1": {"start": "09:00", "end": "17:00"}}),
            "min_advance_hours": 2,
            "max_days_ahead": 30,
            "services": [{"id": "svc_1", "name": "Strzyżenie damskie", "duration_minutes": 60, "price": 120}],
        }
    ]
    return make_tenant(booking_enabled=1, staff=staff, **overrides)


@pytest.fixture
def tenants(monkeypatch):
    """Rejestr tenantów po numerze telefonu; podmienia get_tenant_by_phone wszędzie."""
    registry: dict[str, dict] = {}

    async def fake_get_tenant_by_phone(phone: str):
        digits = re.sub(r"\D", "", phone or "")[-9:]
        for number, tenant in registry.items():
            if re.sub(r"\D", "", number)[-9:] == digits and digits:
                return tenant
        return None

    import bot_gemini_test  # noqa: F401

    patch_everywhere(monkeypatch, "get_tenant_by_phone", fake_get_tenant_by_phone)

    async def no_crm_name(firm_id: str, phone: str) -> str:
        return ""

    patch_everywhere(monkeypatch, "get_crm_contact_name", no_crm_name)
    return registry


# --------------------------------------------------------------------------
# Golden master
# --------------------------------------------------------------------------


def _to_text(value: Any) -> str:
    if isinstance(value, str):
        return value if value.endswith("\n") else value + "\n"
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"


def assert_golden(name: str, value: Any) -> None:
    path = GOLDEN_DIR / name
    text = _to_text(value)
    if os.environ.get("UPDATE_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return
    assert path.exists(), f"Brak wzorca {path.relative_to(ROOT)} — uruchom z UPDATE_GOLDEN=1"
    expected = path.read_text(encoding="utf-8")
    assert text == expected, f"Wynik różni się od wzorca {path.relative_to(ROOT)}"
