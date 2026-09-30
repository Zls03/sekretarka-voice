"""Stan rozmowy OpenAI Realtime i procesory ramek śledzące mowę klienta i bota.

Stan to słownik tworzony osobno dla każdej rozmowy (równoległe rozmowy nie mogą
dzielić zegara ciszy). Znaczenie pól — patrz app/engines/gemini_live/monitors.py;
tutaj `idle_since` jest ustawiany ponownie przy starcie nadzoru, żeby czas zestawiania
połączenia nie liczył się jako cisza klienta.
"""

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


def make_call_state() -> dict:
    now = time.time()
    return {
        "last_user_frame": None,
        "waiting_for_bot_audio": False,
        "idle_since": now,
        "suppress_idle_reset": False,
        "audio_playback_until": now,
        "ended": False,
        # Dopóki nie padło powitanie, cisza się nie liczy — wolne powitanie (nawet 11 s)
        # wywoływało wcześniej dopytanie o połączenie, zanim klient cokolwiek usłyszał.
        "greeted": False,
    }


class UserTranscriptMonitor(FrameProcessor):
    """Mowa klienta wg serwerowego VAD OpenAI: zegar ciszy i punkt startu pomiaru opóźnienia.

    Transkrypcja przychodzi asynchronicznie, więc służy tylko do logowania.
    """

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
    """Mowa bota: przesuwa zegar ciszy na szacowany koniec ODTWARZANIA u klienta.

    Samo "start/stop wypowiedzi" nie wystarcza: długa odpowiedź przekraczała próg ciszy
    w trakcie mówienia, a model generuje audio nierównymi paczkami, szybciej niż klient
    go słucha. Dlatego sumujemy długość paczek (audio_playback_until).

    Komunikaty skryptowe (say_now, flaga suppress_idle_reset) nie przesuwają zegara w
    trakcie trwania — inaczej dopytanie o ciszę odsuwałoby rozłączenie w nieskończoność.
    Po ich zakończeniu zegar startuje od nowa jak po każdej wypowiedzi bota (w tym
    silniku klient często odpowiada tuż po dopytaniu).
    """

    # Zapas na opóźnienie odtwarzania po stronie operatora po zakończeniu naszego audio.
    BOT_STOP_GRACE_SECONDS = 1.2

    def __init__(self, state: dict):
        super().__init__()
        self._state = state

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSTextFrame) and frame.text:
            logger.info(f"⏱️ [BOT] mówi: {frame.text!r}")
        if isinstance(frame, TTSStartedFrame) and not self._state.get("suppress_idle_reset"):
            self._state["idle_since"] = time.time()
        if isinstance(frame, TTSAudioRawFrame):
            self._state["greeted"] = True
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
                    ms = (asyncio.get_event_loop().time() - start) * 1000
                    icon = "🟢" if ms < 1500 else "🟡" if ms < 2500 else "🔴"
                    logger.info(f"⏱️ [TOTAL user->bot audio] {ms:.0f}ms {icon}")
        if isinstance(frame, TTSStoppedFrame):
            if self._state.get("suppress_idle_reset"):
                self._state["suppress_idle_reset"] = False
            self._state["idle_since"] = time.time() + self.BOT_STOP_GRACE_SECONDS
        await self.push_frame(frame, direction)
