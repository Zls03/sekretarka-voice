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
import time
import uuid
import base64
import asyncio

import websockets
from loguru import logger
from fastapi import APIRouter, Request, WebSocket

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask, PipelineParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketTransport, FastAPIWebsocketParams
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.serializers.vonage import VonageFrameSerializer
from pipecat.frames.frames import (
    StartFrame, EndFrame, InputAudioRawFrame, TTSAudioRawFrame, TTSStartedFrame, TTSStoppedFrame,
    InterruptionFrame, VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameProcessor

from helpers import get_tenant_by_phone, db, saas_db
from realtime_prompt import build_realtime_instructions, build_greeting_message
from realtime_tools import (
    send_message_email,
    send_call_summary_email,
    summarize_conversation_lines,
    is_call_allowed,
    _looks_like_vague_meta_message,
    _looks_too_short,
)

router = APIRouter()

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
# Agent domyślny/fallback ze zmiennej środowiskowej — używany TYLKO gdy tenant nie ma
# własnego elevenlabs_agent_id (panel: zakładka "Głos agenta" -> ElevenLabs). Per-tenant
# pole dopisane 2026-09-03 razem z mostem Vonage (patrz run_elevenlabs_vonage_bot niżej) —
# wcześniej to było na sztywno jeden wspólny agent dla WSZYSTKICH tenantów, co dawało
# każdej firmie ten sam prompt-bazę/głos-bazę ElevenLabs (nasze override'y treści promptu
# i tak nadpisują treść per-rozmowa, ale ustawienia samego agenta typu domyślny model LLM,
# tembr itp. były wspólne).
ELEVENLABS_AGENT_ID = os.getenv("ELEVENLABS_AGENT_ID", "")

# tool_id narzędzia "contact_owner" skonfigurowanego w dashboardzie ElevenLabs (agent
# "Bizvoice Test" -> Narzędzia -> contact_owner). Do 2026-09-04 to narzędzie było
# statycznie przypięte do agenta i ZAWSZE technicznie dostępne dla modelu, niezależnie
# od tenant.get("contact_owner_enabled") — jedyną obroną była instrukcja w promptcie
# ("nie masz tej funkcji, nie oferuj jej") + twardy blok wysyłki po stronie serwera
# (patrz elevenlabs_tool_contact_owner niżej), ale model czasem i tak WERBALNIE oferował
# zebranie wiadomości (złapane na żywym telefonie, tenant z contact_owner_enabled=0).
# Od teraz tool_ids jest jawnie nadpisywany per rozmowa (conversation_config_override.
# agent.prompt.tool_ids) — dokładnie ten sam poziom gwarancji co w Gemini Live/OpenAI
# Realtime, gdzie narzędzie po prostu nie istnieje w tools[] danej rozmowy. Wymaga
# włączonego przełącznika "Tools" w platform_settings.overrides.conversation_config_override.
# agent.prompt.tool_ids na agencie (włączone 2026-09-04 przez PATCH /v1/convai/agents —
# bez tego ElevenLabs po cichu ignoruje tool_ids z override i zawsze używa domyślnego
# zestawu narzędzi agenta, czyli błąd wracałby bez żadnego widocznego sygnału).
CONTACT_OWNER_TOOL_ID = "tool_7301m1f7exgvf81a5ysqrbn235ts"

ELEVENLABS_SIP_DOMAIN = "sip.rtc.elevenlabs.io:5060"


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
                },
                timeout=10.0,
            )
        if response.status_code in (200, 201):
            logger.info(f"✅ [ELEVENLABS SIP] Zaimportowano numer {e164} (SIP trunk direct)")
            return True
        if response.status_code == 409:
            return True
        logger.warning(f"⚠️ [ELEVENLABS SIP] Import {e164} nie powiódł się: {response.status_code} {response.text[:200]}")
        return False
    except Exception as e:
        logger.error(f"❌ [ELEVENLABS SIP] Import {e164} — wyjątek: {e}")
        return False


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


def _resolve_agent_id(tenant: dict) -> str:
    """Per-tenant elevenlabs_agent_id (panel: zakładka ElevenLabs), z fallbackiem na
    stałą środowiskową ELEVENLABS_AGENT_ID dla tenantów które go jeszcze nie ustawiły."""
    return (tenant.get("elevenlabs_agent_id") or "").strip() or ELEVENLABS_AGENT_ID


def _build_conversation_config_override(
    tenant: dict, caller_phone: str, called_number: str, call_sid: str = "",
) -> tuple[dict, dict]:
    """Wspólne dla obu transportów (Twilio register_call i Vonage WebSocket, patrz
    run_elevenlabs_vonage_bot) — buduje (conversation_config_override, dynamic_variables)
    z tych samych danych panelu co Gemini Live/OpenAI Realtime (build_realtime_instructions/
    build_greeting_message), plus opcjonalny nadpisany głos per-tenant."""
    contact_owner_available = tenant.get("contact_owner_enabled", 1) == 1
    prompt_text = build_realtime_instructions(
        tenant, None, include_greeting=False, has_contact_owner=contact_owner_available,
    )
    first_message = build_greeting_message(tenant)

    conversation_config_override = {
        "agent": {
            "prompt": {
                "prompt": prompt_text,
                "tool_ids": [CONTACT_OWNER_TOOL_ID] if contact_owner_available else [],
            },
            "first_message": first_message,
            "language": "pl",
        }
    }
    # Głos per-tenant — kolumna elevenlabs_voice_id w bazie, ta sama którą panel
    # zapisuje w zakładce "🔷 ElevenLabs" (Głos agenta). Do 2026-09-03 kod czytał inną
    # nazwę pola (elevenlabs_agent_voice_id), której panel NIGDY nie zapisywał — więc
    # nadpisanie głosu z panelu było martwe od początku, mimo że sam mechanizm
    # (conversation_config_override.tts.voice_id) działał poprawnie na żywym telefonie
    # (potwierdzone 2026-09-02 z ręcznie wstawionym do bazy voice_id "Aleksandra").
    voice_id = tenant.get("elevenlabs_voice_id") or ""
    if voice_id:
        conversation_config_override["tts"] = {"voice_id": voice_id}

    dynamic_variables = {
        "business_name": tenant.get("name") or "",
        "caller_phone": caller_phone,
        "called_number": called_number,
        # call_sid ogólne (Vonage UUID lub Twilio SID) + twilio_call_sid zostaje dla
        # wstecznej zgodności z payloadem który /elevenlabs/post-call już umie czytać —
        # patrz jego aktualizacja niżej, czyta oba klucze.
        "call_sid": call_sid,
        "twilio_call_sid": call_sid,
    }
    return conversation_config_override, dynamic_variables


async def build_register_call_twiml(tenant: dict, caller_phone: str, called_number: str, call_sid: str = "") -> str:
    """"Bring your own Twilio" — patrz punkt 4 w docstringu modułu. Zwraca TwiML
    gotowe do zwrócenia bezpośrednio Twilio (media_type="application/xml").

    Rzuca wyjątek przy braku ELEVENLABS_API_KEY/agent_id lub błędzie API — wołający
    (bot_gemini_test.py) łapie to i zwraca bezpieczny TwiML fallback, żeby błąd
    konfiguracji ElevenLabs nie zostawiał klienta w ciszy bez żadnego komunikatu."""
    agent_id = _resolve_agent_id(tenant)
    if not ELEVENLABS_API_KEY or not agent_id:
        raise RuntimeError("ELEVENLABS_API_KEY lub elevenlabs_agent_id (tenant/env) nieskonfigurowane")

    conversation_config_override, dynamic_variables = _build_conversation_config_override(
        tenant, caller_phone, called_number, call_sid,
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
                    "prompt": {
                        "prompt": "Powiedz uprzejmie po polsku jednym zdaniem, że ten numer jest chwilowo niedostępny, i zakończ rozmowę.",
                        "tool_ids": [],
                    },
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
                "prompt": {
                    "prompt": prompt_text,
                    "tool_ids": [CONTACT_OWNER_TOOL_ID] if contact_owner_available else [],
                },
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

    if tenant.get("contact_owner_enabled", 1) != 1:
        # Twardy blok — narzędzie w ElevenLabs jest statycznie przypięte do agenta
        # (nie da się go usunąć per-rozmowa jak w Gemini Live/OpenAI Realtime, patrz
        # has_contact_owner w realtime_prompt.py), więc nawet gdy model je i tak wywoła
        # wbrew instrukcji w prompcie, tu odmawiamy wysyłki — to jedyne miejsce gdzie
        # ustawienie tenanta jest faktycznie wymuszone, nie tylko sugerowane tekstem.
        logger.warning(f"🚫 [ELEVENLABS AGENT] contact_owner wywołany mimo contact_owner_enabled=0 dla {tenant.get('name')} — odmawiam wysyłki")
        return {"status": "error", "reason": "disabled"}

    to_email = tenant.get("notification_email") or tenant.get("email")
    if not to_email:
        return {"status": "error", "reason": "no_notification_email"}

    ok = await send_message_email(tenant, customer_name, message, caller_phone, to_email)
    return {"status": "ok" if ok else "error"}


async def save_elevenlabs_transcript(tenant: dict, call_sid: str, transcript: list, analysis: dict) -> int:
    """1:1 wzorzec z realtime_tools.py::save_call_transcript (Gemini Live/OpenAI
    Realtime), ale czyta ElevenLabs data.transcript[] zamiast LLMContext.get_messages() —
    role tam to "agent"/"user", u nas w call_transcripts zawsze "assistant"/"user" (patrz
    save_call_transcript), więc mapujemy "agent"->"assistant". NIE tworzy wiersza
    call_logs — ten już istnieje, utworzony przez /twilio/status (patrz docstring
    elevenlabs_post_call), tu tylko dopisujemy transkrypt do call_transcripts."""
    tenant_id = tenant.get("id", "")
    if not tenant_id or not call_sid:
        return 0

    is_saas = tenant_id.startswith("firm_")
    target_db = saas_db if is_saas else db

    saved = 0
    for turn in transcript:
        role = "assistant" if turn.get("role") == "agent" else "user"
        content = (turn.get("message") or "").strip()
        if not content:
            continue
        await target_db.execute(
            """INSERT INTO call_transcripts
               (id, tenant_id, call_sid, role, content, created_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'))""",
            [f"tr_{uuid.uuid4().hex[:12]}", tenant_id, call_sid, role, content[:500]],
        )
        saved += 1

    summary = analysis.get("transcript_summary") or ""
    if summary:
        await target_db.execute(
            """INSERT INTO call_transcripts
               (id, tenant_id, call_sid, role, content, created_at)
               VALUES (?, ?, ?, 'summary', ?, datetime('now'))""",
            [f"tr_{uuid.uuid4().hex[:12]}", tenant_id, call_sid, summary[:1000]],
        )

    return saved


@router.post("/elevenlabs/post-call")
async def elevenlabs_post_call(request: Request):
    """⚠️ NIE nalicza minut/kredytów — to już robi /twilio/status (bot_gemini_test.py),
    dokładnie tym samym mechanizmem co dla Gemini Live/OpenAI Realtime, bo Twilio wysyła
    swój własny "completed" callback niezależnie od tego, który silnik obsłużył audio
    (potwierdzone na żywo 2026-09-02: call_sid się zgadza, oba webhooki widzą tę samą
    rozmowę). Druga próba naliczania tu = podwójne obciążenie klienta.

    Ten handler robi WYŁĄCZNIE to, czego /twilio/status nie ma: transkrypt rozmowy
    (call_transcripts) + mail z podsumowaniem (jeśli tenant ma włączone raporty) — bez
    tego panel nie ma czego pokazać po "rozwinięciu" logu rozmowy dla połączeń przez
    ElevenLabs.

    ⚠️ 2026-09-02: na żywym payloadzie z register_call() (bring-your-own-Twilio) okazało
    się, że body["data"] NIE zawiera klucza "phone_call" w ogóle (zaobserwowane klucze:
    agent_id, metadata, analysis, conversation_initiation_client_data, conversation_id,
    transcript, ...) — inaczej niż wcześniej zakładano na podstawie dokumentacji. Powód:
    register_call() nie przekazuje Twilio CallSid do ElevenLabs (nie ma takiego pola w
    ich API), więc ich webhook nie ma skąd go znać. Naprawione przez ECHO: CallSid i
    called_number są teraz wysyłane w dynamic_variables przy register_call()
    (build_register_call_twiml) i odczytywane z powrotem tutaj z
    data.conversation_initiation_client_data.dynamic_variables — ElevenLabs oddaje ten
    obiekt bez zmian w każdym post-call webhooku. data.metadata.call_duration_secs i
    data.transcript[].{role,message} pozostają bez zmian (potwierdzone działające)."""
    raw_body = await request.body()
    signature_header = request.headers.get("elevenlabs-signature", "")

    try:
        body = json.loads(raw_body)
    except Exception:
        logger.error(f"❌ [ELEVENLABS AGENT] Post-call: nie mogę sparsować JSON: {raw_body[:500]!r}")
        return {"status": "ignored"}

    data = body.get("data") or {}
    metadata = data.get("metadata") or {}
    analysis = data.get("analysis") or {}
    init_data = data.get("conversation_initiation_client_data") or {}
    dyn_vars = init_data.get("dynamic_variables") or {}
    # Fallback na starą ścieżkę (phone_call.*) na wypadek gdyby inny typ połączenia
    # (np. przyszły import numeru zamiast register_call) jednak ją wypełniał.
    phone_call = data.get("phone_call") or {}

    called_number = dyn_vars.get("called_number") or phone_call.get("agent_number") or ""
    caller_phone = dyn_vars.get("caller_phone") or phone_call.get("external_number") or ""
    # call_sid ogólne (dopisane 2026-09-03 razem z mostem Vonage — patrz
    # _build_conversation_config_override) sprawdzane PRZED starym twilio_call_sid,
    # oba klucze i tak niosą tę samą wartość dla nowych połączeń.
    call_sid = dyn_vars.get("call_sid") or dyn_vars.get("twilio_call_sid") or phone_call.get("call_sid") or ""
    duration = int(metadata.get("call_duration_secs") or 0)
    transcript = data.get("transcript") or []

    logger.info(
        f"📊 [ELEVENLABS AGENT] Post-call: {called_number} ({call_sid}, {duration}s, "
        f"status={data.get('status')}, {len(transcript)} tur) | signature={signature_header!r}"
    )

    if not call_sid or not called_number:
        logger.warning(
            f"⚠️ [ELEVENLABS AGENT] Post-call: brak call_sid/called_number w payloadzie, pomijam. "
            f"data.keys()={list(data.keys())} dynamic_variables={dyn_vars}"
        )
        return {"status": "ignored"}

    tenant = await get_tenant_by_phone(called_number)
    if not tenant:
        logger.warning(f"⚠️ [ELEVENLABS AGENT] Post-call: nie znaleziono tenanta dla {called_number}")
        return {"status": "ignored"}

    saved = await save_elevenlabs_transcript(tenant, call_sid, transcript, analysis)
    logger.info(f"📝 [ELEVENLABS AGENT] Transcript saved: {saved} wiadomości ({call_sid})")

    # Mail z podsumowaniem po KAŻDEJ rozmowie — 1:1 z realtime_tools.py::maybe_send_call_summary
    # (Gemini Live/OpenAI Realtime). ZMIANA 2026-09-04: wcześniej brało gotowe streszczenie
    # z ElevenLabs (data.analysis.transcript_summary) — generyczne, bez kontekstu firmy i bez
    # strukturalnej ekstrakcji (kto/powód/szczegóły/wynik). Teraz konwertujemy transcript[]
    # (role "agent"/"user", pole "message") do TEGO SAMEGO formatu list stringów co
    # generate_conversation_summary() używa dla Gemini Live/OpenAI Realtime, i wołamy
    # DOKŁADNIE tę samą funkcję (summarize_conversation_lines) z kontekstem firmy
    # (tenant["additional_info"]) — spójne, per-firmowe podsumowania na wszystkich 3 silnikach.
    lead_email_enabled = int(tenant.get("lead_email_enabled") or 0)
    to_email = tenant.get("lead_email") or tenant.get("notification_email") or tenant.get("email")
    conversation_lines = []
    for turn in transcript:
        role = "assistant" if turn.get("role") == "agent" else "user"
        content = (turn.get("message") or "").strip()
        if len(content) > 2:
            label = "Klient" if role == "user" else "Asystent"
            conversation_lines.append(f"{label}: {content[:200]}")
    summary = await summarize_conversation_lines(conversation_lines, tenant)
    if summary == "Brak treści rozmowy." or summary == "Nie udało się wygenerować streszczenia.":
        # Zapasowo — wbudowane streszczenie ElevenLabs lepsze niż nic, gdyby nasze zawiodło.
        summary = analysis.get("transcript_summary") or ""
    if lead_email_enabled and to_email and summary:
        ok = await send_call_summary_email(tenant, caller_phone or "nieznany", summary, to_email)
        logger.info(f"📧 [ELEVENLABS AGENT] Raport z rozmowy: {'wysłany' if ok else 'błąd wysyłki'} do {to_email}")

    return {"status": "ok"}


# ==========================================================================
# MOST VONAGE — dopisany 2026-09-03. "Bring your own Twilio" (register_call, wyżej)
# NIE ma odpowiednika dla Vonage — to Twilio-specyficzne API ElevenLabs, w którym ICH
# infrastruktura łączy się z Twilio bezpośrednio, z pominięciem naszego pipeline'u.
# Dla Vonage nie ma takiego skrótu: ElevenLabs ma oficjalną integrację (patrz
# elevenlabs.io/docs/conversational-ai/phone-numbers/telephony/vonage), ale w formie
# SAMODZIELNIE HOSTOWANEGO mostu WebSocket — dokładnie tej samej roli co już pełni
# bot_gemini_test.py dla Gemini Live/OpenAI Realtime na Vonage. Więc budujemy go tu,
# reużywając ten sam transport (FastAPIWebsocketTransport+VonageFrameSerializer,
# audio L16/16kHz) i ten sam wzorzec lokalnego VAD dla przerwań (VADProcessor —
# transport.output() sam czyści bufor audio na wykryte lokalnie mówienie klienta,
# patrz PipelineParams(allow_interruptions=True) niżej), zamiast pisać ręcznie parsowanie
# protokołu Vonage od zera.
#
# Protokół WebSocket ElevenLabs Conversational AI (potwierdzony w ich dokumentacji API,
# NIEPOTWIERDZONY jeszcze na żywym telefonie w chwili pisania — patrz TTFB/format audio
# w ElevenLabsRealtimeService, pierwsze realne połączenie może wymagać korekt, tak jak
# przy Gemini Live/OpenAI Realtime): wss://api.elevenlabs.io/v1/convai/conversation
# ?agent_id=..., autoryzacja nagłówkiem "xi-api-key" (serwer-serwer, więc bezpiecznie —
# w odróżnieniu od widgetów w przeglądarce nie potrzebujemy tu signed URL). Klient wysyła
# {"user_audio_chunk": "<base64 PCM>"}, serwer odsyła zdarzenia {"type": "audio", ...},
# "agent_response", "user_transcript", "interruption", "ping" (trzeba odpowiedzieć "pong"),
# "conversation_initiation_metadata" (tu neguje się faktyczny format audio — jeśli
# agent_output_audio_format != pcm_16000, TTFB/jakość ucierpi, bo Vonage jest sztywno na
# L16/16kHz i nie robimy tu resamplingu w wersji 1).
class ElevenLabsRealtimeService(FrameProcessor):
    """Most między pipecat a surowym WebSocketem ElevenLabs Conversational AI — siedzi
    w pipeline TAM gdzie normalnie siedziałby LLMService (np. GeminiLiveLLMService), ale
    to nie jest "prawdziwy" pipecat LLM service (żadnej z ich wbudowanych klas nie ma dla
    ElevenLabs Conversational AI w tej wersji pipecat, patrz github.com/pipecat-ai/pipecat
    issue #2812) — tylko FrameProcessor który połyka InputAudioRawFrame (wysyła do
    ElevenLabs, nic nie przepuszcza dalej — to jest ostateczny konsument audio wejściowego)
    i emituje TTSAudioRawFrame/TTSStartedFrame/TTSStoppedFrame na podstawie zdarzeń
    przychodzących z ich WebSocketu w osobnym tasku czytającym."""

    def __init__(self, tenant: dict, caller_phone: str, called_number: str, call_sid: str, agent_id: str, api_key: str, task_box: dict):
        super().__init__()
        self._tenant = tenant
        self._caller_phone = caller_phone
        self._called_number = called_number
        self._call_sid = call_sid
        self._agent_id = agent_id
        self._api_key = api_key
        # {"task": None} wypełniany PO utworzeniu PipelineTask w run_elevenlabs_vonage_bot
        # (task jeszcze nie istnieje w momencie tworzenia tego serwisu — patrz tam).
        # Potrzebny do _hangup_after_elevenlabs_closed: push_frame() STĄD leci tylko
        # "w dół" do transportu, nigdy nie dociera do samego PipelineTask, więc nie
        # zamyka realnie WebSocketu z Vonage — trzeba go wykolejkować przez sam task,
        # dokładnie jak robi to on_client_disconnected w run_elevenlabs_vonage_bot.
        # Błąd złapany na żywym telefonie 2026-09-03: bez tego EndFrame "wypychał się"
        # bezbłędnie, ale połączenie i tak wisiało w ciszy, aż klient ręcznie się rozłączył.
        self._task_box = task_box
        self._ws = None
        self._reader_task = None
        self._sample_rate = 16000
        self._speaking = False
        # True gdy ElevenLabs zamknął swoją stronę WebSocketu (np. po naturalnym końcu
        # rozmowy) ale Vonage jeszcze przez chwilę dosyła nam audio klienta (telefon
        # fizycznie rozłącza się z opóźnieniem) — bez tej flagi każda kolejna paczka
        # audio (co ~20ms) próbowałaby wysyłkę na martwy socket i logowała identyczny
        # błąd dziesiątki razy na sekundę (złapane na żywym telefonie 2026-09-03: kod
        # zamknięcia 1000/OK po obu stronach, więc to NIE błąd — po prostu koniec
        # rozmowy, nic nie tracimy, bo wysyłka i tak nigdy nie dociera do ElevenLabs).
        self._closed = False
        # True TYLKO gdy MY zainicjowaliśmy rozłączenie (EndFrame przyszedł z pipeline'u,
        # np. bo Vonage rozłączył klienta) — odróżnia to od sytuacji gdy ElevenLabs
        # zamyka swoją stronę PIERWSZY (naturalny koniec rozmowy, agent się pożegnał).
        # W tym drugim przypadku to MY musimy zainicjować rozłączenie Vonage (patrz
        # _read_loop) — w odróżnieniu od Twilio (register_call), gdzie ElevenLabs ma
        # bezpośrednią kontrolę nad połączeniem Twilio i sam je rozłącza; na Vonage to
        # MY jesteśmy właścicielem połączenia, ElevenLabs jest tylko "zdalnym mózgiem".
        # Błąd złapany na żywym telefonie 2026-09-03: bez tego klient zostawał podłączony
        # w ciszy przez 7+ sekund po pożegnaniu bota, aż sam się rozłączył ręcznie.
        self._we_disconnected = False
        # Pomiar TTFB "user->bot audio", kotwiczony o lokalny VAD-stop — dokładnie ten sam
        # wzorzec co GeminiUserMonitor/GeminiBotMonitor w bot_gemini_test.py (patrz tam pełny
        # docstring), żeby liczby były porównywalne 1:1 między silnikami/transportami.
        self._last_user_stop = None
        self._waiting_for_bot_audio = False

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            await self.push_frame(frame, direction)
            await self._connect()
        elif isinstance(frame, InputAudioRawFrame):
            await self._send_audio(frame.audio)
            # NIE przepuszczamy dalej — ElevenLabs sam robi STT/turn-taking po swojej
            # stronie, nic downstream nie potrzebuje surowego audio wejściowego.
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            if self._speaking:
                # Przerwanie wykryte LOKALNIE (nasz Silero VAD), bez czekania na
                # zdarzenie "interruption" z ElevenLabs (przychodzi z opóźnieniem
                # sieciowym — na żywym telefonie 2026-09-03 zaobserwowane 7+ sekund
                # martwego/nieprzerwanego audio bota, klient mówił "Halo?" wielokrotnie
                # zanim cokolwiek się zmieniło). InterruptionFrame to WŁAŚCIWY sygnał
                # który czyści bufor już wykolejkowanego audio w transporcie wyjściowym
                # (Gemini Live/OpenAI Realtime dostają to za darmo wewnątrz swoich
                # własnych serwisów pipecat — u nas trzeba to zrobić jawnie, bo
                # ElevenLabs nie ma wbudowanej klasy serwisu w tej wersji pipecat).
                self._speaking = False
                await self.push_frame(InterruptionFrame(), direction)
                await self.push_frame(TTSStoppedFrame(), direction)
            await self.push_frame(frame, direction)
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._last_user_stop = asyncio.get_event_loop().time()
            self._waiting_for_bot_audio = True
            await self.push_frame(frame, direction)
        elif isinstance(frame, EndFrame):
            self._we_disconnected = True
            await self._disconnect()
            await self.push_frame(frame, direction)
        else:
            await self.push_frame(frame, direction)

    async def _connect(self):
        conversation_config_override, dynamic_variables = _build_conversation_config_override(
            self._tenant, self._caller_phone, self._called_number, self._call_sid,
        )
        url = f"wss://api.elevenlabs.io/v1/convai/conversation?agent_id={self._agent_id}"
        try:
            self._ws = await websockets.connect(url, additional_headers={"xi-api-key": self._api_key})
        except Exception as e:
            logger.error(f"❌ [ELEVENLABS/VONAGE] Połączenie WebSocket nie powiodło się: {e}")
            return
        await self._ws.send(json.dumps({
            "type": "conversation_initiation_client_data",
            "conversation_config_override": conversation_config_override,
            "dynamic_variables": dynamic_variables,
        }))
        logger.info(f"🔌 [ELEVENLABS/VONAGE] Połączono z agentem {self._agent_id} dla {self._tenant.get('name')}")
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _send_audio(self, audio: bytes):
        if self._ws is None or self._closed:
            return
        try:
            b64 = base64.b64encode(audio).decode("ascii")
            await self._ws.send(json.dumps({"user_audio_chunk": b64}))
        except websockets.exceptions.ConnectionClosed:
            # ElevenLabs zamknął stronę pierwszy (koniec rozmowy) — patrz komentarz przy
            # self._closed w __init__. Log RAZ, nie za każdą kolejną paczkę audio.
            self._closed = True
            logger.info("🔌 [ELEVENLABS/VONAGE] WebSocket ElevenLabs już zamknięty — przestaję wysyłać audio")
        except Exception as e:
            logger.error(f"❌ [ELEVENLABS/VONAGE] Wysyłka audio nie powiodła się: {e}")

    async def _read_loop(self):
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                mtype = msg.get("type")

                if mtype == "conversation_initiation_metadata":
                    meta = msg.get("conversation_initiation_metadata_event", {})
                    fmt = meta.get("agent_output_audio_format", "pcm_16000")
                    if fmt != "pcm_16000":
                        # Świadomie tylko log, nie resampling — patrz komentarz nad klasą.
                        logger.warning(f"⚠️ [ELEVENLABS/VONAGE] Nieoczekiwany format audio agenta: {fmt} (oczekiwano pcm_16000, jakość/latencja może ucierpieć)")

                elif mtype == "audio":
                    if not self._speaking:
                        self._speaking = True
                        await self.push_frame(TTSStartedFrame())
                        if self._waiting_for_bot_audio:
                            self._waiting_for_bot_audio = False
                            if self._last_user_stop is not None:
                                ms = (asyncio.get_event_loop().time() - self._last_user_stop) * 1000
                                icon = "🟢" if ms < 1500 else "🟡" if ms < 2500 else "🔴"
                                logger.info(f"⏱️ [ELEVENLABS/VONAGE/TOTAL] user->bot audio {ms:.0f}ms {icon}")
                    b64 = msg.get("audio_event", {}).get("audio_base_64", "")
                    if b64:
                        audio_bytes = base64.b64decode(b64)
                        await self.push_frame(TTSAudioRawFrame(audio=audio_bytes, sample_rate=self._sample_rate, num_channels=1))

                elif mtype == "agent_response_complete":
                    if self._speaking:
                        self._speaking = False
                        await self.push_frame(TTSStoppedFrame())

                elif mtype == "agent_response":
                    text = msg.get("agent_response_event", {}).get("agent_response", "")
                    if text:
                        logger.info(f"⏱️ [ELEVENLABS/VONAGE/BOT] mówi: {text!r}")

                elif mtype == "user_transcript":
                    text = msg.get("user_transcription_event", {}).get("user_transcript", "")
                    if text:
                        logger.info(f"⏱️ [ELEVENLABS/VONAGE/USER] transkrypcja: {text!r}")

                elif mtype == "interruption":
                    # Zapasowy sygnał — GŁÓWNE przerwanie leci teraz lokalnie z VAD
                    # (patrz VADUserStartedSpeakingFrame w process_frame, ten sam wzorzec
                    # co Gemini Live/OpenAI Realtime), bo to zdarzenie sieciowe przychodzi
                    # z zauważalnym opóźnieniem (złapane na żywym telefonie 2026-09-03:
                    # 7+ sekund nieprzerwanego audio bota zanim cokolwiek się zmieniło,
                    # zanim ten fix powstał). Zostaje jako druga linia obrony na wypadek
                    # gdyby lokalny VAD nie złapał jakiegoś przypadku.
                    logger.debug("⏱️ [ELEVENLABS/VONAGE] przerwanie (interruption, zdalne)")
                    if self._speaking:
                        self._speaking = False
                        await self.push_frame(InterruptionFrame())
                        await self.push_frame(TTSStoppedFrame())

                elif mtype == "ping":
                    event_id = msg.get("ping_event", {}).get("event_id")
                    try:
                        await self._ws.send(json.dumps({"type": "pong", "event_id": event_id}))
                    except Exception:
                        pass

                elif mtype == "client_error":
                    logger.error(f"❌ [ELEVENLABS/VONAGE] client_error: {msg}")

        except websockets.exceptions.ConnectionClosed as e:
            self._closed = True
            logger.info(f"🔌 [ELEVENLABS/VONAGE] WebSocket zamknięty: {e}")
        except asyncio.CancelledError:
            # MY anulowaliśmy ten task z _disconnect() (EndFrame już leci z innego
            # powodu, np. Vonage rozłączył klienta) — nic dodatkowego do zrobienia,
            # self._we_disconnected już ustawione w process_frame.
            raise
        except Exception as e:
            logger.error(f"❌ [ELEVENLABS/VONAGE] Błąd w pętli odczytu: {e}")
        finally:
            if not self._we_disconnected:
                # ElevenLabs zamknął stronę PIERWSZY — patrz komentarz przy
                # self._we_disconnected w __init__ po pełne wyjaśnienie różnicy względem
                # Twilio. My musimy teraz sami zainicjować koniec połączenia Vonage.
                asyncio.create_task(self._hangup_after_elevenlabs_closed())

    async def _hangup_after_elevenlabs_closed(self):
        logger.info("👋 [ELEVENLABS/VONAGE] ElevenLabs zakończył rozmowę — rozłączam Vonage")
        # Krótki odstęp na dogranie ewentualnego ogona audio już w buforze transportu
        # (ten sam rząd wielkości co auto_hangup dla end_conversation w realtime_tools.py,
        # tam 3.0s — tu krócej, bo pożegnanie ElevenLabs już w całości poleciało zanim
        # zamknęli WebSocket, w odróżnieniu od tamtej ścieżki gdzie EndFrame leci od razu
        # po samym WYWOŁANIU narzędzia, przed wypowiedzeniem pożegnania).
        await asyncio.sleep(1.5)
        task = self._task_box.get("task")
        if task is None:
            logger.error("❌ [ELEVENLABS/VONAGE] Brak referencji do PipelineTask — nie mogę rozłączyć Vonage")
            return
        try:
            await task.queue_frame(EndFrame())
        except Exception as e:
            logger.error(f"❌ [ELEVENLABS/VONAGE] Nie udało się wykolejkować EndFrame po zamknięciu przez ElevenLabs: {e}")

    async def _disconnect(self):
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None


async def run_elevenlabs_vonage_bot(websocket: WebSocket, tenant: dict, caller_phone: str, called_number: str, call_sid: str):
    """Odpowiednik build_register_call_twiml dla Vonage — patrz komentarz nad sekcją
    "MOST VONAGE" wyżej po pełne wyjaśnienie dlaczego to osobna ścieżka, nie register_call.

    Billing/minuty: NIE tutaj — Vonage nalicza przez /vonage/events (bot_gemini_test.py),
    dokładnie tak samo jak dla Gemini Live/OpenAI Realtime, niezależnie od tego który
    silnik obsłużył audio (ten webhook czyta tylko numer+czas trwania z Vonage, nie wie
    nic o silniku). Transkrypt + mail z podsumowaniem: załatwia już istniejący
    /elevenlabs/post-call (patrz wyżej), wołany przez ElevenLabs niezależnie od transportu."""
    agent_id = _resolve_agent_id(tenant)
    if not ELEVENLABS_API_KEY or not agent_id:
        logger.error(f"❌ [ELEVENLABS/VONAGE] ELEVENLABS_API_KEY lub agent_id nieskonfigurowane dla {tenant.get('name')} — zamykam")
        await websocket.close()
        return

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=VonageFrameSerializer(
                params=VonageFrameSerializer.InputParams(vonage_sample_rate=16000),
            ),
        ),
    )

    # Ten sam lokalny VAD co Gemini Live/OpenAI Realtime na Vonage — daje
    # allow_interruptions realne, natychmiastowe czyszczenie bufora audio Vonage na
    # wykryte lokalnie mówienie klienta, zamiast czekać na "interruption" z ElevenLabs
    # (które i tak przychodzi z opóźnieniem sieciowym).
    vad_processor = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(
            params=VADParams(confidence=0.6, start_secs=0.2, stop_secs=0.2, min_volume=0.4)
        )
    )

    task_box = {"task": None}  # wypełniany niżej, po utworzeniu PipelineTask — patrz komentarz w __init__
    elevenlabs_service = ElevenLabsRealtimeService(
        tenant=tenant, caller_phone=caller_phone, called_number=called_number,
        call_sid=call_sid or "", agent_id=agent_id, api_key=ELEVENLABS_API_KEY,
        task_box=task_box,
    )

    pipeline = Pipeline([
        transport.input(),
        vad_processor,
        elevenlabs_service,
        transport.output(),
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=16000,
        ),
    )
    task_box["task"] = task

    @transport.event_handler("on_client_disconnected")
    async def on_disconnect(transport, client):
        logger.info("📴 [ELEVENLABS/VONAGE] Klient rozłączony")
        await task.queue_frame(EndFrame())

    runner = PipelineRunner()
    logger.info(f"🚀 [ELEVENLABS/VONAGE] Start pipeline dla {tenant.get('name')}")
    try:
        await runner.run(task)
    except Exception as e:
        logger.error(f"❌ [ELEVENLABS/VONAGE] Pipeline error: {e}")
    finally:
        logger.info("🏁 [ELEVENLABS/VONAGE] Koniec połączenia")
