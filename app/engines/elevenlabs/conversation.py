"""Nadpisania konfiguracji rozmowy ElevenLabs (prompt, powitanie, narzędzia, głos) per firma.

Agent ElevenLabs jest skonfigurowany raz w ich panelu; treść rozmowy wstrzykujemy przy
każdym połączeniu przez `conversation_config_override`. Każde nadpisywane pole musi mieć
włączone pozwolenie na nadpisanie w ustawieniach agenta (Zabezpieczenia -> Nadpisania),
inaczej ElevenLabs po cichu je ignoruje.
"""

from app.engines.common import CallFeatures, build_call_prompt
from app.engines.elevenlabs.config import BOOK_APPOINTMENT_TOOL_ID, CONTACT_OWNER_TOOL_ID, MANAGE_BOOKING_TOOL_ID
from app.prompt.instructions import build_greeting_message

# Domyślne suwaki głosu — takie same jak ustawienia bazowe agenta, więc firma, która
# ich nie ruszała, słyszy dokładnie głos agenta.
DEFAULT_TTS_STABILITY = 0.5
DEFAULT_TTS_SPEED = 1.0
DEFAULT_TTS_SIMILARITY_BOOST = 0.8


def _setting(tenant: dict, key: str, default: float) -> float:
    value = tenant.get(key)
    return float(value if value is not None else default)


def build_tts_override(tenant: dict) -> dict:
    """Głos i suwaki (stabilność, prędkość, podobieństwo) z panelu firmy.

    Suwaki wysyłamy zawsze; voice_id tylko gdy firma wybrała własny głos (pusty =
    głos agenta).
    """
    tts_override: dict = {
        "stability": _setting(tenant, "elevenlabs_tts_stability", DEFAULT_TTS_STABILITY),
        "speed": _setting(tenant, "elevenlabs_tts_speed", DEFAULT_TTS_SPEED),
        "similarity_boost": _setting(tenant, "elevenlabs_tts_similarity_boost", DEFAULT_TTS_SIMILARITY_BOOST),
    }
    voice_id = (tenant.get("elevenlabs_voice_id") or "").strip()
    if voice_id:
        tts_override["voice_id"] = voice_id
    return tts_override


async def build_agent_override(tenant: dict, caller_phone: str) -> dict:
    """`conversation_config_override`: prompt, powitanie, dozwolone narzędzia i głos.

    Narzędzia są przypięte do agenta na stałe, więc listą tool_ids wyłączamy te, których
    firma nie ma włączonych — model strukturalnie nie może ich wtedy wywołać.
    """
    features = CallFeatures.for_tenant(tenant)
    tool_ids = []
    if features.contact_owner:
        tool_ids.append(CONTACT_OWNER_TOOL_ID)
    if features.booking:
        tool_ids += [BOOK_APPOINTMENT_TOOL_ID, MANAGE_BOOKING_TOOL_ID]

    return {
        "agent": {
            "prompt": {
                "prompt": await build_call_prompt(tenant, caller_phone, features, include_greeting=False),
                "tool_ids": tool_ids,
            },
            "first_message": build_greeting_message(tenant),
            "language": "pl",
        },
        "tts": build_tts_override(tenant),
    }


async def build_conversation_config_override(
    tenant: dict, caller_phone: str, called_number: str, call_sid: str = "", channel: str = "twilio",
) -> tuple[dict, dict]:
    """(conversation_config_override, dynamic_variables) dla połączeń inicjowanych przez nas.

    ElevenLabs odsyła dynamic_variables w każdym webhooku (narzędzia, post-call) — to
    jedyny sposób, żeby tam poznać numer firmy, uuid/SID połączenia i operatora
    (channel decyduje, którym operatorem wysłać SMS z potwierdzeniem wizyty).
    twilio_call_sid zostaje dla zgodności ze starszymi odczytami w post-call.
    """
    dynamic_variables = {
        "business_name": tenant.get("name") or "",
        "caller_phone": caller_phone,
        "called_number": called_number,
        "call_sid": call_sid,
        "twilio_call_sid": call_sid,
        "channel": channel,
    }
    return await build_agent_override(tenant, caller_phone), dynamic_variables
