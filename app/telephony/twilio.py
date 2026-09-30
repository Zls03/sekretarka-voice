"""Webhooki Twilio: połączenie przychodzące (dispatch po silniku) i status zakończonej rozmowy."""

import time

from fastapi import APIRouter, Request
from fastapi.responses import Response
from loguru import logger

from app.billing import apply_call_charge, is_call_allowed
from app.db import db, saas_db
from app.engines.elevenlabs.twilio import build_register_call_twiml
from app.tenants import get_tenant_by_phone

router = APIRouter()


@router.post("/twilio/status")
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


@router.post("/twilio/incoming-gemini-live-test")
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
