"""Webhooki HTTP: Twilio, Vonage i ElevenLabs — odpowiedzi (TwiML/NCCO/JSON) oraz
efekty uboczne (zapytania SQL, wywołania e-mail/CRM) porównywane z wzorcami."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from conftest import assert_golden, make_booking_tenant, make_tenant, patch_everywhere

TENANT_PHONE = "+48111222333"
CALLER = "+48600700800"


@pytest.fixture
def client():
    from bot_gemini_test import app

    with TestClient(app, base_url="https://bot.test") as c:
        yield c


class Recorder:
    """Asynchroniczny zamiennik funkcji, który zapamiętuje argumenty wywołań."""

    def __init__(self, result=None):
        self.calls: list[dict] = []
        self.result = result

    async def __call__(self, *args, **kwargs):
        self.calls.append({"args": list(args), "kwargs": kwargs})
        return self.result


@pytest.fixture
def credits_ok(fake_db):
    fake_db.on("SELECT balance FROM credits", [{"balance": "50"}])
    return fake_db


def _response(resp):
    content_type = resp.headers.get("content-type", "")
    body = resp.json() if "json" in content_type else resp.text
    return {"status": resp.status_code, "content_type": content_type, "body": body}


# --------------------------------------------------------------------------
# Twilio
# --------------------------------------------------------------------------

def _twilio_incoming(client, path="/twilio/incoming-gemini-live-test"):
    return client.post(path, data={"Called": TENANT_PHONE, "From": CALLER, "CallSid": "CA123"})


@pytest.mark.parametrize("engine", ["gemini", "openai"])
def test_twilio_incoming_streams_to_engine_websocket(client, tenants, credits_ok, engine):
    tenants[TENANT_PHONE] = make_tenant(realtime_engine=engine)
    assert_golden(f"twilio_incoming_{engine}.json", _response(_twilio_incoming(client)))


def test_twilio_incoming_elevenlabs_registers_call(client, tenants, credits_ok, monkeypatch):
    tenants[TENANT_PHONE] = make_tenant(
        realtime_engine="elevenlabs", elevenlabs_voice_id="voice_x", elevenlabs_tts_speed=1.1,
    )
    captured = {}

    class FakeTwilio:
        def register_call(self, **kwargs):
            captured.update(kwargs)
            return "<Response>elevenlabs</Response>"

    class FakeClient:
        class conversational_ai:  # noqa: N801 — kształt API SDK ElevenLabs
            twilio = FakeTwilio()

    patch_everywhere(monkeypatch, "_get_elevenlabs_client", lambda: FakeClient())
    resp = _twilio_incoming(client)
    assert_golden("twilio_incoming_elevenlabs.json", {"response": _response(resp), "register_call": captured})


def test_twilio_incoming_elevenlabs_failure_says_error(client, tenants, credits_ok, monkeypatch):
    tenants[TENANT_PHONE] = make_tenant(realtime_engine="elevenlabs")

    def boom():
        raise RuntimeError("elevenlabs down")

    patch_everywhere(monkeypatch, "_get_elevenlabs_client", boom)
    assert_golden("twilio_incoming_elevenlabs_error.json", _response(_twilio_incoming(client)))


def test_twilio_incoming_unknown_number(client, tenants, fake_db):
    assert_golden("twilio_incoming_unknown.json", _response(_twilio_incoming(client)))


def test_twilio_incoming_without_credits(client, tenants, fake_db):
    fake_db.on("SELECT balance FROM credits", [{"balance": "0.1"}])
    tenants[TENANT_PHONE] = make_tenant()
    assert_golden("twilio_incoming_no_credits.json", _response(_twilio_incoming(client)))


def test_twilio_incoming_openai_legacy_route(client, tenants, credits_ok):
    tenants[TENANT_PHONE] = make_tenant(realtime_engine="openai")
    resp = _twilio_incoming(client, "/twilio/incoming-gemini-test")
    assert_golden("twilio_incoming_openai_legacy.json", _response(resp))


def test_twilio_status_completed_charges_call(client, tenants, fake_db):
    tenants[TENANT_PHONE] = make_tenant()
    fake_db.on("SELECT user_id FROM firms", [{"user_id": "user_1"}])
    fake_db.on("SELECT balance FROM credits", [{"balance": "40"}])
    fake_db.on("SELECT minutes_used, minutes_limit FROM firms", [{"minutes_used": "10", "minutes_limit": "100"}])
    resp = client.post("/twilio/status", data={
        "CallSid": "CA999", "CallStatus": "completed", "CallDuration": "95",
        "To": TENANT_PHONE, "From": CALLER,
    })
    assert_golden("twilio_status_completed.json", {"response": _response(resp), "db": fake_db.calls})


def test_twilio_status_in_progress_is_ignored(client, tenants, fake_db):
    resp = client.post("/twilio/status", data={"CallSid": "CA999", "CallStatus": "in-progress"})
    assert_golden("twilio_status_in_progress.json", {"response": _response(resp), "db": fake_db.calls})


# --------------------------------------------------------------------------
# Vonage — answer
# --------------------------------------------------------------------------

def _vonage_answer(client, path="/vonage/answer-gemini-live", **extra):
    params = {"to": TENANT_PHONE.lstrip("+"), "from": CALLER.lstrip("+"), "uuid": "uuid-1",
              "region_url": "https://api-eu-3.vonage.com", **extra}
    return client.get(path, params=params)


@pytest.mark.parametrize("engine", ["gemini", "openai"])
def test_vonage_answer_connects_websocket(client, tenants, credits_ok, engine):
    tenants[TENANT_PHONE] = make_tenant(realtime_engine=engine)
    assert_golden(f"vonage_answer_{engine}.json", _response(_vonage_answer(client)))


@pytest.mark.parametrize("sip_ready", [True, False])
def test_vonage_answer_elevenlabs(client, tenants, credits_ok, monkeypatch, sip_ready):
    tenants[TENANT_PHONE] = make_tenant(realtime_engine="elevenlabs", elevenlabs_agent_id="agent_tenant")
    ensure = Recorder(result=sip_ready)
    patch_everywhere(monkeypatch, "ensure_elevenlabs_sip_number", ensure)
    resp = _vonage_answer(client)
    assert_golden(
        f"vonage_answer_elevenlabs_sip_{sip_ready}.json",
        {"response": _response(resp), "ensure_sip_calls": ensure.calls},
    )


@pytest.mark.parametrize("sip_username", ["siperb-firma", ""])
def test_vonage_answer_human_first(client, tenants, credits_ok, sip_username):
    tenants[TENANT_PHONE] = make_tenant(
        human_first_enabled=1, siperb_sip_username=sip_username, human_first_timeout_seconds=20,
    )
    assert_golden(f"vonage_answer_human_first_{bool(sip_username)}.json", _response(_vonage_answer(client)))


def test_vonage_answer_unknown_and_blocked(client, tenants, fake_db):
    unknown = _response(_vonage_answer(client))
    tenants[TENANT_PHONE] = make_tenant(is_blocked=1)
    blocked = _response(_vonage_answer(client))
    assert_golden("vonage_answer_unknown_blocked.json", {"unknown": unknown, "blocked": blocked})


def test_vonage_answer_openai_legacy_route(client, tenants, credits_ok):
    tenants[TENANT_PHONE] = make_tenant(realtime_engine="openai")
    assert_golden("vonage_answer_openai_legacy.json", _response(_vonage_answer(client, "/vonage/answer")))


# --------------------------------------------------------------------------
# Vonage — zdarzenia i fallbacki
# --------------------------------------------------------------------------

def test_vonage_events_completed_inbound(client, tenants, fake_db):
    tenants[TENANT_PHONE] = make_tenant()
    fake_db.on("SELECT id FROM call_logs", [{"id": "call_1"}])
    fake_db.on("SELECT user_id FROM firms", [{"user_id": "user_1"}])
    fake_db.on("SELECT balance FROM credits", [{"balance": "0.2"}])
    fake_db.on("SELECT minutes_used, minutes_limit FROM firms", [{"minutes_used": "99.5", "minutes_limit": "100"}])
    resp = client.post("/vonage/events", json={
        "status": "completed", "uuid": "uuid-1", "duration": "61",
        "to": TENANT_PHONE.lstrip("+"), "from": CALLER.lstrip("+"), "direction": "inbound",
    })
    assert_golden("vonage_events_completed.json", {"response": _response(resp), "db": fake_db.calls})


def test_vonage_events_new_admin_call_log(client, tenants, fake_db):
    tenants[TENANT_PHONE] = make_tenant(id="tenant_admin", source="admin")
    fake_db.on("SELECT minutes_used, minutes_limit FROM tenants", [{"minutes_used": "5", "minutes_limit": "100"}])
    resp = client.get("/vonage/events", params={
        "status": "completed", "uuid": "uuid-2", "duration": "30", "to": TENANT_PHONE, "from": "",
    })
    assert_golden("vonage_events_admin.json", {"response": _response(resp), "db": fake_db.calls})


def test_vonage_events_skips_outbound_and_other_statuses(client, tenants, fake_db):
    tenants[TENANT_PHONE] = make_tenant()
    outbound = client.post("/vonage/events", json={
        "status": "completed", "uuid": "u", "to": TENANT_PHONE, "direction": "outbound",
    })
    ringing = client.post("/vonage/events", json={"status": "ringing", "uuid": "u"})
    assert_golden("vonage_events_skipped.json", {
        "outbound": _response(outbound), "ringing": _response(ringing), "db": fake_db.calls,
    })


def test_vonage_transfer_fallback(client, monkeypatch):
    email = Recorder(result=True)
    patch_everywhere(monkeypatch, "send_missed_transfer_email", email)
    resp = client.get("/vonage/transfer-fallback", params={
        "businessName": "Salon Testowy", "callerPhone": CALLER, "ownerEmail": "owner@example.com",
    })
    assert_golden("vonage_transfer_fallback.json", {"response": _response(resp), "email": email.calls})


@pytest.mark.parametrize("status", ["answered", "failed"])
def test_vonage_sip_fallback_elevenlabs(client, status):
    ws_uri = "wss://bot.test/ws-elevenlabs-vonage?phone=48111222333"
    with_uri = client.post(f"/vonage/sip-fallback-elevenlabs?wsUri={ws_uri}", json={"status": status})
    without_uri = client.post("/vonage/sip-fallback-elevenlabs", content=b"not json")
    assert_golden(f"vonage_sip_fallback_{status}.json", {
        "with_uri": _response(with_uri), "without_uri": _response(without_uri),
    })


def test_vonage_human_first_fallback(client, tenants, credits_ok):
    tenants[TENANT_PHONE] = make_tenant()
    params = {"to": TENANT_PHONE, "from": CALLER, "uuid": "uuid-3", "regionUrl": "https://api-eu-3.vonage.com"}
    known = client.post("/vonage/human-first-fallback", params=params, json={"status": "timeout"})
    unknown = client.post("/vonage/human-first-fallback", params={**params, "to": "+48999999999"})
    assert_golden("vonage_human_first_fallback.json", {"known": _response(known), "unknown": _response(unknown)})


def test_vonage_human_first_recording(client, tenants, monkeypatch):
    tenants[TENANT_PHONE] = make_tenant()
    process = Recorder()
    patch_everywhere(monkeypatch, "process_human_first_recording", process)
    params = {"to": TENANT_PHONE, "from": CALLER, "uuid": "uuid-4"}
    ok = client.post("/vonage/human-first-recording", params=params, json={
        "recording_url": "https://api.nexmo.com/rec/1",
        "start_time": "2026-09-30T10:00:00Z", "end_time": "2026-09-30T10:02:05Z",
    })
    no_url = client.post("/vonage/human-first-recording", params=params, json={})
    assert_golden("vonage_human_first_recording.json", {
        "ok": _response(ok), "no_url": _response(no_url), "process": process.calls,
    })


def test_vonage_test_siperb(client, tenants, credits_ok):
    tenants[TENANT_PHONE] = make_tenant()
    other = client.get("/vonage/test-siperb", params={"to": TENANT_PHONE.lstrip("+"), "from": CALLER, "uuid": "u5"})
    bizvoice = client.get("/vonage/test-siperb", params={"to": "48459050542", "from": CALLER})
    event = client.post("/vonage/test-siperb-event", json={"status": "answered"})
    assert_golden("vonage_test_siperb.json", {
        "other": _response(other), "bizvoice": _response(bizvoice), "event": _response(event),
    })


def test_health_endpoints(client):
    assert_golden("health.json", {
        "gemini": client.get("/health-gemini-live-test").json(),
        "openai": client.get("/health-gemini-test").json(),
    })


# --------------------------------------------------------------------------
# ElevenLabs
# --------------------------------------------------------------------------

def test_elevenlabs_personalization(client, tenants, credits_ok):
    tenants[TENANT_PHONE] = make_booking_tenant(realtime_engine="elevenlabs", elevenlabs_voice_id="voice_y")
    known = client.post("/elevenlabs/personalization", json={
        "called_number": TENANT_PHONE, "caller_id": CALLER, "call_sid": "SCL_1",
    })
    unknown = client.post("/elevenlabs/personalization", json={"called_number": "+48999999999"})
    assert_golden("elevenlabs_personalization.json", {"known": known.json(), "unknown": unknown.json()})


def test_elevenlabs_contact_owner_tool(client, tenants, monkeypatch):
    email = Recorder(result=True)
    save_name = Recorder()
    patch_everywhere(monkeypatch, "send_message_email", email)
    patch_everywhere(monkeypatch, "maybe_save_contact_name", save_name)
    body = {"customer_name": "Jan", "message": "Proszę o oddzwonienie w sprawie koloryzacji na sobotę",
            "called_number": TENANT_PHONE, "caller_phone": CALLER, "conversation_id": "conv_1"}

    tenants[TENANT_PHONE] = make_tenant(contact_owner_closing_line="Przekażę wiadomość.")
    sent = client.post("/elevenlabs/tools/contact_owner", json=body).json()
    tenants[TENANT_PHONE] = make_tenant(lead_email_enabled=1)
    deferred = client.post("/elevenlabs/tools/contact_owner", json=body).json()
    tenants[TENANT_PHONE] = make_tenant(contact_owner_enabled=0)
    disabled = client.post("/elevenlabs/tools/contact_owner", json=body).json()
    vague = client.post("/elevenlabs/tools/contact_owner", json={**body, "message": "wiadomość"}).json()
    missing = client.post("/elevenlabs/tools/contact_owner", json={**body, "customer_name": ""}).json()

    assert_golden("elevenlabs_contact_owner.json", {
        "sent": sent, "deferred": deferred, "disabled": disabled, "vague": vague, "missing": missing,
        "emails": email.calls, "saved_names": len(save_name.calls),
    })


def test_elevenlabs_post_call(client, tenants, fake_db, monkeypatch):
    tenants[TENANT_PHONE] = make_tenant(lead_email_enabled=1, transcript_email_enabled=1)
    recorders = {
        "summarize_conversation_lines": Recorder(result="Priorytet: 🟡 STANDARDOWE\nKto dzwonił: Jan"),
        "send_call_summary_email": Recorder(result=True),
        "persist_call_summary": Recorder(),
        "maybe_send_to_crm": Recorder(),
        "_send_push_notifications": Recorder(),
        "send_message_email": Recorder(result=True),
    }
    for name, recorder in recorders.items():
        patch_everywhere(monkeypatch, name, recorder)
    patch_everywhere(monkeypatch, "_processed_post_call_sids", set())
    patch_everywhere(monkeypatch, "_elevenlabs_call_states", {
        "conv_9": {"pending_contact_owner": {"customer_name": "Jan", "message": "Oddzwonić"}},
    })
    payload = {"data": {
        "conversation_id": "conv_9", "status": "done",
        "metadata": {"call_duration_secs": 42},
        "analysis": {"transcript_summary": "fallback"},
        "conversation_initiation_client_data": {"dynamic_variables": {
            "called_number": TENANT_PHONE, "caller_phone": CALLER, "call_sid": "uuid-9",
        }},
        "transcript": [
            {"role": "agent", "message": "Dzień dobry, tu Salon Testowy."},
            {"role": "user", "message": "Chciałbym się umówić."},
            {"role": "user", "message": ""},
        ],
    }}
    first = client.post("/elevenlabs/post-call", content=json.dumps(payload)).json()
    duplicate = client.post("/elevenlabs/post-call", content=json.dumps(payload)).json()
    invalid = client.post("/elevenlabs/post-call", content=b"{").json()
    assert_golden("elevenlabs_post_call.json", {
        "first": first, "duplicate": duplicate, "invalid": invalid, "db": fake_db.calls,
        "calls": {name: r.calls for name, r in recorders.items()},
    })


@pytest.mark.parametrize("tool", ["book_appointment", "manage_booking"])
def test_elevenlabs_booking_tools_delegate(client, tenants, monkeypatch, tool):
    tenants[TENANT_PHONE] = make_booking_tenant()
    handler = Recorder(result={"status": "ask", "say": "Na jaką usługę?"})
    patch_everywhere(monkeypatch, f"_handle_{tool}", handler)
    patch_everywhere(monkeypatch, "_elevenlabs_call_states", {})
    resp = client.post(f"/elevenlabs/tools/{tool}", json={
        "conversation_id": "conv_b", "called_number": TENANT_PHONE, "caller_phone": CALLER,
        "channel": "vonage", "service": "strzyżenie", "action": "cancel",
    })
    call = handler.calls[0]
    assert_golden(f"elevenlabs_tool_{tool}.json", {
        "response": resp.json(), "args": call["args"][0], "caller": call["args"][2],
        "kwargs": call["kwargs"],
    })
