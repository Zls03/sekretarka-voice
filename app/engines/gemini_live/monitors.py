"""Stan rozmowy Gemini Live i procesory ramek śledzące mowę klienta i bota."""

import asyncio
import time

from loguru import logger
from pipecat.frames.frames import (
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
    UserSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameProcessor


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
        "silent_hang_reconnect_used": False,  # Reconnect po cichym zawieszeniu próbujemy TYLKO
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

        if isinstance(frame, TTSStartedFrame) and not self._state.get("suppress_idle_reset"):
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
