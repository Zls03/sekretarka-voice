"""Webhooki Vonage: answer (wybór silnika), zdarzenia i rozliczenie, fallbacki transferu i SIP.

Vonage nie ma webhooka na numerze — numer jest przypięty do "Application" (Voice),
która ma Answer URL (zwracamy NCCO) i Event URL (statusy połączenia).
"""

from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from loguru import logger

from app.background import spawn
from app.billing import is_call_allowed
from app.call_logs import record_call_status
from app.notifications.email import send_missed_transfer_email
from app.telephony.human_first import build_human_first_ncco, process_human_first_recording
from app.telephony.ncco import build_ai_ncco
from app.telephony.responses import (
    MSG_CONNECTION_ERROR,
    MSG_LINE_UNAVAILABLE,
    MSG_NUMBER_INACTIVE,
    ncco_connect_websocket,
    ncco_response,
    ncco_talk,
)
from app.tenants import get_tenant_by_phone

router = APIRouter()

# Numer demo BizVoice — jedyny obsługiwany przez testową trasę /vonage/test-siperb.
SIPERB_TEST_NUMBER = "48459050542"

# Statusy nogi SIP obserwowane przy poprawnych rozmowach; inne warto sprawdzić w logach.
BENIGN_SIP_STATUSES = {"started", "ringing", "answered", "completed"}
VONAGE_RECORDING_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


async def _read_event_data(request: Request) -> dict:
    """Zdarzenie Vonage: JSON w POST albo parametry zapytania w GET."""
    try:
        if request.method == "POST":
            return await request.json()
        return dict(request.query_params)
    except Exception:
        return dict(request.query_params)


async def _read_raw_body(request: Request) -> dict | str:
    """Treść callbacku do logowania: JSON, a gdy się nie da — surowy tekst."""
    try:
        return await request.json()
    except Exception:
        return (await request.body()).decode("utf-8", errors="replace")


def _answer_params(request: Request) -> tuple[str, str, str, str, str]:
    """(to, from, uuid, region_url, host) z webhooka Answer.

    region_url wskazuje regionalne centrum danych Vonage obsługujące TO połączenie —
    przychodzi tylko w Answer, a bez niego transfer w innym regionie kończy się 400/404.
    """
    q = request.query_params
    return (
        q.get("to", ""),
        q.get("from", ""),
        q.get("uuid", ""),
        q.get("region_url", ""),
        request.headers.get("host", "localhost"),
    )


@router.api_route("/vonage/events", methods=["GET", "POST"])
async def vonage_events(request: Request):
    """Event URL — zapis czasu trwania i rozliczenie rozmowy (wszystkie silniki).

    Vonage wysyła "completed" osobno dla każdej nogi połączenia (ten sam numer "to",
    różne uuid), więc rozliczamy wyłącznie nogę inbound — inaczej rozmowa liczyłaby się podwójnie.
    """
    data = await _read_event_data(request)
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
        await record_call_status(tenant, call_uuid, from_number, duration, status, "REALTIME TEST/VONAGE")
    except Exception as e:
        logger.error(f"[REALTIME TEST/VONAGE] vonage_events error: {e}")

    return Response(content="", status_code=200)


@router.get("/vonage/answer-gemini-live")
async def vonage_answer(request: Request):
    """Answer URL wszystkich numerów Vonage — silnik wybiera pole firmy `realtime_engine`."""
    to_number, from_number, call_uuid, region_url, host = _answer_params(request)
    logger.info(f"📞 [GEMINI LIVE TEST/VONAGE] Answer: {from_number} → {to_number} (region={region_url or 'brak'})")

    tenant = await get_tenant_by_phone(to_number)
    if not tenant:
        return ncco_response(ncco_talk(MSG_NUMBER_INACTIVE))
    if not await is_call_allowed(tenant):
        return ncco_response(ncco_talk(MSG_LINE_UNAVAILABLE))

    # "Najpierw dzwoni do właściciela" (apka Siperb). Bez skonfigurowanego konta SIP
    # rozmowa od razu trafia do asystenta AI — klient nigdy nie zostaje bez połączenia.
    if tenant.get("human_first_enabled"):
        human_first_ncco = await build_human_first_ncco(tenant, from_number, to_number, call_uuid, host, region_url)
        if human_first_ncco:
            logger.info(f"📱 [HUMAN-FIRST/SIPERB] Dzwonię najpierw do apki Siperb właściciela: {tenant.get('id')}")
            return ncco_response(human_first_ncco)
        logger.warning(f"📱 [HUMAN-FIRST/SIPERB] Brak siperb_sip_username — od razu sekretarka AI: {tenant.get('id')}")

    return ncco_response(await build_ai_ncco(tenant, from_number, to_number, call_uuid, host, region_url))


@router.api_route("/vonage/transfer-fallback", methods=["GET", "POST"])
async def vonage_transfer_fallback(request: Request):
    """Właściciel nie odebrał transferu (tools/transfer.py) — Vonage czeka na nową NCCO.

    Bez odpowiedzi klient zostałby w ciszy. Dane do e-maila przekazujemy w query stringu
    przy budowie transferu, bo ten webhook nie ma dostępu do stanu rozmowy.
    """
    logger.info(f"📞 [TRANSFER FALLBACK] {await _read_event_data(request)}")

    business_name = request.query_params.get("businessName", "Firma")
    caller_phone = request.query_params.get("callerPhone", "")
    owner_email = request.query_params.get("ownerEmail", "")
    if owner_email:
        spawn(send_missed_transfer_email(business_name, caller_phone, owner_email))

    return ncco_response(ncco_talk("Niestety nie udało się połączyć. Przekażę wiadomość, żeby ktoś oddzwonił."))


@router.api_route("/vonage/human-first-fallback", methods=["GET", "POST"])
async def vonage_human_first_fallback(request: Request):
    """Callback połączenia do apki Siperb — zawsze zwraca NCCO asystenta AI.

    Vonage odpytuje ten adres także przy udanym połączeniu, ale wtedy ignoruje odpowiedź
    (noga już trwa), więc bezwarunkowy fallback jest bezpieczny.
    """
    q = request.query_params
    to_number, from_number, call_uuid, region_url = (
        q.get("to", ""),
        q.get("from", ""),
        q.get("uuid", ""),
        q.get("regionUrl", ""),
    )
    body = await _read_raw_body(request)
    status = body.get("status") if isinstance(body, dict) else None
    logger.info(f"📱 [HUMAN-FIRST/SIPERB] eventUrl odpytany, status={status!r} | body={body}")

    tenant = await get_tenant_by_phone(to_number)
    if not tenant:
        return ncco_response(ncco_talk(MSG_CONNECTION_ERROR))

    host = request.headers.get("host", "localhost")
    return ncco_response(await build_ai_ncco(tenant, from_number, to_number, call_uuid, host, region_url))


@router.api_route("/vonage/human-first-recording", methods=["GET", "POST"])
async def vonage_human_first_recording(request: Request):
    """Nagranie rozmowy odebranej przez właściciela — przetwarzane w tle (transkrypcja, raport)."""
    q = request.query_params
    to_number, from_number, call_uuid = q.get("to", ""), q.get("from", ""), q.get("uuid", "")
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
        start, end = body.get("start_time"), body.get("end_time")
        if start and end:
            duration_seconds = int(
                (
                    datetime.strptime(end, VONAGE_RECORDING_TIME_FORMAT)
                    - datetime.strptime(start, VONAGE_RECORDING_TIME_FORMAT)
                ).total_seconds()
            )
    except Exception:
        pass

    spawn(process_human_first_recording(tenant, recording_url, from_number, call_uuid, duration_seconds))
    return JSONResponse({"status": "ok"})


@router.api_route("/vonage/sip-fallback-elevenlabs", methods=["GET", "POST"])
async def vonage_sip_fallback_elevenlabs(request: Request):
    """Callback połączenia SIP direct z ElevenLabs — zwraca NCCO z mostem websocket.

    Vonage odpytuje ten adres również przy udanych połączeniach (start i koniec) i wtedy
    ignoruje odpowiedź; przy prawdziwej awarii SIP most websocket ratuje rozmowę.
    Gotowy adres mostu (wsUri) przekazujemy w query stringu przy budowie NCCO.
    """
    ws_uri = request.query_params.get("wsUri", "")
    body = await _read_raw_body(request)
    status = body.get("status") if isinstance(body, dict) else None
    benign = status in BENIGN_SIP_STATUSES
    (logger.info if benign else logger.warning)(
        f"{'ℹ️' if benign else '⚠️'} [ELEVENLABS/VONAGE SIP] "
        f"eventUrl odpytany, status={status!r}{'' if benign else ' — nietypowy status, warto sprawdzić'} "
        f"| query={dict(request.query_params)} | body={body}"
    )
    if not ws_uri:
        return ncco_response(ncco_talk(MSG_CONNECTION_ERROR))
    return ncco_response(ncco_connect_websocket(ws_uri))


@router.api_route("/vonage/test-siperb", methods=["GET", "POST"])
async def vonage_test_siperb(request: Request):
    """TYMCZASOWA trasa testowa Vonage SIP -> Siperb (2026-09-27), do usunięcia.

    Bywała ustawiana jako Answer URL całej aplikacji Vonage obsługującej też numer
    realnej firmy — dlatego tylko numer demo BizVoice idzie na test, a każdy inny numer
    dostaje zwykłą ścieżkę AI. Przed usunięciem sprawdzić, że nie jest nigdzie podpięta.
    """
    to_number, from_number, call_uuid, region_url, host = _answer_params(request)
    if to_number.lstrip("+").lstrip("0") != SIPERB_TEST_NUMBER:
        tenant = await get_tenant_by_phone(to_number)
        if not tenant:
            return ncco_response(ncco_talk(MSG_NUMBER_INACTIVE))
        if not await is_call_allowed(tenant):
            return ncco_response(ncco_talk(MSG_LINE_UNAVAILABLE))
        return ncco_response(await build_ai_ncco(tenant, from_number, to_number, call_uuid, host, region_url))

    logger.info(f"🧪 [SIPERB TEST] Answer webhook wywołany: {dict(request.query_params)}")
    return ncco_response(
        [
            {
                "action": "connect",
                "timeout": 20,
                "eventType": "synchronous",
                "eventUrl": [f"https://{host}/vonage/test-siperb-event"],
                "endpoint": [{"type": "sip", "uri": "sip:siperb-bizvoice@eu-west-1-sbc-1.siperb.com;transport=udp"}],
            }
        ]
    )


@router.api_route("/vonage/test-siperb-event", methods=["GET", "POST"])
async def vonage_test_siperb_event(request: Request):
    body = await _read_raw_body(request)
    logger.info(f"🧪 [SIPERB TEST] eventUrl: query={dict(request.query_params)} | body={body}")
    return JSONResponse([])
