"""Most audio Vonage <-> ElevenLabs Conversational AI (fallback, gdy SIP direct zawiedzie)."""

import asyncio
import base64
import json

import websockets
from fastapi import APIRouter, WebSocket
from loguru import logger
from pipecat.frames.frames import (
    EndFrame,
    InputAudioRawFrame,
    InterruptionFrame,
    StartFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.frame_processor import FrameProcessor

from app.background import spawn
from app.engines.common import accept_vonage_stream, call_pipeline_params, create_local_vad, create_transport
from app.engines.elevenlabs.config import ELEVENLABS_API_KEY, _resolve_agent_id
from app.engines.elevenlabs.conversation import build_conversation_config_override

router = APIRouter()


# MOST VONAGE — dopisany 2026-09-03. "Bring your own Twilio" (register_call, wyżej)
# NIE ma odpowiednika dla Vonage — to Twilio-specyficzne API ElevenLabs, w którym ICH
# infrastruktura łączy się z Twilio bezpośrednio, z pominięciem naszego pipeline'u.
# Dla Vonage nie ma takiego skrótu: ElevenLabs ma oficjalną integrację (patrz
# elevenlabs.io/docs/conversational-ai/phone-numbers/telephony/vonage), ale w formie
# SAMODZIELNIE HOSTOWANEGO mostu WebSocket — dokładnie tej samej roli co już pełni
# bot_gemini_test.py dla Gemini Live/OpenAI Realtime na Vonage. Więc budujemy go tu,
# reużywając ten sam transport (FastAPIWebsocketTransport+VonageFrameSerializer,
# audio L16/16kHz) i ten sam wzorzec lokalnego VAD dla przerwań (VADProcessor —
# transport.output() sam czyści bufor audio na wykryte lokalnie mówienie klienta,
# patrz PipelineParams(allow_interruptions=True) niżej), zamiast pisać ręcznie parsowanie
# protokołu Vonage od zera.
#
# Protokół WebSocket ElevenLabs Conversational AI (potwierdzony w ich dokumentacji API,
# NIEPOTWIERDZONY jeszcze na żywym telefonie w chwili pisania — patrz TTFB/format audio
# w ElevenLabsRealtimeService, pierwsze realne połączenie może wymagać korekt, tak jak
# przy Gemini Live/OpenAI Realtime): wss://api.elevenlabs.io/v1/convai/conversation
# ?agent_id=..., autoryzacja nagłówkiem "xi-api-key" (serwer-serwer, więc bezpiecznie —
# w odróżnieniu od widgetów w przeglądarce nie potrzebujemy tu signed URL). Klient wysyła
# {"user_audio_chunk": "<base64 PCM>"}, serwer odsyła zdarzenia {"type": "audio", ...},
# "agent_response", "user_transcript", "interruption", "ping" (trzeba odpowiedzieć "pong"),
# "conversation_initiation_metadata" (tu neguje się faktyczny format audio — jeśli
# agent_output_audio_format != pcm_16000, TTFB/jakość ucierpi, bo Vonage jest sztywno na
# L16/16kHz i nie robimy tu resamplingu w wersji 1).
class ElevenLabsRealtimeService(FrameProcessor):
    """Most między pipecat a surowym WebSocketem ElevenLabs Conversational AI — siedzi
    w pipeline TAM gdzie normalnie siedziałby LLMService (np. GeminiLiveLLMService), ale
    to nie jest "prawdziwy" pipecat LLM service (żadnej z ich wbudowanych klas nie ma dla
    ElevenLabs Conversational AI w tej wersji pipecat, patrz github.com/pipecat-ai/pipecat
    issue #2812) — tylko FrameProcessor który połyka InputAudioRawFrame (wysyła do
    ElevenLabs, nic nie przepuszcza dalej — to jest ostateczny konsument audio wejściowego)
    i emituje TTSAudioRawFrame/TTSStartedFrame/TTSStoppedFrame na podstawie zdarzeń
    przychodzących z ich WebSocketu w osobnym tasku czytającym."""

    def __init__(self, tenant: dict, caller_phone: str, called_number: str, call_sid: str, agent_id: str, api_key: str, task_box: dict):
        super().__init__()
        self._tenant = tenant
        self._caller_phone = caller_phone
        self._called_number = called_number
        self._call_sid = call_sid
        self._agent_id = agent_id
        self._api_key = api_key
        # {"task": None} wypełniany PO utworzeniu PipelineTask w run_elevenlabs_vonage_bot
        # (task jeszcze nie istnieje w momencie tworzenia tego serwisu — patrz tam).
        # Potrzebny do _hangup_after_elevenlabs_closed: push_frame() STĄD leci tylko
        # "w dół" do transportu, nigdy nie dociera do samego PipelineTask, więc nie
        # zamyka realnie WebSocketu z Vonage — trzeba go wykolejkować przez sam task,
        # dokładnie jak robi to on_client_disconnected w run_elevenlabs_vonage_bot.
        # Błąd złapany na żywym telefonie 2026-09-03: bez tego EndFrame "wypychał się"
        # bezbłędnie, ale połączenie i tak wisiało w ciszy, aż klient ręcznie się rozłączył.
        self._task_box = task_box
        self._ws = None
        self._reader_task = None
        self._sample_rate = 16000
        self._speaking = False
        # True gdy ElevenLabs zamknął swoją stronę WebSocketu (np. po naturalnym końcu
        # rozmowy) ale Vonage jeszcze przez chwilę dosyła nam audio klienta (telefon
        # fizycznie rozłącza się z opóźnieniem) — bez tej flagi każda kolejna paczka
        # audio (co ~20ms) próbowałaby wysyłkę na martwy socket i logowała identyczny
        # błąd dziesiątki razy na sekundę (złapane na żywym telefonie 2026-09-03: kod
        # zamknięcia 1000/OK po obu stronach, więc to NIE błąd — po prostu koniec
        # rozmowy, nic nie tracimy, bo wysyłka i tak nigdy nie dociera do ElevenLabs).
        self._closed = False
        # True TYLKO gdy MY zainicjowaliśmy rozłączenie (EndFrame przyszedł z pipeline'u,
        # np. bo Vonage rozłączył klienta) — odróżnia to od sytuacji gdy ElevenLabs
        # zamyka swoją stronę PIERWSZY (naturalny koniec rozmowy, agent się pożegnał).
        # W tym drugim przypadku to MY musimy zainicjować rozłączenie Vonage (patrz
        # _read_loop) — w odróżnieniu od Twilio (register_call), gdzie ElevenLabs ma
        # bezpośrednią kontrolę nad połączeniem Twilio i sam je rozłącza; na Vonage to
        # MY jesteśmy właścicielem połączenia, ElevenLabs jest tylko "zdalnym mózgiem".
        # Błąd złapany na żywym telefonie 2026-09-03: bez tego klient zostawał podłączony
        # w ciszy przez 7+ sekund po pożegnaniu bota, aż sam się rozłączył ręcznie.
        self._we_disconnected = False
        # Pomiar TTFB "user->bot audio", kotwiczony o lokalny VAD-stop — dokładnie ten sam
        # wzorzec co GeminiUserMonitor/GeminiBotMonitor w bot_gemini_test.py (patrz tam pełny
        # docstring), żeby liczby były porównywalne 1:1 między silnikami/transportami.
        self._last_user_stop = None
        self._waiting_for_bot_audio = False

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            await self.push_frame(frame, direction)
            await self._connect()
        elif isinstance(frame, InputAudioRawFrame):
            await self._send_audio(frame.audio)
            # NIE przepuszczamy dalej — ElevenLabs sam robi STT/turn-taking po swojej
            # stronie, nic downstream nie potrzebuje surowego audio wejściowego.
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            if self._speaking:
                # Przerwanie wykryte LOKALNIE (nasz Silero VAD), bez czekania na
                # zdarzenie "interruption" z ElevenLabs (przychodzi z opóźnieniem
                # sieciowym — na żywym telefonie 2026-09-03 zaobserwowane 7+ sekund
                # martwego/nieprzerwanego audio bota, klient mówił "Halo?" wielokrotnie
                # zanim cokolwiek się zmieniło). InterruptionFrame to WŁAŚCIWY sygnał
                # który czyści bufor już wykolejkowanego audio w transporcie wyjściowym
                # (Gemini Live/OpenAI Realtime dostają to za darmo wewnątrz swoich
                # własnych serwisów pipecat — u nas trzeba to zrobić jawnie, bo
                # ElevenLabs nie ma wbudowanej klasy serwisu w tej wersji pipecat).
                self._speaking = False
                await self.push_frame(InterruptionFrame(), direction)
                await self.push_frame(TTSStoppedFrame(), direction)
            await self.push_frame(frame, direction)
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._last_user_stop = asyncio.get_event_loop().time()
            self._waiting_for_bot_audio = True
            await self.push_frame(frame, direction)
        elif isinstance(frame, EndFrame):
            self._we_disconnected = True
            await self._disconnect()
            await self.push_frame(frame, direction)
        else:
            await self.push_frame(frame, direction)

    async def _connect(self):
        conversation_config_override, dynamic_variables = await build_conversation_config_override(
            self._tenant, self._caller_phone, self._called_number, self._call_sid, channel="vonage",
        )
        url = f"wss://api.elevenlabs.io/v1/convai/conversation?agent_id={self._agent_id}"
        try:
            self._ws = await websockets.connect(url, additional_headers={"xi-api-key": self._api_key})
        except Exception as e:
            logger.error(f"❌ [ELEVENLABS/VONAGE] Połączenie WebSocket nie powiodło się: {e}")
            return
        await self._ws.send(json.dumps({
            "type": "conversation_initiation_client_data",
            "conversation_config_override": conversation_config_override,
            "dynamic_variables": dynamic_variables,
        }))
        logger.info(f"🔌 [ELEVENLABS/VONAGE] Połączono z agentem {self._agent_id} dla {self._tenant.get('name')}")
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _send_audio(self, audio: bytes):
        if self._ws is None or self._closed:
            return
        try:
            b64 = base64.b64encode(audio).decode("ascii")
            await self._ws.send(json.dumps({"user_audio_chunk": b64}))
        except websockets.exceptions.ConnectionClosed:
            # ElevenLabs zamknął stronę pierwszy (koniec rozmowy) — patrz komentarz przy
            # self._closed w __init__. Log RAZ, nie za każdą kolejną paczkę audio.
            self._closed = True
            logger.info("🔌 [ELEVENLABS/VONAGE] WebSocket ElevenLabs już zamknięty — przestaję wysyłać audio")
        except Exception as e:
            logger.error(f"❌ [ELEVENLABS/VONAGE] Wysyłka audio nie powiodła się: {e}")

    async def _read_loop(self):
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                mtype = msg.get("type")

                if mtype == "conversation_initiation_metadata":
                    meta = msg.get("conversation_initiation_metadata_event", {})
                    fmt = meta.get("agent_output_audio_format", "pcm_16000")
                    if fmt != "pcm_16000":
                        # Świadomie tylko log, nie resampling — patrz komentarz nad klasą.
                        logger.warning(f"⚠️ [ELEVENLABS/VONAGE] Nieoczekiwany format audio agenta: {fmt} (oczekiwano pcm_16000, jakość/latencja może ucierpieć)")

                elif mtype == "audio":
                    if not self._speaking:
                        self._speaking = True
                        await self.push_frame(TTSStartedFrame())
                        if self._waiting_for_bot_audio:
                            self._waiting_for_bot_audio = False
                            if self._last_user_stop is not None:
                                ms = (asyncio.get_event_loop().time() - self._last_user_stop) * 1000
                                icon = "🟢" if ms < 1500 else "🟡" if ms < 2500 else "🔴"
                                logger.info(f"⏱️ [ELEVENLABS/VONAGE/TOTAL] user->bot audio {ms:.0f}ms {icon}")
                    b64 = msg.get("audio_event", {}).get("audio_base_64", "")
                    if b64:
                        audio_bytes = base64.b64decode(b64)
                        await self.push_frame(TTSAudioRawFrame(audio=audio_bytes, sample_rate=self._sample_rate, num_channels=1))

                elif mtype == "agent_response_complete":
                    if self._speaking:
                        self._speaking = False
                        await self.push_frame(TTSStoppedFrame())

                elif mtype == "agent_response":
                    text = msg.get("agent_response_event", {}).get("agent_response", "")
                    if text:
                        logger.info(f"⏱️ [ELEVENLABS/VONAGE/BOT] mówi: {text!r}")

                elif mtype == "user_transcript":
                    text = msg.get("user_transcription_event", {}).get("user_transcript", "")
                    if text:
                        logger.info(f"⏱️ [ELEVENLABS/VONAGE/USER] transkrypcja: {text!r}")

                elif mtype == "interruption":
                    # Zapasowy sygnał — GŁÓWNE przerwanie leci teraz lokalnie z VAD
                    # (patrz VADUserStartedSpeakingFrame w process_frame, ten sam wzorzec
                    # co Gemini Live/OpenAI Realtime), bo to zdarzenie sieciowe przychodzi
                    # z zauważalnym opóźnieniem (złapane na żywym telefonie 2026-09-03:
                    # 7+ sekund nieprzerwanego audio bota zanim cokolwiek się zmieniło,
                    # zanim ten fix powstał). Zostaje jako druga linia obrony na wypadek
                    # gdyby lokalny VAD nie złapał jakiegoś przypadku.
                    logger.debug("⏱️ [ELEVENLABS/VONAGE] przerwanie (interruption, zdalne)")
                    if self._speaking:
                        self._speaking = False
                        await self.push_frame(InterruptionFrame())
                        await self.push_frame(TTSStoppedFrame())

                elif mtype == "ping":
                    event_id = msg.get("ping_event", {}).get("event_id")
                    try:
                        await self._ws.send(json.dumps({"type": "pong", "event_id": event_id}))
                    except Exception:
                        pass

                elif mtype == "client_error":
                    logger.error(f"❌ [ELEVENLABS/VONAGE] client_error: {msg}")

        except websockets.exceptions.ConnectionClosed as e:
            self._closed = True
            logger.info(f"🔌 [ELEVENLABS/VONAGE] WebSocket zamknięty: {e}")
        except asyncio.CancelledError:
            # MY anulowaliśmy ten task z _disconnect() (EndFrame już leci z innego
            # powodu, np. Vonage rozłączył klienta) — nic dodatkowego do zrobienia,
            # self._we_disconnected już ustawione w process_frame.
            raise
        except Exception as e:
            logger.error(f"❌ [ELEVENLABS/VONAGE] Błąd w pętli odczytu: {e}")
        finally:
            if not self._we_disconnected:
                # ElevenLabs zamknął stronę PIERWSZY — patrz komentarz przy
                # self._we_disconnected w __init__ po pełne wyjaśnienie różnicy względem
                # Twilio. My musimy teraz sami zainicjować koniec połączenia Vonage.
                spawn(self._hangup_after_elevenlabs_closed())

    async def _hangup_after_elevenlabs_closed(self):
        logger.info("👋 [ELEVENLABS/VONAGE] ElevenLabs zakończył rozmowę — rozłączam Vonage")
        # Krótki odstęp na dogranie ewentualnego ogona audio już w buforze transportu
        # (ten sam rząd wielkości co auto_hangup dla end_conversation w realtime_tools.py,
        # tam 3.0s — tu krócej, bo pożegnanie ElevenLabs już w całości poleciało zanim
        # zamknęli WebSocket, w odróżnieniu od tamtej ścieżki gdzie EndFrame leci od razu
        # po samym WYWOŁANIU narzędzia, przed wypowiedzeniem pożegnania).
        await asyncio.sleep(1.5)
        task = self._task_box.get("task")
        if task is None:
            logger.error("❌ [ELEVENLABS/VONAGE] Brak referencji do PipelineTask — nie mogę rozłączyć Vonage")
            return
        try:
            await task.queue_frame(EndFrame())
        except Exception as e:
            logger.error(f"❌ [ELEVENLABS/VONAGE] Nie udało się wykolejkować EndFrame po zamknięciu przez ElevenLabs: {e}")

    async def _disconnect(self):
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None


async def run_elevenlabs_vonage_bot(websocket: WebSocket, tenant: dict, caller_phone: str, called_number: str, call_sid: str):
    """Rozmowa ElevenLabs na Vonage przez nasz most audio.

    Rozliczenie minut robi /vonage/events, a transkrypt i raport — webhook
    /elevenlabs/post-call (oba niezależne od tego, który silnik obsłużył audio).
    """
    agent_id = _resolve_agent_id(tenant)
    if not ELEVENLABS_API_KEY or not agent_id:
        logger.error(f"❌ [ELEVENLABS/VONAGE] ELEVENLABS_API_KEY lub agent_id nieskonfigurowane dla {tenant.get('name')} — zamykam")
        await websocket.close()
        return

    transport = create_transport(websocket, "vonage")
    task_box: dict = {"task": None}  # uzupełniany po utworzeniu PipelineTask — patrz ElevenLabsRealtimeService
    elevenlabs_service = ElevenLabsRealtimeService(
        tenant=tenant, caller_phone=caller_phone, called_number=called_number,
        call_sid=call_sid or "", agent_id=agent_id, api_key=ELEVENLABS_API_KEY,
        task_box=task_box,
    )
    pipeline = Pipeline([
        transport.input(),
        # Lokalny VAD przerywa bota natychmiast, gdy klient zaczyna mówić — zdarzenie
        # "interruption" z ElevenLabs przychodzi ze sporym opóźnieniem sieciowym.
        create_local_vad(),
        elevenlabs_service,
        transport.output(),
    ])
    task = PipelineTask(pipeline, params=call_pipeline_params("vonage"))
    task_box["task"] = task

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("📴 [ELEVENLABS/VONAGE] Klient rozłączony")
        await task.queue_frame(EndFrame())

    logger.info(f"🚀 [ELEVENLABS/VONAGE] Start pipeline dla {tenant.get('name')}")
    try:
        await PipelineRunner().run(task)
    except Exception as e:
        logger.error(f"❌ [ELEVENLABS/VONAGE] Pipeline error: {e}")
    finally:
        logger.info("🏁 [ELEVENLABS/VONAGE] Koniec połączenia")


@router.websocket("/ws-elevenlabs-vonage")
async def elevenlabs_vonage_stream(websocket: WebSocket):
    """Wejście mostu dla firm z realtime_engine == "elevenlabs" na Vonage (fallback SIP direct)."""
    start = await accept_vonage_stream(websocket, "ELEVENLABS/VONAGE")
    if start is None:
        return
    logger.info(f"✅ [ELEVENLABS/VONAGE] Tenant: {start.tenant.get('name')}")
    await run_elevenlabs_vonage_bot(websocket, start.tenant, start.caller_phone, start.tenant_phone, start.call_sid or "")
