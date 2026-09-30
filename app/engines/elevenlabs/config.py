"""Konfiguracja ElevenLabs Conversational AI: klucze, agent, identyfikatory narzędzi."""

from app.config import settings

ELEVENLABS_API_KEY = settings.elevenlabs_api_key
ELEVENLABS_SHARED_SECRET = settings.elevenlabs_shared_secret
ELEVENLABS_SIP_DOMAIN = "sip.rtc.elevenlabs.io:5060"

# Identyfikatory narzędzi webhook skonfigurowanych na agencie w panelu ElevenLabs.
# Per rozmowa wysyłamy listę dozwolonych tool_ids (patrz conversation.py) — wymaga to
# włączonego nadpisywania "Tools" na agencie, inaczej ElevenLabs po cichu je ignoruje.
# Przy przenosinach na inne konto ElevenLabs te ID trzeba podmienić razem z kluczem API.
CONTACT_OWNER_TOOL_ID = "tool_7401m1epk46deb1tfab5se2bmgy6"
BOOK_APPOINTMENT_TOOL_ID = "tool_4601m1z6ej9verx8qg719pfghxkx"
MANAGE_BOOKING_TOOL_ID = "tool_0801m1z6x57se7396k77bqrhxfs7"


def resolve_agent_id(tenant: dict) -> str:
    """Agent firmy (panel: zakładka ElevenLabs), a gdy go nie ustawiła — agent domyślny."""
    return (tenant.get("elevenlabs_agent_id") or "").strip() or settings.elevenlabs_agent_id
