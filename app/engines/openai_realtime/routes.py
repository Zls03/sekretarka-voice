"""Webhooki i websockety ścieżki OpenAI Realtime."""

import asyncio
import json
import os

from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import JSONResponse, Response
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import EndFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.serializers.vonage import VonageFrameSerializer
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport

from app.billing import is_call_allowed
from app.booking.book_appointment import build_book_appointment_tool
from app.booking.manage_booking import build_manage_booking_tool
from app.call_logs import save_call_transcript
from app.crm_contacts import get_crm_contact_name
from app.db import db
from app.engines.openai_realtime.llm import apply_crm_when_ready, build_realtime_llm
from app.engines.openai_realtime.monitors import BotAudioMonitor, UserTranscriptMonitor, make_call_state
from app.engines.openai_realtime.watchdog import monitor_call_health, say_now
from app.panel_client import get_client_profile
from app.post_call.report import maybe_send_call_summary
from app.prompt.instructions import append_known_caller_hint, build_greeting_message, build_realtime_instructions
from app.tenants import get_tenant_by_phone
from app.tools.contact_owner import build_contact_owner_tool
from app.tools.end_conversation import build_end_conversation_tool

router = APIRouter()


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
    contact_owner_available = tenant.get("contact_owner_enabled", 1) == 1
    tools = []
    if contact_owner_available:
        tools.append(build_contact_owner_tool(tenant, caller_phone, task_box, call_state))
    tools.append(build_end_conversation_tool(task_box, call_state))
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
    system_prompt = build_realtime_instructions(
        tenant, None, has_booking=booking_available, has_contact_owner=contact_owner_available
    )
    known_name = await get_crm_contact_name(tenant.get("id", ""), caller_phone)
    if known_name:
        system_prompt = append_known_caller_hint(system_prompt, known_name, has_contact_owner=contact_owner_available)
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
        # Popchnięcie pustego context frame i poleganie na tym, że model SAM przeczyta
        # blok "ROZPOCZĘCIE ROZMOWY" z system promptu, okazało się niewiarygodne na żywym
        # telefonie (2026-09-03: model zignorował dokładny tekst powitania i od razu zaczął
        # recytować cennik) — więc powitanie wymuszamy tym samym mechanizmem co say_now()
        # dla idle-promptu/pożegnania (response.create z jawnym instructions), zamiast
        # ufać systemowej instrukcji przy pustym kontekście.
        await say_now(llm, call_state, build_greeting_message(tenant, None))
        asyncio.create_task(monitor_call_health(task, llm, call_state))
        asyncio.create_task(apply_crm_when_ready(
            llm, tenant, client_profile_task, caller_phone=caller_phone, has_booking=booking_available,
            has_contact_owner=contact_owner_available,
        ))

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
            await save_call_transcript(tenant, call_sid, caller_phone, llm_context)
        except Exception as e:
            logger.error(f"[REALTIME TEST] Call transcript error: {e}")
        try:
            await maybe_send_call_summary(tenant, caller_phone, llm_context, call_state, call_sid=call_sid)
        except Exception as e:
            logger.error(f"[REALTIME TEST] Call summary error: {e}")


@router.get("/health-gemini-test")
async def health():
    return {"status": "ok", "provider": "openai-realtime"}


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
    contact_owner_available = tenant.get("contact_owner_enabled", 1) == 1
    tools = []
    if contact_owner_available:
        tools.append(build_contact_owner_tool(tenant, caller_phone, task_box, call_state))
    tools.append(build_end_conversation_tool(task_box, call_state))
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
    system_prompt = build_realtime_instructions(
        tenant, None, has_booking=booking_available, has_contact_owner=contact_owner_available
    )
    known_name = await get_crm_contact_name(tenant.get("id", ""), caller_phone)
    if known_name:
        system_prompt = append_known_caller_hint(system_prompt, known_name, has_contact_owner=contact_owner_available)
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
        # Patrz komentarz przy on_connect (ścieżka Twilio) — say_now() zamiast pustego
        # context frame, bo poleganie na systemowej instrukcji dawało niewiarygodne
        # pierwsze wypowiedzi (np. cennik zamiast dokładnego tekstu powitania).
        await say_now(llm, call_state, build_greeting_message(tenant, None))
        asyncio.create_task(monitor_call_health(task, llm, call_state))
        asyncio.create_task(apply_crm_when_ready(
            llm, tenant, client_profile_task, caller_phone=caller_phone, has_booking=booking_available,
            has_contact_owner=contact_owner_available,
        ))

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
            await save_call_transcript(tenant, call_sid, caller_phone, llm_context)
        except Exception as e:
            logger.error(f"[REALTIME TEST/VONAGE] Call transcript error: {e}")
        try:
            await maybe_send_call_summary(tenant, caller_phone, llm_context, call_state, call_sid=call_sid)
        except Exception as e:
            logger.error(f"[REALTIME TEST/VONAGE] Call summary error: {e}")
