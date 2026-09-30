"""Składanie pipeline'u rozmowy na websocketach (Twilio/Vonage × Gemini Live/OpenAI
Realtime/ElevenLabs): jakie procesory, w jakiej kolejności, z jakim promptem, narzędziami,
głosem i częstotliwością audio. PipelineRunner/PipelineTask są podmienione, więc żadna
prawdziwa sesja z dostawcą AI nie startuje — testujemy wyłącznie konfigurację."""

from __future__ import annotations

import json

import pytest
from conftest import _project_modules, assert_golden, make_booking_tenant, make_tenant, patch_everywhere
from fastapi.testclient import TestClient

TENANT_PHONE = "+48111222333"
CALLER = "+48600700800"


def _original(name):
    for module in _project_modules():
        obj = vars(module).get(name)
        if obj is not None and getattr(obj, "__module__", "") == module.__name__:
            return obj
    raise AssertionError(name)


def _describe_processor(proc) -> dict | str:
    name = type(proc).__name__
    if name in ("PipelineSource", "PipelineSink"):
        return None
    if hasattr(proc, "_pipelines"):
        return {name: [[d for d in (_describe_processor(p) for p in sub._processors) if d] for sub in proc._pipelines]}
    info: dict = {}
    params = getattr(proc, "_params", None)
    serializer = getattr(params, "serializer", None)
    if serializer is not None:
        info["serializer"] = type(serializer).__name__
        info["add_wav_header"] = getattr(params, "add_wav_header", None)
    controller = getattr(proc, "_vad_controller", None)
    analyzer = getattr(controller, "_vad_analyzer", None) or getattr(proc, "_vad_analyzer", None)
    if analyzer is not None:
        info["vad"] = {k: getattr(analyzer.params, k) for k in ("confidence", "start_secs", "stop_secs", "min_volume")}
    return {name: info} if info else name


class Capture:
    def __init__(self):
        self.tasks: list[dict] = []
        self.llm_builds: list[dict] = []
        self.finalize: list[str] = []


@pytest.fixture
def capture(monkeypatch, tenants, fake_db):
    fake_db.on("SELECT balance FROM credits", [{"balance": "50"}])
    cap = Capture()

    class FakeTask:
        def __init__(self, pipeline, params=None, **kwargs):
            cap.tasks.append(
                {
                    "processors": [d for d in (_describe_processor(p) for p in pipeline._processors) if d],
                    "params": params.model_dump(exclude_unset=True),
                }
            )

        async def queue_frame(self, frame):
            pass

        async def queue_frames(self, frames):
            pass

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, task):
            return None

    patch_everywhere(monkeypatch, "PipelineTask", FakeTask)
    patch_everywhere(monkeypatch, "PipelineRunner", FakeRunner)

    for builder in ("build_gemini_live_llm", "build_realtime_llm"):
        original = _original(builder)

        def recording(system_prompt, tools=None, _original=original, _name=builder, **kwargs):
            cap.llm_builds.append(
                {
                    "builder": _name,
                    "system_prompt": system_prompt,
                    "tools": [t.name for t in (tools or [])],
                    **kwargs,
                }
            )
            return _original(system_prompt, tools=tools, **kwargs)

        patch_everywhere(monkeypatch, builder, recording)

    for name in ("save_call_transcript", "maybe_send_call_summary"):

        async def record(*args, _name=name, **kwargs):
            cap.finalize.append(_name)

        patch_everywhere(monkeypatch, name, record)

    async def no_profile(*args, **kwargs):
        return None

    patch_everywhere(monkeypatch, "get_client_profile", no_profile)
    return cap


@pytest.fixture
def client():
    from bot_gemini_test import app

    with TestClient(app, base_url="https://bot.test") as c:
        yield c


def _twilio_session(client, path):
    with client.websocket_connect(path) as ws:
        ws.send_text(json.dumps({"event": "connected"}))
        ws.send_text(
            json.dumps(
                {
                    "event": "start",
                    "start": {
                        "streamSid": "MZ1",
                        "customParameters": {"phone": TENANT_PHONE, "callerPhone": CALLER, "callSid": "CA1"},
                    },
                }
            )
        )


def _vonage_session(client, path):
    query = f"phone={TENANT_PHONE.lstrip('+')}&callerPhone={CALLER.lstrip('+')}&callSid=uuid-1"
    query += "&regionUrl=https%3A%2F%2Fapi-eu-3.vonage.com"
    with client.websocket_connect(f"{path}?{query}"):
        pass


def _golden(name, cap):
    assert_golden(name, {"tasks": cap.tasks, "llm_builds": cap.llm_builds, "finalize": cap.finalize})


TENANT_VARIANTS = {
    "basic": lambda: make_tenant(tts_provider="openai"),
    "full": lambda: make_booking_tenant(
        tts_provider="openai",
        transfer_enabled=1,
        transfer_number="+48500600700",
        gemini_voice="Aoede",
        realtime_voice="marin",
        speaking_rate=1.2,
    ),
    "no_contact_owner": lambda: make_tenant(tts_provider="openai", contact_owner_enabled=0),
}


@pytest.mark.parametrize("variant", sorted(TENANT_VARIANTS))
@pytest.mark.parametrize(
    "transport,path",
    [
        ("twilio", "/ws-gemini-live-test"),
        ("vonage", "/ws-gemini-live-test-vonage"),
        ("twilio", "/ws-gemini-test"),
        ("vonage", "/ws-gemini-test-vonage"),
    ],
)
def test_engine_websocket_pipeline(client, tenants, capture, variant, transport, path):
    tenants[TENANT_PHONE] = TENANT_VARIANTS[variant]()
    (_twilio_session if transport == "twilio" else _vonage_session)(client, path)
    _golden(f"ws{path.replace('/', '_')}_{variant}.json", capture)


def test_elevenlabs_vonage_bridge_pipeline(client, tenants, capture):
    tenants[TENANT_PHONE] = make_tenant(realtime_engine="elevenlabs")
    _vonage_session(client, "/ws-elevenlabs-vonage")
    _golden("ws_elevenlabs_vonage.json", capture)


def test_websocket_rejects_unknown_tenant(client, tenants, capture):
    _vonage_session(client, "/ws-gemini-live-test-vonage")
    _twilio_session(client, "/ws-gemini-live-test")
    _golden("ws_unknown_tenant.json", capture)
