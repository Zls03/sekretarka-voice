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

PODZIAŁ NA PLIKI (zrobiony gdy ten plik przekroczył ~1200 linii, przed Fazą 3, żeby
nie robiło się jeszcze gorzej — Faza 3 dopisze rezerwacje, najbardziej złożoną część):
  - bot_gemini_test.py (TEN plik) — orkiestrator Gemini Live: FastAPI (app), webhooki
    Vonage współdzielone z OpenAI Realtime, dispatch/websockety Gemini Live.
  - gemini_live_pipeline.py — wydzielone 2026-09-30 (po usunięciu cascade, plik znów
    urósł do ~1750 linii): stałe, monitory ciszy/zdrowia sesji, budowa LLM Gemini Live —
    czyste funkcje/klasy bez FastAPI, zero endpointów.
  - bot_openai_realtime.py — orkiestrator OpenAI Realtime (webhooki, websockety,
    monitoring/idle/latencja tej ścieżki), wydzielony z tego pliku 2026-08-24 wyłącznie
    dla czytelności, patrz jego docstring. Montowany tu przez app.include_router().
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
sprawdzić latencję. Świadomie osobne route'y, zero ingerencji w OpenAI Realtime
(bot_openai_realtime.py od 2026-08-24) — patrz docstring tamtego pliku.

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
  1) W ustawieniach numeru: "A call comes in" -> Webhook (POST)
     https://<twoj-railway-host>/twilio/incoming-gemini-live-test
  2) "Primary handler fails" -> Webhook (POST), ten sam host, /twilio/fallback
     — UWAGA: tej trasy NIE MA w tym pliku (nieużywana), zostaw puste jeśli Twilio wymaga.
  3) "Call status changes" -> Webhook (POST)
     https://<twoj-railway-host>/twilio/status
     — bez tego apply_call_charge() nigdy się nie odpala dla połączeń Twilio (kredyty/minuty
     się nie naliczają, patrz handler /twilio/status niżej, dodany 2026-08-31).

PODŁĄCZENIE (Vonage): patrz sekcja "VONAGE" niżej — bez zmian względem wcześniejszej wersji.

URUCHOMIENIE OBOK ISTNIEJĄCEGO bot.py:
  Ten plik ma WŁASNY obiekt FastAPI (app), własny osobny deploy na Railway
  (requirements-gemini-test.txt, pipecat-ai==1.4.0 — CELOWO inna wersja niż produkcyjny
  bot.py na 0.0.104). Nie instalować obu requirements w tym samym środowisku.
  Start command bez zmian: `uvicorn bot_gemini_test:app` — realtime_prompt.py i
  realtime_tools.py to zwykłe pliki .py w tym samym repo, żadna konfiguracja Railway
  nie musi się zmienić.

CO ZOSTAJE NA PÓŹNIEJ (świadomie NIE tutaj) — STAN NA COMMIT TWORZĄCY TEN PLIK, NIEAKTUALNE:
  - Reszta Fazy 4: zbieranie zgłoszeń (lead collection, wieloturowe), SMS, raport email po rozmowie
  - Żywe przekierowanie rozmowy (transfer) — ani dla Twilio (brak /twilio/after-stream w tym
    pliku) ani dla Vonage (brak mechanizmu w ogóle, wymaga Vonage REST API) — patrz docstring
    realtime_tools.py. Działa TYLKO ścieżka "zostaw wiadomość" (email przez Resend).
  - Faza 3: sprawdz_dostepnosc()/zarezerwuj() jako function-calling tools (ostatnia, bo
    najbardziej ryzykowna — patrz wyżej)
  - Faza 5: credits + call_logs
  ⚠️ POPRAWKA 2026-08-22: powyższe jest już NIEAKTUALNE — booking (book_appointment/
  manage_booking) i transfer (transfer_to_owner, Vonage) SĄ zaimplementowane, patrz has_booking/
  has_transfer w realtime_prompt.py i build_gemini_live_llm/build_contact_owner_tool niżej. Są
  to jednak per-TENANT przełączniki (booking_enabled, transfer_enabled), nie "jeszcze w budowie"
  globalnie — gdy wyłączone na danym tenancie, prompt ma mówić że opcja "nie jest włączona na tej
  linii", NIE że jest w budowie/to wersja testowa (był tu bug: sztywny tekst "jeszcze w budowie"
  mylił brak-per-tenanta z brakiem-funkcji-w-ogóle, złapane na demo-tenancie BizVoice).

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
from pipecat.frames.frames import EndFrame, LLMMessagesAppendFrame
from services.tts_factory import create_tts_service

# Gemini Live — budowa LLM/monitorów/stałych przeniesiona do gemini_live_pipeline.py
# (2026-09-30, pierwszy krok podziału tego pliku po usunięciu cascade — patrz jego
# docstring). Importy pipecat specyficzne dla TEJ sekcji (GeminiLiveLLMService,
# Language, ThinkingConfig, LLMContext, monitory ramek TTS/VAD) mieszkają teraz TAM,
# nie tutaj — tu zostaje tylko `from gemini_live_pipeline import ...` niżej.

# WYCOFANE 2026-09-03 (kilka minut po wdrożeniu): próba wymuszenia language_codes=["pl"] na
# input_audio_transcription (patch podmieniający AudioTranscriptionConfig w namespace modułu
# pipecat) — google-genai SDK deklaruje to pole, ale prawdziwe Live API go NIE obsługuje:
# "ERROR GeminiLiveLLMService#0::_connection_task_handler unexpected exception
# (google/genai/_live_converters.py:32): language_codes parameter is not supported in
# Gemini API." Efekt na żywym telefonie: KAŻDE połączenie (oba, i pierwszy kick-start connect
# i drugi właściwy) wywalało się na starcie — bot w ogóle się nie odzywał, nawet powitania.
# Transkrypcja klienta w obcym języku (np. "Alô. Vitam, alô.") zostaje nierozwiązanym,
# rzadkim problemem — patrz build_gemini_live_llm docstring — dopóki pipecat/google-genai
# nie zaczną realnie wspierać tego pola po stronie samego Live API.

# Reużywamy istniejących modułów: helpers.py (odczyt danych firmy + CRM, bez zależności
# od pipecat — bezpieczny import wprost). Budowanie promptu i tools — osobne pliki,
# patrz docstring wyżej po co ten podział.
from helpers import get_tenant_by_phone, db, saas_db, get_crm_contact_name
from realtime_prompt import build_realtime_instructions, append_known_caller_hint
from realtime_tools import (
    build_contact_owner_tool, build_end_conversation_tool,
    build_transfer_tool, send_missed_transfer_email,
    maybe_send_call_summary, save_call_transcript, apply_call_charge, is_call_allowed,
    build_human_first_ncco, process_human_first_recording,
)
from realtime_booking import build_book_appointment_tool, build_manage_booking_tool

logger.remove()
logger.add(sys.stdout, level="DEBUG", format="{time:HH:mm:ss} | {level} | {message}")

app = FastAPI()

# Sekcja OpenAI Realtime (webhooki, websockety, monitoring) mieszka teraz w osobnym
# pliku (patrz jego docstring) — zamontowana tu jako router, nie zmienia to niczego
# w faktycznych ścieżkach URL (te same route'y co wcześniej, patrz include_router niżej).
from bot_openai_realtime import router as openai_realtime_router
app.include_router(openai_realtime_router)

from bot_elevenlabs_agent import (
    router as elevenlabs_agent_router, build_register_call_twiml, run_elevenlabs_vonage_bot,
    ensure_elevenlabs_sip_number, ELEVENLABS_SIP_DOMAIN, _resolve_agent_id as resolve_elevenlabs_agent_id,
)
app.include_router(elevenlabs_agent_router)


# ==========================================================================
# WSPÓLNE WEBHOOKI VONAGE — używane przez OBIE ścieżki (OpenAI Realtime w
# bot_openai_realtime.py i Gemini Live niżej w tym pliku). NIE przenosić do
# bot_openai_realtime.py — obsługują billing/logi/transfer-fallback dla obu.
# ==========================================================================


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


@app.post("/twilio/status")
async def twilio_status_gemini_live_test(request: Request):
    """Status callback od Twilio — odpowiednik /vonage/events powyżej, dla numerów Twilio
    podpiętych pod ten plik (/twilio/incoming-gemini-live-test, oraz /twilio/incoming-gemini-test
    z bot_openai_realtime.py — router jest zamontowany w tym samym `app`, więc ta jedna trasa
    obsługuje obie ścieżki, tak jak /vonage/events obsługuje obie ścieżki Vonage).

    Bez tego apply_call_charge() nigdy się nie wywoływał dla połączeń Twilio na tym pliku —
    kredyty/minuty się nie naliczały, call_logs miał zerowy/pusty duration_seconds."""
    form = await request.form()

    call_sid = form.get("CallSid", "")
    call_status = form.get("CallStatus", "")
    call_duration = form.get("CallDuration", "0")
    to_number = form.get("To", "")
    from_number = form.get("From", "") or "nieznany"

    logger.info(f"[TWILIO STATUS] {call_sid} | {call_status} | {call_duration}s")

    if call_status not in ("completed", "busy", "no-answer", "failed", "canceled") or not call_sid:
        return Response(content="OK", media_type="text/plain")

    try:
        duration = int(call_duration) if call_duration else 0

        tenant = await get_tenant_by_phone(to_number) if to_number else None
        if not tenant:
            logger.warning(f"⚠️ [TWILIO STATUS] Nie znaleziono tenanta dla {to_number}")
            return Response(content="OK", media_type="text/plain")

        tenant_id = tenant["id"]
        is_saas_tenant = tenant.get("source") == "saas"
        target_db = saas_db if is_saas_tenant else db

        existing = await target_db.execute("SELECT id FROM call_logs WHERE call_sid = ?", [call_sid])
        if existing:
            await target_db.execute(
                "UPDATE call_logs SET duration_seconds = ?, status = ? WHERE call_sid = ?",
                [duration, call_status, call_sid],
            )
            logger.info(f"📊 [TWILIO STATUS] Updated call log: {call_sid} → {duration}s")
        else:
            await target_db.execute(
                """INSERT INTO call_logs
                   (id, tenant_id, call_sid, caller_phone, duration_seconds, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
                [f"call_{int(time.time())}", tenant_id, call_sid, from_number, duration, call_status],
            )
            logger.info(f"📊 [TWILIO STATUS] Created call log: {call_sid} → {duration}s")

        await apply_call_charge(tenant_id, is_saas_tenant, call_sid, call_status, duration)
    except Exception as e:
        logger.error(f"[TWILIO STATUS] twilio_status error: {e}")

    return Response(content="OK", media_type="text/plain")


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




from gemini_live_pipeline import (
    GEMINI_LIVE_MODEL, make_gemini_state, GeminiUserMonitor, GeminiBotMonitor,
    monitor_gemini_call_health, build_gemini_live_llm,
)




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

    # realtime_engine ('gemini'/'openai'/'elevenlabs', panel: zakładka "Głos agenta")
    # decyduje który silnik odbiera ten numer — SAM numer telefonu obsługuje wszystkie
    # trzy, tu jest jedyne miejsce rozgałęzienia.
    if tenant.get("realtime_engine") == "elevenlabs":
        # "Bring your own Twilio" (patrz bot_elevenlabs_agent.py, punkt 4 w docstringu) —
        # MY wołamy ich API i przekazujemy TwiML dalej, Twilio nigdy nie jest podpięte
        # bezpośrednio pod ElevenLabs. Fallback na błąd: krótki komunikat + rozłączenie,
        # NIE cisza — błąd konfiguracji ElevenLabs nie może zostawić klienta bez info.
        try:
            twiml = await build_register_call_twiml(tenant, caller, called, call_sid)
            return Response(content=twiml, media_type="application/xml")
        except Exception as e:
            logger.error(f"❌ [GEMINI LIVE TEST] register_call ElevenLabs nieudany: {e}")
            return Response(
                content='<?xml version="1.0"?><Response><Say language="pl-PL">'
                        'Przepraszamy, wystąpił błąd. Spróbuj ponownie później.</Say><Hangup/></Response>',
                media_type="application/xml",
            )

    host = request.headers.get("host", "localhost")
    # /ws-gemini-test to websocket z bot_openai_realtime.py (montowany w tym samym
    # Railway deployu), identyczny zestaw Parameter co niżej.
    ws_path = "ws-gemini-test" if tenant.get("realtime_engine") == "openai" else "ws-gemini-live-test"
    twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://{host}/{ws_path}">
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
    contact_owner_available = tenant.get("contact_owner_enabled", 1) == 1
    tools = []
    if contact_owner_available:
        tools.append(build_contact_owner_tool(tenant, caller_phone, task_box, gemini_state))
    tools.append(build_end_conversation_tool(task_box, gemini_state))
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

    system_prompt = build_realtime_instructions(
        tenant, None, has_booking=booking_available, has_contact_owner=contact_owner_available
    )
    known_name = await get_crm_contact_name(tenant.get("id", ""), caller_phone)
    if known_name:
        system_prompt = append_known_caller_hint(system_prompt, known_name, has_contact_owner=contact_owner_available)
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
    # Zawsze natywny głos Gemini (modalities=AUDIO) — hybryda z ElevenLabs jako
    # głównym głosem (2026-09-01) usunięta: mierzalnie wolniejsza (SimpleTextAggregator
    # czeka na CAŁE zdanie z Gemini zanim w ogóle wyśle tekst do TTS, patrz logi z
    # 2026-09-01 — user->bot audio ~4.1s śr. z ElevenLabs vs ~2.4s natywnie), a jakość
    # głosu nie rekompensowała tej różnicy w ocenie na żywo. fallback_tts zostaje w
    # OSOBNEJ gałęzi ParallelPipeline z wymuszonym sample_rate=24000 (ten sam co natywne
    # audio Gemini) wyłącznie pod speak_directly() (idle-nudge, "czy nadal jesteśmy
    # połączeni?") — Gemini nie da się w niezawodny sposób poprosić o wypowiedzenie
    # DOKŁADNEGO tekstu z zewnątrz, gdy jego sesja akurat ucichła.
    fallback_tts = create_tts_service(tenant, sample_rate=24000)
    voice_steps = [ParallelPipeline([llm], [fallback_tts])]

    pipeline = Pipeline([
        transport.input(),
        vad_processor,
        user_aggregator,
        gemini_user_monitor,
        *voice_steps,
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
        # EKSPERYMENT 2026-09-10: 1.0s nie było niczym uzasadnione w kodzie/historii —
        # wygląda na nieprzetestowany margines bezpieczeństwa. Skrócone do 0.3s żeby
        # przyspieszyć powitanie; jeśli na żywym telefonie pierwsze słowo powitania
        # zacznie się ucinać/gubić, podnieś z powrotem (sprawdź czy VAD/WS zdążyły się
        # ustabilizować — "Loading Silero VAD model..." w logach tuż po connect).
        await asyncio.sleep(0.3)
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
            # 2026-09-23 — save_call_transcript PRZED maybe_send_call_summary: tworzy wiersz
            # call_logs, który maybe_send_call_summary potem UPDATE'uje (summary/priority dla
            # portalu /crm) — odwrotna kolejność trafiałaby w jeszcze nieistniejący wiersz.
            await save_call_transcript(tenant, call_sid, caller_phone, llm_context)
        except Exception as e:
            logger.error(f"[GEMINI LIVE TEST] Call transcript error: {e}")
        try:
            await maybe_send_call_summary(tenant, caller_phone, llm_context, gemini_state, call_sid=call_sid)
        except Exception as e:
            logger.error(f"[GEMINI LIVE TEST] Call summary error: {e}")


@app.get("/health-gemini-live-test")
async def health_gemini_live():
    return {"status": "ok", "provider": "gemini-live", "model": GEMINI_LIVE_MODEL}


@app.api_route("/vonage/test-siperb", methods=["GET", "POST"])
async def vonage_test_siperb(request: Request):
    """TYMCZASOWY endpoint (2026-09-27, przywrócony 2026-09-26 po odpowiedzi supportu
    Siperb — literówka w polu Username connection "vonage", patrz mail Conrad de Wet)
    — wyłącznie do ręcznego testu Vonage SIP Trunk ("aisekretarka") -> Siperb ("Trunk
    wychodzący", nazwa połączenia "vonage") -> appka Siperb na telefonie. Do usunięcia
    po zakończeniu testu, niezależnie od wyniku.

    2026-09-27 — TYMCZASOWO podpięty jako Answer URL na poziomie CAŁEJ aplikacji Vonage
    ("bizvoice-gemini-test"), która obsługuje DWA numery: Bizvoice (...542, testowy) I
    numer prawdziwej, aktywnej firmy (...552) — złapane na żywo zanim wyrządziło szkodę.
    Dlatego: testowa ścieżka Siperb TYLKO dla numeru Bizvoice, każdy inny numer spada na
    normalną, produkcyjną ścieżkę (identyczną jak vonage_answer_gemini_live), żeby ...552
    działało dokładnie tak jak przed tym testem."""
    to_number = request.query_params.get("to", "")
    if to_number.lstrip("+").lstrip("0") != "48459050542":
        from_number = request.query_params.get("from", "")
        call_uuid = request.query_params.get("uuid", "")
        region_url = request.query_params.get("region_url", "")
        host = request.headers.get("host", "localhost")
        tenant = await get_tenant_by_phone(to_number)
        if not tenant:
            return JSONResponse([{"action": "talk", "text": "Numer testowy nieaktywny.", "language": "pl-PL"}])
        if not await is_call_allowed(tenant):
            return JSONResponse([{"action": "talk", "text": "Przepraszamy, linia jest chwilowo niedostępna.", "language": "pl-PL"}])
        ncco = await build_ai_ncco(tenant, from_number, to_number, call_uuid, host, region_url)
        return JSONResponse(ncco)

    logger.info(f"🧪 [SIPERB TEST] Answer webhook wywołany: {dict(request.query_params)}")
    ncco = [{
        "action": "connect",
        "timeout": 20,
        "eventType": "synchronous",
        "eventUrl": [f"https://{request.headers.get('host', 'localhost')}/vonage/test-siperb-event"],
        "endpoint": [{
            "type": "sip",
            "uri": "sip:siperb-bizvoice@eu-west-1-sbc-1.siperb.com;transport=udp",
        }],
    }]
    return JSONResponse(ncco)


@app.api_route("/vonage/test-siperb-event", methods=["GET", "POST"])
async def vonage_test_siperb_event(request: Request):
    """eventUrl dla powyższego testu — loguje surowy status żeby zobaczyć DOKŁADNIE co
    Vonage/Siperb zwracają (np. sip_code, reason) gdyby połączenie nie doszło do skutku."""
    try:
        body = await request.json()
    except Exception:
        body = (await request.body()).decode("utf-8", errors="replace")
    logger.info(f"🧪 [SIPERB TEST] eventUrl: query={dict(request.query_params)} | body={body}")
    return JSONResponse([])


async def build_ai_ncco(tenant: dict, from_number: str, to_number: str, call_uuid: str, host: str, region_url: str) -> list:
    """Wyodrębnione z vonage_answer_gemini_live (2026-09-25) żeby dało się wołać ten sam
    dispatch po realtime_engine z osobnej funkcji zwracającej listę NCCO zamiast JSONResponse
    (historycznie też z /vonage/human-first-fallback — mechanizm "najpierw dzwoni do
    właściciela" usunięty 2026-09-28, nie działał niezawodnie na zablokowanym telefonie;
    ewentualny powrót do tego pomysłu przez appkę SIP typu Siperb, nie Vonage Users API)."""
    # realtime_engine ('gemini'/'openai'/'elevenlabs', panel: zakładka "Głos agenta")
    # decyduje który pipeline odbiera ten numer — SAM numer telefonu obsługuje wszystkie
    # trzy silniki, tu jest jedyne miejsce rozgałęzienia. /ws-gemini-test-vonage to
    # websocket z bot_openai_realtime.py, /ws-elevenlabs-vonage z bot_elevenlabs_agent.py
    # (oba montowane w tym samym Railway deployu) — żaden z nich nie czyta regionUrl
    # (nie robią transferu Vonage, patrz ich handlery).
    if tenant.get("realtime_engine") == "openai":
        ws_uri = (
            f"wss://{host}/ws-gemini-test-vonage?phone={tenant['phone_number']}"
            f"&callerPhone={from_number}&callSid={call_uuid}"
        )
    elif tenant.get("realtime_engine") == "elevenlabs":
        # SIP direct (2026-09-05) — zamiast mostu WebSocket przez nasz serwer, próbujemy
        # połączyć Vonage BEZPOŚREDNIO z ElevenLabs przez SIP trunk (patrz docstring
        # ensure_elevenlabs_sip_number w bot_elevenlabs_agent.py po pełne wyjaśnienie).
        # Usuwa ~500ms/turę narzutu naszego relaya, potwierdzone na żywych połączeniach.
        # Import numeru jest idempotentny i leniwy — pierwsza rozmowa tej firmy robi
        # faktyczny import, kolejne dostają 409 (już zaimportowany) = też sukces.
        # Przy JAKIMKOLWIEK niepowodzeniu (brak klucza, błąd sieci, ElevenLabs down)
        # spadamy na stary, sprawdzony most WebSocket — klient nigdy nie zostaje bez
        # ścieżki połączenia.
        # 2026-09-05: Vonage API Support (AI assistant) potwierdził że connect->SIP na
        # zewnętrzną domenę JEST wspierane "by design" — sip_code=404/cannot_route NIE
        # znaczy "funkcja niedostępna", tylko że dany request nie mógł dotrzeć do celu.
        # Kolejne ustalenia z tego samego czatu (po tym jak "from" + brak "+" NIE
        # naprawiły błędu na żywym teście): domyślny transport dla NCCO connect->SIP to
        # UDP na porcie 5060, a ElevenLabs SIP endpoint (jak inni SIP-trunk providerzy,
        # patrz ich dokumentacja Telnyx) wymaga TCP. Dodajemy ";transport=tcp" do URI
        # (standardowy mechanizm parametrów SIP URI, RFC 3261, wspierany przez Vonage —
        # potwierdzone w ich dokumentacji SIP Technical Details). Trunk "aisekretarka" z
        # dashboard.vonage.com/sip-trunking (BYOC/SIP Trunking) NIE ma tu znaczenia
        # (osobny produkt) — zostawiony założony, ale nieużywany.
        # 2026-09-05: WYŁĄCZONE PONOWNIE — nawet z "from" + bez "+" + transport=tcp
        # (wszystkie 3 sugestie AI supportu Vonage z rzędu) dalej identyczny
        # sip_code=404/cannot_route na żywym teście. Eskalowane do prawdziwego
        # człowieka w Vonage Support (ticket #3122205).
        # 2026-09-08: człowiek z Vonage Support odpisał — zasugerował że używamy
        # numeru Vonage (LVN) jako identyfikatora w SIP URI i że to może być
        # przyczyną 404. Znaleziony realny mismatch: import numeru w
        # ensure_elevenlabs_sip_number szedł Z "+", a URI tutaj budowane było BEZ
        # "+" (lstrip) — dokumentacja ElevenLabs SIP trunking wprost wymaga
        # identycznego formatu przy imporcie i przy wywołaniu. Naprawione, ALE na
        # żywym teście (2026-09-08 11:28) dalej identyczny sip_code=404/cannot_route
        # — format nie był (jedyną) przyczyną. Potwierdzone niezależnie po stronie
        # ElevenLabs: zero zapisanych prób w ich logach SIP dla obu testów (ani
        # tego, ani próby przez natywny produkt "SIP Trunking" Vonage) — więc
        # INVITE najpewniej nigdy nie opuszczał sieci Vonage.
        # 2026-09-09: kolejna odpowiedź z ticketu #3122205 (człowiek, Aldo) wróciła
        # do tej samej teorii identyfikatora bez odpowiedzi na pytanie czy INVITE
        # w ogóle wyszedł. Podał link do dok. NCCO connect->sip — brak tam
        # wymaganych nagłówków, ALE jest alternatywny sposób zapisu endpointu:
        # pola "user"+"domain" zamiast "uri" (mutually exclusive wg dokumentacji).
        # PRZETESTOWANE na żywo (2026-09-09 10:20): Vonage odrzucił to OD RAZU,
        # na własnej walidacji — reason="invalid sip domain,invalid sip domain
        # user" (dzwoniący usłyszał "numer zajęty" niemal natychmiast, event
        # przyszedł przez /vonage/events, NIE przez eventUrl niżej — to była
        # odmowa żądania jako niepoprawnego, nie porażka próby połączenia).
        # To POTWIERDZA że "uri" (oryginalny format) jest strukturalnie
        # poprawny — "user"+"domain" to najwyraźniej pole pod WŁASNE
        # skonfigurowane trunki Vonage (jak "aisekretarka"), nie pod dowolną
        # zewnętrzną domenę. Wracamy do "uri". WYŁĄCZONE PONOWNIE — ta sama
        # zasada co poprzednio, nie włączaj bez nowych ustaleń z ticketu.
        # Mechanizm eventType=synchronous+eventUrl (/vonage/sip-fallback-elevenlabs)
        # ZOSTAJE w kodzie na przyszłość — nieszkodliwy gdy SIP_DIRECT_ENABLED=False,
        # i realnie działa jako siatka bezpieczeństwa dla porażek NA POZIOMIE
        # połączenia (cannot_route itp.), tylko nie dla odrzuceń walidacji jak ta.
        # 2026-09-10 — Vonage support (Aldo, ticket #3122205) potwierdził że INVITE faktycznie
        # dociera do ElevenLabs i dostaje 404 Not Found z ICH serwera (nie problem formatu/NCCO
        # po naszej/Vonage stronie) — przyczyna znaleziona i naprawiona w
        # ensure_elevenlabs_sip_number (brakujący inbound_trunk_config.allowed_addresses).
        # WŁĄCZONE DLA WSZYSTKICH numerów Vonage na silniku ElevenLabs (na wyraźną prośbę
        # użytkownika, po potwierdzeniu na żywo na numerze testowym Bizvoice: poprawny
        # caller ID, called_number/channel/call_sid w dynamic_variables, X-CALL-ID zgadzający
        # call_sid z UUID Vonage, transkrypt+raport widoczne w panelu). Fallback na most
        # WebSocket (ws-elevenlabs-vonage) NIŻEJ zostaje jako siatka bezpieczeństwa przy
        # jakimkolwiek niepowodzeniu importu/połączenia SIP — klient nigdy nie zostaje bez
        # ścieżki. NIEPRZETESTOWANE JESZCZE na żywo przez czysty SIP direct: contact_owner
        # i book_appointment/manage_booking (tylko przez most) — warto obserwować pierwsze
        # rozmowy firm z tymi włączonymi funkcjami.
        SIP_DIRECT_ENABLED = True
        agent_id = resolve_elevenlabs_agent_id(tenant)
        sip_ready = SIP_DIRECT_ENABLED and await ensure_elevenlabs_sip_number(tenant["phone_number"], agent_id)
        if sip_ready:
            sip_number = to_number if to_number.startswith("+") else f"+{to_number}"
            fallback_ws_uri = (
                f"wss://{host}/ws-elevenlabs-vonage?phone={tenant['phone_number']}"
                f"&callerPhone={from_number}&callSid={call_uuid}"
            )
            event_url = (
                f"https://{host}/vonage/sip-fallback-elevenlabs?wsUri={quote(fallback_ws_uri, safe='')}"
            )
            ncco = [{
                "action": "connect",
                # 2026-09-10 — BUG: tu było "from": sip_number (czyli numer SEKRETARKI,
                # ten sam co "to") zamiast numeru DZWONIĄCEGO. Vonage wysyła tę wartość
                # jako Caller-ID/From w SIP INVITE do ElevenLabs, więc ich webhook
                # personalizacji dostawał caller_id == called_number — mail z raportem
                # pokazywał numer sekretarki zamiast numeru klienta. Złapane na żywym
                # pierwszym poprawnie wysłanym raporcie (2026-09-10, "Telefon: 48459050542"
                # zamiast realnego numeru dzwoniącego).
                "from": from_number.lstrip("+") if from_number else sip_number.lstrip("+"),
                "eventType": "synchronous",
                "eventUrl": [event_url],
                "endpoint": [{
                    "type": "sip",
                    "uri": f"sip:{sip_number}@{ELEVENLABS_SIP_DOMAIN};transport=tcp",
                    # 2026-09-10 — bez tego ElevenLabs generuje WŁASNE call_sid (SCL_xxx) dla
                    # SIP-trunkowej nogi połączenia, całkowicie inne niż UUID Vonage pod którym
                    # zapisujemy wpis w call_logs (panel: "Historia rozmów") — transkrypt
                    # (save_elevenlabs_transcript, keyowany po call_sid z /elevenlabs/post-call)
                    # lądował więc pod ID, do którego panel nigdy nie zajrzy, bo szuka po UUID
                    # Vonage. X-CALL-ID to udokumentowany zarezerwowany nagłówek ElevenLabs SIP
                    # trunking (elevenlabs.io/docs -> sip-trunking, sekcja "Standard Metadata
                    # Headers") — NADPISUJE ich system__call_sid naszym UUID, więc oba systemy
                    # zaczynają się zgadzać od pierwszego webhooka (personalizacja) po ostatni
                    # (post-call). Vonage sam dokleja prefiks "X-" do klucza w "headers".
                    "headers": {"CALL-ID": call_uuid},
                }],
            }]
            logger.info(f"📞 [ELEVENLABS/VONAGE SIP] Bezpośrednie połączenie (uri, z fallbackiem): {sip_number}")
            return ncco
        # 2026-09-10 — ten warning strzelał myląco dla KAŻDEGO tenanta na silniku ElevenLabs,
        # nie tylko numeru testowego — SIP_DIRECT_ENABLED=False (bo to nie jest numer testowy)
        # też ląduje w tej gałęzi, więc "import nie powiódł się" sugerowało realny błąd tam
        # gdzie import w ogóle nie był próbowany (most WebSocket to normalna, oczekiwana ścieżka
        # dla wszystkich poza jednym testowym numerem). Złapane na żywo przy debugowaniu QFX.
        if SIP_DIRECT_ENABLED:
            logger.warning(f"⚠️ [ELEVENLABS/VONAGE SIP] Import numeru nie powiódł się — fallback na most WebSocket")
        ws_uri = (
            f"wss://{host}/ws-elevenlabs-vonage?phone={tenant['phone_number']}"
            f"&callerPhone={from_number}&callSid={call_uuid}"
        )
    else:
        ws_uri = (
            f"wss://{host}/ws-gemini-live-test-vonage?phone={tenant['phone_number']}"
            f"&callerPhone={from_number}&callSid={call_uuid}&regionUrl={quote(region_url, safe='')}"
        )

    return [
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

    # "Najpierw dzwoni do właściciela" v2 (human_first_enabled, panel: zakładka Ustawienia →
    # "Najpierw dzwoni do właściciela") — przez apkę Siperb (SIP), patrz
    # realtime_tools.py::build_human_first_ncco. Domyślnie WYŁĄCZONE (0) dla każdej firmy,
    # więc zero zmiany zachowania dopóki ktoś świadomie tego nie włączy I nie wypełni
    # siperb_sip_username. Porażka przygotowania (brak/puste SIP username) cicho spada na
    # zwykłą ścieżkę AI niżej — właściciel nigdy nie traci połączenia przez błąd tej funkcji.
    if tenant.get("human_first_enabled"):
        human_first_ncco = await build_human_first_ncco(tenant, from_number, to_number, call_uuid, host, region_url)
        if human_first_ncco:
            logger.info(f"📱 [HUMAN-FIRST/SIPERB] Dzwonię najpierw do apki Siperb właściciela: {tenant.get('id')}")
            return JSONResponse(human_first_ncco)
        logger.warning(f"📱 [HUMAN-FIRST/SIPERB] Brak siperb_sip_username — od razu sekretarka AI: {tenant.get('id')}")

    ncco = await build_ai_ncco(tenant, from_number, to_number, call_uuid, host, region_url)
    return JSONResponse(ncco)


@app.api_route("/vonage/human-first-fallback", methods=["GET", "POST"])
async def vonage_human_first_fallback(request: Request):
    """eventUrl (eventType=synchronous) dla connect->sip w build_human_first_ncco — Vonage
    odpytuje to gdy właściciel nie odbierze apki Siperb (timeout/busy/rejected/failed —
    apka niezalogowana/offline daje ten sam efekt, Siperb po prostu nie znajduje
    zarejestrowanego urządzenia). Zwraca BEZWARUNKOWO świeżą NCCO z build_ai_ncco (ten sam
    sprawdzony wzorzec co vonage_sip_fallback_elevenlabs — Vonage odpytuje ten URL NAWET
    przy sukcesie connect, ale wtedy po prostu ignoruje zwróconą NCCO bo leg już żyje).
    to/from/uuid/regionUrl są przekazane w query stringu z miejsca budowania oryginalnej
    NCCO — ten webhook nie ma dostępu do obiektu tenanta, więc odtwarza go po numerze."""
    to_number = request.query_params.get("to", "")
    from_number = request.query_params.get("from", "")
    call_uuid = request.query_params.get("uuid", "")
    region_url = request.query_params.get("regionUrl", "")
    try:
        body = await request.json()
    except Exception:
        body = (await request.body()).decode("utf-8", errors="replace")
    status = body.get("status") if isinstance(body, dict) else None
    logger.info(f"📱 [HUMAN-FIRST/SIPERB] eventUrl odpytany, status={status!r} | body={body}")

    tenant = await get_tenant_by_phone(to_number)
    if not tenant:
        return JSONResponse([{"action": "talk", "text": "Przepraszamy, wystąpił błąd połączenia.", "language": "pl-PL"}])

    host = request.headers.get("host", "localhost")
    ncco = await build_ai_ncco(tenant, from_number, to_number, call_uuid, host, region_url)
    return JSONResponse(ncco)


@app.api_route("/vonage/human-first-recording", methods=["GET", "POST"])
async def vonage_human_first_recording(request: Request):
    """eventUrl dla action "record" w build_human_first_ncco — Vonage POSTuje tu link do
    nagrania PO zakończeniu połączenia, niezależnie czy właściciel odebrał czy nie (jeśli
    connect->sip nigdy się nie połączył, nagranie jest puste/krótkie —
    process_human_first_recording cicho pomija zapis gdy Deepgram nie zwróci żadnego
    transkryptu). Fire-and-forget: Vonage dostaje szybkie potwierdzenie, faktyczne
    pobranie+transkrypcja+podsumowanie (kilka-kilkanaście sekund) dzieje się w tle."""
    to_number = request.query_params.get("to", "")
    from_number = request.query_params.get("from", "")
    call_uuid = request.query_params.get("uuid", "")
    try:
        body = await request.json()
    except Exception:
        body = {}
    recording_url = body.get("recording_url") if isinstance(body, dict) else None
    logger.info(f"📼 [HUMAN-FIRST/RECORDING] eventUrl odpytany | body={body}")
    if not recording_url:
        return JSONResponse({"status": "ignored"})

    tenant = await get_tenant_by_phone(to_number)
    if not tenant:
        return JSONResponse({"status": "ignored"})

    duration_seconds = 0
    try:
        start = body.get("start_time")
        end = body.get("end_time")
        if start and end:
            from datetime import datetime as _dt
            fmt = "%Y-%m-%dT%H:%M:%SZ"
            duration_seconds = int((_dt.strptime(end, fmt) - _dt.strptime(start, fmt)).total_seconds())
    except Exception:
        pass

    asyncio.create_task(process_human_first_recording(tenant, recording_url, from_number, call_uuid, duration_seconds))
    return JSONResponse({"status": "ok"})


@app.api_route("/vonage/sip-fallback-elevenlabs", methods=["GET", "POST"])
async def vonage_sip_fallback_elevenlabs(request: Request):
    """eventUrl (eventType=synchronous) dla connect->SIP w vonage_answer_gemini_live —
    Vonage odpytuje to gdy próba SIP direct do ElevenLabs zawiedzie (failed/rejected/
    timeout/busy) i oczekuje w odpowiedzi ŚWIEŻEJ NCCO. Bez tego (stan sprzed
    2026-09-09) porażka connect->SIP PO WYSŁANIU NCCO kończyła połączenie bez żadnej
    ścieżki dla dzwoniącego — złapane na żywo dwa razy. wsUri (już zbudowany, gotowy
    URI mostu WebSocket) jest przekazywany w query stringu z miejsca budowania
    oryginalnej NCCO, więc tu nic nie trzeba odtwarzać z tenanta na nowo.

    2026-09-18 — POPRAWKA: wcześniej ten endpoint zakładał bezwarunkowo (samym faktem
    bycia wywołanym), że SIP connect zawiódł, i logował to jako WARNING. Na żywo
    (Voice Inspector + brak "WebSocket connected" w logach most nigdy się realnie NIE
    uruchamiał) potwierdzone, że Vonage odpytuje ten eventUrl RÓWNIEŻ przy udanych
    połączeniach (widziane 2x na rozmowę — raz przy starcie, raz przy końcu), nie tylko
    przy realnej awarii. Zwracamy fresh NCCO z fallbackiem jak dotychczas (siatka
    bezpieczeństwa zostaje — na wypadek gdyby TYM razem to było prawdziwe niepowodzenie,
    Vonage i tak zignoruje tę NCCO jeśli leg już żyje), ale logujemy SUROWĄ treść którą
    Vonage faktycznie przysłał zamiast zgadywać — dopiero to pozwoli kiedyś odróżnić
    realną awarię od nieszkodliwego zdarzenia."""
    ws_uri = request.query_params.get("wsUri", "")
    try:
        body = await request.json()
    except Exception:
        body = (await request.body()).decode("utf-8", errors="replace")
    # 2026-09-18 — status'y realnie zaobserwowane na żywo (2 pełne, udane rozmowy,
    # potwierdzone Voice Inspectorem) dla NORMALNEGO przebiegu leg'a SIP: started →
    # ringing → answered → completed. Tylko coś SPOZA tej listy (np. failed/rejected/
    # busy/timeout/cannot_route — nazwy nie potwierdzone na żywo, bo jeszcze nie
    # złapaliśmy prawdziwej awarii PO naprawie allowed_addresses) oznacza że warto na
    # to realnie zerknąć — stąd WARNING tylko dla nieznanego statusu, reszta to INFO.
    _BENIGN_SIP_STATUSES = {"started", "ringing", "answered", "completed"}
    status = body.get("status") if isinstance(body, dict) else None
    log_fn = logger.info if status in _BENIGN_SIP_STATUSES else logger.warning
    log_fn(
        f"{'ℹ️' if status in _BENIGN_SIP_STATUSES else '⚠️'} [ELEVENLABS/VONAGE SIP] "
        f"eventUrl odpytany, status={status!r} (spoza {_BENIGN_SIP_STATUSES} = warto sprawdzić) "
        f"| query={dict(request.query_params)} | body={body}"
    )
    if not ws_uri:
        return JSONResponse([{"action": "talk", "text": "Przepraszamy, wystąpił błąd połączenia.", "language": "pl-PL"}])
    ncco = [{
        "action": "connect",
        "endpoint": [{"type": "websocket", "uri": ws_uri, "content-type": "audio/l16;rate=16000"}],
    }]
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
    contact_owner_available = tenant.get("contact_owner_enabled", 1) == 1
    tools = []
    if contact_owner_available:
        tools.append(build_contact_owner_tool(
            tenant, caller_phone, task_box, gemini_state, has_transfer_tool=transfer_available
        ))
    tools.append(build_end_conversation_tool(task_box, gemini_state))
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

    system_prompt = build_realtime_instructions(
        tenant, None, has_transfer=transfer_available, has_booking=booking_available,
        has_contact_owner=contact_owner_available,
    )
    known_name = await get_crm_contact_name(tenant.get("id", ""), caller_phone)
    if known_name:
        system_prompt = append_known_caller_hint(system_prompt, known_name, has_contact_owner=contact_owner_available)
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
    # Zawsze natywny głos Gemini (modalities=AUDIO) — hybryda z ElevenLabs jako
    # głównym głosem (2026-09-01) usunięta: mierzalnie wolniejsza (SimpleTextAggregator
    # czeka na CAŁE zdanie z Gemini zanim w ogóle wyśle tekst do TTS, patrz logi z
    # 2026-09-01 — user->bot audio ~4.1s śr. z ElevenLabs vs ~2.4s natywnie), a jakość
    # głosu nie rekompensowała tej różnicy w ocenie na żywo. fallback_tts zostaje w
    # OSOBNEJ gałęzi ParallelPipeline z wymuszonym sample_rate=24000 (ten sam co natywne
    # audio Gemini) wyłącznie pod speak_directly() (idle-nudge, "czy nadal jesteśmy
    # połączeni?") — Gemini nie da się w niezawodny sposób poprosić o wypowiedzenie
    # DOKŁADNEGO tekstu z zewnątrz, gdy jego sesja akurat ucichła.
    fallback_tts = create_tts_service(tenant, sample_rate=24000)
    voice_steps = [ParallelPipeline([llm], [fallback_tts])]

    pipeline = Pipeline([
        transport.input(),
        vad_processor,
        user_aggregator,
        gemini_user_monitor,
        *voice_steps,
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
        # EKSPERYMENT 2026-09-10 — patrz komentarz w on_connect (Twilio) wyżej, ten sam eksperyment.
        await asyncio.sleep(0.3)
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
            await save_call_transcript(tenant, call_sid, caller_phone, llm_context)
        except Exception as e:
            logger.error(f"[GEMINI LIVE TEST/VONAGE] Call transcript error: {e}")
        try:
            await maybe_send_call_summary(tenant, caller_phone, llm_context, gemini_state, call_sid=call_sid)
        except Exception as e:
            logger.error(f"[GEMINI LIVE TEST/VONAGE] Call summary error: {e}")


@app.websocket("/ws-elevenlabs-vonage")
async def websocket_elevenlabs_vonage(websocket: WebSocket):
    """Wejście dla realtime_engine == 'elevenlabs' na Vonage — patrz sekcja "MOST VONAGE"
    w bot_elevenlabs_agent.py po pełne wyjaśnienie. Sama funkcja tylko: parsuje query
    params (ten sam wzorzec co /ws-gemini-live-test-vonage wyżej), znajduje tenanta,
    i oddaje sterowanie run_elevenlabs_vonage_bot() — cała logika pipeline'u/WebSocketu
    ElevenLabs mieszka w bot_elevenlabs_agent.py, żeby nie duplikować jej w dwóch plikach."""
    tenant_phone = websocket.query_params.get("phone")
    caller_phone = websocket.query_params.get("callerPhone", "nieznany")
    call_sid = websocket.query_params.get("callSid")
    if not tenant_phone:
        logger.error("❌ [ELEVENLABS/VONAGE] Brak phone w query params — zamykam")
        await websocket.close()
        return

    await websocket.accept()
    logger.info(f"🔌 [ELEVENLABS/VONAGE] WebSocket connected, phone={tenant_phone}")

    tenant = await get_tenant_by_phone(tenant_phone)
    if not tenant:
        logger.error("❌ [ELEVENLABS/VONAGE] Nie znaleziono tenanta — zamykam")
        await websocket.close()
        return

    logger.info(f"✅ [ELEVENLABS/VONAGE] Tenant: {tenant.get('name')}")
    await run_elevenlabs_vonage_bot(websocket, tenant, caller_phone, tenant_phone, call_sid or "")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8001)))
