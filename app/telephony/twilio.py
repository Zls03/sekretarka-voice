"""Webhooki Twilio: połączenie przychodzące (wybór silnika) i status zakończonej rozmowy."""

from fastapi import APIRouter, Request
from fastapi.responses import Response
from loguru import logger

from app.billing import is_call_allowed
from app.call_logs import record_call_status
from app.engines.elevenlabs.twilio import build_register_call_twiml
from app.telephony.responses import MSG_LINE_UNAVAILABLE, MSG_NUMBER_INACTIVE, twiml, twiml_media_stream, twiml_say
from app.tenants import get_tenant_by_phone

router = APIRouter()

# Statusy końcowe połączenia wg Twilio — tylko one są rozliczane.
FINAL_CALL_STATUSES = ("completed", "busy", "no-answer", "failed", "canceled")

# Websockety silników Pipecat, na które kierujemy strumień audio.
ENGINE_STREAM_PATHS = {"openai": "ws-gemini-test", "gemini": "ws-gemini-live-test"}


@router.post("/twilio/incoming-gemini-live-test")
async def twilio_incoming_call(request: Request):
    """Wspólne wejście wszystkich numerów Twilio — silnik wybiera pole firmy `realtime_engine`."""
    form = await request.form()
    called = form.get("Called", form.get("To", ""))
    caller = form.get("From", "")
    call_sid = form.get("CallSid", "")
    logger.info(f"📞 [GEMINI LIVE TEST] Incoming: {caller} → {called} (CallSid: {call_sid})")

    tenant = await get_tenant_by_phone(called)
    if not tenant:
        return twiml_say(MSG_NUMBER_INACTIVE)
    if not await is_call_allowed(tenant):
        return twiml_say(MSG_LINE_UNAVAILABLE, hangup=True)

    engine = tenant.get("realtime_engine")
    if engine == "elevenlabs":
        # ElevenLabs zwraca własny TwiML ("bring your own Twilio"). Przy błędzie klient
        # słyszy komunikat zamiast ciszy.
        try:
            return twiml(await build_register_call_twiml(tenant, caller, called, call_sid))
        except Exception as e:
            logger.error(f"❌ [GEMINI LIVE TEST] register_call ElevenLabs nieudany: {e}")
            return twiml_say("Przepraszamy, wystąpił błąd. Spróbuj ponownie później.", hangup=True)

    ws_path = ENGINE_STREAM_PATHS["openai" if engine == "openai" else "gemini"]
    return twiml_media_stream(
        request.headers.get("host", "localhost"),
        ws_path,
        call_sid=call_sid,
        tenant_phone=tenant["phone_number"],
        caller_phone=caller,
    )


@router.post("/twilio/status")
async def twilio_call_status(request: Request):
    """Status callback Twilio — zapis czasu trwania i rozliczenie rozmowy (wszystkie silniki).

    Numer musi mieć w konsoli Twilio ustawione "Call status changes" na ten adres,
    inaczej rozmowy Twilio nie są rozliczane.
    """
    form = await request.form()
    call_sid = form.get("CallSid", "")
    call_status = form.get("CallStatus", "")
    call_duration = form.get("CallDuration", "0")
    to_number = form.get("To", "")
    from_number = form.get("From", "") or "nieznany"
    logger.info(f"[TWILIO STATUS] {call_sid} | {call_status} | {call_duration}s")

    if call_status not in FINAL_CALL_STATUSES or not call_sid:
        return Response(content="OK", media_type="text/plain")

    try:
        duration = int(call_duration) if call_duration else 0
        tenant = await get_tenant_by_phone(to_number) if to_number else None
        if not tenant:
            logger.warning(f"⚠️ [TWILIO STATUS] Nie znaleziono tenanta dla {to_number}")
            return Response(content="OK", media_type="text/plain")
        await record_call_status(tenant, call_sid, from_number, duration, call_status, "TWILIO STATUS")
    except Exception as e:
        logger.error(f"[TWILIO STATUS] twilio_status error: {e}")

    return Response(content="OK", media_type="text/plain")
