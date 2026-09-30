"""NCCO dla Vonage: wybór silnika głosowego (realtime_engine) i sposobu połączenia."""

from urllib.parse import quote

from loguru import logger

from app.engines.elevenlabs.config import ELEVENLABS_SIP_DOMAIN
from app.engines.elevenlabs.config import _resolve_agent_id as resolve_elevenlabs_agent_id
from app.engines.elevenlabs.sip import ensure_elevenlabs_sip_number
from app.telephony.responses import ncco_connect_websocket

# Bezpośrednie połączenie Vonage -> ElevenLabs przez SIP (bez naszego mostu audio,
# ~500 ms mniej opóźnienia na turę). Historia uruchamiania: docs/HISTORIA.md.
ELEVENLABS_SIP_DIRECT_ENABLED = True


def _stream_query(tenant: dict, from_number: str, call_uuid: str) -> str:
    return f"phone={tenant['phone_number']}&callerPhone={from_number}&callSid={call_uuid}"


async def build_ai_ncco(
    tenant: dict, from_number: str, to_number: str, call_uuid: str, host: str, region_url: str,
) -> list:
    """NCCO łączące rozmowę z asystentem AI silnika wybranego w panelu firmy."""
    engine = tenant.get("realtime_engine")
    query = _stream_query(tenant, from_number, call_uuid)

    if engine == "openai":
        return ncco_connect_websocket(f"wss://{host}/ws-gemini-test-vonage?{query}")

    if engine == "elevenlabs":
        bridge_uri = f"wss://{host}/ws-elevenlabs-vonage?{query}"
        if ELEVENLABS_SIP_DIRECT_ENABLED:
            agent_id = resolve_elevenlabs_agent_id(tenant)
            if await ensure_elevenlabs_sip_number(tenant["phone_number"], agent_id):
                return _elevenlabs_sip_ncco(to_number, from_number, call_uuid, host, bridge_uri)
            logger.warning("⚠️ [ELEVENLABS/VONAGE SIP] Import numeru nie powiódł się — fallback na most WebSocket")
        return ncco_connect_websocket(bridge_uri)

    # Gemini Live: region_url potrzebny do transferu rozmowy (patrz telephony/vonage.py).
    return ncco_connect_websocket(
        f"wss://{host}/ws-gemini-live-test-vonage?{query}&regionUrl={quote(region_url, safe='')}"
    )


def _elevenlabs_sip_ncco(to_number: str, from_number: str, call_uuid: str, host: str, bridge_uri: str) -> list:
    """connect -> SIP do ElevenLabs z fallbackiem na most websocket (eventUrl).

    - "from" to numer DZWONIĄCEGO — Vonage wysyła go jako Caller-ID w SIP INVITE,
      z niego ElevenLabs wie, kto dzwoni (raporty, CRM).
    - Nagłówek X-CALL-ID (Vonage sam dokleja "X-") nadpisuje wewnętrzne call_sid ElevenLabs
      naszym uuid Vonage, dzięki czemu transkrypt z post-call trafia do właściwego wpisu
      w call_logs.
    - transport=tcp — ElevenLabs nie przyjmuje SIP po domyślnym UDP.
    """
    sip_number = to_number if to_number.startswith("+") else f"+{to_number}"
    event_url = f"https://{host}/vonage/sip-fallback-elevenlabs?wsUri={quote(bridge_uri, safe='')}"
    logger.info(f"📞 [ELEVENLABS/VONAGE SIP] Bezpośrednie połączenie (uri, z fallbackiem): {sip_number}")
    return [{
        "action": "connect",
        "from": from_number.lstrip("+") if from_number else sip_number.lstrip("+"),
        "eventType": "synchronous",
        "eventUrl": [event_url],
        "endpoint": [{
            "type": "sip",
            "uri": f"sip:{sip_number}@{ELEVENLABS_SIP_DOMAIN};transport=tcp",
            "headers": {"CALL-ID": call_uuid},
        }],
    }]
