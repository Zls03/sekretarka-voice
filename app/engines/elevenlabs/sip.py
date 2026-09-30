"""Import numeru do ElevenLabs jako SIP trunk — bezpośrednie połączenie Vonage -> ElevenLabs."""

from loguru import logger

from app.engines.elevenlabs.config import ELEVENLABS_API_KEY


async def ensure_elevenlabs_sip_number(phone_number: str, agent_id: str) -> bool:
    """Importuje numer do ElevenLabs jako SIP trunk number, podpięty do agenta —
    dopisane 2026-09-05, żeby Vonage+ElevenLabs mogło łączyć się BEZPOŚREDNIO z
    ElevenLabs (SIP), z pominięciem naszego mostu WebSocket (ws-elevenlabs-vonage
    niżej) i jego ~500ms narzutu na turę (potwierdzone na żywych połączeniach —
    patrz historia sesji). Woła się leniwie, przy KAŻDYM /vonage/answer-gemini-live
    dla tenanta na ElevenLabs+Vonage (patrz tam) — pierwsze wywołanie faktycznie
    importuje numer (jednorazowo), każde kolejne dostaje 409 resource_already_exists,
    co też traktujemy jako sukces. Brak osobnej kolumny w DB do cache'owania stanu
    importu ŚWIADOMIE — ten extra request do ElevenLabs kosztuje ~jednorazowo przy
    ZESTAWIANIU połączenia (nie per turę jak stary most), więc nie ma tu presji
    na optymalizację; cache można dopisać później jeśli się okaże że jednak zależy.

    KLUCZOWE: to działa TYLKO dlatego że webhook /elevenlabs/personalization (patrz
    niżej) jest wspierany dla połączeń przez SIP trunk — nie tylko Twilio (potwierdzone
    w dokumentacji ElevenLabs: "inbound telephony... Twilio voice, Exotel, SIP trunk,
    WhatsApp"). Numer→agent jest sztywne, ale TREŚĆ którą agent mówi nadal leci z
    naszego webhooka na żywo — nic nie trzeba ręcznie konfigurować per firma poza
    tym jednorazowym importem numeru, robionym automatycznie.

    Zwraca True gdy numer jest gotowy do bezpośredniego SIP (import się udał LUB
    numer był już zaimportowany), False przy jakimkolwiek błędzie — wołający MUSI
    wtedy spaść na stary most WebSocket, żeby klient nigdy nie został bez żadnej
    ścieżki połączenia (patrz użycie w bot_gemini_test.py::vonage_answer_gemini_live)."""
    if not ELEVENLABS_API_KEY or not phone_number or not agent_id:
        return False
    e164 = phone_number if phone_number.startswith("+") else f"+{phone_number}"
    import httpx

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.elevenlabs.io/v1/convai/phone-numbers",
                headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
                json={
                    "phone_number": e164,
                    "label": f"Vonage SIP direct — {e164}",
                    "provider": "sip_trunk",
                    "agent_id": agent_id,
                    # 2026-09-10 — PRAWDZIWA przyczyna sip_code=404/cannot_route (potwierdzona
                    # przez Vonage support, ticket #3122205, Aldo: INVITE dociera do ElevenLabs
                    # i dostaje 404 Not Found od ICH serwera SIP — nie problem formatu/NCCO).
                    # Import numeru bez inbound_trunk_config tworzy tylko "pusty" wpis
                    # (supports_inbound=false w ich API) — ElevenLabs nigdy nie dostawał
                    # instrukcji JAK ma przyjmować INVITE na ten numer. allowed_addresses to
                    # oficjalne "Primary Subnets" Vonage dla SIP (developer.vonage.com/
                    # api.support.vonage.com, artykuł "Which IP addresses... SIP Trunking").
                    "inbound_trunk_config": {
                        "allowed_addresses": ["216.147.0.0/18", "168.100.64.0/18"],
                    },
                },
                timeout=10.0,
            )
        if response.status_code in (200, 201):
            logger.info(f"✅ [ELEVENLABS SIP] Zaimportowano numer {e164} (SIP trunk direct)")
            return True
        if response.status_code == 409:
            return True
        logger.warning(
            f"⚠️ [ELEVENLABS SIP] Import {e164} nie powiódł się: {response.status_code} {response.text[:200]}"
        )
        return False
    except Exception as e:
        logger.error(f"❌ [ELEVENLABS SIP] Import {e164} — wyjątek: {e}")
        return False
