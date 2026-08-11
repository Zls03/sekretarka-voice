# bot_gemini_test.py — orkiestrator Fazy 1-2-4 migracji Cascade -> OpenAI Realtime
# (patrz CLAUDE.md). FastAPI, webhooki, websockety, transport, monitoring/idle.
"""
Historia: ten plik powstał jako izolowany test latencji audio-to-audio (Gemini Live
vs OpenAI Realtime) na tenantcie testowym (firm_1774140338448_8905c, Vonage). Wyniki
(patrz tabelka w CLAUDE.md) przesądziły o wyborze OpenAI Realtime (gpt-realtime-2.1-mini,
~0.6s user->bot, wszystko 🟢). Od tego commitu plik realizuje Fazy 1-2-4 planu migracji:
prawdziwy system prompt z danych panelu (cennik, godziny, adres, FAQ, ton branży,
tożsamość asystenta) + personalizacja powitania dla powracającego klienta (CRM),
wykrywanie ciszy (dopytanie/rozłączenie) i limit czasu rozmowy, oraz function-calling
(contact_owner, end_conversation).

PODZIAŁ NA 3 PLIKI (zrobiony gdy ten plik przekroczył ~1200 linii, przed Fazą 3, żeby
nie robiło się jeszcze gorzej — Faza 3 dopisze rezerwacje, najbardziej złożoną część):
  - bot_gemini_test.py (TEN plik) — orkiestrator: FastAPI, webhooki, websockety,
    transport, monitoring/idle/latencja. Odpowiednik roli bot.py.
  - realtime_prompt.py — budowanie system_instruction (tożsamość/styl/biznes/CRM/greeting).
    Odpowiednik roli flows_helpers.py.
  - realtime_tools.py — function-calling tools (contact_owner, end_conversation, wysyłka
    emaila). Odpowiednik roli flows_contact.py / flows_booking_simple.py.

KOLEJNOŚĆ FAZ ŚWIADOMIE ODWRÓCONA względem CLAUDE.md: Faza 4 (kontakt/zgłoszenia) PRZED
Fazą 3 (rezerwacje) — booking jest najbardziej ryzykowną częścią (błąd = podwójna
rezerwacja/zmyślony termin) i wymaga jeszcze podpięcia Google Calendar, więc lepiej
dopracować prostszy tryb informacyjny i function-calling na niższą stawkę (contact_owner)
zanim zabierzemy się za booking.

Gemini Live wcześniej USUNIĘTY (decyzja zapadła na rzecz OpenAI Realtime) — ale
DOŁOŻONY z powrotem na końcu pliku (sekcja "-gemini-live") jako doraźny, ubogi test
porównawczy (2026-08-11), bo Gemini wypuściło gemini-3.1-flash-live-preview i warto
sprawdzić latencję. Świadomie osobne route'y, zero ingerencji w sekcję OpenAI
Realtime wyżej — patrz docstring tamtej sekcji.

NIE dotyka produkcyjnego bot.py. Zero FlowManagera. Treść promptu (realtime_prompt.py)
jest ŚWIADOMIE skopiowana z flows.py/flows_helpers.py zamiast zaimportowana stamtąd
wprost — te moduły ciągną `pipecat_flows`, spięty z pipecat-ai==0.0.104 (stary kontekst
OpenAILLMContext) — import pod pipecat-ai==1.4.0 (wymagany tu do OpenAIRealtimeLLMService)
byłby kruchy. Patrz docstring realtime_prompt.py po pełne wyjaśnienie.

WYMAGANE ZMIENNE ŚRODOWISKOWE (te same co w Railway):
  OPENAI_API_KEY       — klucz OpenAI Realtime
  TWILIO_AUTH_TOKEN    — do walidacji podpisu Twilio (opcjonalnie, można pominąć na testach)
  TEST_TENANT_ID        — wymuszony tenant dla ścieżki Vonage (patrz /vonage/answer)
  RESEND_API_KEY        — do wysyłki emaila w contact_owner (Faza 4) — bez tego funkcja
                          zwróci klientowi uczciwy błąd zamiast fałszywie potwierdzić wysyłkę

PODŁĄCZENIE (Twilio):
  1) Wybierz numer testowy w konsoli Twilio (osobny lub tymczasowo przełącz istniejący)
  2) W ustawieniach numeru: "A call comes in" -> Webhook
     POST https://<twoj-railway-host>/twilio/incoming-gemini-test
  3. To wystarczy — nic więcej w konfiguracji Twilio nie trzeba zmieniać.

PODŁĄCZENIE (Vonage): patrz sekcja "VONAGE" niżej — bez zmian względem wcześniejszej wersji.

URUCHOMIENIE OBOK ISTNIEJĄCEGO bot.py:
  Ten plik ma WŁASNY obiekt FastAPI (app), własny osobny deploy na Railway
  (requirements-gemini-test.txt, pipecat-ai==1.4.0 — CELOWO inna wersja niż produkcyjny
  bot.py na 0.0.104). Nie instalować obu requirements w tym samym środowisku.
  Start command bez zmian: `uvicorn bot_gemini_test:app` — realtime_prompt.py i
  realtime_tools.py to zwykłe pliki .py w tym samym repo, żadna konfiguracja Railway
  nie musi się zmienić.

CO ZOSTAJE NA PÓŹNIEJ (świadomie NIE tutaj):
  - Reszta Fazy 4: zbieranie zgłoszeń (lead collection, wieloturowe), SMS, raport email po rozmowie
  - Żywe przekierowanie rozmowy (transfer) — ani dla Twilio (brak /twilio/after-stream w tym
    pliku) ani dla Vonage (brak mechanizmu w ogóle, wymaga Vonage REST API) — patrz docstring
    realtime_tools.py. Działa TYLKO ścieżka "zostaw wiadomość" (email przez Resend).
  - Faza 3: sprawdz_dostepnosc()/zarezerwuj() jako function-calling tools (ostatnia, bo
    najbardziej ryzykowna — patrz wyżej)
  - Faza 5: credits + call_logs
  Prompt (realtime_prompt.py) wprost mówi klientowi, że rezerwacje/zgłoszenia/żywe
  przekierowanie są jeszcze w budowie — żeby model niczego nie obiecywał, czego nie umie wykonać.

FAZA 2 — jak działa wykrywanie ciszy/limitu (patrz monitor_call_health poniżej):
  10s ciszy -> "Przepraszam, czy nadal jesteśmy połączeni?" | 20s ciszy -> pożegnanie + rozłączenie
  | 4 min rozmowy - 30s -> uprzedzenie że kończymy | 4 min -> pożegnanie + rozłączenie.
  Realizowane przez say_now() (response.create z jednorazowym `instructions`), bo
  TTSSpeakFrame/LLMMessagesAppendFrame z cascade NIE działają z tą usługą (patrz
  komentarz przy say_now).
  Rozłączenie po pożegnaniu NIE czeka na realny koniec odtwarzania audio (Realtime
  nie daje eventu "TTS na pewno skończył mówić widziany z zewnątrz w porę do tego") —
  to stały sleep(3.0) po wysłaniu polecenia, potem EndFrame. Cascade (bot.py) robi
  DOKŁADNIE to samo (sleep 2.0-2.5s), więc to nie uproszczenie względem produkcji,
  tylko ten sam, już sprawdzony trik.
"""

import os
import sys
import json
import time
import asyncio

from loguru import logger
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import Response, JSONResponse

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask, PipelineParams
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketTransport,
    FastAPIWebsocketParams,
)
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.serializers.vonage import VonageFrameSerializer
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.frames.frames import (
    EndFrame, TranscriptionFrame, TTSAudioRawFrame, TTSTextFrame,
    TTSStartedFrame, TTSStoppedFrame,
    UserStartedSpeakingFrame, UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameProcessor

from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService
from pipecat.services.openai.realtime.events import (
    AudioConfiguration,
    AudioInput,
    AudioOutput,
    InputAudioTranscription,
    ResponseCreateEvent,
    ResponseProperties,
    SessionProperties,
    SessionUpdateEvent,
)
# Gemini Live — TYLKO do szybkiego testu porównawczego latencji (patrz sekcja na końcu
# pliku, route'y "-gemini-live"). Nie dotyka niczego z OpenAI Realtime powyżej.
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService

# Reużywamy istniejących modułów: helpers.py (odczyt danych firmy + CRM, bez zależności
# od pipecat — bezpieczny import wprost). Budowanie promptu i tools — osobne pliki,
# patrz docstring wyżej po co ten podział.
from helpers import get_tenant_by_phone, get_client_profile, db, saas_db
from realtime_prompt import build_realtime_instructions
from realtime_tools import (
    build_contact_owner_tool, build_end_conversation_tool, build_submit_lead_tool,
    maybe_send_call_summary, save_call_transcript, apply_call_charge, is_call_allowed,
)

logger.remove()
logger.add(sys.stdout, level="DEBUG", format="{time:HH:mm:ss} | {level} | {message}")

app = FastAPI()

OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1-mini")
# OpenAI Realtime nie ma osobnych głosów per-język (jak Google pl-PL-...) — to
# uniwersalne persony głosowe, które mówią w języku z tekstu/instrukcji. "marin" to
# obecnie flagowy, najbardziej naturalny głos OpenAI Realtime (stan na moją wiedzę).
OPENAI_REALTIME_VOICE = os.getenv("OPENAI_REALTIME_VOICE", "cedar")

# ==========================================
# PER-POŁĄCZENIE: pomiar latencji + wykrywanie ciszy (Faza 2)
# ==========================================
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
            if self._state.get("suppress_idle_reset"):
                self._state["suppress_idle_reset"] = False
            else:
                self._state["idle_since"] = time.time() + self.BOT_STOP_GRACE_SECONDS
        await self.push_frame(frame, direction)


# ==========================================
# IDLE TIMEOUT + MAX CALL DURATION (Faza 2)
# ==========================================

IDLE_WARNING_SECONDS = 10   # tyle ciszy -> "Halo, czy mnie słyszysz?"
IDLE_HANGUP_SECONDS = 20    # tyle ciszy (10s po dopytaniu) -> kończymy połączenie
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


def build_realtime_llm(
    system_prompt: str,
    tools: list | None = None,
    voice: str | None = None,
    speed: float | None = None,
):
    """Buduje OpenAIRealtimeLLMService + parę context aggregatorów.

    tools: lista FunctionSchema (z handler ustawionym na schemacie — LLMService
    rejestruje je automatycznie z LLMContext, bez osobnego register_function).

    voice/speed: per-tenant, NIE globalne — patrz wywołanie w websocket handlerach
    (czytane z tenant.get("realtime_voice")/tenant.get("speaking_rate"), z fallbackiem
    na OPENAI_REALTIME_VOICE/domyślne API gdy tenant jeszcze nic nie ustawił — to
    pozwala docelowo wybierać głos/tempo w panelu per-firma, tak jak już działa dla
    cascade, zamiast na sztywno w kodzie/zmiennej środowiskowej dla całego serwisu)."""
    resolved_voice = voice or OPENAI_REALTIME_VOICE
    logger.info(f"🧠 OpenAI Realtime, model={OPENAI_REALTIME_MODEL}, voice={resolved_voice}, speed={speed or 'domyślne API'}")
    llm = OpenAIRealtimeLLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        settings=OpenAIRealtimeLLMService.Settings(
            model=OPENAI_REALTIME_MODEL,
            system_instruction=system_prompt,
            # Niżej niż domyślne (OpenAI: 0.8) — mniej "kreatywnych" dopowiedzeń/wstawek
            # konwersacyjnych, bardziej dosłowne trzymanie się instrukcji z promptu.
            # 0.6 to udokumentowane minimum dla tego API (niżej API i tak by przycięło).
            temperature=0.6,
            session_properties=SessionProperties(
                # Twardy sufit na długość JEDNEJ odpowiedzi — zabezpieczenie przed rozgadaniem
                # się modelu (obserwowane wcześniej: 12s odpowiedź, patrz bug z idle timerem)
                # i przed kosztem pojedynczej odpowiedzi wymykającej się spod kontroli.
                # ~600 tokenów to z zapasem więcej niż jakakolwiek sensowna odpowiedź głosowa.
                max_output_tokens=600,
                audio=AudioConfiguration(
                    output=AudioOutput(voice=resolved_voice, speed=speed),
                    # Transkrypcja usera domyślnie WYŁĄCZONA w OpenAI — bez tego
                    # UserTranscriptMonitor nigdy nie widzi TranscriptionFrame.
                    # language="pl" wymuszony, bo auto-detekcja na krótkich,
                    # telefonicznych próbkach potrafi rozpoznać zupełnie inny język.
                    input=AudioInput(transcription=InputAudioTranscription(language="pl")),
                )
            ),
        ),
    )

    context = LLMContext(tools=tools or [])
    # realtime_service_mode=True: usługa realtime emituje inaczej UserStarted/StoppedSpeakingFrame,
    # więc zapisy do kontekstu muszą iść w trybie "trailing" zamiast czekać na te ramki.
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context, realtime_service_mode=True
    )
    # context zwracany też osobno — potrzebny na końcu rozmowy do raportu
    # (realtime_tools.py::maybe_send_call_summary czyta context.get_messages()).
    return llm, user_aggregator, assistant_aggregator, context


async def apply_crm_when_ready(llm: OpenAIRealtimeLLMService, tenant: dict, client_profile_task: asyncio.Task) -> dict | None:
    """Powitanie leci OD RAZU z generycznym promptem (bez czekania na CRM, ~2-3s HTTP
    do panelu) — ta funkcja czeka na wynik w tle i, jeśli okaże się że dzwoni znany
    klient, dosyła zaktualizowany prompt (session.update) w trakcie rozmowy, żeby
    dane CRM (historia wizyt) były dostępne gdy klient o nie zapyta. include_greeting=False
    (patrz realtime_prompt.py::build_realtime_instructions) — bez tego model mógłby
    zrozumieć aktualizację jako polecenie przywitania się jeszcze raz."""
    client_profile = await client_profile_task
    if client_profile:
        logger.info(f"👤 [REALTIME TEST] CRM (spóźniony): {client_profile.get('name')} (wizyty: {client_profile.get('visit_count', 0)})")
        updated_prompt = build_realtime_instructions(tenant, client_profile, include_greeting=False)
        await llm.send_client_event(SessionUpdateEvent(session=SessionProperties(instructions=updated_prompt)))
    return client_profile


# ==========================================
# TWILIO INCOMING (testowy webhook)
# ==========================================

@app.post("/twilio/incoming-gemini-test")
async def twilio_incoming_test(request: Request):
    form = await request.form()
    called = form.get("Called", form.get("To", ""))
    caller = form.get("From", "")
    call_sid = form.get("CallSid", "")

    logger.info(f"📞 [REALTIME TEST] Incoming: {caller} → {called} (CallSid: {call_sid})")

    tenant = await get_tenant_by_phone(called)
    if not tenant:
        return Response(
            content='<?xml version="1.0"?><Response><Say language="pl-PL">'
                    'Numer testowy nieaktywny.</Say></Response>',
            media_type="application/xml",
        )

    if not await is_call_allowed(tenant):
        return Response(
            content='<?xml version="1.0"?><Response><Say language="pl-PL">'
                    'Przepraszamy, linia jest chwilowo niedostępna.</Say><Hangup/></Response>',
            media_type="application/xml",
        )

    host = request.headers.get("host", "localhost")
    # phone zamiast tenantId — patrz komentarz w vonage_answer: unika drugiego
    # round-tripu do bazy w websocket handlerze, żeby odzyskać ten sam numer z ID.
    twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://{host}/ws-gemini-test">
            <Parameter name="callSid" value="{call_sid}" />
            <Parameter name="phone" value="{tenant['phone_number']}" />
            <Parameter name="callerPhone" value="{caller}" />
        </Stream>
    </Connect>
</Response>'''
    return Response(content=twiml, media_type="application/xml")


# ==========================================
# WEBSOCKET (testowy pipeline)
# ==========================================

@app.websocket("/ws-gemini-test")
async def websocket_gemini_test(websocket: WebSocket):
    await websocket.accept()
    logger.info("🔌 [REALTIME TEST] WebSocket connected")

    stream_sid = None
    tenant = None
    caller_phone = "nieznany"
    call_sid = None

    try:
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)
            event = data.get("event")

            if event == "connected":
                continue

            if event == "start":
                start_data = data.get("start", {})
                stream_sid = start_data.get("streamSid")
                custom_params = start_data.get("customParameters", {})
                tenant_phone = custom_params.get("phone")
                caller_phone = custom_params.get("callerPhone", "nieznany")
                call_sid = custom_params.get("callSid")

                if tenant_phone:
                    tenant = await get_tenant_by_phone(tenant_phone)
                break
    except Exception as e:
        logger.error(f"[REALTIME TEST] Błąd startu: {e}")
        await websocket.close()
        return

    if not stream_sid or not tenant:
        logger.error("❌ [REALTIME TEST] Brak stream_sid lub tenant — zamykam")
        await websocket.close()
        return

    logger.info(f"✅ [REALTIME TEST] Tenant: {tenant.get('name')}")

    # CRM lookup w tle — NIE czekamy na niego przed powitaniem (patrz apply_crm_when_ready
    # niżej): powitanie leci od razu generyczne, a jeśli CRM znajdzie znanego klienta,
    # prompt jest dosyłany w trakcie rozmowy (session.update).
    client_profile_task = asyncio.create_task(get_client_profile(tenant.get("id", ""), caller_phone))

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(confidence=0.6, start_secs=0.2, stop_secs=0.3, min_volume=0.4)
            ),
            serializer=TwilioFrameSerializer(
                stream_sid=stream_sid,
                params=TwilioFrameSerializer.InputParams(auto_hang_up=False),
            ),
        ),
    )

    task_box = {"task": None}
    context_box = {"context": None}
    call_state = make_call_state()
    tools = [
        build_contact_owner_tool(tenant, caller_phone, task_box, call_state),
        build_end_conversation_tool(task_box, call_state),
    ]
    if tenant.get("lead_mode", 0) == 1:
        # "Zbieranie zgłoszeń" — ten sam checkbox w panelu co w cascade. Warunkowe, jak tam:
        # tenanty bez tego trybu (np. salon fryzjerski) nie dostają narzędzia, którego i tak
        # by nie użyły — mniej szumu w tools = mniej okazji do pomyłki którego użyć.
        tools.append(build_submit_lead_tool(tenant, caller_phone, context_box))
    system_prompt = build_realtime_instructions(tenant, None)
    # Per-tenant głos/tempo (jeszcze bez UI w panelu — pole "realtime_voice" dopiero powstanie,
    # "speaking_rate" już istnieje, reużywany z cascade). Brak wartości = fallback na
    # OPENAI_REALTIME_VOICE / domyślne tempo API, więc nic się nie psuje zanim panel dojrzeje.
    realtime_voice = (tenant.get("realtime_voice") or "").strip() or None
    realtime_speed = float(tenant["speaking_rate"]) if tenant.get("speaking_rate") else None
    llm, user_aggregator, assistant_aggregator, llm_context = build_realtime_llm(
        system_prompt, tools=tools, voice=realtime_voice, speed=realtime_speed
    )
    context_box["context"] = llm_context
    user_transcript_monitor = UserTranscriptMonitor(call_state)
    bot_audio_monitor = BotAudioMonitor(call_state)

    pipeline = Pipeline([
        transport.input(),
        user_aggregator,
        user_transcript_monitor,
        llm,
        bot_audio_monitor,
        transport.output(),
        assistant_aggregator,
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
            audio_in_sample_rate=8000,
            audio_out_sample_rate=8000,
        ),
    )
    task_box["task"] = task

    @transport.event_handler("on_client_connected")
    async def on_connect(transport, client):
        logger.info("🎤 [REALTIME TEST] Klient połączony — wybudzam Realtime do przywitania")
        # Usługa realtime nie odzywa się pierwsza sama z siebie — trzeba popchnąć
        # pusty context frame, żeby wygenerowała pierwszą odpowiedź z system promptu.
        await user_aggregator.push_context_frame()
        asyncio.create_task(monitor_call_health(task, llm, call_state))
        asyncio.create_task(apply_crm_when_ready(llm, tenant, client_profile_task))

    @transport.event_handler("on_client_disconnected")
    async def on_disconnect(transport, client):
        logger.info("📴 [REALTIME TEST] Klient rozłączony")
        call_state["ended"] = True
        await task.queue_frame(EndFrame())

    runner = PipelineRunner()
    logger.info("🚀 [REALTIME TEST] Start pipeline")
    try:
        await runner.run(task)
    except Exception as e:
        logger.error(f"[REALTIME TEST] Pipeline error: {e}")
    finally:
        logger.info("🏁 [REALTIME TEST] Koniec połączenia")
        try:
            await maybe_send_call_summary(tenant, caller_phone, llm_context)
        except Exception as e:
            logger.error(f"[REALTIME TEST] Call summary error: {e}")
        try:
            await save_call_transcript(tenant, call_sid, caller_phone, llm_context)
        except Exception as e:
            logger.error(f"[REALTIME TEST] Call transcript error: {e}")


@app.get("/health-gemini-test")
async def health():
    return {"status": "ok", "provider": "openai-realtime"}


# ==========================================
# VONAGE — ścieżka alternatywna (obok Twilio) — TU jest test tenant
# ==========================================
"""
Vonage nie ma pojedynczego pola "webhook" na numerze — numer musi być
przypisany do Vonage "Application" (Voice), a ta aplikacja ma:
  - Answer URL (GET)  -> tu zwracamy NCCO (JSON, nie TwiML)
  - Event URL (POST)  -> status callback (odpowiednik Twilio /twilio/status)

Audio idzie jako surowe PCM 16-bit (nie base64 mu-law jak w Twilio),
dlatego osobny websocket + VonageFrameSerializer zamiast TwilioFrameSerializer.
Tenant przekazujemy przez query param w URI websocketu (Vonage na to pozwala),
więc nie trzeba parsować żadnego eventu "start" jak w Twilio.
"""


@app.get("/vonage/answer")
async def vonage_answer(request: Request):
    to_number = request.query_params.get("to", "")
    from_number = request.query_params.get("from", "")
    call_uuid = request.query_params.get("uuid", "")
    logger.info(f"📞 [REALTIME TEST/VONAGE] Answer: {from_number} → {to_number}")

    # Numer Vonage jest nowy i nie ma go w bazie tenantów — na czas testu
    # ładujemy istniejącego tenanta na sztywno przez zmienną środowiskową,
    # zamiast szukać po numerze (który i tak nie pasowałby do żadnego wpisu).
    forced_tenant_id = os.getenv("TEST_TENANT_ID", "")
    if forced_tenant_id:
        rows = await db.execute("SELECT phone_number FROM tenants WHERE id = ?", [forced_tenant_id])
        tenant = await get_tenant_by_phone(rows[0]["phone_number"]) if rows else None
        logger.info(f"📞 [REALTIME TEST/VONAGE] Using forced tenant_id={forced_tenant_id} -> {tenant.get('name') if tenant else 'NOT FOUND'}")
    else:
        tenant = await get_tenant_by_phone(to_number)

    if not tenant:
        ncco = [{"action": "talk", "text": "Numer testowy nieaktywny.", "language": "pl-PL"}]
        return JSONResponse(ncco)

    if not await is_call_allowed(tenant):
        ncco = [{"action": "talk", "text": "Przepraszamy, linia jest chwilowo niedostępna.", "language": "pl-PL"}]
        return JSONResponse(ncco)

    host = request.headers.get("host", "localhost")
    # Przekazujemy phone_number zamiast tenantId — mamy go już w `tenant` z lookupu
    # wyżej, więc websocket handler może wywołać get_tenant_by_phone() od razu,
    # zamiast najpierw robić ekstra round-trip do bazy żeby ten numer odzyskać z ID
    # (tak było wcześniej: tenantId -> SELECT phone_number -> get_tenant_by_phone,
    # czyli ten sam tenant ładowany DWA razy — to ~1-2s czystej straty na starcie
    # każdego połączenia, widoczne w logach jako drugie "Found firm").
    ws_uri = f"wss://{host}/ws-gemini-test-vonage?phone={tenant['phone_number']}&callerPhone={from_number}&callSid={call_uuid}"

    ncco = [
        {
            "action": "connect",
            "endpoint": [
                {
                    "type": "websocket",
                    "uri": ws_uri,
                    "content-type": "audio/l16;rate=16000",
                }
            ],
        }
    ]
    return JSONResponse(ncco)


@app.api_route("/vonage/events", methods=["GET", "POST"])
async def vonage_events(request: Request):
    """Status callback od Vonage — aktualizuje call_logs i nalicza minuty/kredyty.
    1:1 z bot.py::vonage_events (sama logika, port). Samodzielnie ustala tenanta po
    numerze "to" — NIE polega na tym że call_logs już istnieje, bo save_call_transcript()
    (koniec pipeline'u) i ten webhook to dwa niezależne w czasie zdarzenia, ten webhook
    może przyjść pierwszy.

    Vonage wysyła "completed" osobno dla KAŻDEJ nogi połączenia (inbound i outbound,
    ten sam numer "to", różne uuid) — przetwarzamy TYLKO direction=inbound, inaczej
    naliczylibyśmy podwójnie."""
    try:
        if request.method == "POST":
            data = await request.json()
        else:
            data = dict(request.query_params)
    except Exception:
        data = dict(request.query_params)

    status = data.get("status", "")
    call_uuid = data.get("uuid", "")
    duration_str = data.get("duration", "0")
    to_number = data.get("to", "")
    direction = data.get("direction", "")

    logger.info(f"[VONAGE EVENT] {call_uuid} | {status} | {duration_str}s | direction={direction}")

    if status != "completed" or not call_uuid:
        return Response(content="", status_code=200)

    if direction and direction != "inbound":
        logger.info(f"[VONAGE EVENT] Pomijam noga={direction} (liczymy tylko inbound)")
        return Response(content="", status_code=200)

    try:
        duration = int(duration_str) if duration_str else 0

        tenant = await get_tenant_by_phone(to_number) if to_number else None
        if not tenant:
            logger.warning(f"⚠️ [REALTIME TEST/VONAGE] Nie znaleziono tenanta dla {to_number}")
            return Response(content="", status_code=200)

        tenant_id = tenant["id"]
        is_saas_tenant = tenant.get("source") == "saas"
        target_db = saas_db if is_saas_tenant else db

        existing = await target_db.execute("SELECT id FROM call_logs WHERE call_sid = ?", [call_uuid])
        if existing:
            await target_db.execute(
                "UPDATE call_logs SET duration_seconds = ?, status = ? WHERE call_sid = ?",
                [duration, status, call_uuid],
            )
            logger.info(f"📊 [REALTIME TEST/VONAGE] Updated call log: {call_uuid} → {duration}s")
        else:
            await target_db.execute(
                """INSERT INTO call_logs
                   (id, tenant_id, call_sid, caller_phone, duration_seconds, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
                [f"call_{int(time.time())}", tenant_id, call_uuid, "nieznany", duration, status],
            )
            logger.info(f"📊 [REALTIME TEST/VONAGE] Created call log: {call_uuid} → {duration}s")

        await apply_call_charge(tenant_id, is_saas_tenant, call_uuid, status, duration)
    except Exception as e:
        logger.error(f"[REALTIME TEST/VONAGE] vonage_events error: {e}")

    return Response(content="", status_code=200)


@app.websocket("/ws-gemini-test-vonage")
async def websocket_gemini_test_vonage(websocket: WebSocket):
    tenant_phone = websocket.query_params.get("phone")
    caller_phone = websocket.query_params.get("callerPhone", "nieznany")
    call_sid = websocket.query_params.get("callSid")
    if not tenant_phone:
        logger.error("❌ [REALTIME TEST/VONAGE] Brak phone w query params — zamykam")
        await websocket.close()
        return

    await websocket.accept()
    logger.info(f"🔌 [REALTIME TEST/VONAGE] WebSocket connected, phone={tenant_phone}")

    # Jeden lookup zamiast dwóch (patrz komentarz w vonage_answer) — /vonage/answer
    # już raz przeszedł przez get_tenant_by_phone, tu robimy to drugi i OSTATNI raz
    # (żeby dostać PEŁNE, aktualne dane tenanta — usługi/godziny/FAQ), zamiast
    # najpierw doszukiwać się phone_number po tenantId.
    tenant = await get_tenant_by_phone(tenant_phone)
    if not tenant:
        logger.error("❌ [REALTIME TEST/VONAGE] Nie znaleziono tenanta — zamykam")
        await websocket.close()
        return

    logger.info(f"✅ [REALTIME TEST/VONAGE] Tenant: {tenant.get('name')}")

    # CRM lookup w tle — NIE czekamy na niego przed powitaniem (patrz apply_crm_when_ready
    # wyżej w pliku): powitanie leci od razu generyczne, a jeśli CRM znajdzie znanego
    # klienta, prompt jest dosyłany w trakcie rozmowy (session.update).
    client_profile_task = asyncio.create_task(get_client_profile(tenant.get("id", ""), caller_phone))

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(confidence=0.6, start_secs=0.2, stop_secs=0.3, min_volume=0.4)
            ),
            serializer=VonageFrameSerializer(
                params=VonageFrameSerializer.InputParams(vonage_sample_rate=16000),
            ),
        ),
    )

    task_box = {"task": None}
    context_box = {"context": None}
    call_state = make_call_state()
    tools = [
        build_contact_owner_tool(tenant, caller_phone, task_box, call_state),
        build_end_conversation_tool(task_box, call_state),
    ]
    if tenant.get("lead_mode", 0) == 1:
        # "Zbieranie zgłoszeń" — ten sam checkbox w panelu co w cascade. Warunkowe, jak tam:
        # tenanty bez tego trybu (np. salon fryzjerski) nie dostają narzędzia, którego i tak
        # by nie użyły — mniej szumu w tools = mniej okazji do pomyłki którego użyć.
        tools.append(build_submit_lead_tool(tenant, caller_phone, context_box))
    system_prompt = build_realtime_instructions(tenant, None)
    # Per-tenant głos/tempo (jeszcze bez UI w panelu — pole "realtime_voice" dopiero powstanie,
    # "speaking_rate" już istnieje, reużywany z cascade). Brak wartości = fallback na
    # OPENAI_REALTIME_VOICE / domyślne tempo API, więc nic się nie psuje zanim panel dojrzeje.
    realtime_voice = (tenant.get("realtime_voice") or "").strip() or None
    realtime_speed = float(tenant["speaking_rate"]) if tenant.get("speaking_rate") else None
    llm, user_aggregator, assistant_aggregator, llm_context = build_realtime_llm(
        system_prompt, tools=tools, voice=realtime_voice, speed=realtime_speed
    )
    context_box["context"] = llm_context
    user_transcript_monitor = UserTranscriptMonitor(call_state)
    bot_audio_monitor = BotAudioMonitor(call_state)

    pipeline = Pipeline([
        transport.input(),
        user_aggregator,
        user_transcript_monitor,
        llm,
        bot_audio_monitor,
        transport.output(),
        assistant_aggregator,
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=16000,
        ),
    )
    task_box["task"] = task

    @transport.event_handler("on_client_connected")
    async def on_connect_vonage(transport, client):
        logger.info("🎤 [REALTIME TEST/VONAGE] Klient połączony — wybudzam Realtime do przywitania")
        await user_aggregator.push_context_frame()
        asyncio.create_task(monitor_call_health(task, llm, call_state))
        asyncio.create_task(apply_crm_when_ready(llm, tenant, client_profile_task))

    @transport.event_handler("on_client_disconnected")
    async def on_disconnect_vonage(transport, client):
        logger.info("📴 [REALTIME TEST/VONAGE] Klient rozłączony")
        call_state["ended"] = True
        await task.queue_frame(EndFrame())

    runner = PipelineRunner()
    logger.info("🚀 [REALTIME TEST/VONAGE] Start pipeline")
    try:
        await runner.run(task)
    except Exception as e:
        logger.error(f"[REALTIME TEST/VONAGE] Pipeline error: {e}")
    finally:
        logger.info("🏁 [REALTIME TEST/VONAGE] Koniec połączenia")
        try:
            await maybe_send_call_summary(tenant, caller_phone, llm_context)
        except Exception as e:
            logger.error(f"[REALTIME TEST/VONAGE] Call summary error: {e}")
        try:
            await save_call_transcript(tenant, call_sid, caller_phone, llm_context)
        except Exception as e:
            logger.error(f"[REALTIME TEST/VONAGE] Call transcript error: {e}")


# ==========================================================================
# 🧪 GEMINI LIVE — szybki test porównawczy latencji, OBOK OpenAI Realtime
# ==========================================================================
"""
Cel: TYLKO zmierzyć latencję/jakość gemini-3.1-flash-live-preview na tym samym
tenancie testowym, do porównania z OpenAI Realtime powyżej. Świadomie ubogie
względem sekcji OpenAI Realtime — bez tools (contact_owner/submit_lead/
end_conversation), bez idle-timeout, bez CRM w tle. To NIE jest kandydat do
rozbudowy 1:1 — jeśli Gemini Live wygra test latencji, wtedy dopiero warto
dociągnąć brakujące funkcje analogicznie do sekcji OpenAI Realtime wyżej.

Osobne route'y (inna ścieżka niż OpenAI Realtime) — NIC z sekcji wyżej nie jest
ruszane. Żeby faktycznie przetestować, trzeba w konsoli Vonage/Twilio ręcznie
przełączyć Answer URL/webhook na endpoint poniżej, i przełączyć z powrotem po
teście.

GeminiLiveLLMService NIE emituje UserStartedSpeakingFrame/UserStoppedSpeakingFrame
(server VAD Gemini nie ma odpowiednika tych zdarzeń w pipecat, patrz docstring
serwisu) — stąd pomiar latencji niżej kotwiczy się o TranscriptionFrame (moment
dotarcia transkrypcji), tak jak w pierwotnym izolowanym teście tego pliku (patrz
git log, commit z pierwszej wersji). Mniej precyzyjne niż w sekcji OpenAI Realtime
(TranscriptionFrame to boczny kanał, przychodzi z pewnym opóźnieniem względem
faktycznego końca mowy), ale wystarczające do zgrubnego porównania.
"""

GEMINI_LIVE_MODEL = "gemini-3.1-flash-live-preview"  # zweryfikowane w docs.ai.google.dev (sierpień 2026)


class GeminiLatencyMonitor(FrameProcessor):
    """Prosty pomiar: dotarcie transkrypcji usera -> pierwsza ramka audio bota.
    Świadomie odrębna klasa od UserTranscriptMonitor/BotAudioMonitor (OpenAI Realtime
    wyżej) — te są zbudowane wokół call_state/idle_since specyficznych dla tamtej
    sekcji, tu nie ma idle-timeoutu więc nie ma sensu ich reużywać."""

    def __init__(self):
        super().__init__()
        self._last_user_frame = None
        self._waiting_for_bot_audio = False

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        now = asyncio.get_event_loop().time()

        if isinstance(frame, TranscriptionFrame):
            self._last_user_frame = now
            self._waiting_for_bot_audio = True
            logger.info(f"⏱️ [GEMINI LIVE/USER] transkrypcja: {frame.text!r}")

        if isinstance(frame, TTSAudioRawFrame) and self._waiting_for_bot_audio:
            self._waiting_for_bot_audio = False
            if self._last_user_frame:
                ms = (now - self._last_user_frame) * 1000
                icon = "🟢" if ms < 1500 else "🟡" if ms < 2500 else "🔴"
                logger.info(f"⏱️ [GEMINI LIVE/TOTAL] user->bot audio {ms:.0f}ms {icon}")

        await self.push_frame(frame, direction)


def build_gemini_live_llm(system_prompt: str):
    """Analogiczne do build_realtime_llm() wyżej, ale dla Gemini Live. Bez tools —
    patrz docstring sekcji. Voice "Kore" — ten sam głos (Chirp 3 HD) co
    pl-PL-Chirp3-HD-Kore używany w cascade (tts_factory.py), top damski głos PL
    wg rankingu użytkownika."""
    logger.info(f"🧠 Gemini Live, model={GEMINI_LIVE_MODEL}")
    llm = GeminiLiveLLMService(
        api_key=os.getenv("GOOGLE_API_KEY"),
        model=GEMINI_LIVE_MODEL,
        voice_id="Kore",
        system_instruction=system_prompt,
    )
    context = LLMContext()
    # realtime_service_mode=True — patrz docstring GeminiLiveLLMService: usługa nie
    # emituje UserStarted/StoppedSpeakingFrame, więc zapisy do kontekstu muszą iść
    # w trybie "trailing" (tak samo jak dla OpenAI Realtime wyżej).
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context, realtime_service_mode=True
    )
    return llm, user_aggregator, assistant_aggregator


@app.post("/twilio/incoming-gemini-live-test")
async def twilio_incoming_gemini_live_test(request: Request):
    form = await request.form()
    called = form.get("Called", form.get("To", ""))
    caller = form.get("From", "")
    call_sid = form.get("CallSid", "")

    logger.info(f"📞 [GEMINI LIVE TEST] Incoming: {caller} → {called} (CallSid: {call_sid})")

    tenant = await get_tenant_by_phone(called)
    if not tenant:
        return Response(
            content='<?xml version="1.0"?><Response><Say language="pl-PL">'
                    'Numer testowy nieaktywny.</Say></Response>',
            media_type="application/xml",
        )

    host = request.headers.get("host", "localhost")
    twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://{host}/ws-gemini-live-test">
            <Parameter name="callSid" value="{call_sid}" />
            <Parameter name="phone" value="{tenant['phone_number']}" />
            <Parameter name="callerPhone" value="{caller}" />
        </Stream>
    </Connect>
</Response>'''
    return Response(content=twiml, media_type="application/xml")


@app.websocket("/ws-gemini-live-test")
async def websocket_gemini_live_test(websocket: WebSocket):
    await websocket.accept()
    logger.info("🔌 [GEMINI LIVE TEST] WebSocket connected")

    stream_sid = None
    tenant = None
    caller_phone = "nieznany"

    try:
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)
            event = data.get("event")
            if event == "connected":
                continue
            if event == "start":
                start_data = data.get("start", {})
                stream_sid = start_data.get("streamSid")
                custom_params = start_data.get("customParameters", {})
                tenant_phone = custom_params.get("phone")
                caller_phone = custom_params.get("callerPhone", "nieznany")
                if tenant_phone:
                    tenant = await get_tenant_by_phone(tenant_phone)
                break
    except Exception as e:
        logger.error(f"[GEMINI LIVE TEST] Błąd startu: {e}")
        await websocket.close()
        return

    if not stream_sid or not tenant:
        logger.error("❌ [GEMINI LIVE TEST] Brak stream_sid lub tenant — zamykam")
        await websocket.close()
        return

    logger.info(f"✅ [GEMINI LIVE TEST] Tenant: {tenant.get('name')}")

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(confidence=0.6, start_secs=0.2, stop_secs=0.3, min_volume=0.4)
            ),
            serializer=TwilioFrameSerializer(
                stream_sid=stream_sid,
                params=TwilioFrameSerializer.InputParams(auto_hang_up=False),
            ),
        ),
    )

    system_prompt = build_realtime_instructions(tenant, None)
    llm, user_aggregator, assistant_aggregator = build_gemini_live_llm(system_prompt)
    latency_monitor = GeminiLatencyMonitor()

    pipeline = Pipeline([
        transport.input(),
        user_aggregator,
        llm,
        latency_monitor,
        transport.output(),
        assistant_aggregator,
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
            audio_in_sample_rate=8000,
            audio_out_sample_rate=8000,
        ),
    )

    @transport.event_handler("on_client_connected")
    async def on_connect(transport, client):
        logger.info("🎤 [GEMINI LIVE TEST] Klient połączony — wybudzam do przywitania")
        await user_aggregator.push_context_frame()

    @transport.event_handler("on_client_disconnected")
    async def on_disconnect(transport, client):
        logger.info("📴 [GEMINI LIVE TEST] Klient rozłączony")
        await task.queue_frame(EndFrame())

    runner = PipelineRunner()
    logger.info("🚀 [GEMINI LIVE TEST] Start pipeline")
    try:
        await runner.run(task)
    except Exception as e:
        logger.error(f"[GEMINI LIVE TEST] Pipeline error: {e}")
    finally:
        logger.info("🏁 [GEMINI LIVE TEST] Koniec połączenia")


@app.get("/health-gemini-live-test")
async def health_gemini_live():
    return {"status": "ok", "provider": "gemini-live", "model": GEMINI_LIVE_MODEL}


@app.get("/vonage/answer-gemini-live")
async def vonage_answer_gemini_live(request: Request):
    to_number = request.query_params.get("to", "")
    from_number = request.query_params.get("from", "")
    logger.info(f"📞 [GEMINI LIVE TEST/VONAGE] Answer: {from_number} → {to_number}")

    tenant = await get_tenant_by_phone(to_number)
    if not tenant:
        ncco = [{"action": "talk", "text": "Numer testowy nieaktywny.", "language": "pl-PL"}]
        return JSONResponse(ncco)

    host = request.headers.get("host", "localhost")
    ws_uri = f"wss://{host}/ws-gemini-live-test-vonage?phone={tenant['phone_number']}&callerPhone={from_number}"

    ncco = [
        {
            "action": "connect",
            "endpoint": [
                {
                    "type": "websocket",
                    "uri": ws_uri,
                    "content-type": "audio/l16;rate=16000",
                }
            ],
        }
    ]
    return JSONResponse(ncco)


@app.websocket("/ws-gemini-live-test-vonage")
async def websocket_gemini_live_test_vonage(websocket: WebSocket):
    tenant_phone = websocket.query_params.get("phone")
    caller_phone = websocket.query_params.get("callerPhone", "nieznany")
    if not tenant_phone:
        logger.error("❌ [GEMINI LIVE TEST/VONAGE] Brak phone w query params — zamykam")
        await websocket.close()
        return

    await websocket.accept()
    logger.info(f"🔌 [GEMINI LIVE TEST/VONAGE] WebSocket connected, phone={tenant_phone}")

    tenant = await get_tenant_by_phone(tenant_phone)
    if not tenant:
        logger.error("❌ [GEMINI LIVE TEST/VONAGE] Nie znaleziono tenanta — zamykam")
        await websocket.close()
        return

    logger.info(f"✅ [GEMINI LIVE TEST/VONAGE] Tenant: {tenant.get('name')}")

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(confidence=0.6, start_secs=0.2, stop_secs=0.3, min_volume=0.4)
            ),
            serializer=VonageFrameSerializer(
                params=VonageFrameSerializer.InputParams(vonage_sample_rate=16000),
            ),
        ),
    )

    system_prompt = build_realtime_instructions(tenant, None)
    llm, user_aggregator, assistant_aggregator = build_gemini_live_llm(system_prompt)
    latency_monitor = GeminiLatencyMonitor()

    pipeline = Pipeline([
        transport.input(),
        user_aggregator,
        llm,
        latency_monitor,
        transport.output(),
        assistant_aggregator,
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=16000,
        ),
    )

    @transport.event_handler("on_client_connected")
    async def on_connect_vonage(transport, client):
        logger.info("🎤 [GEMINI LIVE TEST/VONAGE] Klient połączony — wybudzam do przywitania")
        await user_aggregator.push_context_frame()

    @transport.event_handler("on_client_disconnected")
    async def on_disconnect_vonage(transport, client):
        logger.info("📴 [GEMINI LIVE TEST/VONAGE] Klient rozłączony")
        await task.queue_frame(EndFrame())

    runner = PipelineRunner()
    logger.info("🚀 [GEMINI LIVE TEST/VONAGE] Start pipeline")
    try:
        await runner.run(task)
    except Exception as e:
        logger.error(f"[GEMINI LIVE TEST/VONAGE] Pipeline error: {e}")
    finally:
        logger.info("🏁 [GEMINI LIVE TEST/VONAGE] Koniec połączenia")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8001)))
