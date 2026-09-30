"""Ładowanie firmy po numerze telefonu (baza admina, potem SaaS) i mapowanie jej pól."""

import asyncio

import pytest
from conftest import _project_modules, assert_golden


def _get_tenant_by_phone():
    import bot_gemini_test  # noqa: F401

    for module in _project_modules():
        fn = vars(module).get("get_tenant_by_phone")
        if fn is not None and fn.__module__ == module.__name__:
            return fn
    raise AssertionError("get_tenant_by_phone")


FIRM = {
    "id": "firm_1",
    "user_id": "user_1",
    "name": "Salon Testowy",
    "industry": "fryzjer",
    "address": "ul. Testowa 1",
    "email": "owner@example.com",
    "phone_number": "+48 111-222-333",
    "is_active": "1",
    "is_blocked": "0",
    "booking_enabled": "1",
    "realtime_engine": None,
    "speaking_rate": None,
    "crm_api_key": "",
}

SAAS_TTS_VARIANTS = {
    "defaults": {},
    "legacy_voice_in_provider": {"tts_provider": "pl-PL-Chirp3-HD-Kore"},
    "azure_voice_in_provider": {"tts_provider": "pl-PL-ZofiaNeural"},
    "cartesia": {"tts_provider": "cartesia", "azure_voice_id": "cartesia-voice"},
    "elevenlabs_ignores_shared_voice": {
        "tts_provider": "elevenlabs",
        "voice_id": "pl-PL-Chirp3-HD-Leda",
        "elevenlabs_voice_id": "el_voice",
    },
    "legacy_voice_only": {"voice_id": "pl-PL-Chirp3-HD-Leda"},
    "explicit_openai": {"tts_provider": "openai"},
    "full_settings": {
        "contact_owner_enabled": "0",
        "transfer_enabled": "1",
        "transfer_number": "+48500",
        "human_first_enabled": "1",
        "human_first_timeout_seconds": "25",
        "siperb_sip_username": "sip-user",
        "lead_email_enabled": "1",
        "lead_email": "leads@example.com",
        "custom_report_format": "1",
        "report_empty_calls": "1",
        "transcript_email_enabled": "1",
        "crm_enabled": "1",
        "crm_provider": "pipedrive",
        "crm_domain": "firma",
        "gemini_voice": "Aoede",
        "realtime_engine": "openai",
        "elevenlabs_tts_stability": "0.7",
        "speaking_rate": "1.2",
        "booking_enabled": None,
    },
}


def _saas_rules(fake_db, firm):
    fake_db.on("FROM tenants", [], label="admin")
    fake_db.on("FROM firms WHERE", [firm], label="saas")
    fake_db.on(
        "FROM services WHERE firm_id",
        [
            {
                "id": "svc_1",
                "name": "Strzyżenie",
                "duration_minutes": "60",
                "price": "100",
                "description": "",
                "price_text": "",
                "duration_text": "",
            },
        ],
        label="saas",
    )
    fake_db.on(
        "FROM working_hours",
        [
            {"day_of_week": "1", "open_time": "09:00", "close_time": "17:00"},
            {"day_of_week": "0", "open_time": None, "close_time": None},
        ],
        label="saas",
    )
    fake_db.on("FROM faqs", [{"question": "Parking?", "answer": "Tak"}], label="saas")
    fake_db.on("FROM staff WHERE", [{"id": "staff_1", "name": "Kasia", "description": None}], label="saas")
    fake_db.on("JOIN staff_services", [{"id": "svc_1", "name": "Strzyżenie", "duration_minutes": "60", "price": "100"}])
    fake_db.on("FROM users", [{"twilio_account_sid": "AC_user", "twilio_auth_token": ""}], label="saas")


@pytest.mark.parametrize("variant", sorted(SAAS_TTS_VARIANTS))
def test_saas_tenant_mapping(fake_db, variant):
    _saas_rules(fake_db, {**FIRM, **SAAS_TTS_VARIANTS[variant]})
    tenant = asyncio.run(_get_tenant_by_phone()("+48 111 222 333"))
    assert_golden(f"tenant_saas_{variant}.json", {"tenant": tenant, "db": fake_db.calls})


def test_admin_tenant_mapping(fake_db):
    fake_db.on(
        "FROM tenants WHERE",
        [{"id": "t_1", "name": "Gabinet", "phone_number": "+48111222333", "is_active": "1", "minutes_limit": "200"}],
        label="admin",
    )
    fake_db.on(
        "FROM services",
        [{"id": "s1", "name": "Wizyta", "duration_minutes": "30", "price": "150", "description": ""}],
        label="admin",
    )
    fake_db.on("FROM working_hours", [{"day_of_week": "2", "open_time": "08:00", "close_time": "16:00"}], label="admin")
    fake_db.on("FROM tenant_faq", [], label="admin")
    fake_db.on("FROM info_services", [], label="admin")
    tenant = asyncio.run(_get_tenant_by_phone()("111222333"))
    assert_golden("tenant_admin.json", {"tenant": tenant, "db": fake_db.calls})


def test_unknown_number(fake_db):
    assert asyncio.run(_get_tenant_by_phone()("+48999999999")) is None
