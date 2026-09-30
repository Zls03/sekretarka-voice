""""Bring your own Twilio": rejestracja połączenia w ElevenLabs i TwiML dla Twilio."""

import asyncio

from loguru import logger

from app.engines.elevenlabs.config import ELEVENLABS_API_KEY, resolve_agent_id
from app.engines.elevenlabs.conversation import build_conversation_config_override

_elevenlabs_client = None


def _get_elevenlabs_client():
    """Leniwa inicjalizacja — import/klient tworzony tylko gdy realtime_engine
    faktycznie wybiera ElevenLabs, żeby brak ELEVENLABS_API_KEY nie wywalał
    reszty serwisu (Gemini Live/OpenAI Realtime) przy starcie."""
    global _elevenlabs_client
    if _elevenlabs_client is None:
        from elevenlabs.client import ElevenLabs
        _elevenlabs_client = ElevenLabs(api_key=ELEVENLABS_API_KEY)
    return _elevenlabs_client


async def build_register_call_twiml(tenant: dict, caller_phone: str, called_number: str, call_sid: str = "") -> str:
    """"Bring your own Twilio" — patrz punkt 4 w docstringu modułu. Zwraca TwiML
    gotowe do zwrócenia bezpośrednio Twilio (media_type="application/xml").

    Rzuca wyjątek przy braku ELEVENLABS_API_KEY/agent_id lub błędzie API — wołający
    (bot_gemini_test.py) łapie to i zwraca bezpieczny TwiML fallback, żeby błąd
    konfiguracji ElevenLabs nie zostawiał klienta w ciszy bez żadnego komunikatu."""
    agent_id = resolve_agent_id(tenant)
    if not ELEVENLABS_API_KEY or not agent_id:
        raise RuntimeError("ELEVENLABS_API_KEY lub elevenlabs_agent_id (tenant/env) nieskonfigurowane")

    conversation_config_override, dynamic_variables = await build_conversation_config_override(
        tenant, caller_phone, called_number, call_sid, channel="twilio",
    )

    client = _get_elevenlabs_client()
    twiml = await asyncio.to_thread(
        client.conversational_ai.twilio.register_call,
        agent_id=agent_id,
        from_number=caller_phone,
        to_number=called_number,
        direction="inbound",
        conversation_initiation_client_data={
            "conversation_config_override": conversation_config_override,
            # business_name/caller_phone/called_number/twilio_call_sid celowo w
            # dynamic_variables: ElevenLabs echouje ten obiekt z powrotem w post-call
            # webhooku (data.conversation_initiation_client_data.dynamic_variables) —
            # to jedyny sposób by /elevenlabs/post-call poznał NASZE Twilio CallSid i
            # numer firmy, bo register_call() nie przyjmuje ich jako osobnych pól, a
            # payload post-call NIE zawiera "phone_call" (potwierdzone na żywo
            # 2026-09-02 — inaczej niż wcześniej zakładano, patrz elevenlabs_post_call).
            "dynamic_variables": dynamic_variables,
        },
    )
    logger.info(f"📞 [ELEVENLABS AGENT] register_call OK dla {tenant.get('name')} ({caller_phone} → {called_number})")
    return twiml
