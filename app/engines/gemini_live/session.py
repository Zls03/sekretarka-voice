"""Przebieg jednej rozmowy Gemini Live — wspólny dla Twilio i Vonage."""

from __future__ import annotations

import asyncio

from loguru import logger
from pipecat.frames.frames import EndFrame, LLMMessagesAppendFrame
from pipecat.pipeline.parallel_pipeline import ParallelPipeline
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.transports.websocket.fastapi import FastAPIWebsocketTransport

from app.background import spawn
from app.engines.common import (
    CallFeatures,
    Channel,
    build_call_prompt,
    build_call_tools,
    call_pipeline_params,
    create_local_vad,
    finalize_call,
)
from app.engines.gemini_live.llm import build_gemini_live_llm
from app.engines.gemini_live.monitors import GeminiBotMonitor, GeminiUserMonitor, make_gemini_state
from app.engines.gemini_live.watchdog import monitor_gemini_call_health
from app.tts import create_tts_service

# Opóźnienie "zapłonu" powitania po połączeniu klienta — daje transportowi i VAD chwilę
# na start. Skrócone z 1.0 s (eksperyment 2026-09-10); jeśli pierwsze słowo powitania
# zacznie być ucinane, podnieść z powrotem.
GREETING_KICK_DELAY_SECONDS = 0.3

# Zapasowy TTS musi generować audio w tej samej częstotliwości co natywny głos Gemini:
# transport wyjściowy ma jeden stanowy resampler i nie obsługuje dwóch różnych wejść.
GEMINI_AUDIO_SAMPLE_RATE = 24000


async def run_gemini_live_call(
    transport: FastAPIWebsocketTransport,
    tenant: dict,
    caller_phone: str,
    call_sid: str | None,
    *,
    channel: Channel,
    features: CallFeatures,
    log_tag: str,
    region_url: str | None = None,
    host: str | None = None,
) -> None:
    logger.info(f"✅ [{log_tag}] Tenant: {tenant.get('name')}")

    gemini_state = make_gemini_state()
    task_box: dict = {"task": None}
    context_box: dict = {"context": None}
    tools = build_call_tools(
        tenant, caller_phone, features,
        call_state=gemini_state, task_box=task_box, context_box=context_box,
        channel=channel, call_sid=call_sid, region_url=region_url, host=host,
    )
    system_prompt = await build_call_prompt(tenant, caller_phone, features)
    gemini_voice = (tenant.get("gemini_voice") or "").strip() or None
    llm, user_aggregator, assistant_aggregator, llm_context = build_gemini_live_llm(
        system_prompt, tools=tools, voice=gemini_voice
    )
    context_box["context"] = llm_context

    # Bot zawsze mówi natywnym głosem Gemini. fallback_tts w równoległej gałęzi służy
    # wyłącznie do komunikatów, które muszą paść dosłownie (dopytanie o ciszę, pożegnanie
    # — patrz watchdog.speak_directly): gdy sesja Gemini ucichnie, nie da się jej o to prosić.
    fallback_tts = create_tts_service(tenant, sample_rate=GEMINI_AUDIO_SAMPLE_RATE)

    pipeline = Pipeline([
        transport.input(),
        # Lokalny VAD przed monitorem użytkownika: wczesny sygnał "klient mówi" chroni
        # przed dopytaniem o ciszę w środku dłuższej wypowiedzi klienta.
        create_local_vad(),
        user_aggregator,
        GeminiUserMonitor(gemini_state),
        ParallelPipeline([llm], [fallback_tts]),
        GeminiBotMonitor(gemini_state),
        transport.output(),
        assistant_aggregator,
    ])
    task = PipelineTask(pipeline, params=call_pipeline_params(channel))
    task_box["task"] = task

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        # Powitanie wywołujemy krótką wiadomością-zapłonem zamiast pustym kontekstem:
        # pusty kontekst sprawiał, że Gemini doklejał cały system prompt drugi raz,
        # a pierwsze słowo padało dopiero po ~15 s. Treść powitania i tak dyktuje prompt.
        logger.info(f"🎤 [{log_tag}] Klient połączony — wybudzam do przywitania")
        await asyncio.sleep(GREETING_KICK_DELAY_SECONDS)
        await task.queue_frames([
            LLMMessagesAppendFrame(
                messages=[{"role": "user", "content": "(początek rozmowy)"}],
                run_llm=True,
            )
        ])
        logger.info(f"🎤 [{log_tag}] Kick startowy wysłany")
        spawn(monitor_gemini_call_health(task, gemini_state, llm))

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info(f"📴 [{log_tag}] Klient rozłączony")
        gemini_state["ended"] = True
        await task.queue_frame(EndFrame())

    logger.info(f"🚀 [{log_tag}] Start pipeline")
    try:
        await PipelineRunner().run(task)
    except Exception as e:
        logger.error(f"[{log_tag}] Pipeline error: {e}")
    finally:
        logger.info(f"🏁 [{log_tag}] Koniec połączenia")
        await finalize_call(tenant, call_sid, caller_phone, llm_context, gemini_state, log_tag)
