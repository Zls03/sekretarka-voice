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
from urllib.parse import quote

from loguru import logger
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import Response, JSONResponse

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.parallel_pipeline import ParallelPipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask, PipelineParams
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketTransport,
    FastAPIWebsocketParams,
)
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.serializers.vonage import VonageFrameSerializer
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.frames.frames import (
    EndFrame, TranscriptionFrame, TTSAudioRawFrame, TTSTextFrame,
    TTSStartedFrame, TTSStoppedFrame, TTSSpeakFrame,
    UserStartedSpeakingFrame, UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame, UserSpeakingFrame,
    LLMMessagesAppendFrame,
)
from pipecat.processors.frame_processor import FrameProcessor
from services.tts_factory import create_tts_service

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
from pipecat.transcriptions.language import Language

# Reużywamy istniejących modułów: helpers.py (odczyt danych firmy + CRM, bez zależności
# od pipecat — bezpieczny import wprost). Budowanie promptu i tools — osobne pliki,
# patrz docstring wyżej po co ten podział.
from helpers import get_tenant_by_phone, get_client_profile, db, saas_db
from realtime_prompt import build_realtime_instructions
from realtime_tools import (
    build_contact_owner_tool, build_end_conversation_tool, build_submit_lead_tool,
    build_transfer_tool, send_missed_transfer_email,
    maybe_send_call_summary, save_call_transcript, apply_call_charge, is_call_allowed,
)
from realtime_booking import build_book_appointment_tool, build_manage_booking_tool

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


# ==========================================
# IDLE TIMEOUT + MAX CALL DURATION (Faza 2)
# ==========================================

IDLE_WARNING_SECONDS = 6    # tyle ciszy -> "Halo, czy mnie słyszysz?" (skrócone z 10s
                            # 16.08.2026 — żywy telefon pokazał, że przy cichym zawieszeniu
                            # sesji Gemini Live klient siedział w martwej ciszy do ~29s zanim
                            # padło JAKIEKOLWIEK pytanie/rozłączenie; krótsze progi nie naprawiają
                            # przyczyny, ale skracają czas oczekiwania klienta w ciszy)
IDLE_HANGUP_SECONDS = 14    # tyle ciszy (8s po dopytaniu) -> kończymy połączenie
MAX_CALL_DURATION = 4 * 60  # ta sama wartość co w produkcyjnym bot.py
SILENT_HANG_TIMEOUT = 5     # Gemini Live: tyle sekund BEZ ŻADNEJ reakcji modelu po tym jak
                            # klient realnie coś powiedział (patrz GeminiUserMonitor) ->
                            # uznajemy sesję za cicho zawieszoną (WebSocket otwarty, brak
                            # błędu, ale server_content przestaje przychodzić — znany problem
                            # community, złapany na żywym telefonie 16.08.2026) i wymuszamy
                            # reconnect zamiast czekać, aż to sam pipecat wykryje (nie wykryje —
                            # jego reconnect odpala się tylko na wyjątek w pętli odbiorczej).


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


async def apply_crm_when_ready(
    llm: OpenAIRealtimeLLMService, tenant: dict, client_profile_task: asyncio.Task,
    has_booking: bool = False,
) -> dict | None:
    """Powitanie leci OD RAZU z generycznym promptem (bez czekania na CRM, ~2-3s HTTP
    do panelu) — ta funkcja czeka na wynik w tle i, jeśli okaże się że dzwoni znany
    klient, dosyła zaktualizowany prompt (session.update) w trakcie rozmowy, żeby
    dane CRM (historia wizyt) były dostępne gdy klient o nie zapyta. include_greeting=False
    (patrz realtime_prompt.py::build_realtime_instructions) — bez tego model mógłby
    zrozumieć aktualizację jako polecenie przywitania się jeszcze raz.

    has_booking: MUSI być przekazane z tego samego booking_available co przy budowie
    tools/system_prompt na starcie połączenia — bez tego ta aktualizacja w trakcie
    rozmowy nadpisałaby prompt z powrotem na "rezerwacje jeszcze w budowie", mimo że
    book_appointment cały czas jest zarejestrowane (dokładnie ten sam błąd co wcześniej
    znaleziony przy transfer_to_owner/has_transfer, patrz historia tego pliku)."""
    client_profile = await client_profile_task
    if client_profile:
        logger.info(f"👤 [REALTIME TEST] CRM (spóźniony): {client_profile.get('name')} (wizyty: {client_profile.get('visit_count', 0)})")
        updated_prompt = build_realtime_instructions(tenant, client_profile, include_greeting=False, has_booking=has_booking)
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
    # 1:1 z bot.py (cascade): booking_enabled BEZ domyślnej wartości (brak pola =
    # wyłączone, nie włączone) + wymóg że co najmniej jeden pracownik ma połączony
    # Google Calendar i przypisaną usługę — bez tego cascade sam wymusza 0
    # ("booking_enabled forced to 0 — no staff with calendar+services"), więc to samo
    # tenanty musi dawać ten sam wynik tutaj, inaczej zachowanie się rozjeżdża między
    # systemami dla identycznej konfiguracji.
    booking_available = tenant.get("booking_enabled") == 1 and any(
        s.get("google_connected") and len(s.get("services", [])) > 0
        for s in tenant.get("staff", [])
    )
    if booking_available:
        tools.append(build_book_appointment_tool(tenant, caller_phone, call_state, context_box))
        tools.append(build_manage_booking_tool(tenant, caller_phone, call_state))
    system_prompt = build_realtime_instructions(tenant, None, has_booking=booking_available)
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
        asyncio.create_task(apply_crm_when_ready(llm, tenant, client_profile_task, has_booking=booking_available))

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
    from_number = data.get("from", "") or "nieznany"
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
            # from_number zamiast zaszytego "nieznany" — bug znaleziony na żywym telefonie:
            # ten webhook i save_call_transcript() (koniec pipeline'u websocketu) to dwa
            # niezależne w czasie zdarzenia, ten webhook może przyjść PIERWSZY (potwierdzone
            # w logu: "Created call log" tu wyprzedziło "Transcript saved"). Kto pierwszy
            # stworzy wiersz, tego caller_phone zostaje na stałe — save_call_transcript()
            # widzi że wiersz już istnieje i nie insertuje drugi raz. Wcześniej ten webhook
            # zawsze wpisywał "nieznany" niezależnie od tego czy dane były dostępne — a SĄ,
            # Vonage przekazuje numer dzwoniącego jako "from" w tym samym evencie.
            await target_db.execute(
                """INSERT INTO call_logs
                   (id, tenant_id, call_sid, caller_phone, duration_seconds, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
                [f"call_{int(time.time())}", tenant_id, call_uuid, from_number, duration, status],
            )
            logger.info(f"📊 [REALTIME TEST/VONAGE] Created call log: {call_uuid} → {duration}s")

        await apply_call_charge(tenant_id, is_saas_tenant, call_uuid, status, duration)
    except Exception as e:
        logger.error(f"[REALTIME TEST/VONAGE] vonage_events error: {e}")

    return Response(content="", status_code=200)


@app.api_route("/vonage/transfer-fallback", methods=["GET", "POST"])
async def vonage_transfer_fallback(request: Request):
    """eventUrl akcji "connect" z transfer_vonage_call (realtime_tools.py) — Vonage odpytuje
    TU (eventType=synchronous) gdy próba połączenia z właścicielem kończy się timeout/busy/
    rejected/failed/unanswered. MUSIMY zwrócić nową NCCO, która zastępuje bieżącą — inaczej
    klient zostaje w martwej ciszy aż połączenie samo się urwie (dokładnie to zaobserwowano
    na żywym telefonie przed tą zmianą, z domyślnym 60s timeout i brakiem jakiegokolwiek
    fallbacku). businessName/callerPhone/ownerEmail lecą w query stringu — sami je tam
    wstawiliśmy w build_transfer_tool, bo ten webhook nie ma dostępu do żadnego stanu
    rozmowy (nowe, niezależne wywołanie od Vonage)."""
    try:
        if request.method == "POST":
            data = await request.json()
        else:
            data = dict(request.query_params)
    except Exception:
        data = dict(request.query_params)
    logger.info(f"📞 [TRANSFER FALLBACK] {data}")

    business_name = request.query_params.get("businessName", "Firma")
    caller_phone = request.query_params.get("callerPhone", "")
    owner_email = request.query_params.get("ownerEmail", "")
    if owner_email:
        asyncio.create_task(send_missed_transfer_email(business_name, caller_phone, owner_email))

    ncco = [
        {
            "action": "talk",
            "text": "Niestety nie udało się połączyć. Przekażę wiadomość, żeby ktoś oddzwonił.",
            "language": "pl-PL",
        }
    ]
    return JSONResponse(ncco)


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
    # 1:1 z bot.py (cascade): booking_enabled BEZ domyślnej wartości (brak pola =
    # wyłączone, nie włączone) + wymóg że co najmniej jeden pracownik ma połączony
    # Google Calendar i przypisaną usługę — bez tego cascade sam wymusza 0
    # ("booking_enabled forced to 0 — no staff with calendar+services"), więc to samo
    # tenanty musi dawać ten sam wynik tutaj, inaczej zachowanie się rozjeżdża między
    # systemami dla identycznej konfiguracji.
    booking_available = tenant.get("booking_enabled") == 1 and any(
        s.get("google_connected") and len(s.get("services", [])) > 0
        for s in tenant.get("staff", [])
    )
    if booking_available:
        tools.append(build_book_appointment_tool(tenant, caller_phone, call_state, context_box, channel="vonage"))
        tools.append(build_manage_booking_tool(tenant, caller_phone, call_state))
    system_prompt = build_realtime_instructions(tenant, None, has_booking=booking_available)
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
        asyncio.create_task(apply_crm_when_ready(llm, tenant, client_profile_task, has_booking=booking_available))

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


def make_gemini_state() -> dict:
    now = time.time()
    return {
        "last_user_frame": None,
        "waiting_for_bot_audio": False,
        # Od tu w dół: pola pod idle-timeout (Faza 2), patrz speak_directly() /
        # monitor_gemini_call_health() niżej — te same nazwy pól i ta sama logika
        # co make_call_state()/BotAudioMonitor w sekcji OpenAI Realtime wyżej,
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
    (patrz warning w logu na żywym telefonie), więc w odróżnieniu od OpenAI Realtime (gdzie
    UserStoppedSpeakingFrame jest dokładniejszym sygnałem) TranscriptionFrame była DOTĄD
    jedynym potwierdzonym sygnałem aktywności usera — ale przychodzi dopiero PO tym jak
    Gemini skończy przetwarzać CAŁĄ wypowiedź klienta (nawet kilkanaście sekund mowy+namysłu).

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
        elif isinstance(frame, TranscriptionFrame):
            self._state["last_user_frame"] = asyncio.get_event_loop().time()
            self._state["waiting_for_bot_audio"] = True
            self._state["idle_since"] = time.time()
            # Klient realnie coś powiedział — model MUSI zareagować. Jeśli nie zareaguje
            # w SILENT_HANG_TIMEOUT sekund, monitor_gemini_call_health uzna sesję za
            # zawieszoną (patrz "awaiting_model_response_since" niżej).
            self._state["awaiting_model_response_since"] = time.time()
            logger.info(f"⏱️ [GEMINI LIVE/USER] transkrypcja: {frame.text!r}")
        await self.push_frame(frame, direction)


class GeminiBotMonitor(FrameProcessor):
    """Łapie tekst i audio bota (oba lecą DOWNSTREAM z llm, więc ta klasa siedzi PO
    llm — symetrycznie do GeminiUserMonitor, który siedzi PRZED).

    Odświeżanie idle_since na TTSStarted/TTSAudioRawFrame/TTSStoppedFrame + honorowanie
    suppress_idle_reset — logika 1:1 skopiowana z BotAudioMonitor (sekcja OpenAI Realtime
    wyżej, tam pełny docstring z historią 3 warstw bugów). Nie odkrywam tu koła na nowo —
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
    """Wypowiada DOKŁADNY tekst przez niezależny, awaryjny silnik TTS (fallback_tts —
    utworzony przez create_tts_service(tenant), TEN SAM provider/głos co ma
    skonfigurowany dany tenant w cascade), z całkowitym pominięciem Gemini Live.

    PO CO: poprzednia wersja (gemini_say_now) prosiła o to SAM MODEL — działało
    tylko gdy sesja Gemini Live żyje. Złapane na żywym telefonie 16.08.2026: gdy
    sesja cicho się zawiesza, prośba wysłana DO modelu nie daje efektu. Rozwiązanie
    z cascade to TTSSpeakFrame idący prosto do TTS z pominięciem LLM — tu robimy to
    samo dla Realtime/Gemini Live.

    ⚠️ DRUGIE PODEJŚCIE (16.08.2026, pierwsze zepsuło żywe połączenia): fallback_tts
    NIE MOŻE siedzieć w głównym łańcuchu bezpośrednio po `llm` — TTSService reaguje
    na KAŻDĄ TextFrame (sprawdzone w źródle pipecat), a TTSTextFrame (własny
    transkrypt Gemini) dziedziczy po TextFrame, więc fallback próbował na nowo
    syntetyzować WSZYSTKO co Gemini już powiedziało. Poprawka: fallback_tts siedzi
    w OSOBNEJ gałęzi ParallelPipeline([llm], [fallback_tts]) — gałęzie dzielą tylko
    wspólne WEJŚCIE (audio z telefonu + ten TTSSpeakFrame), własna mowa Gemini
    powstaje wewnątrz gałęzi z `llm` i fizycznie nie dociera do gałęzi fallbacku.

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
    """Odpowiednik monitor_call_health() (sekcja OpenAI Realtime wyżej) dla Gemini
    Live — ta sama logika progów (IDLE_WARNING_SECONDS/IDLE_HANGUP_SECONDS/MAX_CALL_DURATION,
    stałe zdefiniowane raz, wyżej w pliku), tylko wywołuje speak_directly() zamiast say_now().

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
                await speak_directly(task, call_state, "Przepraszam, czy nadal jesteśmy połączeni?")
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
    rzadko (0 razy w 2 ostatnich pełnych testach) i ryzyko łatki nie jest tego warte."""
    resolved_voice = voice or "Kore"
    logger.info(f"🧠 Gemini Live, model={GEMINI_LIVE_MODEL}, voice={resolved_voice}, tools={[t.name for t in (tools or [])]}")
    llm = GeminiLiveLLMService(
        api_key=os.getenv("GOOGLE_API_KEY"),
        settings=GeminiLiveLLMService.Settings(
            model=GEMINI_LIVE_MODEL,
            voice=resolved_voice,
            language=Language.PL,
        ),
        system_instruction=system_prompt,
    )
    context = LLMContext(tools=tools or [])
    # realtime_service_mode=True — patrz docstring GeminiLiveLLMService: usługa nie
    # emituje UserStarted/StoppedSpeakingFrame, więc zapisy do kontekstu muszą iść
    # w trybie "trailing" (tak samo jak dla OpenAI Realtime wyżej).
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context, realtime_service_mode=True
    )
    # context zwracany osobno — ten sam powód co w build_realtime_llm (raport rozmowy
    # + transkrypt czytają context.get_messages() po zakończeniu połączenia).
    return llm, user_aggregator, assistant_aggregator, context


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

    if not await is_call_allowed(tenant):
        return Response(
            content='<?xml version="1.0"?><Response><Say language="pl-PL">'
                    'Przepraszamy, linia jest chwilowo niedostępna.</Say><Hangup/></Response>',
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
        logger.error(f"[GEMINI LIVE TEST] Błąd startu: {e}")
        await websocket.close()
        return

    if not stream_sid or not tenant:
        logger.error("❌ [GEMINI LIVE TEST] Brak stream_sid lub tenant — zamykam")
        await websocket.close()
        return

    if not await is_call_allowed(tenant):
        logger.warning(f"🚫 [GEMINI LIVE TEST] Tenant {tenant.get('id')} zablokowany — zamykam")
        await websocket.close()
        return

    logger.info(f"✅ [GEMINI LIVE TEST] Tenant: {tenant.get('name')}")

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            # ⚠️ `vad_analyzer=` TU jest martwym parametrem (17.08.2026, zweryfikowane w
            # źródle pipecat) — TransportParams/FastAPIWebsocketParams w ogóle nie ma
            # takiego pola, Pydantic po cichu je ignoruje (extra="ignore" domyślnie).
            # Realny lokalny VAD jest teraz osobnym procesorem w pipeline (vad_processor
            # niżej) — patrz komentarz przy jego tworzeniu.
            serializer=TwilioFrameSerializer(
                stream_sid=stream_sid,
                params=TwilioFrameSerializer.InputParams(auto_hang_up=False),
            ),
        ),
    )

    gemini_state = make_gemini_state()
    task_box = {"task": None}
    context_box = {"context": None}
    # Reużywamy WPROST build_*_tool z realtime_tools.py (patrz docstring
    # build_gemini_live_llm) — call_state=gemini_state, bo ma już pole "ended"
    # którego te handlery potrzebują (patrz make_gemini_state()).
    tools = [
        build_contact_owner_tool(tenant, caller_phone, task_box, gemini_state),
        build_end_conversation_tool(task_box, gemini_state),
    ]
    if tenant.get("lead_mode", 0) == 1:
        tools.append(build_submit_lead_tool(tenant, caller_phone, context_box))
    # 1:1 z bot.py (cascade): booking_enabled BEZ domyślnej wartości (brak pola =
    # wyłączone, nie włączone) + wymóg że co najmniej jeden pracownik ma połączony
    # Google Calendar i przypisaną usługę — bez tego cascade sam wymusza 0
    # ("booking_enabled forced to 0 — no staff with calendar+services"), więc to samo
    # tenanty musi dawać ten sam wynik tutaj, inaczej zachowanie się rozjeżdża między
    # systemami dla identycznej konfiguracji.
    booking_available = tenant.get("booking_enabled") == 1 and any(
        s.get("google_connected") and len(s.get("services", [])) > 0
        for s in tenant.get("staff", [])
    )
    if booking_available:
        tools.append(build_book_appointment_tool(tenant, caller_phone, gemini_state, context_box))
        tools.append(build_manage_booking_tool(tenant, caller_phone, gemini_state))

    system_prompt = build_realtime_instructions(tenant, None, has_booking=booking_available)
    gemini_voice = (tenant.get("gemini_voice") or "").strip() or None
    llm, user_aggregator, assistant_aggregator, llm_context = build_gemini_live_llm(
        system_prompt, tools=tools, voice=gemini_voice
    )
    context_box["context"] = llm_context
    # Lokalny VAD jako wczesny sygnał "klient mówi" dla GeminiUserMonitor (patrz jego
    # docstring — bug ze złapanym w środku wypowiedzi klienta nudge'em, 17.08.2026).
    # Siedzi zaraz po transport.input(), PRZED user_aggregator/gemini_user_monitor,
    # żeby analizować surowe audio jak najwcześniej. Ten sam SileroVADAnalyzer/VADParams
    # co poprzednio (bezużytecznie) siedział w FastAPIWebsocketParams — teraz faktycznie
    # coś robi, bo VADProcessor to prawdziwy, wspierany mechanizm w tej wersji pipecat
    # (nie parametr transportu).
    vad_processor = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(
            # stop_secs=0.2 — zgodnie z zalecanym domyślnym progiem pipecat, na którym
            # oparte są ich wbudowane szacunki latencji P99 dla Smart Turn (WARNING w
            # logu przy 0.3: "Built-in p99 latency values assume stop_secs=0.2").
            params=VADParams(confidence=0.6, start_secs=0.2, stop_secs=0.2, min_volume=0.4)
        )
    )
    gemini_user_monitor = GeminiUserMonitor(gemini_state)
    gemini_bot_monitor = GeminiBotMonitor(gemini_state)
    # Awaryjny TTS, niezależny od Gemini Live — patrz speak_directly(). TEN SAM
    # tts_provider/głos co tenant ma skonfigurowany w cascade (create_tts_service).
    #
    # ⚠️ MUSI siedzieć w OSOBNEJ gałęzi ParallelPipeline, NIE bezpośrednio po `llm`
    # w jednym łańcuchu — pierwsza próba (16.08.2026) tak zrobiła i zepsuła żywe
    # połączenia: TTSService reaguje na KAŻDĄ TextFrame (sprawdzone w źródle
    # pipecat), a TTSTextFrame (własny transkrypt Gemini) dziedziczy po TextFrame,
    # więc fallback zaczynał od nowa syntetyzować WSZYSTKO co Gemini już powiedziało
    # (kaskada błędów resamplera, słyszalne psucie audio). ParallelPipeline([llm],
    # [fallback_tts]) dzieli gałęzie tak, że mają WSPÓLNE tylko wejście (audio z
    # telefonu + nasz TTSSpeakFrame wstrzyknięty przez speak_directly) — mowa
    # wygenerowana PRZEZ Gemini powstaje wewnątrz gałęzi z `llm` i fizycznie nigdy
    # nie dociera do gałęzi z fallback_tts. Sprawdzone w źródle pipecat 1.4.0:
    # transkrypcja, którą Gemini wypycha "w górę" (UPSTREAM), poprawnie przebija
    # się z powrotem przez ParallelPipeline do gemini_user_monitor (przez
    # PipelineSource/_parallel_push_frame), więc pomiar latencji i wykrywanie
    # ciszy per-turn nie są tym zaburzone. gemini_bot_monitor siedzi PO parze
    # gałęzi (nie w żadnej z nich), żeby widzieć ZMERGOWANE audio z obu źródeł —
    # inaczej nie zauważyłby mowy fallbacku i idle_since/greeted by się nie
    # odświeżały poprawnie po awaryjnej wypowiedzi.
    #
    # ⚠️ DRUGI, ODDZIELNY BUG złapany na żywym telefonie (16.08.2026) PO
    # wdrożeniu ParallelPipeline powyżej: FastAPIWebsocketOutputTransport ma
    # JEDEN, per-połączeniowy, stanowy resampler (SOXRStreamAudioResampler),
    # współdzielony przez WSZYSTKIE źródła audio przechodzące przez transport
    # (Gemini I fallback_tts kończą w tym samym miejscu — to jest OK i
    # oczekiwane, obie gałęzie muszą trafić do tej samej linii telefonicznej).
    # Ten resampler PINUJE się na pierwszej parze (in_rate, out_rate) jaką
    # zobaczy i RZUCA WYJĄTKIEM (nie: reinicjalizuje się) przy każdej kolejnej
    # innej parze. Gemini Live emituje natywnie 24000 Hz — jeśli fallback_tts
    # dostanie sample_rate providera dobrany pod kaskadę (Google/Cartesia/
    # Azure = 8000 Hz), KAŻDA ramka audio fallbacku ginie jako ErrorFrame i
    # nigdy nie dociera do rozmówcy. Gorzej: TTSStoppedFrame z fallbacku i tak
    # przechodzi przez gemini_bot_monitor PRZED zepsutym resamplerem, więc
    # zegar ciszy (idle_since) resetuje się mimo że nikt nic nie usłyszał —
    # połączenie wisi w nieskończonej cichej pętli zamiast się rozłączyć.
    # Fix: wymusić sample_rate=24000 (ten sam co Gemini) niezależnie od
    # providera, żeby resampler przez cały czas trwania połączenia widział
    # tylko JEDNĄ parę (in_rate, out_rate).
    fallback_tts = create_tts_service(tenant, sample_rate=24000)

    pipeline = Pipeline([
        transport.input(),
        vad_processor,
        user_aggregator,
        gemini_user_monitor,
        ParallelPipeline([llm], [fallback_tts]),
        gemini_bot_monitor,
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
        logger.info("🎤 [GEMINI LIVE TEST] Klient połączony — wybudzam do przywitania")
        # Poprzednia wersja wołała user_aggregator.push_context_frame() z pustym
        # kontekstem, licząc że GeminiLiveLLMService._handle_context() samo doda seed
        # (patrz źródło: gdy messages puste, dokleja CAŁY system_instruction PONOWNIE
        # jako wiadomość "system" w kontekście, żeby było co wysłać). Efekt na żywym
        # telefonie: pierwsza odpowiedź (powitanie) potrafiła wypaść dopiero ~15-17s
        # po connect, i wyglądało to jakby bot czekał aż klient odezwie się pierwszy
        # ("Halo?"), a nie proaktywnie sam zaczynał. Podejrzenie: model musi wtedy
        # przetworzyć ogromny (~150 linii) system prompt DWA razy — raz jako
        # system_instruction sesji, raz jako doklejony seed — zanim w ogóle wygeneruje
        # pierwszy dźwięk.
        # Teraz zamiast pustego kontekstu wysyłamy jawną, krótką wiadomość startową
        # przez LLMMessagesAppendFrame — GeminiLiveLLMService ma na to bezpośrednią
        # obsługę (_create_single_response / _create_initial_response), a treść do
        # powiedzenia i tak dyktuje system_instruction ("ROZPOCZĘCIE ROZMOWY: ..."),
        # więc ta wiadomość jest tylko "zapłonem" do wywołania inferencji, nie
        # duplikuje całego promptu.
        await asyncio.sleep(1.0)
        logger.info("🎤 [GEMINI LIVE TEST] Wysyłam LLMMessagesAppendFrame (kick startowy)")
        await task.queue_frames([
            LLMMessagesAppendFrame(
                messages=[{"role": "user", "content": "(początek rozmowy)"}],
                run_llm=True,
            )
        ])
        logger.info("🎤 [GEMINI LIVE TEST] Kick startowy wysłany bez wyjątku")
        asyncio.create_task(monitor_gemini_call_health(task, gemini_state, llm))

    @transport.event_handler("on_client_disconnected")
    async def on_disconnect(transport, client):
        logger.info("📴 [GEMINI LIVE TEST] Klient rozłączony")
        gemini_state["ended"] = True
        await task.queue_frame(EndFrame())

    runner = PipelineRunner()
    logger.info("🚀 [GEMINI LIVE TEST] Start pipeline")
    try:
        await runner.run(task)
    except Exception as e:
        logger.error(f"[GEMINI LIVE TEST] Pipeline error: {e}")
    finally:
        logger.info("🏁 [GEMINI LIVE TEST] Koniec połączenia")
        try:
            await maybe_send_call_summary(tenant, caller_phone, llm_context)
        except Exception as e:
            logger.error(f"[GEMINI LIVE TEST] Call summary error: {e}")
        try:
            await save_call_transcript(tenant, call_sid, caller_phone, llm_context)
        except Exception as e:
            logger.error(f"[GEMINI LIVE TEST] Call transcript error: {e}")


@app.get("/health-gemini-live-test")
async def health_gemini_live():
    return {"status": "ok", "provider": "gemini-live", "model": GEMINI_LIVE_MODEL}


@app.get("/vonage/answer-gemini-live")
async def vonage_answer_gemini_live(request: Request):
    to_number = request.query_params.get("to", "")
    from_number = request.query_params.get("from", "")
    call_uuid = request.query_params.get("uuid", "")
    # region_url — bug znaleziony na żywym telefonie (400 Bad Request przy transferze,
    # mimo poprawnego JSON body): Vonage przypisuje KAŻDE połączenie do konkretnego
    # regionalnego centrum danych (potwierdzone przez Vonage API Support: "if you
    # receive a 400 or 404 response... your call is likely residing on a different
    # Data Center"). Ten region_url przychodzi TYLKO w tym evencie Answer i trzeba go
    # zapamiętać na całą rozmowę — sztywne api.nexmo.com trafia w złe centrum danych
    # dla połączeń spoza jego regionu.
    region_url = request.query_params.get("region_url", "")
    logger.info(f"📞 [GEMINI LIVE TEST/VONAGE] Answer: {from_number} → {to_number} (region={region_url or 'brak'})")

    tenant = await get_tenant_by_phone(to_number)
    if not tenant:
        ncco = [{"action": "talk", "text": "Numer testowy nieaktywny.", "language": "pl-PL"}]
        return JSONResponse(ncco)

    if not await is_call_allowed(tenant):
        ncco = [{"action": "talk", "text": "Przepraszamy, linia jest chwilowo niedostępna.", "language": "pl-PL"}]
        return JSONResponse(ncco)

    host = request.headers.get("host", "localhost")
    ws_uri = (
        f"wss://{host}/ws-gemini-live-test-vonage?phone={tenant['phone_number']}"
        f"&callerPhone={from_number}&callSid={call_uuid}&regionUrl={quote(region_url, safe='')}"
    )

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
    call_sid = websocket.query_params.get("callSid")
    region_url = websocket.query_params.get("regionUrl") or None
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
            # ⚠️ `vad_analyzer=` TU jest martwym parametrem — patrz komentarz przy
            # tej samej sytuacji w websocket_gemini_live_test (trasa Twilio) wyżej.
            serializer=VonageFrameSerializer(
                params=VonageFrameSerializer.InputParams(vonage_sample_rate=16000),
            ),
        ),
    )

    gemini_state = make_gemini_state()
    task_box = {"task": None}
    context_box = {"context": None}
    transfer_available = tenant.get("transfer_enabled", 0) == 1
    tools = [
        build_contact_owner_tool(tenant, caller_phone, task_box, gemini_state, has_transfer_tool=transfer_available),
        build_end_conversation_tool(task_box, gemini_state),
    ]
    if tenant.get("lead_mode", 0) == 1:
        tools.append(build_submit_lead_tool(tenant, caller_phone, context_box))
    # 1:1 z bot.py (cascade): booking_enabled BEZ domyślnej wartości (brak pola =
    # wyłączone, nie włączone) + wymóg że co najmniej jeden pracownik ma połączony
    # Google Calendar i przypisaną usługę — bez tego cascade sam wymusza 0
    # ("booking_enabled forced to 0 — no staff with calendar+services"), więc to samo
    # tenanty musi dawać ten sam wynik tutaj, inaczej zachowanie się rozjeżdża między
    # systemami dla identycznej konfiguracji.
    booking_available = tenant.get("booking_enabled") == 1 and any(
        s.get("google_connected") and len(s.get("services", [])) > 0
        for s in tenant.get("staff", [])
    )
    if booking_available:
        tools.append(build_book_appointment_tool(tenant, caller_phone, gemini_state, context_box, channel="vonage"))
        tools.append(build_manage_booking_tool(tenant, caller_phone, gemini_state))
    if transfer_available:
        # Tylko Vonage (patrz docstring build_transfer_tool w realtime_tools.py) — Twilio
        # ma osobny, już istniejący mechanizm (transfer_requests + /twilio/after-stream),
        # nietknięty tym kodem.
        transfer_host = websocket.headers.get("host", "localhost")
        tools.append(build_transfer_tool(
            tenant, call_sid, gemini_state, region_url,
            caller_phone=caller_phone, host=transfer_host,
        ))

    system_prompt = build_realtime_instructions(tenant, None, has_transfer=transfer_available, has_booking=booking_available)
    gemini_voice = (tenant.get("gemini_voice") or "").strip() or None
    llm, user_aggregator, assistant_aggregator, llm_context = build_gemini_live_llm(
        system_prompt, tools=tools, voice=gemini_voice
    )
    context_box["context"] = llm_context
    # Lokalny VAD jako wczesny sygnał "klient mówi" dla GeminiUserMonitor (patrz jego
    # docstring — bug ze złapanym w środku wypowiedzi klienta nudge'em, 17.08.2026).
    # Siedzi zaraz po transport.input(), PRZED user_aggregator/gemini_user_monitor,
    # żeby analizować surowe audio jak najwcześniej. Ten sam SileroVADAnalyzer/VADParams
    # co poprzednio (bezużytecznie) siedział w FastAPIWebsocketParams — teraz faktycznie
    # coś robi, bo VADProcessor to prawdziwy, wspierany mechanizm w tej wersji pipecat
    # (nie parametr transportu).
    vad_processor = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(
            # stop_secs=0.2 — zgodnie z zalecanym domyślnym progiem pipecat, na którym
            # oparte są ich wbudowane szacunki latencji P99 dla Smart Turn (WARNING w
            # logu przy 0.3: "Built-in p99 latency values assume stop_secs=0.2").
            params=VADParams(confidence=0.6, start_secs=0.2, stop_secs=0.2, min_volume=0.4)
        )
    )
    gemini_user_monitor = GeminiUserMonitor(gemini_state)
    gemini_bot_monitor = GeminiBotMonitor(gemini_state)
    # Awaryjny TTS, niezależny od Gemini Live — patrz speak_directly(). TEN SAM
    # tts_provider/głos co tenant ma skonfigurowany w cascade (create_tts_service).
    #
    # ⚠️ MUSI siedzieć w OSOBNEJ gałęzi ParallelPipeline, NIE bezpośrednio po `llm`
    # w jednym łańcuchu — pierwsza próba (16.08.2026) tak zrobiła i zepsuła żywe
    # połączenia: TTSService reaguje na KAŻDĄ TextFrame (sprawdzone w źródle
    # pipecat), a TTSTextFrame (własny transkrypt Gemini) dziedziczy po TextFrame,
    # więc fallback zaczynał od nowa syntetyzować WSZYSTKO co Gemini już powiedziało
    # (kaskada błędów resamplera, słyszalne psucie audio). ParallelPipeline([llm],
    # [fallback_tts]) dzieli gałęzie tak, że mają WSPÓLNE tylko wejście (audio z
    # telefonu + nasz TTSSpeakFrame wstrzyknięty przez speak_directly) — mowa
    # wygenerowana PRZEZ Gemini powstaje wewnątrz gałęzi z `llm` i fizycznie nigdy
    # nie dociera do gałęzi z fallback_tts. Sprawdzone w źródle pipecat 1.4.0:
    # transkrypcja, którą Gemini wypycha "w górę" (UPSTREAM), poprawnie przebija
    # się z powrotem przez ParallelPipeline do gemini_user_monitor (przez
    # PipelineSource/_parallel_push_frame), więc pomiar latencji i wykrywanie
    # ciszy per-turn nie są tym zaburzone. gemini_bot_monitor siedzi PO parze
    # gałęzi (nie w żadnej z nich), żeby widzieć ZMERGOWANE audio z obu źródeł —
    # inaczej nie zauważyłby mowy fallbacku i idle_since/greeted by się nie
    # odświeżały poprawnie po awaryjnej wypowiedzi.
    #
    # ⚠️ DRUGI, ODDZIELNY BUG złapany na żywym telefonie (16.08.2026) PO
    # wdrożeniu ParallelPipeline powyżej: FastAPIWebsocketOutputTransport ma
    # JEDEN, per-połączeniowy, stanowy resampler (SOXRStreamAudioResampler),
    # współdzielony przez WSZYSTKIE źródła audio przechodzące przez transport
    # (Gemini I fallback_tts kończą w tym samym miejscu — to jest OK i
    # oczekiwane, obie gałęzie muszą trafić do tej samej linii telefonicznej).
    # Ten resampler PINUJE się na pierwszej parze (in_rate, out_rate) jaką
    # zobaczy i RZUCA WYJĄTKIEM (nie: reinicjalizuje się) przy każdej kolejnej
    # innej parze. Gemini Live emituje natywnie 24000 Hz — jeśli fallback_tts
    # dostanie sample_rate providera dobrany pod kaskadę (Google/Cartesia/
    # Azure = 8000 Hz), KAŻDA ramka audio fallbacku ginie jako ErrorFrame i
    # nigdy nie dociera do rozmówcy. Gorzej: TTSStoppedFrame z fallbacku i tak
    # przechodzi przez gemini_bot_monitor PRZED zepsutym resamplerem, więc
    # zegar ciszy (idle_since) resetuje się mimo że nikt nic nie usłyszał —
    # połączenie wisi w nieskończonej cichej pętli zamiast się rozłączyć.
    # Fix: wymusić sample_rate=24000 (ten sam co Gemini) niezależnie od
    # providera, żeby resampler przez cały czas trwania połączenia widział
    # tylko JEDNĄ parę (in_rate, out_rate).
    fallback_tts = create_tts_service(tenant, sample_rate=24000)

    pipeline = Pipeline([
        transport.input(),
        vad_processor,
        user_aggregator,
        gemini_user_monitor,
        ParallelPipeline([llm], [fallback_tts]),
        gemini_bot_monitor,
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
        logger.info("🎤 [GEMINI LIVE TEST/VONAGE] Klient połączony — wybudzam do przywitania")
        # Patrz komentarz w on_connect (Twilio) wyżej — zamiast pustego push_context_frame()
        # (który dublował cały system prompt jako seed i dawał ~15-17s do pierwszego dźwięku
        # na żywym telefonie), wysyłamy krótki jawny "zapłon" przez LLMMessagesAppendFrame.
        await asyncio.sleep(1.0)
        logger.info("🎤 [GEMINI LIVE TEST/VONAGE] Wysyłam LLMMessagesAppendFrame (kick startowy)")
        await task.queue_frames([
            LLMMessagesAppendFrame(
                messages=[{"role": "user", "content": "(początek rozmowy)"}],
                run_llm=True,
            )
        ])
        logger.info("🎤 [GEMINI LIVE TEST/VONAGE] Kick startowy wysłany bez wyjątku")
        asyncio.create_task(monitor_gemini_call_health(task, gemini_state, llm))

    @transport.event_handler("on_client_disconnected")
    async def on_disconnect_vonage(transport, client):
        logger.info("📴 [GEMINI LIVE TEST/VONAGE] Klient rozłączony")
        gemini_state["ended"] = True
        await task.queue_frame(EndFrame())

    runner = PipelineRunner()
    logger.info("🚀 [GEMINI LIVE TEST/VONAGE] Start pipeline")
    try:
        await runner.run(task)
    except Exception as e:
        logger.error(f"[GEMINI LIVE TEST/VONAGE] Pipeline error: {e}")
    finally:
        logger.info("🏁 [GEMINI LIVE TEST/VONAGE] Koniec połączenia")
        try:
            await maybe_send_call_summary(tenant, caller_phone, llm_context)
        except Exception as e:
            logger.error(f"[GEMINI LIVE TEST/VONAGE] Call summary error: {e}")
        try:
            await save_call_transcript(tenant, call_sid, caller_phone, llm_context)
        except Exception as e:
            logger.error(f"[GEMINI LIVE TEST/VONAGE] Call transcript error: {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8001)))
