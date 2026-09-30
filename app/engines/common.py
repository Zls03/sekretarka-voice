"""Klocki wspólne dla silników opartych o Pipecat (Gemini Live, OpenAI Realtime).

Jedno źródło prawdy dla tego, co wcześniej było skopiowane w każdym websockecie:
które funkcje są dostępne w rozmowie, jakie narzędzia dostaje model, jak powstaje
prompt, jak wygląda transport audio i co dzieje się po rozłączeniu.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import WebSocket
from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADAnalyzer, VADParams
from pipecat.pipeline.task import PipelineParams
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.serializers.vonage import VonageFrameSerializer
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport

from app.booking.book_appointment import build_book_appointment_tool
from app.booking.manage_booking import build_manage_booking_tool
from app.call_logs import save_call_transcript
from app.crm_contacts import get_crm_contact_name
from app.post_call.report import maybe_send_call_summary
from app.prompt.instructions import append_known_caller_hint, build_realtime_instructions
from app.tenants import get_tenant_by_phone
from app.tools.contact_owner import build_contact_owner_tool
from app.tools.end_conversation import build_end_conversation_tool
from app.tools.transfer import build_transfer_tool

Channel = Literal["twilio", "vonage"]

# Progi nadzoru rozmowy — wspólne dla silników Pipecat, strojone razem na żywych rozmowach.
IDLE_WARNING_SECONDS = 6    # cisza -> "czy nadal jesteśmy połączeni?"
IDLE_HANGUP_SECONDS = 14    # cisza -> pożegnanie i rozłączenie
MAX_CALL_DURATION = 4 * 60  # twardy limit długości rozmowy

# Flaga suppress_idle_reset (komunikat skryptowy nie resetuje zegara ciszy) wygasa sama po
# tym czasie — przerwana w pół słowa wypowiedź może nie wygenerować TTSStoppedFrame.
SCRIPTED_UTTERANCE_SUPPRESS_SECONDS = 8.0

# Twilio Media Streams przesyła mu-law 8 kHz, Vonage — surowe PCM 16-bit 16 kHz.
SAMPLE_RATES: dict[Channel, int] = {"twilio": 8000, "vonage": 16000}


def is_booking_available(tenant: dict) -> bool:
    """Rezerwacje wymagają włączenia w panelu ORAZ pracownika z kalendarzem Google i usługami.

    Brak pola booking_enabled oznacza "wyłączone". Ta sama bramka obowiązuje we wszystkich
    silnikach, żeby identyczna konfiguracja firmy dawała identyczne zachowanie.
    """
    return tenant.get("booking_enabled") == 1 and any(
        s.get("google_connected") and len(s.get("services", [])) > 0
        for s in tenant.get("staff", [])
    )


@dataclass(frozen=True)
class CallFeatures:
    """Funkcje dostępne w danej rozmowie — sterują zarówno listą narzędzi, jak i promptem."""

    contact_owner: bool
    booking: bool
    transfer: bool

    @classmethod
    def for_tenant(cls, tenant: dict, *, transfer_supported: bool = False) -> CallFeatures:
        """transfer_supported: żywy transfer działa tylko na Vonage (wymaga uuid połączenia)."""
        return cls(
            contact_owner=tenant.get("contact_owner_enabled", 1) == 1,
            booking=is_booking_available(tenant),
            transfer=transfer_supported and tenant.get("transfer_enabled", 0) == 1,
        )


def build_call_tools(
    tenant: dict,
    caller_phone: str,
    features: CallFeatures,
    *,
    call_state: dict,
    task_box: dict,
    context_box: dict,
    channel: Channel,
    call_sid: str | None = None,
    region_url: str | None = None,
    host: str | None = None,
) -> list[FunctionSchema]:
    """Narzędzia function-calling dla rozmowy; kolejność ma znaczenie dla modelu, nie zmieniać."""
    tools = []
    if features.contact_owner:
        tools.append(build_contact_owner_tool(
            tenant, caller_phone, task_box, call_state, has_transfer_tool=features.transfer,
        ))
    tools.append(build_end_conversation_tool(task_box, call_state))
    if features.booking:
        tools.append(build_book_appointment_tool(tenant, caller_phone, call_state, context_box, channel=channel))
        tools.append(build_manage_booking_tool(tenant, caller_phone, call_state))
    if features.transfer:
        tools.append(build_transfer_tool(
            tenant, call_sid, call_state, region_url, caller_phone=caller_phone, host=host,
        ))
    return tools


async def build_call_prompt(
    tenant: dict,
    caller_phone: str,
    features: CallFeatures,
    *,
    client_profile: dict | None = None,
    include_greeting: bool = True,
) -> str:
    """System prompt rozmowy + podpowiedź o znanym rozmówcy z kartoteki /crm."""
    prompt = build_realtime_instructions(
        tenant, client_profile, include_greeting=include_greeting,
        has_transfer=features.transfer, has_booking=features.booking,
        has_contact_owner=features.contact_owner,
    )
    known_name = await get_crm_contact_name(tenant.get("id", ""), caller_phone)
    if known_name:
        prompt = append_known_caller_hint(prompt, known_name, has_contact_owner=features.contact_owner)
    return prompt


def create_transport(
    websocket: WebSocket,
    channel: Channel,
    *,
    stream_sid: str | None = None,
    vad_analyzer: VADAnalyzer | None = None,
) -> FastAPIWebsocketTransport:
    """Transport audio websocketu dla danego operatora.

    vad_analyzer jest przekazywany tylko przez ścieżkę OpenAI Realtime; w pipecat 1.4
    FastAPIWebsocketParams nie ma już tego pola (jest ignorowane) — lokalny VAD to
    osobny procesor, patrz create_local_vad().
    """
    extra: dict[str, Any] = {"vad_analyzer": vad_analyzer} if vad_analyzer else {}
    if channel == "twilio":
        serializer = TwilioFrameSerializer(
            stream_sid=stream_sid,
            params=TwilioFrameSerializer.InputParams(auto_hang_up=False),
        )
        params = FastAPIWebsocketParams(
            audio_in_enabled=True, audio_out_enabled=True, **extra, serializer=serializer,
        )
    else:
        serializer = VonageFrameSerializer(
            params=VonageFrameSerializer.InputParams(vonage_sample_rate=SAMPLE_RATES["vonage"]),
        )
        params = FastAPIWebsocketParams(
            audio_in_enabled=True, audio_out_enabled=True, add_wav_header=False, **extra, serializer=serializer,
        )
    return FastAPIWebsocketTransport(websocket=websocket, params=params)


def create_local_vad() -> VADProcessor:
    """Lokalny VAD (Silero) — natychmiastowy sygnał "klient mówi", niezależny od dostawcy AI.

    stop_secs=0.2 to próg, pod który pipecat kalibruje swoje szacunki latencji.
    """
    return VADProcessor(
        vad_analyzer=SileroVADAnalyzer(
            params=VADParams(confidence=0.6, start_secs=0.2, stop_secs=0.2, min_volume=0.4)
        )
    )


def call_pipeline_params(channel: Channel) -> PipelineParams:
    # allow_interruptions: w pipecat 1.4 przerwania są zawsze włączone, a pole jest
    # ignorowane — zostaje dla zgodności ze starszymi wersjami biblioteki.
    sample_rate = SAMPLE_RATES[channel]
    return PipelineParams(
        allow_interruptions=True,
        enable_metrics=True,
        audio_in_sample_rate=sample_rate,
        audio_out_sample_rate=sample_rate,
    )


@dataclass(frozen=True)
class TwilioStreamStart:
    stream_sid: str
    tenant: dict
    caller_phone: str
    call_sid: str | None


async def read_twilio_stream_start(websocket: WebSocket, log_tag: str) -> TwilioStreamStart | None:
    """Czyta zdarzenia Twilio Media Streams do "start" i ustala firmę z parametrów strumienia.

    Zwraca None (po zamknięciu websocketu), gdy start się nie powiódł lub firmy nie ma.
    """
    stream_sid = None
    tenant = None
    caller_phone = "nieznany"
    call_sid = None
    try:
        while True:
            data = json.loads(await websocket.receive_text())
            event = data.get("event")
            if event == "connected":
                continue
            if event == "start":
                start_data = data.get("start", {})
                stream_sid = start_data.get("streamSid")
                custom_params = start_data.get("customParameters", {})
                tenant_phone = custom_params.get("phone")
                caller_phone = custom_params.get("callerPhone", "nieznany")
                call_sid = custom_params.get("callSid")
                if tenant_phone:
                    tenant = await get_tenant_by_phone(tenant_phone)
                break
    except Exception as e:
        logger.error(f"[{log_tag}] Błąd startu: {e}")
        await websocket.close()
        return None

    if not stream_sid or not tenant:
        logger.error(f"❌ [{log_tag}] Brak stream_sid lub tenant — zamykam")
        await websocket.close()
        return None
    return TwilioStreamStart(stream_sid, tenant, caller_phone, call_sid)


@dataclass(frozen=True)
class VonageStreamStart:
    tenant: dict
    tenant_phone: str
    caller_phone: str
    call_sid: str | None
    region_url: str | None


async def accept_vonage_stream(websocket: WebSocket, log_tag: str) -> VonageStreamStart | None:
    """Vonage przekazuje dane połączenia w query stringu websocketu (patrz telephony/ncco.py).

    Zwraca None (po zamknięciu websocketu), gdy brakuje numeru firmy lub firmy nie ma.
    """
    tenant_phone = websocket.query_params.get("phone")
    caller_phone = websocket.query_params.get("callerPhone", "nieznany")
    call_sid = websocket.query_params.get("callSid")
    region_url = websocket.query_params.get("regionUrl") or None
    if not tenant_phone:
        logger.error(f"❌ [{log_tag}] Brak phone w query params — zamykam")
        await websocket.close()
        return None

    await websocket.accept()
    logger.info(f"🔌 [{log_tag}] WebSocket connected, phone={tenant_phone}")

    tenant = await get_tenant_by_phone(tenant_phone)
    if not tenant:
        logger.error(f"❌ [{log_tag}] Nie znaleziono tenanta — zamykam")
        await websocket.close()
        return None
    return VonageStreamStart(tenant, tenant_phone, caller_phone, call_sid, region_url)


async def finalize_call(
    tenant: dict, call_sid: str | None, caller_phone: str, context: LLMContext, call_state: dict, log_tag: str,
) -> None:
    """Po rozłączeniu: zapis transkryptu, potem podsumowanie/raport.

    Kolejność ma znaczenie — save_call_transcript tworzy wiersz call_logs, który
    maybe_send_call_summary następnie uzupełnia. Błąd jednego kroku nie blokuje drugiego.
    """
    try:
        await save_call_transcript(tenant, call_sid, caller_phone, context)
    except Exception as e:
        logger.error(f"[{log_tag}] Call transcript error: {e}")
    try:
        await maybe_send_call_summary(tenant, caller_phone, context, call_state, call_sid=call_sid)
    except Exception as e:
        logger.error(f"[{log_tag}] Call summary error: {e}")


def schedule_idle_reset_release(call_state: dict) -> None:
    """Zdejmuje suppress_idle_reset po SCRIPTED_UTTERANCE_SUPPRESS_SECONDS (zabezpieczenie)."""

    async def _release() -> None:
        await asyncio.sleep(SCRIPTED_UTTERANCE_SUPPRESS_SECONDS)
        call_state["suppress_idle_reset"] = False

    asyncio.create_task(_release())
