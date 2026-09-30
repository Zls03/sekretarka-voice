# gemini_live_pipeline.py — wydzielone z bot_gemini_test.py (2026-09-30), krok 1
# porządkowania po usunięciu cascade (patrz CLAUDE.md "Project Overview").
"""
Czyste funkcje/klasy budujące i monitorujące sesję Gemini Live — zero zależności od
FastAPI/app, zero endpointów. Wydzielone jako pierwszy, najniższego ryzyka krok
podziału bot_gemini_test.py (ten plik ma zero mutowalnego stanu na poziomie modułu i
nic go nie importuje z powrotem z bot_gemini_test.py — bezpieczne do przenoszenia).

Eksportuje: GEMINI_LIVE_MODEL, make_gemini_state, GeminiUserMonitor, GeminiBotMonitor,
speak_directly, monitor_gemini_call_health, build_gemini_live_llm — używane przez
websockety Gemini Live (Twilio i Vonage) w bot_gemini_test.py.
"""

import os
import time
import asyncio

from loguru import logger

from pipecat.pipeline.task import PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.frames.frames import (
    EndFrame, TranscriptionFrame, TTSAudioRawFrame, TTSTextFrame,
    TTSStartedFrame, TTSStoppedFrame, TTSSpeakFrame,
    VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame, UserSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
from pipecat.transcriptions.language import Language
from google.genai.types import ThinkingConfig


# ==========================================================================
# 🧪 GEMINI LIVE — szybki test porównawczy latencji, OBOK OpenAI Realtime
# ==========================================================================
"""
Cel: TYLKO zmierzyć latencję/jakość gemini-3.1-flash-live-preview na tym samym
tenancie testowym, do porównania z OpenAI Realtime (patrz bot_openai_realtime.py).
Świadomie ubogie względem tamtej sekcji — bez tools (contact_owner/submit_lead/
end_conversation), bez idle-timeout, bez CRM w tle. To NIE jest kandydat do
rozbudowy 1:1 — jeśli Gemini Live wygra test latencji, wtedy dopiero warto
dociągnąć brakujące funkcje analogicznie do bot_openai_realtime.py.

Osobne route'y (inna ścieżka niż OpenAI Realtime) — NIC z bot_openai_realtime.py nie
jest ruszane. Żeby faktycznie przetestować, trzeba w konsoli Vonage/Twilio ręcznie
przełączyć Answer URL/webhook na endpoint poniżej, i przełączyć z powrotem po
teście.

GeminiLiveLLMService NIE emituje UserStartedSpeakingFrame/UserStoppedSpeakingFrame
(server VAD Gemini nie ma odpowiednika tych zdarzeń w pipecat, patrz docstring
serwisu) — pierwotnie pomiar latencji niżej kotwiczył się więc o TranscriptionFrame
(moment dotarcia transkrypcji). POPRAWKA 2026-08-18: to dawało fałszywie niskie
liczby — na żywej rozmowie TranscriptionFrame przychodził ~2.8s PO realnym końcu
mowy (Gemini batchuje/opóźnia transkrypcję), więc "TOTAL user->bot audio" pokazywał
np. 286ms, podczas gdy realny TTFB logowany przez GeminiLiveLLMService (i policzony
ręcznie z timestampów VAD-stop -> pierwsze audio bota) wynosił 3.1s. Anchor
przełączony na VADUserStoppedSpeakingFrame z lokalnego VADProcessor (patrz pipeline
niżej — analizuje audio lokalnie, niezależnie od Gemini) — to ten sam sygnał co
GeminiUserMonitor już i tak używa do odświeżania idle_since na starcie mowy
(VADUserStartedSpeakingFrame), więc żadnej nowej zależności nie dokłada.
"""

GEMINI_LIVE_MODEL = "gemini-3.1-flash-live-preview"  # zweryfikowane w docs.ai.google.dev (sierpień 2026)
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


def make_gemini_state() -> dict:
    now = time.time()
    return {
        "last_user_frame": None,
        "waiting_for_bot_audio": False,
        # Od tu w dół: pola pod idle-timeout (Faza 2), patrz speak_directly() /
        # monitor_gemini_call_health() niżej — te same nazwy pól i ta sama logika
        # co make_call_state()/BotAudioMonitor w bot_openai_realtime.py,
        # przeniesione 1:1 (nie duplikowane, świadomie skopiowane).
        "idle_since": now,
        "suppress_idle_reset": False,
        "audio_playback_until": now,
        "ended": False,
        "greeted": False,  # patrz komentarz przy tym samym polu w make_call_state() wyżej
        "awaiting_model_response_since": None,  # not None = czekamy na odpowiedź MODELU po
                                                 # tym jak klient realnie coś powiedział (patrz
                                                 # GeminiUserMonitor — TYLKO realne tury klienta,
                                                 # nasze własne komunikaty idą przez speak_directly()
                                                 # niezależnym silnikiem TTS, więc nie czekają na
                                                 # Gemini w ogóle). Czyszczone w GeminiBotMonitor
                                                 # na pierwszym dowodzie życia modelu. Jeśli
                                                 # zostaje ustawione dłużej niż SILENT_HANG_TIMEOUT
                                                 # — sesja Gemini Live ucichła bez błędu/wyjątku
                                                 # (potwierdzony na żywym telefonie 16.08.2026,
                                                 # znany problem community — WebSocket zostaje
                                                 # otwarty, ale server_content przestaje przychodzić).
                                                 # Pipecat 1.4.0 reconnectuje TYLKO na wyjątek w
                                                 # pętli odbiorczej (sprawdzone w źródle), więc ten
                                                 # przypadek nigdy by się sam nie naprawił.
        "silent_hang_reconnect_used": False,    # Reconnect po cichym zawieszeniu próbujemy TYLKO
                                                 # RAZ na całe połączenie, nie w kółko — złapane na
                                                 # żywym telefonie 16.08.2026: druga próba, wysłana
                                                 # zaraz po pierwszym reconnect, sama trafiła w tę
                                                 # samą ścianę ciszy (bo _reconnect() zwraca się
                                                 # zanim sesja jest faktycznie w pełni gotowa), co
                                                 # dawało dwa reconnecty pod rząd zamiast czystego
                                                 # rozłączenia. Jeśli sesja ucichnie DRUGI raz mimo
                                                 # reconnectu — kończymy połączenie, tak jak przy
                                                 # zwykłej długiej ciszy klienta, zamiast prób w
                                                 # nieskończoność. Reset na False po pierwszej
                                                 # udanej odpowiedzi modelu (GeminiBotMonitor) —
                                                 # jeden przejściowy hiccup w długiej rozmowie nie
                                                 # powinien "zużywać" jedynej próby na stałe.
    }


class GeminiUserMonitor(FrameProcessor):
    """Łapie transkrypcję usera. MUSI siedzieć PRZED llm w pipeline (nie po) — bug
    znaleziony na żywym telefonie: GeminiLiveLLMService wypycha TranscriptionFrame
    kierunkiem UPSTREAM (w stronę user_aggregatora), nie DOWNSTREAM. Poprzednia wersja
    tego monitora siedziała PO llm i przez to NIGDY nie widziała żadnej transkrypcji
    (potwierdzone: zero logów mimo że pipecat sam logował transkrypcje wewnętrznie),
    mimo że audio realnie leciało — stąd zero zmierzonych opóźnień w poprzednim teście.

    Odświeża też idle_since — GeminiLiveLLMService NIE emituje UserStarted/StoppedSpeakingFrame
    (patrz warning w logu na żywym telefonie). Pomiar TTFB/"user->bot audio" (last_user_frame)
    kotwiczy się o VADUserStoppedSpeakingFrame z lokalnego VADProcessor, NIE o TranscriptionFrame
    — poprawka 2026-08-18, patrz komentarz przy anchorze niżej i docstring modułu wyżej.
    TranscriptionFrame zostaje TYLKO do odświeżania idle_since/awaiting_model_response_since
    (potwierdzenie że Gemini realnie usłyszał treść) i do logowania transkryptu — jako sygnał
    czasowy jest bezużyteczny, bo przychodzi dopiero PO tym jak Gemini skończy przetwarzać
    CAŁĄ wypowiedź klienta (nawet kilkanaście sekund mowy+namysłu).

    ⚠️ BUG złapany na żywym telefonie (17.08.2026): jeśli klient mówi dłużej niż
    IDLE_WARNING_SECONDS, watchdog nie ma o tym pojęcia (idle_since stoi w miejscu od
    końca ostatniej wypowiedzi bota) i odpala nudge "czy nadal jesteśmy połączeni?"
    W ŚRODKU wypowiedzi klienta — transkrypcja przychodzi dosłownie sekundę PO nudge'u.
    Fix: VADUserStartedSpeakingFrame/UserSpeakingFrame z VADProcessor (patrz pipeline
    niżej) — lokalna analiza audio, bez czekania na Gemini. UserSpeakingFrame leci co
    ~0.2s PRZEZ CAŁY czas trwania mowy (nie tylko na starcie), więc idle_since jest
    stale odświeżane podczas długiej wypowiedzi, nie tylko w jej pierwszej sekundzie —
    to jest kluczowe, bo sam VADUserStartedSpeakingFrame (jednorazowy, na starcie)
    NIE wystarczyłby dla dłuższych wypowiedzi. Zweryfikowane w źródle pipecat: te typy
    ramek NIE dziedziczą po UserStartedSpeakingFrame, więc GeminiLiveLLMService (który ma
    własny handler na UserStartedSpeakingFrame, wysyłający activity_start do Gemini gdy
    self._vad_disabled=True) w ogóle ich nie złapie — a nawet gdyby złapał, ten kod jest
    i tak wyłączony w naszej konfiguracji (używamy domyślnego, serwerowego VAD Gemini,
    nie GeminiVADParams(disabled=True)). Zero wpływu na barge-in/turn-taking Gemini."""

    def __init__(self, state: dict):
        super().__init__()
        self._state = state

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, (VADUserStartedSpeakingFrame, UserSpeakingFrame)):
            self._state["idle_since"] = time.time()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            # Anchor pomiaru TTFB/"user->bot audio" — patrz poprawka 2026-08-18: lokalny VAD
            # (moment realnego końca mowy) zamiast TranscriptionFrame (przychodzi z boku,
            # zmierzone opóźnienie względem VAD-stop na żywej rozmowie: ~2.8s), więc dawało
            # to fałszywie niskie liczby ("286ms 🟢" przy realnym TTFB logowanym przez
            # GeminiLiveLLMService na 3.1s). Nadpisywane przy KAŻDYM VAD-stopie w obrębie tej
            # samej tury (klient robi pauzę, potem mówi dalej -> kilka VAD-stopów zanim padnie
            # EndOfTurnState.COMPLETE) — więc w momencie gdy bot faktycznie odpowie, tu i tak
            # zostaje timestamp OSTATNIEGO, właściwego końca wypowiedzi.
            self._state["last_user_frame"] = asyncio.get_event_loop().time()
            self._state["waiting_for_bot_audio"] = True
        elif isinstance(frame, TranscriptionFrame):
            self._state["idle_since"] = time.time()
            # Klient realnie coś powiedział — model MUSI zareagować. Jeśli nie zareaguje
            # w SILENT_HANG_TIMEOUT sekund, monitor_gemini_call_health uzna sesję za
            # zawieszoną (patrz "awaiting_model_response_since" niżej). Zostaje na
            # TranscriptionFrame (nie VAD-stop) świadomie — to jedyny sygnał potwierdzający,
            # że Gemini realnie usłyszał treść, a nie sam szum/krótki VAD blip.
            self._state["awaiting_model_response_since"] = time.time()
            logger.info(f"⏱️ [GEMINI LIVE/USER] transkrypcja: {frame.text!r}")
        await self.push_frame(frame, direction)


class GeminiBotMonitor(FrameProcessor):
    """Łapie tekst i audio bota (oba lecą DOWNSTREAM z llm, więc ta klasa siedzi PO
    llm — symetrycznie do GeminiUserMonitor, który siedzi PRZED).

    Odświeżanie idle_since na TTSStarted/TTSAudioRawFrame/TTSStoppedFrame + honorowanie
    suppress_idle_reset — logika 1:1 skopiowana z BotAudioMonitor (bot_openai_realtime.py,
    tam pełny docstring z historią 3 warstw bugów). Nie odkrywam tu koła na nowo —
    to już raz znaleziony i sprawdzony na żywym telefonie mechanizm."""

    BOT_STOP_GRACE_SECONDS = 1.2

    def __init__(self, state: dict):
        super().__init__()
        self._state = state
        self._heard_any_bot_audio = False

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        now_loop = asyncio.get_event_loop().time()

        if isinstance(frame, TTSTextFrame) and frame.text:
            logger.info(f"⏱️ [GEMINI LIVE/BOT] mówi: {frame.text!r}")
            # Najwcześniejszy możliwy dowód że model żyje — patrz "awaiting_model_response_since"
            # w make_gemini_state(). Zdejmujemy tu, nie dopiero na audio, żeby watchdog nie
            # zdążył wystrzelić fałszywie w wąskim oknie między startem generowania a audio.
            self._state["awaiting_model_response_since"] = None
            # Model realnie odpowiedział — jeśli mieliśmy za sobą reconnect po cichym zawieszeniu,
            # to znaczy że sesja faktycznie wróciła do zdrowia. Odblokuj jedną próbę reconnectu
            # na wypadek gdyby ucichła ZNOWU później w tej samej, długiej rozmowie.
            self._state["silent_hang_reconnect_used"] = False

        if isinstance(frame, TTSStartedFrame):
            if not self._state.get("suppress_idle_reset"):
                self._state["idle_since"] = time.time()

        if isinstance(frame, TTSAudioRawFrame):
            self._state["greeted"] = True
            if not self._heard_any_bot_audio:
                self._heard_any_bot_audio = True
                logger.info("⏱️ [GEMINI LIVE] Pierwsza ramka audio bota dotarła (np. powitanie)")
            if not self._state.get("suppress_idle_reset"):
                now = time.time()
                duration_s = len(frame.audio) / (frame.sample_rate * frame.num_channels * 2)
                playback_until = max(self._state.get("audio_playback_until", now), now) + duration_s
                self._state["audio_playback_until"] = playback_until
                self._state["idle_since"] = playback_until
            if self._state["waiting_for_bot_audio"]:
                self._state["waiting_for_bot_audio"] = False
                start = self._state.get("last_user_frame")
                if start:
                    ms = (now_loop - start) * 1000
                    icon = "🟢" if ms < 1500 else "🟡" if ms < 2500 else "🔴"
                    logger.info(f"⏱️ [GEMINI LIVE/TOTAL] user->bot audio {ms:.0f}ms {icon}")

        if isinstance(frame, TTSStoppedFrame):
            # ⚠️ DRUGI, NOWY BUG złapany na żywym telefonie (16.08.2026, ta sama rozmowa co
            # sample_rate fix wyżej): jeśli TTSStoppedFrame z wymuszonej dogrywki
            # (suppress_idle_reset=True — idle-nudge "czy nadal jesteśmy połączeni?", ostrzeżenie
            # o limicie czasu) TEŻ resetuje idle_since, to watchdog NIGDY nie osiąga progu
            # rozłączenia — sam nudge, wywołany WŁAŚNIE DLATEGO że klient milczy, zerował własny
            # zegar ciszy i powodował nieskończoną pętlę dopytywania zamiast rozłączenia po
            # ustalonym czasie (potwierdzone: klient zgłosił dokładnie ten objaw). TTSStartedFrame/
            # TTSAudioRawFrame wyżej już poprawnie honorują suppress_idle_reset (nie ruszają
            # idle_since podczas dogrywki) — ten branch był jedyną niespójnością.
            #
            # Fix: dla wymuszonych dogrywek NIE resetuj idle_since (silence rośnie dalej, nie
            # przerywana przez własne nagabywanie) — tylko zdejmij flagę, i to od razu tutaj
            # (precyzyjniej niż timeout 8s w speak_directly), żeby kolejna PRAWDZIWA odpowiedź
            # Gemini w tym samym oknie znów poprawnie resetowała zegar. Realne odpowiedzi bota
            # (suppress_idle_reset=False) resetują jak dotychczas.
            if self._state.get("suppress_idle_reset"):
                self._state["suppress_idle_reset"] = False
            else:
                self._state["idle_since"] = time.time() + self.BOT_STOP_GRACE_SECONDS

        await self.push_frame(frame, direction)


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


def build_gemini_live_llm(system_prompt: str, tools: list | None = None, voice: str | None = None):
    """Analogiczne do build_realtime_llm() wyżej, ale dla Gemini Live.

    tools: lista FunctionSchema — DOKŁADNIE ten sam format co dla OpenAI Realtime
    (pipecat.adapters.schemas.function_schema.FunctionSchema jest zamierzenie
    provider-agnostic, GeminiLiveLLMService konwertuje ją pod spodem przez
    GeminiLLMAdapter — sprawdzone w źródle pipecat), więc realtime_tools.py::build_*_tool
    są reużywane WPROST, bez żadnej gemini-specyficznej wersji.

    voice: per-tenant (tenant.get("gemini_voice"), NA RAZIE bez UI w panelu — jak
    "realtime_voice" dla OpenAI zanim dostał zakładkę). Fallback "Kore" — #1 damski
    głos PL wg rankingu użytkownika (11.08.2026). Osobne pole nazwy od "realtime_voice",
    bo zestawy nazw głosów OpenAI i Gemini się NIE pokrywają (np. "cedar" nic nie znaczy
    dla Gemini, "Kore" nic nie znaczy dla OpenAI) — wspólne pole ryzykowałoby wysłaniem
    złej nazwy do złego dostawcy.

    ⚠️ BRAK kontroli tempa/prędkości mówienia — sprawdzone w źródle pipecat:
    GeminiLiveLLMService.Settings nie ma odpowiednika OpenAI Realtime AudioOutput(speed=...).
    To ograniczenie samego Gemini Live API, nie brak wpięcia z naszej strony — nie da się
    obecnie tego podpiąć pod "speaking_rate" tak jak działa to dla OpenAI Realtime/cascade.

    settings=... (zamiast przestarzałych kwargs model=/voice_id=) — świadomie żeby móc
    ustawić language=Language.PL. Bug znaleziony na żywym telefonie: bez tego domyślny
    język transkrypcji to EN_US (patrz InputParams w źródle pipecat), więc pierwsze
    tury rozmowy transkrybowały się jako bełkot w losowych językach (niemiecki,
    hiszpański, portugalski) zanim model jakoś "złapał" polski w dalszej części rozmowy.
    ⚠️ To ustawia język TYLKO dla generowania odpowiedzi (speech_config.language_code) —
    sprawdzone w źródle: input_audio_transcription (rozpoznawanie mowy KLIENTA) jest
    tworzone przez pipecat 1.4.0 BEZ żadnej podpowiedzi językowej (pusty
    AudioTranscriptionConfig()), mimo że google-genai SDK wspiera pole `language_codes`
    właśnie do tego. Pipecat 1.4.0 tego pola jeszcze nie przekazuje — to prawdopodobnie
    prawdziwa przyczyna sporadycznego bełkotu w obcym języku w transkrypcji KLIENTA,
    obserwowanego na żywych telefonach nawet po ustawieniu language=Language.PL. Naprawa
    wymagałaby nadpisania wewnętrznej metody connect() biblioteki (fragile, może się
    zepsuć przy update pipecat) — świadomie NIE zrobione teraz, bo problem wystąpił
    rzadko (0 razy w 2 ostatnich pełnych testach) i ryzyko łatki nie jest tego warte.

    HYBRYDA GŁOSU (dodana i usunięta 2026-09-01): próba podmiany natywnego głosu
    Gemini na ElevenLabs (przez blokowanie własnego audio Gemini filtrem i syntezę
    transkryptu przez zewnętrzny TTS) — po pomiarze na żywych połączeniach usunięta:
    ~4.1s śr. user->bot audio z ElevenLabs vs ~2.4s natywnie (SimpleTextAggregator
    czeka na całe zdanie z Gemini zanim wyśle tekst do TTS), różnica w jakości głosu
    nie rekompensowała tej różnicy w opóźnieniu. Zostajemy przy DOMYŚLNYM
    modalities=AUDIO (nie ustawiane tu jawnie) i natywnym głosie Gemini zawsze.

    thinking=ThinkingConfig(thinking_level="minimal") — POPRAWKA 2026-08-22, analiza
    latencji po testowych rozmowach (TTFB 1.3-2.8s na zwykłych turach, nie ~0.6s jak
    sugerowała wcześniejsza notatka w CLAUDE.md o samym powitaniu). Sprawdzone w źródle
    pipecat 1.4.0 (services/google/gemini_live/llm.py): gdy `thinking` zostaje None (co
    było tu domyślnie), pipecat NIE wysyła w ogóle thinking_config do Gemini — a
    ThinkingConfig w google-genai SDK dokumentuje że dla modeli Gemini 3 (nasz
    gemini-3.1-flash-live-preview się łapie) brak jawnego ustawienia oznacza domyślne
    "high". Dla porównania: zwykły (nie-Live) GoogleLLMService w tej samej wersji pipecat
    MA wbudowaną automatyczną optymalizację ustawiającą minimalny thinking dla modeli
    flash właśnie w scenariuszach realtime — wariant Live tej automatyki nie ma, trzeba
    ręcznie. Kompromis: "minimal" to maksymalna szybkość kosztem najmniejszego namysłu
    modelu — jeśli po wdrożeniu pogorszy się trzymanie sztywnych reguł promptu (np.
    dosłowne say_exactly przy rezerwacji, wybór właściwej funkcji kontaktowej) rozważyć
    podniesienie do "low"."""
    resolved_voice = voice or "Kore"
    logger.info(f"🧠 Gemini Live, model={GEMINI_LIVE_MODEL}, voice={resolved_voice}, tools={[t.name for t in (tools or [])]}")
    llm = GeminiLiveLLMService(
        api_key=os.getenv("GOOGLE_API_KEY"),
        settings=GeminiLiveLLMService.Settings(
            model=GEMINI_LIVE_MODEL,
            voice=resolved_voice,
            language=Language.PL,
            thinking=ThinkingConfig(thinking_level="minimal"),
        ),
        system_instruction=system_prompt,
    )
    context = LLMContext(tools=tools or [])
    # realtime_service_mode=True — patrz docstring GeminiLiveLLMService: usługa nie
    # emituje UserStarted/StoppedSpeakingFrame, więc zapisy do kontekstu muszą iść
    # w trybie "trailing" (tak samo jak dla OpenAI Realtime w bot_openai_realtime.py).
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context, realtime_service_mode=True
    )
    # context zwracany osobno — ten sam powód co w build_realtime_llm (raport rozmowy
    # + transkrypt czytają context.get_messages() po zakończeniu połączenia).
    return llm, user_aggregator, assistant_aggregator, context
