"""Przebieg jednej rozmowy OpenAI Realtime — wspólny dla Twilio i Vonage."""

from __future__ import annotations

import asyncio

from fastapi import WebSocket
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import EndFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask

from app.background import spawn
from app.engines.common import (
    CallFeatures,
    Channel,
    build_call_prompt,
    build_call_tools,
    call_pipeline_params,
    create_transport,
    finalize_call,
)
from app.engines.openai_realtime.llm import apply_crm_when_ready, build_realtime_llm
from app.engines.openai_realtime.monitors import BotAudioMonitor, UserTranscriptMonitor, make_call_state
from app.engines.openai_realtime.watchdog import monitor_call_health, say_now
from app.panel_client import get_client_profile
from app.prompt.instructions import build_greeting_message


async def run_openai_realtime_call(
    websocket: WebSocket,
    tenant: dict,
    caller_phone: str,
    call_sid: str | None,
    *,
    channel: Channel,
    log_tag: str,
    stream_sid: str | None = None,
) -> None:
    logger.info(f"✅ [{log_tag}] Tenant: {tenant.get('name')}")

    # Profil klienta z panelu pobieramy w tle — powitanie nie czeka (patrz apply_crm_when_ready).
    client_profile_task = asyncio.create_task(get_client_profile(tenant.get("id", ""), caller_phone))

    transport = create_transport(
        websocket,
        channel,
        stream_sid=stream_sid,
        vad_analyzer=SileroVADAnalyzer(params=VADParams(confidence=0.6, start_secs=0.2, stop_secs=0.3, min_volume=0.4)),
    )

    task_box: dict = {"task": None}
    context_box: dict = {"context": None}
    call_state = make_call_state()
    features = CallFeatures.for_tenant(tenant)
    tools = build_call_tools(
        tenant,
        caller_phone,
        features,
        call_state=call_state,
        task_box=task_box,
        context_box=context_box,
        channel=channel,
    )
    system_prompt = await build_call_prompt(tenant, caller_phone, features)
    # Głos i tempo per firma; brak wartości = domyślne ustawienia (patrz build_realtime_llm).
    realtime_voice = (tenant.get("realtime_voice") or "").strip() or None
    realtime_speed = float(tenant["speaking_rate"]) if tenant.get("speaking_rate") else None
    llm, user_aggregator, assistant_aggregator, llm_context = build_realtime_llm(
        system_prompt, tools=tools, voice=realtime_voice, speed=realtime_speed
    )
    context_box["context"] = llm_context

    pipeline = Pipeline(
        [
            transport.input(),
            user_aggregator,
            UserTranscriptMonitor(call_state),
            llm,
            BotAudioMonitor(call_state),
            transport.output(),
            assistant_aggregator,
        ]
    )
    task = PipelineTask(pipeline, params=call_pipeline_params(channel))
    task_box["task"] = task

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        # Powitanie wymuszamy dosłownie (say_now) — przy pustym kontekście model potrafił
        # zignorować tekst powitania z promptu i zacząć np. od cennika.
        logger.info(f"🎤 [{log_tag}] Klient połączony — wybudzam Realtime do przywitania")
        await say_now(llm, call_state, build_greeting_message(tenant))
        spawn(monitor_call_health(task, llm, call_state))
        spawn(apply_crm_when_ready(llm, tenant, client_profile_task, caller_phone, features))

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info(f"📴 [{log_tag}] Klient rozłączony")
        call_state["ended"] = True
        await task.queue_frame(EndFrame())

    logger.info(f"🚀 [{log_tag}] Start pipeline")
    try:
        await PipelineRunner().run(task)
    except Exception as e:
        logger.error(f"[{log_tag}] Pipeline error: {e}")
    finally:
        logger.info(f"🏁 [{log_tag}] Koniec połączenia")
        await finalize_call(tenant, call_sid, caller_phone, llm_context, call_state, log_tag)
