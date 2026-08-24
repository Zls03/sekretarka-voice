# bot_openai_realtime.py — sekcja OpenAI Realtime, wydzielona z bot_gemini_test.py
# (ten plik urósł do ~2077 linii mieszając dwie niezależne implementacje; Gemini Live
# jest teraz aktywnie rozwijaną/testowaną ścieżką, a OpenAI Realtime to sprawdzony
# fallback — patrz CLAUDE.md, sekcja "PIVOT"). APIRouter, nie własny FastAPI app —
# montowany w bot_gemini_test.py przez app.include_router(router).
"""
Historia: ten plik był wcześniej częścią bot_gemini_test.py (linie ~163-1072, sekcja
"OpenAI Realtime"). Wydzielony 2026-08-24 wyłącznie dla czytelności/utrzymania — ZERO
zmian w logice. Zweryfikowane przed podziałem: sekcja OpenAI Realtime i sekcja Gemini
Live (zostająca w bot_gemini_test.py) nie mają między sobą żadnych faktycznych
wywołań — jedyne odwołania w drugą stronę to komentarze/docstringi.

Dwa route'y NIE trafiły tutaj mimo że fizycznie siedziały w tej sekcji: /vonage/events
i /vonage/transfer-fallback. Obsługują połączenia z OBU providerów (billing, logi,
transcript, fallback po nieudanym transferze) — zostają w bot_gemini_test.py, tam gdzie
`app`.

WYMAGANE ZMIENNE ŚRODOWISKOWE (te same co w Railway):
  OPENAI_API_KEY       — klucz OpenAI Realtime
  TWILIO_AUTH_TOKEN    — do walidacji podpisu Twilio (opcjonalnie, można pominąć na testach)
  TEST_TENANT_ID        — wymuszony tenant dla ścieżki Vonage (patrz /vonage/answer)
  RESEND_API_KEY        — do wysyłki emaila w contact_owner — bez tego funkcja
                          zwróci klientowi uczciwy błąd zamiast fałszywie potwierdzić wysyłkę

PODŁĄCZENIE (Twilio):
  1) Wybierz numer testowy w konsoli Twilio (osobny lub tymczasowo przełącz istniejący)
  2) W ustawieniach numeru: "A call comes in" -> Webhook
     POST https://<twoj-railway-host>/twilio/incoming-gemini-test
  3. To wystarczy — nic więcej w konfiguracji Twilio nie trzeba zmieniać.

PODŁĄCZENIE (Vonage): patrz sekcja "VONAGE" niżej.

FAZA 2 — jak działa wykrywanie ciszy/limitu (patrz monitor_call_health poniżej):
  6s ciszy -> "Przepraszam, czy nadal jesteśmy połączeni?" | 14s ciszy -> pożegnanie + rozłączenie
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
import time
import asyncio
import json

from loguru import logger
from dotenv import load_dotenv
load_dotenv()

from fastapi import APIRouter, WebSocket, Request
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

from helpers import get_tenant_by_phone, get_client_profile, db
from realtime_prompt import build_realtime_instructions
from realtime_tools import (
    build_contact_owner_tool, build_end_conversation_tool, build_submit_lead_tool,
    maybe_send_call_summary, save_call_transcript, is_call_allowed,
)
from realtime_booking import build_book_appointment_tool, build_manage_booking_tool

router = APIRouter()

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

@router.post("/twilio/incoming-gemini-test")
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

@router.websocket("/ws-gemini-test")
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


@router.get("/health-gemini-test")
async def health():
    return {"status": "ok", "provider": "openai-realtime"}


# ==========================================
# VONAGE — ścieżka alternatywna (obok Twilio) — TU jest test tenant
# ==========================================
"""
Vonage nie ma pojedynczego pola "webhook" na numerze — numer musi być
przypisany do Vonage "Application" (Voice), a ta aplikacja ma:
  - Answer URL (GET)  -> tu zwracamy NCCO (JSON, nie TwiML)
  - Event URL (POST)  -> status callback (odpowiednik Twilio /twilio/status,
    ale wspólny dla obu providerów — patrz vonage_events w bot_gemini_test.py)

Audio idzie jako surowe PCM 16-bit (nie base64 mu-law jak w Twilio),
dlatego osobny websocket + VonageFrameSerializer zamiast TwilioFrameSerializer.
Tenant przekazujemy przez query param w URI websocketu (Vonage na to pozwala),
więc nie trzeba parsować żadnego eventu "start" jak w Twilio.
"""


@router.get("/vonage/answer")
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


@router.websocket("/ws-gemini-test-vonage")
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
