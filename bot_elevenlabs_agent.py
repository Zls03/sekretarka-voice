# bot_elevenlabs_agent.py — MVP integracja z ElevenLabs Conversational AI (ElevenAgents).
# APIRouter, nie własny FastAPI app — montowany w bot_gemini_test.py przez
# app.include_router(router), tak samo jak openai_realtime_router.
"""
Rozmowa NIE leci przez nasz Pipecat pipeline (w odróżnieniu od Gemini Live/OpenAI
Realtime powyżej) — agenta (prompt/głos/LLM) konfigurujesz RĘCZNIE w dashboardzie
ElevenLabs (elevenlabs.io -> Agents). Te trzy endpointy to WYŁĄCZNIE mostek między ich
platformą a naszymi danymi/logiką biznesową, wołany PRZEZ ElevenLabs, nie przez nas:

1. POST /elevenlabs/personalization — "conversation initiation client data" webhook.
   ElevenLabs woła to PRZED startem rozmowy (równolegle z łączeniem Twilio, więc klient
   słyszy sygnał łączenia zamiast ciszy) i dostaje w odpowiedzi override promptu +
   pierwszej wiadomości, zbudowane z panelu tak samo jak dla Gemini Live/OpenAI Realtime
   (reużywamy build_realtime_instructions/build_greeting_message 1:1). Skonfiguruj w
   dashboardzie agenta: Settings -> Advanced -> "Fetch conversation initiation data from
   webhook", URL: https://<railway-host>/elevenlabs/personalization.

2. POST /elevenlabs/tools/contact_owner — webhook tool wywoływany PRZEZ agenta w trakcie
   rozmowy (jak contact_owner w Gemini Live/OpenAI Realtime). Skonfiguruj w dashboardzie
   agenta: Tools -> Add tool -> Webhook, method POST, URL jak wyżej + "/tools/contact_owner",
   body params: customer_name (string), message (string), called_number (string, wartość
   {{system__called_number}} jeśli ta zmienna systemowa istnieje w Twoim workspace —
   sprawdź w edytorze zmiennych po prawej), caller_phone (string, {{system__caller_id}}).

3. POST /elevenlabs/post-call — post-call webhook (Settings -> Webhooks na poziomie CAŁEGO
   workspace, nie per-agent) — nalicza minuty/kredyty tym samym apply_call_charge() co
   Gemini Live/OpenAI Realtime.

4. build_register_call_twiml() — WOŁANE PRZEZ NAS (odwrotny kierunek niż 1-3), z
   twilio_incoming_gemini_live_test() w bot_gemini_test.py, gdy tenant["realtime_engine"]
   == "elevenlabs". Powód istnienia: "Importuj numer -> Z Twilio" w dashboardzie ElevenLabs
   miało (wg ich docs) samo nadpisać webhook Twilio numeru na ich infrastrukturę — na żywo
   sprawdzone 2026-09-02 że NIE nadpisuje (numer nadal wskazywał na nasz Railway po dwóch
   próbach importu), a ręczne wpisanie ich webhooka było niemożliwe bez udokumentowanego
   adresu (ryzyko całkowitego wyłączenia numeru realnego klienta przy błędnym zgadnięciu).
   To "bring your own Twilio" API (conversational_ai.twilio.register_call, patrz
   elevenlabs.io/docs/eleven-agents/phone-numbers/twilio-integration/register-call)
   odwraca kierunek: Twilio zostaje podpięty pod NASZ webhook (nic nie trzeba zmieniać w
   Twilio ani importować numeru do ElevenLabs), a MY przy każdym połączeniu wołamy ich API
   z agent_id + from/to number, dostajemy z powrotem TwiML i przekazujemy je Twilio 1:1.
   Prompt/pierwsza wiadomość budowane 1:1 jak w (1), ale przekazane INLINE w
   conversation_initiation_client_data zamiast osobnego webhooka — mniej round-tripów.

⚠️ AUTORYZACJA — DWIE RÓŻNE, ŻADNA NIEZWERYFIKOWANA NA ŻYWYM WEBHOOKU W MOMENCIE
NAPISANIA (2026-09-02):
- (1) i (2): prosty shared secret w nagłówku (ELEVENLABS_SHARED_SECRET) — ustaw ten sam
  string w Railway i w polu "Custom header" przy konfiguracji webhooka/tool w dashboardzie
  ElevenLabs (nagłówek x-bizvoice-secret). Jeśli ELEVENLABS_SHARED_SECRET puste, check jest
  pomijany (celowo, żeby dało się to podłączyć i przetestować ZANIM ustalisz sekret) —
  DOPISZ go przed jakimkolwiek użyciem produkcyjnym.
- (3) ma osobny mechanizm: HMAC podpis w nagłówku "elevenlabs-signature". Dokumentacja
  ElevenLabs nie podaje dokładnego formatu tego nagłówka/schematu podpisu bez ich SDK
  (którego tu nie ma jako zależności) — na razie TYLKO logujemy nagłówek i całe body przy
  odbiorze, żeby dopisać realną weryfikację na podstawie prawdziwych danych z pierwszego
  live webhooka, zamiast zgadywać format i dawać fałszywe poczucie bezpieczeństwa.

Wszystkie trzy endpointy defensywnie parsują pola z wielu możliwych nazw (np.
called_number/to_number) i szeroko logują surowe body — dokumentacja ElevenLabs nie
pokazuje pełnych przykładowych payloadów dla (1) i (2), więc dokładne nazwy pól
potwierdzimy dopiero na pierwszym prawdziwym połączeniu telefonicznym.
"""

import os
import json
import asyncio

from loguru import logger
from fastapi import APIRouter, Request

from helpers import get_tenant_by_phone
from realtime_prompt import build_realtime_instructions, build_greeting_message
from realtime_tools import (
    send_message_email,
    apply_call_charge,
    is_call_allowed,
    _looks_like_vague_meta_message,
    _looks_too_short,
)

router = APIRouter()

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
# Domyślny agent na czas testu jednego tenanta — patrz TEST_TENANT_ID w
# bot_openai_realtime.py dla analogicznego wzorca. Docelowo (wielu tenantów)
# to powinno być polem per-tenant w panelu (elevenlabs_agent_id), nie stałą
# środowiskową — świadomie uproszczone teraz, żeby dało się w ogóle przetestować.
ELEVENLABS_AGENT_ID = os.getenv("ELEVENLABS_AGENT_ID", "")

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


async def build_register_call_twiml(tenant: dict, caller_phone: str, called_number: str) -> str:
    """"Bring your own Twilio" — patrz punkt 4 w docstringu modułu. Zwraca TwiML
    gotowe do zwrócenia bezpośrednio Twilio (media_type="application/xml").

    Rzuca wyjątek przy braku ELEVENLABS_API_KEY/ELEVENLABS_AGENT_ID lub błędzie
    API — wołający (bot_gemini_test.py) łapie to i zwraca bezpieczny TwiML
    fallback, żeby błąd konfiguracji ElevenLabs nie zostawiał klienta w ciszy
    bez żadnego komunikatu."""
    if not ELEVENLABS_API_KEY or not ELEVENLABS_AGENT_ID:
        raise RuntimeError("ELEVENLABS_API_KEY lub ELEVENLABS_AGENT_ID nieskonfigurowane")

    contact_owner_available = tenant.get("contact_owner_enabled", 1) == 1
    prompt_text = build_realtime_instructions(
        tenant, None, include_greeting=False, has_contact_owner=contact_owner_available,
    )
    first_message = build_greeting_message(tenant)

    agent_override = {
        "prompt": {"prompt": prompt_text},
        "first_message": first_message,
        "language": "pl",
    }
    conversation_config_override = {"agent": agent_override}
    # Głos per-tenant — patrz elevenlabs_agent_voice_id w helpers.py (kolumna
    # elevenlabs_voice_id w bazie, ustawiana per firma w panelu, niezależnie od
    # tts_provider kaskady). PRZYWRÓCONE 2026-09-02 po tym jak pierwszy test padał z
    # "Override for field 'voice_id'..." — przyczyną był stary/nieprawidłowy voice_id
    # zaszły w bazie z wcześniejszych testów (sprzed dzisiejszych zmian), NIE brak
    # wsparcia dla nadpisywania tts w register_call. Potwierdzone poprawnym, świeżo
    # dodanym do "My Voices" głosem (Aleksandra) — jeśli mimo to znów padnie z tym samym
    # błędem, podejrzewaj format/uprawnienia konkretnego voice_id, nie sam mechanizm.
    voice_id = tenant.get("elevenlabs_agent_voice_id") or ""
    if voice_id:
        conversation_config_override["tts"] = {"voice_id": voice_id}

    client = _get_elevenlabs_client()
    twiml = await asyncio.to_thread(
        client.conversational_ai.twilio.register_call,
        agent_id=ELEVENLABS_AGENT_ID,
        from_number=caller_phone,
        to_number=called_number,
        direction="inbound",
        conversation_initiation_client_data={
            "conversation_config_override": conversation_config_override,
            "dynamic_variables": {
                "business_name": tenant.get("name") or "",
                "caller_phone": caller_phone,
            },
        },
    )
    logger.info(f"📞 [ELEVENLABS AGENT] register_call OK dla {tenant.get('name')} ({caller_phone} → {called_number})")
    return twiml

ELEVENLABS_SHARED_SECRET = os.getenv("ELEVENLABS_SHARED_SECRET", "")


def _check_shared_secret(request: Request) -> bool:
    """True = OK (albo sekret nieskonfigurowany, patrz docstring modułu)."""
    if not ELEVENLABS_SHARED_SECRET:
        return True
    got = request.headers.get("x-bizvoice-secret", "")
    if got != ELEVENLABS_SHARED_SECRET:
        logger.warning("🚫 [ELEVENLABS AGENT] Zły/brak x-bizvoice-secret — odrzucam webhook")
        return False
    return True


@router.post("/elevenlabs/personalization")
async def elevenlabs_personalization(request: Request):
    if not _check_shared_secret(request):
        return {"type": "conversation_initiation_client_data"}

    body = await request.json()
    called_number = body.get("called_number") or body.get("to_number") or body.get("to") or ""
    caller_id = body.get("caller_id") or body.get("from_number") or body.get("from") or ""
    logger.info(f"📞 [ELEVENLABS AGENT] Personalization: {caller_id} → {called_number} | raw={body}")

    tenant = await get_tenant_by_phone(called_number) if called_number else None
    if not tenant or not await is_call_allowed(tenant):
        return {
            "type": "conversation_initiation_client_data",
            "conversation_config_override": {
                "agent": {
                    "prompt": {"prompt": "Powiedz uprzejmie po polsku jednym zdaniem, że ten numer jest chwilowo niedostępny, i zakończ rozmowę."},
                    "first_message": "Przepraszam, ten numer jest chwilowo niedostępny.",
                    "language": "pl",
                }
            },
        }

    contact_owner_available = tenant.get("contact_owner_enabled", 1) == 1
    prompt_text = build_realtime_instructions(
        tenant, None, include_greeting=False, has_contact_owner=contact_owner_available,
    )
    first_message = build_greeting_message(tenant)

    return {
        "type": "conversation_initiation_client_data",
        "conversation_config_override": {
            "agent": {
                "prompt": {"prompt": prompt_text},
                "first_message": first_message,
                "language": "pl",
            }
        },
        "dynamic_variables": {
            "business_name": tenant.get("name") or "",
            "caller_phone": caller_id,
        },
    }


@router.post("/elevenlabs/tools/contact_owner")
async def elevenlabs_tool_contact_owner(request: Request):
    if not _check_shared_secret(request):
        return {"status": "error", "reason": "unauthorized"}

    body = await request.json()
    logger.info(f"📞 [ELEVENLABS AGENT] contact_owner tool wywołany | raw={body}")

    customer_name = str(body.get("customer_name") or "").strip()
    message = str(body.get("message") or "").strip()
    called_number = body.get("called_number") or body.get("to_number") or ""
    caller_phone = body.get("caller_phone") or body.get("system__caller_id") or "nieznany"

    if not customer_name or not message:
        return {"status": "error", "reason": "missing_fields"}
    if _looks_too_short(message) or _looks_like_vague_meta_message(message):
        return {"status": "error", "reason": "message_too_vague"}

    tenant = await get_tenant_by_phone(called_number) if called_number else None
    if not tenant:
        return {"status": "error", "reason": "tenant_not_found"}

    to_email = tenant.get("notification_email") or tenant.get("email")
    if not to_email:
        return {"status": "error", "reason": "no_notification_email"}

    ok = await send_message_email(tenant, customer_name, message, caller_phone, to_email)
    return {"status": "ok" if ok else "error"}


@router.post("/elevenlabs/post-call")
async def elevenlabs_post_call(request: Request):
    raw_body = await request.body()
    signature_header = request.headers.get("elevenlabs-signature", "")
    logger.info(f"📊 [ELEVENLABS AGENT] Post-call webhook | elevenlabs-signature={signature_header!r}")

    try:
        body = json.loads(raw_body)
    except Exception:
        logger.error(f"❌ [ELEVENLABS AGENT] Post-call: nie mogę sparsować JSON: {raw_body[:500]!r}")
        return {"status": "ignored"}

    logger.info(f"📊 [ELEVENLABS AGENT] Post-call payload (do ustalenia dokładnych nazw pól): {json.dumps(body)[:6000]}")

    data = body.get("data") or body
    called_number = data.get("called_number") or data.get("agent_number") or data.get("to_number") or ""
    duration = int(data.get("call_duration_secs") or data.get("duration_secs") or 0)
    conversation_id = data.get("conversation_id") or body.get("conversation_id") or "unknown"

    if not called_number or duration <= 0:
        logger.warning("⚠️ [ELEVENLABS AGENT] Post-call: brak called_number lub duration<=0 — pomijam naliczanie, patrz zalogowany payload wyżej")
        return {"status": "ignored"}

    tenant = await get_tenant_by_phone(called_number)
    if not tenant:
        logger.warning(f"⚠️ [ELEVENLABS AGENT] Post-call: nie znaleziono tenanta dla {called_number}")
        return {"status": "ignored"}

    await apply_call_charge(
        tenant_id=tenant["id"],
        is_saas_tenant=(tenant.get("source") == "saas"),
        call_sid=str(conversation_id),
        call_status="completed",
        duration=duration,
    )
    return {"status": "ok"}
