"""Nadzór nad rozmową Gemini Live: cisza, limit czasu, ciche zawieszenie sesji modelu."""

import asyncio
import time

from loguru import logger
from pipecat.frames.frames import EndFrame, TTSSpeakFrame
from pipecat.pipeline.task import PipelineTask

# Próba użycia gemini-2.5-flash-native-audio-preview-12-2025 (2026-09-03) odrzucona natychmiast
# przez samo Gemini: "1007 None. Unsupported language code 'pl' for model
# models/gemini-2.5-flash-native-audio-preview-12-2025" — ten wariant w ogóle nie obsługuje
# polskiego, więc nie nadaje się jako zamiennik niezależnie od TTFB. Zostajemy przy 3.1 preview
# (jedyny Live API model ze wsparciem 'pl' jaki mamy) i traktujemy jego epizody podwyższonego
# TTFB (3-11s, potwierdzone A/B testem) jako coś po stronie Google — patrz OpenAI Realtime
# jako sprawdzony fallback (realtime_engine='openai' w panelu, zakładka "Realtime GPT").

# Progi idle/max-duration — WARTOŚCI MUSZĄ być takie same jak w bot_openai_realtime.py
# (obie ścieżki tuningowane razem na żywych telefonach, patrz historia tego pliku).
# Duplikacja świadoma: to proste stałe int, nie logika — import z drugiego modułu
# tylko po te 3 liczby dokładałby sztuczną zależność bez żadnej korzyści.
IDLE_WARNING_SECONDS = 6


IDLE_HANGUP_SECONDS = 14


MAX_CALL_DURATION = 4 * 60


# SILENT_HANG_TIMEOUT — TYLKO Gemini Live (patrz docstring GeminiUserMonitor/
# monitor_gemini_call_health niżej), OpenAI Realtime nie ma tego trybu awarii.
SILENT_HANG_TIMEOUT = 5


async def speak_directly(task: PipelineTask, call_state: dict, text: str):
    """Wypowiada DOKŁADNY tekst przez `fallback_tts`, z całkowitym pominięciem Gemini
    Live — wstrzykuje TTSSpeakFrame prosto do kolejki pipeline'u. Gemini mówi zawsze
    własnym głosem (modalities=AUDIO); `fallback_tts` siedzi w osobnej gałęzi
    ParallelPipeline wyłącznie pod TTSSpeakFrame (idle-nudge, patrz komentarz przy
    budowie pipeline'u) — tam TTSService ma zdefiniowaną obsługę
    TTSSpeakFrame jako niezależnej, doraźnej wypowiedzi (patrz źródło pipecat
    tts_service.py) — działa tak samo, gdy Gemini akurat też coś streamuje.

    PO CO: poprzednia wersja (gemini_say_now) prosiła o to SAM MODEL — działało
    tylko gdy sesja Gemini Live żyje. Złapane na żywym telefonie 16.08.2026: gdy
    sesja cicho się zawiesza, prośba wysłana DO modelu nie daje efektu. Rozwiązanie
    z cascade to TTSSpeakFrame idący prosto do TTS z pominięciem LLM — tu robimy to
    samo dla Realtime/Gemini Live.

    Dlatego NIE ustawia "awaiting_model_response_since" — nie czekamy tu na Gemini,
    to pole zostaje zarezerwowane wyłącznie dla wykrywania braku odpowiedzi na
    REALNE pytania klienta (ustawiane w GeminiUserMonitor)."""
    call_state["suppress_idle_reset"] = True
    await task.queue_frame(TTSSpeakFrame(text=text))

    async def _clear_suppress_after_timeout():
        await asyncio.sleep(8.0)
        call_state["suppress_idle_reset"] = False

    asyncio.create_task(_clear_suppress_after_timeout())


async def monitor_gemini_call_health(task: PipelineTask, call_state: dict, llm=None):
    """Odpowiednik monitor_call_health() (bot_openai_realtime.py) dla Gemini
    Live — ta sama logika progów (IDLE_WARNING_SECONDS/IDLE_HANGUP_SECONDS/MAX_CALL_DURATION,
    stałe zduplikowane celowo w obu plikach z tymi samymi wartościami, patrz komentarz
    przy ich definicji wyżej w tym pliku), tylko wywołuje speak_directly() zamiast say_now().

    `llm`: instancja GeminiLiveLLMService — potrzebna do wymuszenia reconnectu przy cichym
    zawieszeniu sesji (patrz SILENT_HANG_TIMEOUT). Opcjonalna (None) dla wstecznej zgodności,
    ale bez niej watchdog tylko zaloguje problem, nie naprawi go."""
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

        # Cichy hang sesji: klient realnie coś powiedział (GeminiUserMonitor)
        # i minęło SILENT_HANG_TIMEOUT bez ŻADNEJ reakcji — ani audio, ani tekstu. To NIE jest
        # zwykła cisza klienta (ta jest obsłużona niżej przez IDLE_*), tylko martwa sesja Gemini
        # Live bez wyjątku po stronie WebSocketu — pipecat sam tego nie wykryje (patrz stała).
        awaiting_since = call_state.get("awaiting_model_response_since")
        if awaiting_since and (time.time() - awaiting_since) > SILENT_HANG_TIMEOUT:
            hang_s = time.time() - awaiting_since
            call_state["awaiting_model_response_since"] = None
            call_state["suppress_idle_reset"] = False

            if call_state.get("silent_hang_reconnect_used"):
                # Reconnect już raz próbowaliśmy w tej rozmowie i sesja mimo to ucichła
                # DRUGI raz — złapane na żywym telefonie 16.08.2026: druga próba, wysłana
                # zaraz po pierwszym reconnect, sama trafiła w tę samą ścianę ciszy (bo
                # _reconnect() zwraca się zanim sesja jest faktycznie w pełni gotowa), co
                # dawało dwa reconnecty pod rząd zamiast czystego rozłączenia. Traktujemy to
                # teraz tak jak zwykłą długą ciszę klienta — kończymy połączenie, bez próby
                # mówienia pożegnania (ten kanał już dwa razy zawiódł, nie ma sensu próbować
                # trzeci raz).
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
            # Po reconnect dajemy modelowi świeży zegar ciszy zamiast od razu liczyć dalej —
            # inaczej mogłoby natychmiast wystrzelić IDLE_HANGUP poniżej na starym idle_since.
            call_state["idle_since"] = time.time()
            if reconnect_ok:
                # KRYTYCZNE dla UX: bez tego klient słyszy martwą ciszę aż do NASTĘPNEGO
                # normalnego cyklu IDLE_WARNING_SECONDS (do 10s więcej) — sesja jest już
                # naprawiona, ale nikt mu tego nie mówi. Odzywamy się od razu po reconnect.
                # Jeśli TA wiadomość też przepadnie (sesja jeszcze się nie rozgrzała) —
                # kolejne wykrycie trafi w gałąź "już próbowaliśmy" powyżej i po prostu
                # się rozłączy, zamiast reconnectować w kółko.
                #
                # POPRAWKA 2026-08-23: było to samo zdanie co przy zwykłej ciszy klienta
                # ("czy nadal jesteśmy połączeni?") — mylące, bo tu to NIE klient milczał,
                # tylko model nie odpowiedział na coś co klient realnie powiedział (stąd w
                # ogóle SILENT_HANG_TIMEOUT/reconnect, patrz gałąź wyżej). Złapane na żywym
                # telefonie 23.08.2026: klient zadał pytanie, sesja ucichła, po reconnect
                # usłyszał "czy nadal jesteśmy połączeni?" i pomyślał że to on nie został
                # usłyszany od początku — musiał powtarzać pytanie od nowa. Nowy tekst prosi
                # wprost o powtórzenie, zamiast sugerować że to klient zamilkł. Celowo BEZ
                # "mogłabym"/"mogłabym" itp. (forma żeńska) — to zdanie leci sztywno przez TTS
                # dla KAŻDEGO tenanta, niezależnie od głosu (żeński/męski), więc musi być
                # tak samo neutralne jak reszta scripted-utterance w tym pliku (bezokolicznik
                # po "proszę", zero odmiany przez rodzaj).
                await speak_directly(task, call_state, "Przepraszam, proszę powtórzyć pytanie.")
            continue

        # Patrz komentarz przy tej samej gałęzi w monitor_call_health() (sekcja OpenAI
        # Realtime) — dopóki bot nie wypowiedział choćby powitania, nie liczymy ciszy.
        if not call_state.get("greeted"):
            if elapsed > IDLE_HANGUP_SECONDS * 2:
                logger.warning(f"🔇 [GEMINI LIVE TEST] Powitanie nie nadeszło po {elapsed:.0f}s — kończę połączenie")
                call_state["ended"] = True
                await task.queue_frame(EndFrame())
                break
            continue

        if silence > IDLE_HANGUP_SECONDS:
            # ⚠️ Race złapany na żywym telefonie (16.08.2026, ta sama sesja co sample_rate/
            # idle-nudge fixy wyżej): transkrypcja Gemini ma opóźnienie ~1-2s względem
            # faktycznej mowy klienta. Gdy klient zaczął odpowiadać dosłownie w tej samej
            # sekundzie w której ten warunek się spełnił, jego transkrypcja (i reset idle_since
            # w GeminiUserMonitor) potrafiła dotrzeć KILKASET MS PO TYM jak już zdążyliśmy
            # zakolejkować pożegnanie — efekt zaobserwowany na żywo: prawdziwa odpowiedź
            # Gemini ("Najtańszy pakiet, czyli Starter...") i nasze "Nie słyszę odpowiedzi..."
            # zaczęły grać JEDNOCZEŚNIE (dwie niezależne gałęzie audio w ParallelPipeline), a
            # samo rozłączenie i tak się odwlokło aż do końca tej realnej odpowiedzi
            # (GeminiLiveLLMService sam odkłada EndFrame do końca tury bota — "Deferring
            # handling EndFrame until bot turn is finished"). Fix: krótka dogrywka na
            # dogonienie STT tuż PRZED nieodwracalnym rozłączeniem — jeśli w tym oknie
            # idle_since jednak się odświeżył (klient naprawdę coś powiedział), odpuszczamy
            # TĘ próbę zamiast mówić na raz z prawdziwą odpowiedzią.
            await asyncio.sleep(1.5)
            silence = time.time() - call_state["idle_since"]
            if call_state.get("ended") or silence <= IDLE_HANGUP_SECONDS:
                continue

            logger.warning(f"🔇 [GEMINI LIVE TEST] Brak odpowiedzi {silence:.0f}s — kończę połączenie")
            call_state["ended"] = True
            goodbye_started_at = time.time()
            await speak_directly(task, call_state, "Nie słyszę odpowiedzi. Dziękuję za kontakt, do widzenia!")
            await asyncio.sleep(3.0)
            # ⚠️ DRUGA linia obrony (16.08.2026, kolejny test tej samej sesji): 1.5s dogrywka
            # wyżej nie zawsze wystarcza — transkrypcja Gemini potrafi spóźnić się bardziej
            # (złapane na żywo: ~2.2s). Jeśli klient JEDNAK zdążył odpowiedzieć W TRAKCIE
            # mówienia pożegnania lub tego sleep(3.0) — GeminiUserMonitor już zdążył odświeżyć
            # idle_since na TranscriptionFrame (nie licząc scripted-utterance resetów, te są
            # wyłączone przez suppress_idle_reset od commitu 6f5e4ca) — cofamy rozłączenie
            # zamiast ucinać rozmowę EndFrame'em w środku realnej odpowiedzi Gemini na to,
            # co klient właśnie powiedział. Pojedyncze nałożenie się audio (pożegnanie +
            # zaczynająca się odpowiedź Gemini) może się zdarzyć — akceptowalne, priorytetem
            # jest żeby rozmowa się NIE URYWAŁA gdy klient jednak coś powiedział.
            if call_state["idle_since"] > goodbye_started_at:
                logger.info("↩️ [GEMINI LIVE TEST] Klient jednak odpowiedział w trakcie pożegnania — anuluję rozłączenie")
                call_state["ended"] = False
                continue
            await task.queue_frame(EndFrame())
            break

        if silence > IDLE_WARNING_SECONDS and not idle_warning_given:
            if call_state.get("waiting_for_bot_audio"):
                # POPRAWKA 2026-08-31: złapane na żywym telefonie — klient zadał dłuższe
                # pytanie, lokalny VAD poprawnie zarejestrował koniec jego wypowiedzi
                # (waiting_for_bot_audio=True), ale Gemini tym razem potrzebował >6s na
                # odpowiedź (zaobserwowane TTFB do 7.7s w tej samej rozmowie — normalna
                # zmienność, nie zawieszenie). idle_since nie ma jak się odświeżyć w tym
                # oknie (nic nowego nie leci ani od klienta, ani od bota), więc licznik
                # ciszy rósł mimo że klient WŁAŚNIE skończył mówić — nudge "czy nadal
                # jesteśmy połączeni?" (fallback_tts) wystartował i zagrał RÓWNOLEGLE z
                # prawdziwą odpowiedzią Gemini, gdy ta w końcu nadeszła sekundę później.
                # Fix: dopóki lokalnie wiemy że czekamy na odpowiedź po realnej wypowiedzi
                # klienta, nie traktuj tego jak ciszy — pomiń TEN cykl ostrzeżenia.
                # IDLE_HANGUP_SECONDS niżej (znacznie dłuższy próg) i tak zabezpiecza przed
                # realnym zawieszeniem sesji niezależnie od tej flagi.
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
            await speak_directly(task, call_state, "Za chwilę będę kończyć rozmowę — czy mogę jeszcze w czymś szybko pomóc?")

        if elapsed > MAX_CALL_DURATION:
            logger.warning(f"🛑 [GEMINI LIVE TEST] Limit czasu osiągnięty ({elapsed:.0f}s) — kończę połączenie")
            call_state["ended"] = True
            await speak_directly(task, call_state, "Przepraszam, czas rozmowy się skończył. Dziękuję i do widzenia!")
            await asyncio.sleep(3.0)
            await task.queue_frame(EndFrame())
            break
