"""Treść system promptu i powitania — najważniejszy "produkt" backendu: każda zmiana
słowa zmienia zachowanie asystenta na żywym telefonie, więc refaktor musi dawać
identyczny tekst bajt w bajt."""

import pytest
from conftest import assert_golden, make_booking_tenant, make_tenant


def _prompt_module():
    from conftest import _project_modules

    import bot_gemini_test  # noqa: F401

    for module in _project_modules():
        if hasattr(module, "build_realtime_instructions") and hasattr(module, "build_role_prompt"):
            return module
    raise AssertionError("Nie znaleziono modułu z build_realtime_instructions")


CLIENT_PROFILE = {
    "name": "Anna Kowalska",
    "visit_count": 3,
    "upcoming_visits": [{"scheduled_at": "2026-10-02T14:00:00Z", "service": "Koloryzacja", "staff": "Kasia"}],
    "past_visits": [{"scheduled_at": "2026-08-01T10:00:00Z", "service": "Strzyżenie damskie", "staff": "Kasia"}],
    "notes": "Lubi kawę",
}

VARIANTS = {
    "basic": dict(tenant=make_tenant()),
    "no_contact_owner": dict(tenant=make_tenant(contact_owner_enabled=0), has_contact_owner=False),
    "transfer": dict(tenant=make_tenant(transfer_enabled=1), has_transfer=True),
    "booking": dict(tenant=make_booking_tenant(), has_booking=True),
    "booking_crm_no_greeting": dict(
        tenant=make_booking_tenant(),
        client_profile=CLIENT_PROFILE,
        include_greeting=False,
        has_booking=True,
    ),
    "male_assistant_gym": dict(tenant=make_tenant(assistant_name="Marek", industry="siłownia")),
    "minimal_tenant": dict(
        tenant=make_tenant(
            services=[],
            faq=[],
            working_hours=[],
            address="",
            industry="",
            additional_info="",
            first_message="",
        )
    ),
}


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_realtime_instructions(variant):
    module = _prompt_module()
    kwargs = dict(VARIANTS[variant])
    tenant = kwargs.pop("tenant")
    client_profile = kwargs.pop("client_profile", None)
    assert_golden(f"prompt_{variant}.txt", module.build_realtime_instructions(tenant, client_profile, **kwargs))


def test_greeting_messages():
    module = _prompt_module()
    assert_golden(
        "greetings.json",
        {
            "default": module.build_greeting_message(make_tenant()),
            "no_first_message": module.build_greeting_message(make_tenant(first_message="")),
        },
    )


@pytest.mark.parametrize("has_contact_owner", [True, False])
def test_known_caller_hint(has_contact_owner):
    module = _prompt_module()
    result = module.append_known_caller_hint("PROMPT", "Jan Nowak", has_contact_owner=has_contact_owner)
    assert_golden(f"known_caller_hint_{has_contact_owner}.txt", result)
