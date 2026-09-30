"""Nadzór nad rozmową Gemini Live: cisza, limit czasu, ciche zawieszenie sesji modelu."""

import asyncio
import time

from loguru import logger
from pipecat.frames.frames import EndFrame, TTSSpeakFrame
from pipecat.pipeline.task import PipelineTask

from app.engines.common import (
    IDLE_HANGUP_SECONDS,
    IDLE_WARNING_SECONDS,
    MAX_CALL_DURATION,
    schedule_idle_reset_release,
)

# Tyle sekund bez żadnej reakcji modelu na wypowiedź klienta = sesja Gemini Live ucichła
# (znany problem: websocket żyje, ale odpowiedzi przestają przychodzić).
SILENT_HANG_TIMEOUT = 5


async def speak_directly(task: PipelineTask, call_state: dict, text: str):
    """Wypowiada dokładnie `text` zapasowym TTS, z pominięciem Gemini.

    Prośba skierowana do modelu nie zadziała, gdy jego sesja właśnie ucichła — a to
    wtedy najbardziej potrzebujemy się odezwać. Nie ustawia awaiting_model_response_since:
    to pole dotyczy wyłącznie odpowiedzi modelu na wypowiedzi klienta.
    """
    call_state["suppress_idle_reset"] = True
    await task.queue_frame(TTSSpeakFrame(text=text))

    schedule_idle_reset_release(call_state)


async def monitor_gemini_call_health(task: PipelineTask, call_state: dict, llm=None):
    """Pętla nadzoru rozmowy (co 2 s): zawieszenie modelu, cisza klienta, limit czasu.

    `llm` (GeminiLiveLLMService) jest potrzebny do reconnectu po cichym zawieszeniu —
    bez niego problem zostanie tylko zalogowany.
    """
    call_start = time.time()
    call_state["idle_since"] = call_start
    idle_warning_given = False
    duration_warning_given = False

    while True:
        await asyncio.sleep(2)

        if call_state.get("ended"):
            logger.info("⏱️ [GEMINI LIVE TEST] Monitor zatrzymany — połączenie zakończone")
            break

        elapsed = time.time() - call_start
        silence = time.time() - call_state["idle_since"]

        # Ciche zawieszenie: klient coś powiedział, a model nie reaguje (pipecat sam
        # reconnectuje tylko po wyjątku, a tu wyjątku nie ma).
        awaiting_since = call_state.get("awaiting_model_response_since")
        if awaiting_since and (time.time() - awaiting_since) > SILENT_HANG_TIMEOUT:
            hang_s = time.time() - awaiting_since
            call_state["awaiting_model_response_since"] = None
            call_state["suppress_idle_reset"] = False

            if call_state.get("silent_hang_reconnect_used"):
                # Drugie zawieszenie mimo reconnectu — kończymy zamiast reconnectować w kółko.
                logger.warning(
                    f"🧟 [GEMINI LIVE TEST] Model nie odpowiedział {hang_s:.0f}s po wysłaniu, "
                    "PO RAZ DRUGI mimo reconnectu — kończę połączenie zamiast próbować dalej"
                )
                call_state["ended"] = True
                await task.queue_frame(EndFrame())
                break

            logger.warning(
                f"🧟 [GEMINI LIVE TEST] Model nie odpowiedział {hang_s:.0f}s po wysłaniu — "
                "sesja wygląda na cicho zawieszoną, wymuszam reconnect (jedyna próba na tę rozmowę)"
            )
            call_state["silent_hang_reconnect_used"] = True
            reconnect_ok = False
            if llm is not None:
                try:
                    await llm._reconnect()
                    reconnect_ok = True
                    logger.info("🔄 [GEMINI LIVE TEST] Reconnect po cichym zawieszeniu wykonany")
                except Exception as e:
                    logger.error(f"🔄 [GEMINI LIVE TEST] Reconnect po cichym zawieszeniu NIEUDANY: {e}")
            # Świeży zegar ciszy — inaczej od razu zadziałałoby rozłączenie z powodu ciszy.
            call_state["idle_since"] = time.time()
            if reconnect_ok:
                # Odzywamy się od razu i prosimy o powtórzenie — to model nie usłyszał,
                # a nie klient zamilkł. Zdanie neutralne rodzajowo (ten sam tekst dla każdego głosu).
                await speak_directly(task, call_state, "Przepraszam, proszę powtórzyć pytanie.")
            continue

        # Dopóki nie padło powitanie, nie liczymy ciszy — ale nie czekamy na nie w nieskończoność.
        if not call_state.get("greeted"):
            if elapsed > IDLE_HANGUP_SECONDS * 2:
                logger.warning(f"🔇 [GEMINI LIVE TEST] Powitanie nie nadeszło po {elapsed:.0f}s — kończę połączenie")
                call_state["ended"] = True
                await task.queue_frame(EndFrame())
                break
            continue

        if silence > IDLE_HANGUP_SECONDS:
            # Transkrypcja Gemini spóźnia się 1-2 s względem mowy — dajemy chwilę, żeby nie
            # pożegnać klienta, który właśnie zaczął odpowiadać.
            await asyncio.sleep(1.5)
            silence = time.time() - call_state["idle_since"]
            if call_state.get("ended") or silence <= IDLE_HANGUP_SECONDS:
                continue

            logger.warning(f"🔇 [GEMINI LIVE TEST] Brak odpowiedzi {silence:.0f}s — kończę połączenie")
            call_state["ended"] = True
            goodbye_started_at = time.time()
            await speak_directly(task, call_state, "Nie słyszę odpowiedzi. Dziękuję za kontakt, do widzenia!")
            await asyncio.sleep(3.0)
            # Klient jednak odpowiedział w trakcie pożegnania — nie urywamy rozmowy.
            if call_state["idle_since"] > goodbye_started_at:
                logger.info(
                    "↩️ [GEMINI LIVE TEST] Klient jednak odpowiedział w trakcie pożegnania — anuluję rozłączenie"
                )
                call_state["ended"] = False
                continue
            await task.queue_frame(EndFrame())
            break

        if silence > IDLE_WARNING_SECONDS and not idle_warning_given:
            if call_state.get("waiting_for_bot_audio"):
                # Klient skończył mówić, model jeszcze myśli (bywa >6 s) — to nie cisza.
                # Przed prawdziwym zawieszeniem chroni i tak dłuższy próg IDLE_HANGUP_SECONDS.
                pass
            else:
                logger.warning(f"🔇 [GEMINI LIVE TEST] Cisza {silence:.0f}s — dopytuję czy słyszy")
                idle_warning_given = True
                await speak_directly(task, call_state, "Przepraszam, czy nadal jesteśmy połączeni?")
        elif silence < IDLE_WARNING_SECONDS:
            idle_warning_given = False

        if elapsed > MAX_CALL_DURATION - 30 and not duration_warning_given:
            duration_warning_given = True
            logger.warning(f"⚠️ [GEMINI LIVE TEST] Zbliża się limit czasu: {elapsed:.0f}s/{MAX_CALL_DURATION}s")
            await speak_directly(
                task, call_state, "Za chwilę będę kończyć rozmowę — czy mogę jeszcze w czymś szybko pomóc?"
            )

        if elapsed > MAX_CALL_DURATION:
            logger.warning(f"🛑 [GEMINI LIVE TEST] Limit czasu osiągnięty ({elapsed:.0f}s) — kończę połączenie")
            call_state["ended"] = True
            await speak_directly(task, call_state, "Przepraszam, czas rozmowy się skończył. Dziękuję i do widzenia!")
            await asyncio.sleep(3.0)
            await task.queue_frame(EndFrame())
            break
