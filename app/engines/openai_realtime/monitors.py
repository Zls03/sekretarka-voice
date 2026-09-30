"""Stan rozmowy OpenAI Realtime i procesory ramek śledzące mowę klienta i bota."""

import asyncio
import time

from loguru import logger
from pipecat.frames.frames import (
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameProcessor


# `_t_state` był wcześniej zmienną globalną modułu — przy jednej rozmowie na raz
# w teście to nie szkodziło, ale teraz stan zasila też logikę idle/max-duration,
# która MUSI być per-połączenie (dwie równoległe rozmowy nie mogą dzielić zegara
# ciszy). Stąd każdy websocket handler tworzy własny `call_state` dict i wstrzykuje
# go do obu monitorów poniżej.
#
# `idle_since` = moment ostatniej aktywności (user zaczął/skończył mówić, bot
# zaczął/skończył mówić) — okrąża go monitor_call_health(), licząc ciszę jako
# czas odkąd NIKT (ani user, ani bot) nic nie robi. To ten sam pomysł co
# UserIdleController w pipecat 1.4.0 (start timer na BotStoppedSpeaking, cancel na
# UserStarted/BotStarted), tylko zaimplementowany ręcznie prostą pętlą asyncio —
# UserIdleController to osobny BaseObject z własnym cyklem życia/task managerem,
# niepotrzebna komplikacja dla testowego pliku, gdzie i tak mamy już asyncio loop
# wzorowany na bot.py::check_max_duration().
def make_call_state() -> dict:
    now = time.time()
    return {
        "last_user_frame": None,       # event-loop time, tylko do pomiaru TTFB
        "waiting_for_bot_audio": False,
        "idle_since": now,             # wall-clock, do wykrywania ciszy — nadpisywany
                                        # ponownie w monitor_call_health() przy starcie,
                                        # żeby nie liczyć czasu setupu (CRM, VAD, connect)
                                        # jako "ciszy klienta"
        "suppress_idle_reset": False,  # True = kolejny TTSStoppedFrame to say_now()
                                        # (dopytanie/pożegnanie), nie prawdziwa tura bota
        "audio_playback_until": now,   # estymowany czas zakończenia odtwarzania zbuforowanego
                                        # audio (patrz BotAudioMonitor) — kumuluje realny czas
                                        # trwania paczek, nie tylko moment ich odebrania
        "ended": False,
        "greeted": False,              # True dopiero gdy padnie PIERWSZA ramka audio bota
                                        # (powitanie). Bug złapany na żywym telefonie 16.08.2026:
                                        # gdy TTFB powitania był anomalnie wolny (11s zamiast
                                        # ~0.7s), monitor_call_health i tak liczył ten czas jako
                                        # "ciszę klienta" i wystrzelił wymuszone "czy nadal jesteśmy
                                        # połączeni?" ZANIM klient usłyszał choćby powitanie —
                                        # transkrypt pokazał to wprost: (początek rozmowy) →
                                        # od razu wymuszona dogrywka, bez powitania między nimi.
    }


class UserTranscriptMonitor(FrameProcessor):
    """Mierzy koniec tury usera (TTFB) i odświeża zegar aktywności (idle detection).
    Anchor pomiaru to UserStoppedSpeakingFrame (sygnał serwerowego VAD OpenAI,
    input_audio_buffer.speech_stopped) — TranscriptionFrame jest asynchroniczny
    side-channel i przychodzi za późno/wcześnie do pomiaru czasu, zostaje tylko do logowania."""

    def __init__(self, state: dict):
        super().__init__()
        self._state = state

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, UserStartedSpeakingFrame):
            self._state["idle_since"] = time.time()
        if isinstance(frame, UserStoppedSpeakingFrame):
            self._state["last_user_frame"] = asyncio.get_event_loop().time()
            self._state["waiting_for_bot_audio"] = True
            self._state["idle_since"] = time.time()
        if isinstance(frame, TranscriptionFrame):
            logger.info(f"⏱️ [USER] transkrypcja: {frame.text!r}")
        await self.push_frame(frame, direction)


class BotAudioMonitor(FrameProcessor):
    """Łapie pierwszą ramkę audio bota (downstream, za LLM-em), liczy deltę od
    końca wypowiedzi usera, i resetuje zegar ciszy PRZEZ CAŁY CZAS TRWANIA
    wypowiedzi bota (powitanie, odpowiedź), nie tylko na jej start/koniec.

    ⚠️ HISTORIA BUGA (2 warstwy, obie znalezione na żywym telefonie):
    1) Zegar resetowany TYLKO na Stop — długa odpowiedź (kilka-kilkanaście sekund
       audio) nie resetowała zegara dopóki się nie skończyła, więc licznik ciszy
       dalej liczył od momentu kiedy KLIENT ostatnio przestał mówić, i przekraczał
       próg ZANIM bot skończył. Fix: reset także na Start.
    2) Reset na START NIE WYSTARCZYŁ — to tylko przesuwa punkt odniesienia, nie
       chroni całej wypowiedzi. Jeśli SAMO audio bota trwa dłużej niż próg (np.
       dwuzdaniowa odpowiedź prawnika ~12-15s), zegar mimo to wygasa W TRAKCIE
       mówienia bota, bo nic go nie odświeżało między Start a Stop. Potwierdzone
       w logu: TTSStoppedFrame dla danej tury czasem w ogóle nie pojawia się w
       oczekiwanym czasie (audio realnie jeszcze leci), a "processing time" z
       Realtime mierzy WYGENEROWANIE tekstu, nie odtworzenie audio — nie da się
       na nim polegać jako sygnale "bot skończył mówić".
    3) RESET NA "TERAZ" PRZY KAŻDEJ PACZCE TEŻ NIE WYSTARCZYŁ — potwierdzone na żywym
       telefonie: przy dłuższych, złożonych odpowiedziach model ma nierówne przerwy w
       GENEROWANIU kolejnych paczek audio (widać w logu jako nierówne odstępy między
       tokenami), a w takiej przerwie MY nic nie dostajemy, więc zegar resetowany do
       time.time() i tak zaczynał liczyć ciszę — mimo że telefon klienta W TEJ CHWILI
       WCIĄŻ ODTWARZA wcześniej wysłane, zbuforowane audio (WYSŁANIE paczki ≠ MOMENT
       jej odtworzenia). FIX: zamiast resetować do "teraz", kumulujemy estymowany czas
       zakończenia odtwarzania (audio_playback_until = poprzednia estymacja LUB teraz,
       cokolwiek późniejsze, + realny czas trwania tej paczki z jej rozmiaru/sample_rate)
       — to poprawnie przetrwa przerwy w GENEROWANIU, bo bufor po stronie klienta nie
       jest pusty tylko dlatego że MY akurat nic nowego nie wysłaliśmy.
    Start/Stop zostają jako dodatkowe warstwy (pierwsza/ostatnia ramka, grace period po Stop).

    ⚠️ GRACE PERIOD po Stop (BOT_STOP_GRACE_SECONDS): ochrona przed opóźnieniem
    odtwarzania u dostawcy telefonii (Vonage) — nasz TTSStoppedFrame odpala się
    gdy MY skończymy wysyłać audio, ale telefon klienta może je jeszcze przez
    chwilę odtwarzać. Bez tego zapasu cisza mogłaby zacząć się liczyć nieco
    wcześniej niż realnie klient przestał słyszeć bota.

    ⚠️ WYJĄTEK: automatyczne dopytanie/pożegnanie z say_now() (monitor_call_health) jest
    OZNACZONE flagą `call_state["suppress_idle_reset"]` — ŻADNA ramka (Start/Stop/Audio)
    TEJ konkretnej wypowiedzi nie rusza zegara (inaczej własne "Halo?" bota resetowałoby
    zegar który ma doprowadzić do rozłączenia, i bot pytałby w kółko bez końca —
    dokładniej opisane w monitor_call_health). Flaga jest czyszczona dopiero na Stopped
    (plus samoczyszczący timeout w say_now, patrz tam), więc zostaje aktywna przez całą
    wypowiedź nudge'a, nie tylko pierwszą napotkaną ramkę."""

    BOT_STOP_GRACE_SECONDS = 1.2

    def __init__(self, state: dict):
        super().__init__()
        self._state = state

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSTextFrame) and frame.text:
            logger.info(f"⏱️ [BOT] mówi: {frame.text!r}")
        if isinstance(frame, TTSStartedFrame):
            if not self._state.get("suppress_idle_reset"):
                self._state["idle_since"] = time.time()
        if isinstance(frame, TTSAudioRawFrame):
            self._state["greeted"] = True
            if not self._state.get("suppress_idle_reset"):
                # Ciągłe odświeżanie — patrz punkt (3) w docstringu klasy. To jest
                # GŁÓWNA linia obrony, Start/Stop to tylko brzegowe uzupełnienie.
                # Estymujemy KIEDY ta paczka faktycznie skończy grać, nie kiedy ją
                # odebraliśmy — kumulacja czasu trwania audio, odporna na przerwy
                # w generowaniu (bufor po stronie klienta nie jest wtedy pusty).
                now = time.time()
                duration_s = len(frame.audio) / (frame.sample_rate * frame.num_channels * 2)
                playback_until = max(self._state.get("audio_playback_until", now), now) + duration_s
                self._state["audio_playback_until"] = playback_until
                self._state["idle_since"] = playback_until
            if self._state["waiting_for_bot_audio"]:
                self._state["waiting_for_bot_audio"] = False
                start = self._state.get("last_user_frame")
                if start:
                    ms = (asyncio.get_event_loop().time() - start) * 1000
                    icon = "🟢" if ms < 1500 else "🟡" if ms < 2500 else "🔴"
                    logger.info(f"⏱️ [TOTAL user->bot audio] {ms:.0f}ms {icon}")
        if isinstance(frame, TTSStoppedFrame):
            # BUG (znaleziony na żywym telefonie 16.08.2026): poprzednio, gdy suppress_idle_reset
            # było True, ta gałąź TYLKO czyściła flagę i pomijała odświeżenie idle_since —
            # zegar zamrażał się na starej wartości sprzed nudge'a. Jeśli klient odpowiedział
            # realnie w trakcie/tuż po nudge'u, czas mówienia bota (odpowiedź na PRAWDZIWE
            # pytanie) i tak liczył się jako "cisza", aż przekraczał IDLE_HANGUP_SECONDS i
            # rozłączał połączenie mimo aktywnej rozmowy. Flaga ma chronić TYLKO ramki Start/Audio
            # PODCZAS wypowiedzi nudge'a (żeby jego własne audio nie resetowało zegara w kółko) —
            # PO jej zakończeniu zegar zawsze powinien wystartować na nowo, tak jak po każdej
            # innej wypowiedzi bota.
            if self._state.get("suppress_idle_reset"):
                self._state["suppress_idle_reset"] = False
            self._state["idle_since"] = time.time() + self.BOT_STOP_GRACE_SECONDS
        await self.push_frame(frame, direction)
