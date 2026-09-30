"""Nadzór nad rozmową OpenAI Realtime: cisza i limit czasu."""

import asyncio
import time

from loguru import logger
from pipecat.frames.frames import EndFrame
from pipecat.pipeline.task import PipelineTask
from pipecat.services.openai.realtime.events import ResponseCreateEvent, ResponseProperties
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

IDLE_WARNING_SECONDS = 6    # tyle ciszy -> "Halo, czy mnie słyszysz?" (skrócone z 10s


                            # 16.08.2026 — żywy telefon pokazał, że przy cichym zawieszeniu
                            # sesji Gemini Live klient siedział w martwej ciszy do ~29s zanim
                            # padło JAKIEKOLWIEK pytanie/rozłączenie; krótsze progi nie naprawiają
                            # przyczyny, ale skracają czas oczekiwania klienta w ciszy)
IDLE_HANGUP_SECONDS = 14    # tyle ciszy (8s po dopytaniu) -> kończymy połączenie


MAX_CALL_DURATION = 4 * 60  # ta sama wartość co w produkcyjnym bot.py


async def say_now(llm: OpenAIRealtimeLLMService, call_state: dict, text: str):
    """Każe modelowi Realtime powiedzieć DOKŁADNIE ten tekst, jako jednorazową
    odpowiedź (response.create z instructions), bez dopisywania niczego do historii
    rozmowy. Odpowiednik TTSSpeakFrame z cascade — TTSSpeakFrame/LLMMessagesAppendFrame
    NIE działają z OpenAIRealtimeLLMService (brak osobnego stopnia TTS w pipeline,
    a _handle_messages_append to w pipecat 1.4.0 wciąż pusty stub).

    Ustawia suppress_idle_reset, żeby BotAudioMonitor NIE zresetował zegara ciszy
    na tę wypowiedź — to automatyczne dopytanie/pożegnanie, nie prawdziwa tura bota.

    tool_choice="none": zaobserwowany na żywym telefonie bug — ten wymuszony response.create
    (z samym "powiedz dokładnie X") potrafił RÓWNIEŻ wywołać contact_owner z treścią "Halo?
    Czy mnie słyszysz?" jako message, wysyłając śmieciowy email do właściciela. Tools zostają
    zarejestrowane na poziomie SESJI, więc bez tego jawnego wyłączenia model miał do nich
    dostęp nawet w tej jednorazowej, wymuszonej wypowiedzi. tool_choice="none" gwarantuje że
    ta odpowiedź może być WYŁĄCZNIE mową, żadnego wywołania funkcji.

    Timeout na wyczyszczenie suppress_idle_reset (zamiast polegać WYŁĄCZNIE na TTSStoppedFrame
    w BotAudioMonitor): podejrzewany, nie w 100% potwierdzony bug — jeśli klient wejdzie
    w słowo w trakcie TEGO dopytania (barge-in, zaobserwowane na żywym telefonie: "Halo? Czy
    mnie słysz" ucięte w połowie), przerwana wypowiedź może nie wygenerować czystego
    TTSStoppedFrame. Bez tego timeoutu flaga zostałaby WTEDY zapalona już na resztę rozmowy —
    KAŻDY kolejny, prawdziwy Start/Stop bota przestałby resetować zegar ciszy (bo trafiałby
    w gałąź "to była nasza wymuszona wypowiedź"), więc zegar rósłby mimo aktywnej, płynnej
    rozmowy. Ten timeout samoczynnie leczy flagę niezależnie od tego czy TTSStoppedFrame
    w ogóle nadejdzie."""
    call_state["suppress_idle_reset"] = True
    await llm.send_client_event(
        ResponseCreateEvent(
            response=ResponseProperties(
                instructions=f'Powiedz DOKŁADNIE: "{text}" i nic więcej.',
                tool_choice="none",
            )
        )
    )

    async def _clear_suppress_after_timeout():
        await asyncio.sleep(8.0)
        call_state["suppress_idle_reset"] = False

    asyncio.create_task(_clear_suppress_after_timeout())


async def monitor_call_health(task: PipelineTask, llm: OpenAIRealtimeLLMService, call_state: dict):
    """Odpowiednik bot.py::check_max_duration(), przepisany pod Realtime (say_now
    zamiast TTSSpeakFrame) i uproszczony do JEDNEGO mechanizmu ciszy zamiast dwóch
    równoległych (UserIdleProcessor + osobny silence-check) jak w cascade — to
    duplikowało się tam bez wyraźnego powodu, tu wystarczy jeden zegar idle_since."""
    call_start = time.time()
    # Nadpisujemy idle_since dopiero TERAZ (nie w make_call_state()) — inaczej cały
    # czas setupu przed tym momentem (CRM, ładowanie VAD, connect do modelu, TTFB
    # powitania) liczyłby się jako "cisza klienta", i "Halo?" mogło wystrzelić
    # prawie natychmiast po przywitaniu, zanim klient zdążył cokolwiek powiedzieć.
    call_state["idle_since"] = call_start
    idle_warning_given = False
    duration_warning_given = False

    while True:
        # 2s zamiast 5s — na żywym telefonie próg 10s ciszy potrafił faktycznie wystrzelić
        # dopiero po 12-14s (10s + do 5s spóźnienia z samej granulacji tej pętli). Klient
        # odbierał to jako "nie doczekał nawet 10 sekund", choć log pokazywał realnie WIĘCEJ
        # niż próg — to była kwestia opóźnienia sprawdzania, nie błędnego liczenia ciszy.
        await asyncio.sleep(2)

        if call_state.get("ended"):
            logger.info("⏱️ [REALTIME TEST] Monitor zatrzymany — połączenie zakończone")
            break

        elapsed = time.time() - call_start
        silence = time.time() - call_state["idle_since"]

        # Dopóki bot nie wypowiedział choćby powitania, nie liczymy "ciszy" wcale —
        # patrz komentarz przy "greeted" w make_call_state(). Bez tego anomalnie wolny
        # TTFB samego powitania (np. 11s zamiast ~0.7s, zdarzyło się na żywym telefonie)
        # sam w sobie wyzwalał wymuszoną dogrywkę zanim klient cokolwiek usłyszał.
        if not call_state.get("greeted"):
            # Zabezpieczenie: jeśli powitanie NIGDY nie przyjdzie (model się zawiesił,
            # padło połączenie z API) — nie trzymamy rozmowy otwartej bez końca. Próg
            # wyraźnie wyższy niż normalny IDLE_HANGUP_SECONDS, bo to inny scenariusz
            # (błąd startu, nie cisza klienta).
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
            # "Pan/Pani" NIE nadaje się tu literalnie — say_now każe wypowiedzieć tekst
            # DOKŁADNIE, więc TTS przeczytałby ten znak "/" na głos. Neutralna wersja bez
            # zwrotu grzecznościowego, żeby nie zgadywać płci dzwoniącego.
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
