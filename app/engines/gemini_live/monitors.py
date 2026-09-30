"""Stan rozmowy Gemini Live i procesory ramek śledzące mowę klienta i bota.

Stan jest zwykłym słownikiem współdzielonym przez monitory, watchdog i narzędzia:
  idle_since            — moment ostatniej aktywności (zegar ciszy watchdoga),
  suppress_idle_reset   — trwa nasz komunikat skryptowy; nie resetuje zegara ciszy,
  audio_playback_until  — szacowany koniec odtwarzania audio bota u klienta,
  greeted               — padło już powitanie (wcześniej ciszy nie liczymy),
  awaiting_model_response_since — klient coś powiedział, czekamy na reakcję modelu
                          (dłużej niż SILENT_HANG_TIMEOUT = sesja ucichła),
  silent_hang_reconnect_used — jedyna próba reconnectu po cichym zawieszeniu już zużyta,
  last_user_frame / waiting_for_bot_audio — pomiar opóźnienia odpowiedzi,
  ended                 — rozmowa się kończy.
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
        "idle_since": now,
        "suppress_idle_reset": False,
        "audio_playback_until": now,
        "ended": False,
        "greeted": False,
        "awaiting_model_response_since": None,
        "silent_hang_reconnect_used": False,
    }


def _latency_icon(ms: float) -> str:
    return "🟢" if ms < 1500 else "🟡" if ms < 2500 else "🔴"


class GeminiUserMonitor(FrameProcessor):
    """Śledzi mowę klienta. Musi stać PRZED usługą Gemini — ta wysyła transkrypcje w górę pipeline'u.

    - Ramki lokalnego VAD (start/trwanie mowy) odświeżają zegar ciszy przez całą
      wypowiedź, więc dopytanie "czy nadal jesteśmy połączeni?" nie wpadnie w środek
      długiej wypowiedzi klienta. Gemini sam nie emituje zdarzeń mowy użytkownika.
    - Koniec mowy wg lokalnego VAD to punkt odniesienia pomiaru opóźnienia — transkrypcja
      Gemini przychodzi ~3 s później i zaniżałaby wynik.
    - Transkrypcja potwierdza, że model faktycznie usłyszał treść: od tej chwili czekamy
      na jego reakcję (wykrywanie cichego zawieszenia sesji).
    """

    def __init__(self, state: dict):
        super().__init__()
        self._state = state

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, (VADUserStartedSpeakingFrame, UserSpeakingFrame)):
            self._state["idle_since"] = time.time()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            # Nadpisywane przy każdej pauzie — zostaje moment OSTATNIEGO końca mowy w turze.
            self._state["last_user_frame"] = asyncio.get_event_loop().time()
            self._state["waiting_for_bot_audio"] = True
        elif isinstance(frame, TranscriptionFrame):
            self._state["idle_since"] = time.time()
            self._state["awaiting_model_response_since"] = time.time()
            logger.info(f"⏱️ [GEMINI LIVE/USER] transkrypcja: {frame.text!r}")
        await self.push_frame(frame, direction)


class GeminiBotMonitor(FrameProcessor):
    """Śledzi mowę bota (stoi ZA usługą Gemini) i przesuwa zegar ciszy na czas jej trwania.

    Zegar przesuwamy na szacowany koniec ODTWARZANIA u klienta (suma długości paczek
    audio), a nie na moment ich odebrania — model generuje audio szybciej, niż klient
    go słucha, z przerwami. Komunikaty skryptowe (suppress_idle_reset) zegara nie
    resetują — inaczej samo dopytanie o ciszę odsuwałoby rozłączenie w nieskończoność.
    """

    # Zapas na opóźnienie odtwarzania po stronie operatora po zakończeniu naszego audio.
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
            # Pierwszy dowód, że model żyje — zdejmujemy czekanie już na tekście, nie na audio.
            self._state["awaiting_model_response_since"] = None
            # Sesja odpowiada — gdyby znów ucichła, przysługuje nowa próba reconnectu.
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
                    logger.info(f"⏱️ [GEMINI LIVE/TOTAL] user->bot audio {ms:.0f}ms {_latency_icon(ms)}")

        if isinstance(frame, TTSStoppedFrame):
            if self._state.get("suppress_idle_reset"):
                # Koniec komunikatu skryptowego: zdejmujemy flagę, zegar ciszy biegnie dalej.
                self._state["suppress_idle_reset"] = False
            else:
                self._state["idle_since"] = time.time() + self.BOT_STOP_GRACE_SECONDS

        await self.push_frame(frame, direction)
