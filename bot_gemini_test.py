# bot_gemini_test.py — IZOLOWANY test modeli audio-to-audio (Gemini Live / OpenAI Realtime)
"""
Cel: sprawdzić latencję i jakość rozpoznawania polskich nazw usług/pracowników
przy użyciu modelu audio-to-audio (Gemini Live lub OpenAI Realtime) zamiast
Deepgram+GPT+TTS.

NIE dotyka produkcyjnego bot.py. Zero FlowManagera, zero logiki rezerwacji.
Reużywa Twojej bazy tenantów (get_tenant_by_phone / db) tylko do ODCZYTU danych firmy.

WYBÓR DOSTAWCY: zmienna środowiskowa REALTIME_PROVIDER
  "google" (domyślnie) — Gemini Live, wymaga GOOGLE_API_KEY
  "openai"             — OpenAI Realtime, wymaga OPENAI_API_KEY (ten sam klucz co w bot.py)
  Model dla OpenAI ustawiany przez OPENAI_REALTIME_MODEL (domyślnie "gpt-realtime-2.1-mini"
  — NIE zweryfikowałem tej nazwy na żywo w katalogu modeli OpenAI; jeśli dostaniesz 404,
  spróbuj "gpt-realtime-2", czyli domyślnego modelu w pipecat 1.4.0).

WYMAGANE ZMIENNE ŚRODOWISKOWE (te same co w Railway):
  GOOGLE_API_KEY       — wymagane gdy REALTIME_PROVIDER=google
  OPENAI_API_KEY       — wymagane gdy REALTIME_PROVIDER=openai
  TWILIO_AUTH_TOKEN    — do walidacji podpisu Twilio (opcjonalnie, można pominąć na testach)

PODŁĄCZENIE (Twilio):
  1) Wybierz numer testowy w konsoli Twilio (osobny lub tymczasowo przełącz istniejący)
  2) W ustawieniach numeru: "A call comes in" -> Webhook
     POST https://<twoj-railway-host>/twilio/incoming-gemini-test
  3. To wystarczy — nic więcej w konfiguracji Twilio nie trzeba zmieniać.

URUCHOMIENIE OBOK ISTNIEJĄCEGO bot.py:
  Ten plik ma WŁASNY obiekt FastAPI (app). Jeśli wdrażasz go jako osobny serwis
  na Railway — po prostu deployujesz ten plik zamiast bot.py (osobny serwis/URL).
  Jeśli wolisz trzymać w tym samym serwisie co produkcja — możesz podłączyć te
  endpointy do istniejącego `app` w bot.py przez `app.include_router(...)`,
  ale na start bezpieczniej jest mieć to jako kompletnie osobny proces/deploy,
  żeby nic nie mogło wywrócić produkcji.

WYMAGANY PIPECAT: >=1.4.0 (NIE ten sam pin co bot.py, który siedzi na 0.0.104
  i używa starego OpenAILLMContext API). Ten plik musi być zainstalowany z
  osobnego pliku wymagań — patrz requirements-gemini-test.txt — i wdrożony
  jako OSOBNY serwis Railway z własnym build commandem, inaczej podbicie
  pipecat-ai w requirements.txt wywali produkcyjny bot.py (patrz sekcja niżej).

UWAGI / RZECZY DO SPRAWDZENIA W TEŚCIE:
  - Oba serwisy generują audio natywnie w innym sample rate niż telefonia
    (Gemini 24kHz, OpenAI 24kHz vs Twilio 8kHz mu-law / Vonage 16kHz PCM).
    Pipecat powinien to resamplować automatycznie w transporcie, ale posłuchaj
    uważnie czy nie ma artefaktów/przycięć w głosie — to częsty problem na starcie.
  - VAD: tu zostawiony Silero (jak w produkcji) RÓWNOLEGLE z wbudowanym VAD
    usługi. Jeśli usłyszysz dziwne przerywanie wypowiedzi bota — spróbuj
    usunąć vad_analyzer z transportu i zdać się w 100% na VAD dostawcy.
  - To NIE mierzy dokładnie STT/LLM/TTS osobno (to jeden strumień audio-in/out).
    Mierzysz całość: koniec mowy użytkownika (transkrypcja od usługi) ->
    pierwsza ramka audio bota. Pomiar jest w dwóch osobnych procesorach
    (UserTranscriptMonitor / BotAudioMonitor), bo transkrypcja usera leci
    UPSTREAM z usługi, a audio bota DOWNSTREAM — jeden processor za LLM-em
    (jak było wcześniej) nigdy nie widział transkrypcji i pomiar się nie
    uruchamiał (0 pomiarów w poprzednich testach).
"""

import os
import sys
import json
import asyncio
from datetime import datetime

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
from pipecat.frames.frames import EndFrame, TranscriptionFrame, TTSAudioRawFrame
from pipecat.processors.frame_processor import FrameProcessor

# Pipecat >=1.2 przeniósł Gemini Live pod nową nazwę/ścieżkę (bez "Multimodal")
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService
from pipecat.services.openai.realtime.events import AudioConfiguration, AudioOutput, SessionProperties

# Reużywamy Twoich istniejących modułów TYLKO do odczytu danych firmy
from helpers import get_tenant_by_phone, db, saas_db

logger.remove()
logger.add(sys.stdout, level="DEBUG", format="{time:HH:mm:ss} | {level} | {message}")

app = FastAPI()

REALTIME_PROVIDER = os.getenv("REALTIME_PROVIDER", "google").lower()  # "google" | "openai"
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1-mini")
# OpenAI Realtime nie ma osobnych głosów per-język (jak Google pl-PL-...) — to
# uniwersalne persony głosowe, które mówią w języku z tekstu/instrukcji. Jakości
# polskiego akcentu nie da się zweryfikować bez żywego testu — stąd zmienna env,
# żeby dało się to przełączać bez zmian w kodzie. "marin" to obecnie flagowy,
# najbardziej naturalny głos OpenAI Realtime (stan na moją wiedzę — może się zmienić).
OPENAI_REALTIME_VOICE = os.getenv("OPENAI_REALTIME_VOICE", "marin")

# prosty stoper do zmierzenia całościowego opóźnienia user->bot
_t_state = {"last_user_frame": None, "waiting_for_bot_audio": False}


class UserTranscriptMonitor(FrameProcessor):
    """Łapie transkrypcję usera. UWAGA: usługi realtime (Gemini/OpenAI) pushują
    TranscriptionFrame w kierunku UPSTREAM (do context aggregatora), nie downstream
    do transportu — dlatego ten processor musi siedzieć MIĘDZY user_aggregator a llm,
    a nie za LLM-em (tam by nigdy tej ramki nie zobaczył — tak było w poprzednich testach)."""

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame):
            _t_state["last_user_frame"] = asyncio.get_event_loop().time()
            _t_state["waiting_for_bot_audio"] = True
            logger.info(f"⏱️ [USER] transkrypcja: {frame.text!r}")
        await self.push_frame(frame, direction)


class BotAudioMonitor(FrameProcessor):
    """Łapie pierwszą ramkę audio bota (downstream, za LLM-em) i liczy deltę od transkrypcji usera."""

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSAudioRawFrame) and _t_state["waiting_for_bot_audio"]:
            _t_state["waiting_for_bot_audio"] = False
            start = _t_state.get("last_user_frame")
            if start:
                ms = (asyncio.get_event_loop().time() - start) * 1000
                icon = "🟢" if ms < 1500 else "🟡" if ms < 2500 else "🔴"
                logger.info(f"⏱️ [TOTAL user->bot audio] {ms:.0f}ms {icon}")
        await self.push_frame(frame, direction)


def build_realtime_llm(system_prompt: str):
    """Buduje usługę audio-to-audio + parę context aggregatorów wg REALTIME_PROVIDER.

    Ten sam trigger (`user_aggregator.push_context_frame()` po connect) działa dla
    obu dostawców: Gemini seeduje kontekst systemową instrukcją, OpenAI bezwarunkowo
    odpala `_create_response()` na pierwszym LLMContextFrame — więc dla OpenAI ten
    mechanizm powinien być NIEZAWODNY (Gemini bywa kapryśny, patrz notatka w kodzie
    poniżej o audio-input / history-recall).
    """
    if REALTIME_PROVIDER == "openai":
        logger.info(
            f"🧠 REALTIME_PROVIDER=openai, model={OPENAI_REALTIME_MODEL}, voice={OPENAI_REALTIME_VOICE}"
        )
        llm = OpenAIRealtimeLLMService(
            api_key=os.getenv("OPENAI_API_KEY"),
            model=OPENAI_REALTIME_MODEL,
            settings=OpenAIRealtimeLLMService.Settings(
                system_instruction=system_prompt,
                session_properties=SessionProperties(
                    audio=AudioConfiguration(output=AudioOutput(voice=OPENAI_REALTIME_VOICE))
                ),
            ),
        )
    else:
        logger.info("🧠 REALTIME_PROVIDER=google (Gemini Live)")
        llm = GeminiLiveLLMService(
            api_key=os.getenv("GOOGLE_API_KEY"),
            model="models/gemini-2.5-flash-native-audio-preview-12-2025",  # stary "preview-native-audio-dialog" zwracał 404 (wycofany)
            voice_id="Aoede",
            system_instruction=system_prompt,
        )

    context = LLMContext()
    # realtime_service_mode=True: usługi realtime nie emitują (Gemini) lub emitują
    # inaczej (OpenAI) UserStarted/StoppedSpeakingFrame, więc zapisy do kontekstu
    # muszą iść w trybie "trailing" zamiast czekać na te ramki.
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context, realtime_service_mode=True
    )
    return llm, user_aggregator, assistant_aggregator


def build_system_prompt(tenant: dict) -> str:
    """Prosty system prompt z danych firmy — BEZ logiki rezerwacji."""
    services = tenant.get("services", []) or tenant.get("info_services", [])
    if services:
        svc_lines = "\n".join(
            f"- {s.get('name', '')}: {s.get('price', 'zapytaj o cenę')}"
            for s in services
        )
    else:
        svc_lines = "brak danych o usługach w systemie testowym"

    hours = tenant.get("working_hours", [])
    if hours:
        hours_lines = "\n".join(
            f"- {h.get('day', '')}: {h.get('open', '')}-{h.get('close', '')}"
            for h in hours
        )
    else:
        hours_lines = "brak danych o godzinach w systemie testowym"

    return f"""Jesteś asystentką głosową firmy {tenant.get('name', '')}.
Mówisz WYŁĄCZNIE po polsku, naturalnie i zwięźle, krótkimi zdaniami.

Usługi i cennik:
{svc_lines}

Godziny otwarcia:
{hours_lines}

To jest wersja TESTOWA systemu. NIE umawiasz wizyt — jeśli klient chce się
umówić, powiedz że rezerwacje telefoniczne są chwilowo w budowie i zaproponuj
kontakt w innej formie. Odpowiadaj tylko na pytania o firmę, usługi, ceny
i godziny otwarcia."""


# ==========================================
# TWILIO INCOMING (testowy webhook)
# ==========================================

@app.post("/twilio/incoming-gemini-test")
async def twilio_incoming_test(request: Request):
    form = await request.form()
    called = form.get("Called", form.get("To", ""))
    call_sid = form.get("CallSid", "")

    logger.info(f"📞 [GEMINI TEST] Incoming: {called} (CallSid: {call_sid})")

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
        <Stream url="wss://{host}/ws-gemini-test">
            <Parameter name="callSid" value="{call_sid}" />
            <Parameter name="tenantId" value="{tenant['id']}" />
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
    logger.info("🔌 [GEMINI TEST] WebSocket connected")

    stream_sid = None
    tenant = None

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
                tenant_id = custom_params.get("tenantId")

                rows = await db.execute(
                    "SELECT phone_number FROM tenants WHERE id = ?", [tenant_id]
                )
                if rows and rows[0].get("phone_number"):
                    tenant = await get_tenant_by_phone(rows[0]["phone_number"])
                break
    except Exception as e:
        logger.error(f"[GEMINI TEST] Błąd startu: {e}")
        await websocket.close()
        return

    if not stream_sid or not tenant:
        logger.error("❌ [GEMINI TEST] Brak stream_sid lub tenant — zamykam")
        await websocket.close()
        return

    logger.info(f"✅ [GEMINI TEST] Tenant: {tenant.get('name')}")

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

    system_prompt = build_system_prompt(tenant)
    llm, user_aggregator, assistant_aggregator = build_realtime_llm(system_prompt)
    user_transcript_monitor = UserTranscriptMonitor()
    bot_audio_monitor = BotAudioMonitor()

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

    conversation_ended = False

    @transport.event_handler("on_client_connected")
    async def on_connect(transport, client):
        logger.info(f"🎤 [GEMINI TEST] Klient połączony — wybudzam {REALTIME_PROVIDER} do przywitania")
        # Usługa realtime nie odzywa się pierwsza sama z siebie — trzeba popchnąć
        # pusty context frame, żeby wygenerowała pierwszą odpowiedź z system promptu
        # (patrz build_realtime_llm — mechanizm różni się między Gemini a OpenAI).
        await user_aggregator.push_context_frame()

    @transport.event_handler("on_client_disconnected")
    async def on_disconnect(transport, client):
        nonlocal conversation_ended
        logger.info("📴 [GEMINI TEST] Klient rozłączony")
        conversation_ended = True
        await task.queue_frame(EndFrame())

    runner = PipelineRunner()
    logger.info("🚀 [GEMINI TEST] Start pipeline")
    try:
        await runner.run(task)
    except Exception as e:
        logger.error(f"[GEMINI TEST] Pipeline error: {e}")
    finally:
        logger.info("🏁 [GEMINI TEST] Koniec połączenia")


@app.get("/health-gemini-test")
async def health():
    return {"status": "ok", "provider": REALTIME_PROVIDER}


# ==========================================
# VONAGE — ścieżka alternatywna (obok Twilio)
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
    logger.info(f"📞 [VONAGE TEST] Answer: {from_number} → {to_number}")

    # Numer Vonage jest nowy i nie ma go w bazie tenantów — na czas testu
    # ładujemy istniejącego tenanta na sztywno przez zmienną środowiskową,
    # zamiast szukać po numerze (który i tak nie pasowałby do żadnego wpisu).
    forced_tenant_id = os.getenv("TEST_TENANT_ID", "")
    if forced_tenant_id:
        rows = await db.execute("SELECT phone_number FROM tenants WHERE id = ?", [forced_tenant_id])
        tenant = await get_tenant_by_phone(rows[0]["phone_number"]) if rows else None
        logger.info(f"📞 [VONAGE TEST] Using forced tenant_id={forced_tenant_id} -> {tenant.get('name') if tenant else 'NOT FOUND'}")
    else:
        tenant = await get_tenant_by_phone(to_number)

    if not tenant:
        ncco = [{"action": "talk", "text": "Numer testowy nieaktywny.", "language": "pl-PL"}]
        return JSONResponse(ncco)

    host = request.headers.get("host", "localhost")
    ws_uri = f"wss://{host}/ws-gemini-test-vonage?tenantId={tenant['id']}"

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
    try:
        if request.method == "POST":
            data = await request.json()
            logger.info(f"[VONAGE EVENT] {data.get('status', data)}")
        else:
            status = request.query_params.get("status", "")
            logger.info(f"[VONAGE EVENT] status={status}")
    except Exception:
        pass
    return Response(content="", status_code=200)


@app.websocket("/ws-gemini-test-vonage")
async def websocket_gemini_test_vonage(websocket: WebSocket):
    tenant_id = websocket.query_params.get("tenantId")
    if not tenant_id:
        logger.error("❌ [VONAGE TEST] Brak tenantId w query params — zamykam")
        await websocket.close()
        return

    await websocket.accept()
    logger.info(f"🔌 [VONAGE TEST] WebSocket connected, tenant_id={tenant_id}")

    rows = None
    is_saas = tenant_id.startswith("firm_")
    if is_saas:
        rows = await saas_db.execute("SELECT phone_number FROM firms WHERE id = ?", [tenant_id])
    else:
        rows = await db.execute("SELECT phone_number FROM tenants WHERE id = ?", [tenant_id])

    if not rows or not rows[0].get("phone_number"):
        logger.error("❌ [VONAGE TEST] Nie znaleziono tenanta — zamykam")
        await websocket.close()
        return

    tenant = await get_tenant_by_phone(rows[0]["phone_number"])
    if not tenant:
        await websocket.close()
        return

    logger.info(f"✅ [VONAGE TEST] Tenant: {tenant.get('name')}")

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

    system_prompt = build_system_prompt(tenant)
    llm, user_aggregator, assistant_aggregator = build_realtime_llm(system_prompt)
    user_transcript_monitor = UserTranscriptMonitor()
    bot_audio_monitor = BotAudioMonitor()

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

    @transport.event_handler("on_client_connected")
    async def on_connect_vonage(transport, client):
        logger.info(f"🎤 [VONAGE TEST] Klient połączony — wybudzam {REALTIME_PROVIDER} do przywitania")
        await user_aggregator.push_context_frame()

    @transport.event_handler("on_client_disconnected")
    async def on_disconnect_vonage(transport, client):
        logger.info("📴 [VONAGE TEST] Klient rozłączony")
        await task.queue_frame(EndFrame())

    runner = PipelineRunner()
    logger.info("🚀 [VONAGE TEST] Start pipeline")
    try:
        await runner.run(task)
    except Exception as e:
        logger.error(f"[VONAGE TEST] Pipeline error: {e}")
    finally:
        logger.info("🏁 [VONAGE TEST] Koniec połączenia")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8001)))