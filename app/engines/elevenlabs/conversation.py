"""Nadpisania konfiguracji rozmowy ElevenLabs (prompt, powitanie, narzędzia, głos) per firma."""

from app.crm_contacts import get_crm_contact_name
from app.engines.elevenlabs.config import BOOK_APPOINTMENT_TOOL_ID, CONTACT_OWNER_TOOL_ID, MANAGE_BOOKING_TOOL_ID
from app.prompt.instructions import append_known_caller_hint, build_greeting_message, build_realtime_instructions


def _build_tts_override(tenant: dict) -> dict:
    """2026-09-10 — głos + 3 suwaki (stabilność/prędkość/podobieństwo) per-tenant, panel
    "🔷 ElevenLabs". Wspólne dla WSZYSTKICH torów (most WebSocket/register_call przez
    _build_conversation_config_override NIŻEJ, i webhook personalizacji SIP direct wyżej)
    — wcześniej każdy tor miał to zaimplementowane osobno (albo wcale, patrz historia
    elevenlabs_personalization), co dawało rozjazd między silnikami dla tej samej firmy.

    Stabilność/prędkość/podobieństwo MAJĄ sensowne domyślne wartości (te same co domyślne
    ustawienia agenta: 0.5/1.0/0.8, patrz MIGRACJA_ELEVENLABS_NOTATKI.txt) — w odróżnieniu
    od voice_id (pusty = dziedzicz głos agenta), te trzy zawsze się wysyła, bo suwak w
    panelu zawsze ma jakąś wartość, nie ma stanu "nieustawiony". Dla firmy która nigdy nie
    ruszyła suwaków efekt jest identyczny jak bez nadpisania (te same liczby co ma agent).

    WYMAGA włączonego pozwolenia na nadpisywanie tts.stability/speed/similarity_boost na
    agencie (Zabezpieczenia -> Nadpisania) — bez tego ElevenLabs po cichu je ignoruje,
    dokładnie tak jak z tool_ids/prompt wcześniej. Włączone ręcznie przez PATCH
    /v1/convai/agents/{id} 2026-09-10, patrz historia sesji."""
    tts_override: dict = {
        "stability": float(tenant.get("elevenlabs_tts_stability") if tenant.get("elevenlabs_tts_stability") is not None else 0.5),
        "speed": float(tenant.get("elevenlabs_tts_speed") if tenant.get("elevenlabs_tts_speed") is not None else 1.0),
        "similarity_boost": float(tenant.get("elevenlabs_tts_similarity_boost") if tenant.get("elevenlabs_tts_similarity_boost") is not None else 0.8),
    }
    voice_id = (tenant.get("elevenlabs_voice_id") or "").strip()
    if voice_id:
        tts_override["voice_id"] = voice_id
    return tts_override


async def _build_conversation_config_override(
    tenant: dict, caller_phone: str, called_number: str, call_sid: str = "", channel: str = "twilio",
) -> tuple[dict, dict]:
    """Wspólne dla obu transportów (Twilio register_call i Vonage WebSocket, patrz
    run_elevenlabs_vonage_bot) — buduje (conversation_config_override, dynamic_variables)
    z tych samych danych panelu co Gemini Live/OpenAI Realtime (build_realtime_instructions/
    build_greeting_message), plus opcjonalny nadpisany głos per-tenant.

    channel: "vonage" lub "twilio" — echowane w dynamic_variables, bo narzędzia rezerwacji
    (elevenlabs_tool_book_appointment niżej) muszą wiedzieć którym dostawcą SMS potwierdzić
    wizytę (send_booking_sms_vonage vs send_booking_sms), a same webhooki ElevenLabs nie
    mają pojęcia którym torem leciało połączenie."""
    contact_owner_available = tenant.get("contact_owner_enabled", 1) == 1
    # Ta sama bramka co booking_available w bot_gemini_test.py (Gemini Live/OpenAI
    # Realtime) — MUSI dawać identyczny wynik dla tego samego tenanta, inaczej
    # zachowanie rezerwacji rozjeżdżałoby się między silnikami.
    booking_available = tenant.get("booking_enabled") == 1 and any(
        s.get("google_connected") and len(s.get("services", [])) > 0
        for s in tenant.get("staff", [])
    )
    prompt_text = build_realtime_instructions(
        tenant, None, include_greeting=False,
        has_contact_owner=contact_owner_available, has_booking=booking_available,
    )
    known_name = await get_crm_contact_name(tenant.get("id", ""), caller_phone)
    if known_name:
        prompt_text = append_known_caller_hint(prompt_text, known_name, has_contact_owner=contact_owner_available)
    first_message = build_greeting_message(tenant)

    tool_ids = []
    if contact_owner_available:
        tool_ids.append(CONTACT_OWNER_TOOL_ID)
    if booking_available:
        tool_ids += [BOOK_APPOINTMENT_TOOL_ID, MANAGE_BOOKING_TOOL_ID]

    conversation_config_override = {
        "agent": {
            "prompt": {
                "prompt": prompt_text,
                "tool_ids": tool_ids,
            },
            "first_message": first_message,
            "language": "pl",
        }
    }
    # Głos + stabilność/prędkość/podobieństwo per-tenant — patrz _build_tts_override wyżej.
    # Do 2026-09-03 kod czytał inną nazwę pola (elevenlabs_agent_voice_id), której panel
    # NIGDY nie zapisywał — nadpisanie głosu z panelu było martwe od początku, mimo że sam
    # mechanizm (conversation_config_override.tts.voice_id) działał poprawnie na żywym
    # telefonie (potwierdzone 2026-09-02 z ręcznie wstawionym do bazy voice_id "Aleksandra").
    conversation_config_override["tts"] = _build_tts_override(tenant)

    dynamic_variables = {
        "business_name": tenant.get("name") or "",
        "caller_phone": caller_phone,
        "called_number": called_number,
        # call_sid ogólne (Vonage UUID lub Twilio SID) + twilio_call_sid zostaje dla
        # wstecznej zgodności z payloadem który /elevenlabs/post-call już umie czytać —
        # patrz jego aktualizacja niżej, czyta oba klucze.
        "call_sid": call_sid,
        "twilio_call_sid": call_sid,
        "channel": channel,
    }
    return conversation_config_override, dynamic_variables
