"""Klient Vonage Voice REST API: JWT aplikacji, transfer rozmowy, pobieranie nagrań."""

import time
import uuid

from loguru import logger

from app.config import settings

#
# Twilio ma OSOBNY, już istniejący mechanizm (transfer_requests table + TwiML <Dial>
# w /twilio/after-stream, patrz flows_contact.py) — ten kod go NIE zastępuje ani nie
# dotyka, jest wyłącznie dla Vonage, który (w odróżnieniu od Twilio) nie ma
# dwuetapowego triku dostępnego w tym serwisie (brak /twilio/after-stream w
# bot_gemini_test.py — patrz CLAUDE.md, ta granica była tam już wcześniej opisana).
#
# Mechanizm: Vonage Voice REST API, PUT /v1/calls/{call_uuid} z action="transfer" +
# nowe NCCO — to PODMIENIA bieżący "leg" połączenia (który dotąd był podłączony do
# NASZEGO websocketu) na nowy (talk + connect do telefonu właściciela). Efekt:
# Vonage sam zamyka nasz websocket gdy nowe NCCO przejmuje kontrolę — NIE wysyłamy
# własnego EndFrame po udanym transferze (w odróżnieniu od contact_owner/end_conversation),
# żeby nie ścigać się z tym zamknięciem od strony Vonage. call_state["ended"]=True
# nadal ustawiane, żeby monitor_gemini_call_health przestał liczyć ciszę.
#
# Autoryzacja: JWT RS256 podpisany kluczem prywatnym Aplikacji Vonage (VONAGE_APPLICATION_ID
# + VONAGE_PRIVATE_KEY) — to INNE poświadczenie niż to co obsługuje Answer/Event URL
# (te w ogóle nie wymagają autoryzacji, są tylko webhookami). Ten mechanizm jest
# NIEPRZETESTOWANY na żywym połączeniu w momencie napisania — pierwszy telefon z
# transfer_enabled=1 to pierwszy prawdziwy test.
VONAGE_API_BASE = "https://api.nexmo.com"


# Ile dzwoni telefon właściciela zanim uznamy że nikt nie odbiera. Domyślny timeout
# Vonage dla akcji "connect" to 60s (sprawdzone w ncco-reference) — zbyt długo, klient
# wisiałby w martwej ciszy prawie minutę. 15s (~4 sygnały) to dolna typowa wartość używana
# w call center/IVR zanim leci fallback na pocztę głosową/wiadomość.
TRANSFER_RING_TIMEOUT = 15


def _generate_vonage_jwt() -> str | None:
    """JWT krótkożyjący (60s) do jednego wywołania REST API — Vonage wymaga nowego
    tokenu per-request (albo bardzo krótkiego TTL), nie długożyjącego API key jak
    część innych dostawców."""
    app_id = settings.vonage_application_id
    private_key = settings.vonage_private_key
    if not app_id or not private_key:
        logger.warning("📞 [TRANSFER] Brak VONAGE_APPLICATION_ID/VONAGE_PRIVATE_KEY — transfer niedostępny")
        return None
    # Railway env vars są jednolinijkowe — klucz PEM wklejony z dosłownymi "\n"
    # zamiast prawdziwych złamań linii trzeba odtworzyć, inaczej podpis RS256 nie zweryfikuje się.
    private_key = private_key.replace("\\n", "\n")
    import jwt as pyjwt

    now = int(time.time())
    payload = {
        "iat": now,
        "exp": now + 60,
        "jti": str(uuid.uuid4()),
        "application_id": app_id,
    }
    return pyjwt.encode(payload, private_key, algorithm="RS256")


async def transfer_vonage_call(
    call_uuid: str,
    destination_number: str,
    from_number: str,
    announce_text: str,
    api_base: str | None = None,
    fallback_url: str | None = None,
) -> bool:
    """PUT /v1/calls/{uuid} — podmienia NCCO trwającego połączenia. `announce_text`
    leci jako "talk" ZANIM Vonage podłączy telefon właściciela (natywny mechanizm
    Vonage, nie nasz pipeline — unika wyścigu z Gemini Live próbującym powiedzieć
    to samo przez nasze audio, które i tak zaraz zostanie odcięte).

    from_number: bug znaleziony na żywym telefonie (400 Bad Request) — akcja "connect"
    formalnie ma pole "from" jako opcjonalne w schemacie, ale sprawdzone wprost w
    dokumentacji Vonage (ncco-reference#connect): "This must be one of your Vonage
    virtual numbers if you're connecting to a real phone, as the call won't connect
    otherwise" — w praktyce wymagane. To musi być NASZ numer Vonage (tenant['phone_number']),
    nie numer docelowy (właściciela).

    api_base: DRUGI bug za 400 (po naprawie "from") — potwierdzone przez Vonage API
    Support: każde połączenie jest przypisane do konkretnego REGIONALNEGO centrum
    danych (np. api-eu-3.vonage.com), przekazywanego jako "region_url" TYLKO w evencie
    Answer. "Jeśli dostajesz 400/404 przy modyfikacji aktywnego połączenia... Twoje
    połączenie prawdopodobnie siedzi w innym Data Center" — request wysłany na sztywne
    api.nexmo.com trafia w złe centrum dla połączeń spoza jego regionu. Fallback na
    VONAGE_API_BASE gdy region_url nie zostało przechwycone (np. jakiś stary/inny call)."""
    token = _generate_vonage_jwt()
    if not token:
        return False
    if not call_uuid:
        logger.warning("📞 [TRANSFER] Brak call_uuid — nie mogę przekierować")
        return False

    base = (api_base or VONAGE_API_BASE).rstrip("/")

    connect_action = {
        "action": "connect",
        "from": from_number,
        "timeout": TRANSFER_RING_TIMEOUT,
        "endpoint": [{"type": "phone", "number": destination_number}],
    }
    if fallback_url:
        # eventType=synchronous — sprawdzone wprost w dokumentacji Vonage (ncco-reference#connect):
        # gdy połączenie z endpointem skończy się timeout/busy/rejected/failed/unanswered, Vonage
        # odpytuje eventUrl i oczekuje NOWEJ NCCO w odpowiedzi, która ZASTĘPUJE bieżącą. Bez tego
        # (bug zaobserwowany na żywym telefonie): po ring_timeout cała rozmowa po prostu się urywa,
        # klient słyszy ciszę i rozłączenie zamiast jakiegokolwiek wyjaśnienia.
        connect_action["eventType"] = "synchronous"
        connect_action["eventUrl"] = [fallback_url]  # MUSI być lista, nawet z jednym URL-em (schemat Vonage)
    ncco = [
        {"action": "talk", "text": announce_text, "language": "pl-PL"},
        connect_action,
    ]
    body = {"action": "transfer", "destination": {"type": "ncco", "ncco": ncco}}
    logger.info(f"📞 [TRANSFER] Wysyłam do Vonage ({base}): uuid={call_uuid} body={body}")
    try:
        import httpx

        async with httpx.AsyncClient() as client:
            response = await client.put(
                f"{base}/v1/calls/{call_uuid}",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=body,
                timeout=10.0,
            )
            if response.status_code in (200, 204):
                logger.info(f"📞 [TRANSFER] Vonage transfer OK: {call_uuid} → {destination_number}")
                return True
            logger.error(f"📞 [TRANSFER] Vonage API error: {response.status_code} — {response.text}")
            return False
    except Exception as e:
        logger.error(f"📞 [TRANSFER] Vonage API exception: {e}")
        return False


async def _download_vonage_recording(recording_url: str) -> bytes | None:
    """Pobiera nagranie z Vonage — wymaga JWT (te same poświadczenia co REST API/transfer,
    _generate_vonage_jwt), sam recording_url z webhooka NIE jest publicznie dostępny."""
    token = _generate_vonage_jwt()
    if not token:
        return None
    try:
        import httpx

        async with httpx.AsyncClient() as client:
            response = await client.get(
                recording_url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30.0,
            )
            if response.status_code == 200:
                return response.content
            logger.error(f"📼 [HUMAN-FIRST/RECORDING] Pobranie nagrania nie powiodło się: {response.status_code}")
            return None
    except Exception as e:
        logger.error(f"📼 [HUMAN-FIRST/RECORDING] Pobranie nagrania — wyjątek: {e}")
        return None
