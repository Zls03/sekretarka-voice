"""Wejścia ścieżki OpenAI Realtime: starsze webhooki operatorów i websockety rozmów.

Główne webhooki (telephony/twilio.py, telephony/vonage.py) kierują tu rozmowy firm z
realtime_engine == "openai". Trasy /twilio/incoming-gemini-test i /vonage/answer to
starsze, bezpośrednie wejścia tej ścieżki — zostają, bo mogą być wpisane w konfiguracji
numerów. Nazwy ścieżek są historyczne ("gemini-test" mimo OpenAI).
"""

from fastapi import APIRouter, Request, WebSocket
from loguru import logger

from app.billing import is_call_allowed
from app.config import settings
from app.db import db
from app.engines.common import accept_vonage_stream, read_twilio_stream_start
from app.engines.openai_realtime.session import run_openai_realtime_call
from app.telephony.responses import (
    MSG_LINE_UNAVAILABLE,
    MSG_NUMBER_INACTIVE,
    ncco_connect_websocket,
    ncco_response,
    ncco_talk,
    twiml_media_stream,
    twiml_say,
)
from app.tenants import get_tenant_by_phone

router = APIRouter()
TWILIO_LOG_TAG = "REALTIME TEST"
VONAGE_LOG_TAG = "REALTIME TEST/VONAGE"


@router.post("/twilio/incoming-gemini-test")
async def openai_realtime_twilio_incoming(request: Request):
    form = await request.form()
    called = form.get("Called", form.get("To", ""))
    caller = form.get("From", "")
    call_sid = form.get("CallSid", "")
    logger.info(f"📞 [{TWILIO_LOG_TAG}] Incoming: {caller} → {called} (CallSid: {call_sid})")

    tenant = await get_tenant_by_phone(called)
    if not tenant:
        return twiml_say(MSG_NUMBER_INACTIVE)
    if not await is_call_allowed(tenant):
        return twiml_say(MSG_LINE_UNAVAILABLE, hangup=True)

    return twiml_media_stream(
        request.headers.get("host", "localhost"),
        "ws-gemini-test",
        call_sid=call_sid,
        tenant_phone=tenant["phone_number"],
        caller_phone=caller,
    )


@router.websocket("/ws-gemini-test")
async def openai_realtime_twilio_stream(websocket: WebSocket):
    await websocket.accept()
    logger.info(f"🔌 [{TWILIO_LOG_TAG}] WebSocket connected")

    start = await read_twilio_stream_start(websocket, TWILIO_LOG_TAG)
    if start is None:
        return
    await run_openai_realtime_call(
        websocket,
        start.tenant,
        start.caller_phone,
        start.call_sid,
        channel="twilio",
        log_tag=TWILIO_LOG_TAG,
        stream_sid=start.stream_sid,
    )


@router.get("/health-gemini-test")
async def openai_realtime_health():
    return {"status": "ok", "provider": "openai-realtime"}


@router.get("/vonage/answer")
async def openai_realtime_vonage_answer(request: Request):
    to_number = request.query_params.get("to", "")
    from_number = request.query_params.get("from", "")
    call_uuid = request.query_params.get("uuid", "")
    logger.info(f"📞 [{VONAGE_LOG_TAG}] Answer: {from_number} → {to_number}")

    # TEST_TENANT_ID: numer testowy spoza bazy firm obsługiwany jako wskazana firma.
    forced_tenant_id = settings.test_tenant_id
    if forced_tenant_id:
        rows = await db.execute("SELECT phone_number FROM tenants WHERE id = ?", [forced_tenant_id])
        tenant = await get_tenant_by_phone(rows[0]["phone_number"]) if rows else None
        logger.info(
            f"📞 [{VONAGE_LOG_TAG}] Using forced tenant_id={forced_tenant_id} -> "
            f"{tenant.get('name') if tenant else 'NOT FOUND'}"
        )
    else:
        tenant = await get_tenant_by_phone(to_number)

    if not tenant:
        return ncco_response(ncco_talk(MSG_NUMBER_INACTIVE))
    if not await is_call_allowed(tenant):
        return ncco_response(ncco_talk(MSG_LINE_UNAVAILABLE))

    host = request.headers.get("host", "localhost")
    ws_uri = (
        f"wss://{host}/ws-gemini-test-vonage?phone={tenant['phone_number']}"
        f"&callerPhone={from_number}&callSid={call_uuid}"
    )
    return ncco_response(ncco_connect_websocket(ws_uri))


@router.websocket("/ws-gemini-test-vonage")
async def openai_realtime_vonage_stream(websocket: WebSocket):
    start = await accept_vonage_stream(websocket, VONAGE_LOG_TAG)
    if start is None:
        return
    await run_openai_realtime_call(
        websocket,
        start.tenant,
        start.caller_phone,
        start.call_sid,
        channel="vonage",
        log_tag=VONAGE_LOG_TAG,
    )
