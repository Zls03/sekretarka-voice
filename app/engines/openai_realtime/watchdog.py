"""Nadzór nad rozmową OpenAI Realtime: cisza i limit czasu."""

import asyncio
import time

from loguru import logger
from pipecat.frames.frames import EndFrame
from pipecat.pipeline.task import PipelineTask
from pipecat.services.openai.realtime.events import ResponseCreateEvent, ResponseProperties
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

from app.engines.common import (
    IDLE_HANGUP_SECONDS,
    IDLE_WARNING_SECONDS,
    MAX_CALL_DURATION,
    schedule_idle_reset_release,
)


async def say_now(llm: OpenAIRealtimeLLMService, call_state: dict, text: str):
    """Każe modelowi powiedzieć dokładnie `text` jako jednorazową odpowiedź (response.create).

    TTSSpeakFrame nie działa z OpenAI Realtime (brak osobnego etapu TTS).
    - tool_choice="none": bez tego wymuszona wypowiedź potrafiła wywołać contact_owner
      z treścią "Halo? Czy mnie słyszysz?" i wysłać właścicielowi pusty e-mail.
    - suppress_idle_reset: to nie jest prawdziwa tura bota, nie resetuje zegara ciszy;
      flaga wygasa też sama (patrz schedule_idle_reset_release), bo przerwana wypowiedź
      może nie wygenerować TTSStoppedFrame.
    """
    call_state["suppress_idle_reset"] = True
    await llm.send_client_event(
        ResponseCreateEvent(
            response=ResponseProperties(
                instructions=f'Powiedz DOKŁADNIE: "{text}" i nic więcej.',
                tool_choice="none",
            )
        )
    )

    schedule_idle_reset_release(call_state)


async def monitor_call_health(task: PipelineTask, llm: OpenAIRealtimeLLMService, call_state: dict):
    """Pętla nadzoru rozmowy (co 2 s): cisza klienta i limit czasu rozmowy."""
    call_start = time.time()
    # Zegar ciszy startuje dopiero teraz — czas zestawiania połączenia to nie cisza klienta.
    call_state["idle_since"] = call_start
    idle_warning_given = False
    duration_warning_given = False

    while True:
        # Krótki interwał: próg ciszy nie może się spóźniać o kilka sekund.
        await asyncio.sleep(2)

        if call_state.get("ended"):
            logger.info("⏱️ [REALTIME TEST] Monitor zatrzymany — połączenie zakończone")
            break

        elapsed = time.time() - call_start
        silence = time.time() - call_state["idle_since"]

        # Dopóki nie padło powitanie, cisza się nie liczy — ale nie czekamy na nie w nieskończoność.
        if not call_state.get("greeted"):
            if elapsed > IDLE_HANGUP_SECONDS * 2:
                logger.warning(f"🔇 [REALTIME TEST] Powitanie nie nadeszło po {elapsed:.0f}s — kończę połączenie")
                call_state["ended"] = True
                await task.queue_frame(EndFrame())
                break
            continue

        if silence > IDLE_HANGUP_SECONDS:
            logger.warning(f"🔇 [REALTIME TEST] Brak odpowiedzi {silence:.0f}s — kończę połączenie")
            call_state["ended"] = True
            await say_now(llm, call_state, "Nie słyszę odpowiedzi. Dziękuję za kontakt, do widzenia!")
            await asyncio.sleep(3.0)
            await task.queue_frame(EndFrame())
            break

        if silence > IDLE_WARNING_SECONDS and not idle_warning_given:
            logger.warning(f"🔇 [REALTIME TEST] Cisza {silence:.0f}s — dopytuję czy słyszy")
            idle_warning_given = True
            # Tekst neutralny — say_now czyta dosłownie, "Pan/Pani" zostałoby przeczytane ze slashem.
            await say_now(llm, call_state, "Przepraszam, czy nadal jesteśmy połączeni?")
        elif silence < IDLE_WARNING_SECONDS:
            idle_warning_given = False

        if elapsed > MAX_CALL_DURATION - 30 and not duration_warning_given:
            duration_warning_given = True
            logger.warning(f"⚠️ [REALTIME TEST] Zbliża się limit czasu: {elapsed:.0f}s/{MAX_CALL_DURATION}s")
            await say_now(llm, call_state, "Za chwilę będę kończyć rozmowę — czy mogę jeszcze w czymś szybko pomóc?")

        if elapsed > MAX_CALL_DURATION:
            logger.warning(f"🛑 [REALTIME TEST] Limit czasu osiągnięty ({elapsed:.0f}s) — kończę połączenie")
            call_state["ended"] = True
            await say_now(llm, call_state, "Przepraszam, czas rozmowy się skończył. Dziękuję i do widzenia!")
            await asyncio.sleep(3.0)
            await task.queue_frame(EndFrame())
            break
