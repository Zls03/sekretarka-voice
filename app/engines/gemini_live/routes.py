"""Websockety rozmów Gemini Live (Twilio Media Streams i Vonage).

Ścieżki URL zawierają historyczny sufiks "-test" — są wpisane w konfiguracji numerów
u operatorów, więc zostają bez zmian.
"""

from fastapi import APIRouter, WebSocket
from loguru import logger

from app.billing import is_call_allowed
from app.engines.common import CallFeatures, accept_vonage_stream, create_transport, read_twilio_stream_start
from app.engines.gemini_live.llm import GEMINI_LIVE_MODEL
from app.engines.gemini_live.session import run_gemini_live_call

router = APIRouter()

TWILIO_LOG_TAG = "GEMINI LIVE TEST"
VONAGE_LOG_TAG = "GEMINI LIVE TEST/VONAGE"


@router.websocket("/ws-gemini-live-test")
async def gemini_live_twilio_stream(websocket: WebSocket):
    await websocket.accept()
    logger.info(f"🔌 [{TWILIO_LOG_TAG}] WebSocket connected")

    start = await read_twilio_stream_start(websocket, TWILIO_LOG_TAG)
    if start is None:
        return
    if not await is_call_allowed(start.tenant):
        logger.warning(f"🚫 [{TWILIO_LOG_TAG}] Tenant {start.tenant.get('id')} zablokowany — zamykam")
        await websocket.close()
        return

    await run_gemini_live_call(
        create_transport(websocket, "twilio", stream_sid=start.stream_sid),
        start.tenant, start.caller_phone, start.call_sid,
        channel="twilio",
        features=CallFeatures.for_tenant(start.tenant),
        log_tag=TWILIO_LOG_TAG,
    )


@router.websocket("/ws-gemini-live-test-vonage")
async def gemini_live_vonage_stream(websocket: WebSocket):
    start = await accept_vonage_stream(websocket, VONAGE_LOG_TAG)
    if start is None:
        return

    await run_gemini_live_call(
        create_transport(websocket, "vonage"),
        start.tenant, start.caller_phone, start.call_sid,
        channel="vonage",
        features=CallFeatures.for_tenant(start.tenant, transfer_supported=True),
        log_tag=VONAGE_LOG_TAG,
        region_url=start.region_url,
        host=websocket.headers.get("host", "localhost"),
    )


@router.get("/health-gemini-live-test")
async def gemini_live_health():
    return {"status": "ok", "provider": "gemini-live", "model": GEMINI_LIVE_MODEL}
