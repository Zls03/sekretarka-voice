"""Webhooki wołane przez ElevenLabs: personalizacja, narzędzia w trakcie rozmowy, post-call."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from fastapi import APIRouter, Request
from loguru import logger

from app.background import spawn
from app.billing import is_call_allowed
from app.booking.book_appointment import book_appointment_step
from app.booking.manage_booking import manage_booking_step
from app.call_logs import persist_call_summary
from app.crm_contacts import maybe_save_contact_name
from app.db import db, saas_db
from app.engines.elevenlabs.config import (
    ELEVENLABS_SHARED_SECRET,
)
from app.engines.elevenlabs.conversation import build_agent_override
from app.notifications.email import send_call_summary_email, send_message_email
from app.notifications.push import send_push_notifications
from app.post_call.crm_sync import is_crm_test_tenant, maybe_send_to_crm
from app.post_call.summary import summarize_conversation_lines
from app.tenants import get_tenant_by_phone
from app.tools.guards import looks_like_vague_meta_message, looks_too_short

router = APIRouter()


# Stan rezerwacji "w toku" per-rozmowa (2026-09-08) — odpowiednik call_state["booking"]/
# call_state["manage_booking"] z Gemini Live/OpenAI Realtime, ale tam to zwykły Python
# dict żyjący w pamięci jednego długo działającego procesu Pipecat (ten sam obiekt przez
# całe połączenie). Tu ElevenLabs woła nasze webhooki jako osobne, bezstanowe requesty
# HTTP przy każdym wywołaniu narzędzia — nic nie "pamięta" poprzedniej tury samo z siebie.
# Klucz: conversation_id (system__conversation_id, unikalny per-rozmowa w ElevenLabs).
# Bezpieczne założenie: serwis już działa jako jeden proces (most Vonage to żywy
# WebSocket trzymany w jednym procesie z definicji), więc globalny dict w pamięci nie
# wprowadza nowej kategorii kruchości. Sprzątane w elevenlabs_post_call niżej.
_elevenlabs_call_states: dict[str, dict] = {}


# 2026-09-14 — ElevenLabs potrafi wywołać /elevenlabs/post-call dwukrotnie dla TEJ SAMEJ
# rozmowy (potwierdzone na żywo: identyczna notatka x2 w CRM z jednego telefonu) — typowe
# zachowanie webhooków przy wolnej odpowiedzi (retry). Bez tej blokady cały handler
# (transkrypt, mail, CRM) wykonywał się drugi raz. Klucz: call_sid, pamiętany na całe
# życie procesu — to samo bezpieczne założenie co przy _elevenlabs_call_states wyżej.
_processed_post_call_sids: set[str] = set()


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
    # call_sid musi wrócić w dynamic_variables — post-call bez niego pomija cały raport.
    call_sid = body.get("call_sid") or body.get("twilio_call_sid") or ""
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

    return {
        "type": "conversation_initiation_client_data",
        "conversation_config_override": await build_agent_override(tenant, caller_id),
        # Wszystkie trzy zmienne są wymagane przez narzędzia agenta — bez nich ElevenLabs
        # odrzuca rozmowę zaraz po starcie (agent_configuration_error). Ten webhook woła
        # wyłącznie SIP direct z Vonage (Twilio dostaje dane inline), stąd channel="vonage".
        "dynamic_variables": {
            "business_name": tenant.get("name") or "",
            "caller_phone": caller_id,
            "called_number": called_number,
            "call_sid": call_sid,
            "channel": "vonage",
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
    conversation_id = body.get("conversation_id") or ""

    if not customer_name or not message:
        return {"status": "error", "reason": "missing_fields"}
    if looks_too_short(message) or looks_like_vague_meta_message(message):
        return {"status": "error", "reason": "message_too_vague"}

    tenant = await get_tenant_by_phone(called_number) if called_number else None
    if not tenant:
        return {"status": "error", "reason": "tenant_not_found"}

    # Auto-zapis imienia do portalu /crm (zakładka Klienci) — 1:1 z handle_contact_owner w
    # tools/contact_owner.py (Gemini Live/OpenAI Realtime): TYLKO gdy dla tego numeru jeszcze nie
    # ma żadnego imienia, i tylko gdy klient sam wprost je podał (nie "Nieznany"/puste).
    if customer_name and customer_name != "Nieznany":
        spawn(maybe_save_contact_name(tenant.get("id", ""), caller_phone, customer_name))

    if tenant.get("contact_owner_enabled", 1) != 1:
        # Twardy blok — narzędzie w ElevenLabs jest statycznie przypięte do agenta
        # (nie da się go usunąć per-rozmowa jak w Gemini Live/OpenAI Realtime, patrz
        # has_contact_owner w prompt/instructions.py), więc nawet gdy model je i tak wywoła
        # wbrew instrukcji w prompcie, tu odmawiamy wysyłki — to jedyne miejsce gdzie
        # ustawienie tenanta jest faktycznie wymuszone, nie tylko sugerowane tekstem.
        logger.warning(
            f"🚫 [ELEVENLABS AGENT] contact_owner wywołany mimo contact_owner_enabled=0 dla {tenant.get('name')} — odmawiam wysyłki"
        )
        return {"status": "error", "reason": "disabled"}

    to_email = tenant.get("notification_email") or tenant.get("email")
    if not to_email:
        return {"status": "error", "reason": "no_notification_email"}

    # 2026-09-09 — 1:1 z handle_contact_owner w tools/contact_owner.py: gdy ta sama firma ma TEŻ
    # raport z rozmowy (lead_email_enabled) na TEN SAM adres, odkładamy wiadomość do
    # _elevenlabs_call_states (ten sam mechanizm co booking, patrz wyżej) zamiast wysyłać
    # osobny mail teraz — elevenlabs_post_call skleja ją z podsumowaniem w JEDEN mail. Bez
    # conversation_id nie da się skorelować z post-call, więc wtedy wysyłamy od razu (bezpieczny
    # fallback — nigdy nie gubimy zgłoszenia tylko dlatego że nie możemy go połączyć).
    report_to_email = tenant.get("lead_email") or tenant.get("notification_email") or tenant.get("email")
    defer_to_report = bool(
        conversation_id
        and int(tenant.get("lead_email_enabled") or 0)
        and report_to_email
        and report_to_email == to_email
    )
    if defer_to_report:
        _elevenlabs_call_states.setdefault(conversation_id, {})["pending_contact_owner"] = {
            "customer_name": customer_name,
            "message": message,
        }
        ok = True
        logger.info(f"📞 [ELEVENLABS AGENT] contact_owner: odłożone do połączonego raportu ({to_email})")
    else:
        ok = await send_message_email(tenant, customer_name, message, caller_phone, to_email)
    result = {"status": "ok" if ok else "error"}
    closing_line = (tenant.get("contact_owner_closing_line") or "").strip()
    if ok and closing_line:
        result["say_exactly"] = closing_line
    return result


@router.post("/elevenlabs/tools/book_appointment")
async def elevenlabs_tool_book_appointment(request: Request):
    """Webhook narzędzia rezerwacji — port pod ElevenLabs, patrz docstring
    _elevenlabs_call_states wyżej po wyjaśnienie mechanizmu stanu między turami.
    Reużywa 1:1 book_appointment_step z booking/book_appointment.py (ta sama funkcja co
    Gemini Live/OpenAI Realtime), zero duplikacji logiki biznesowej/walidacji terminów."""
    if not _check_shared_secret(request):
        return {"status": "error", "reason": "unauthorized"}

    body = await request.json()
    logger.info(f"📅 [ELEVENLABS AGENT] book_appointment tool wywołany | raw={body}")

    conversation_id = body.get("conversation_id") or ""
    called_number = body.get("called_number") or body.get("to_number") or ""
    caller_phone = body.get("caller_phone") or body.get("system__caller_id") or "nieznany"
    channel = body.get("channel") or "twilio"

    tenant = await get_tenant_by_phone(called_number) if called_number else None
    if not tenant:
        return {"status": "error", "reason": "tenant_not_found"}
    if not conversation_id:
        logger.warning("⚠️ [ELEVENLABS AGENT] book_appointment bez conversation_id — stan nie przetrwa kolejnej tury")

    call_state = _elevenlabs_call_states.setdefault(conversation_id or f"_no_id_{called_number}", {})

    args = {
        "service": body.get("service"),
        "staff": body.get("staff"),
        "date_text": body.get("date_text"),
        "time_text": body.get("time_text"),
        "customer_name": body.get("customer_name"),
        "confirmation": body.get("confirmation", "none"),
        "change_field": body.get("change_field"),
        "question": body.get("question"),
        "notes": body.get("notes"),
    }
    result = await book_appointment_step(args, tenant, caller_phone, call_state, {"context": None}, channel=channel)
    return result


@router.post("/elevenlabs/tools/manage_booking")
async def elevenlabs_tool_manage_booking(request: Request):
    """Webhook odwoływania/przekładania wcześniej umówionej wizyty — analogicznie do
    elevenlabs_tool_book_appointment wyżej, reużywa manage_booking_step 1:1."""
    if not _check_shared_secret(request):
        return {"status": "error", "reason": "unauthorized"}

    body = await request.json()
    logger.info(f"📅 [ELEVENLABS AGENT] manage_booking tool wywołany | raw={body}")

    conversation_id = body.get("conversation_id") or ""
    called_number = body.get("called_number") or body.get("to_number") or ""
    caller_phone = body.get("caller_phone") or body.get("system__caller_id") or "nieznany"

    tenant = await get_tenant_by_phone(called_number) if called_number else None
    if not tenant:
        return {"status": "error", "reason": "tenant_not_found"}
    if not conversation_id:
        logger.warning("⚠️ [ELEVENLABS AGENT] manage_booking bez conversation_id — stan nie przetrwa kolejnej tury")

    call_state = _elevenlabs_call_states.setdefault(conversation_id or f"_no_id_{called_number}", {})

    args = {
        "action": body.get("action"),
        "date_text": body.get("date_text"),
        "time_text": body.get("time_text"),
        "confirmation": body.get("confirmation", "none"),
        "which_visit": body.get("which_visit"),
    }
    result = await manage_booking_step(args, tenant, caller_phone, call_state)
    return result


async def save_elevenlabs_transcript(tenant: dict, call_sid: str, transcript: list, analysis: dict) -> int:
    """1:1 wzorzec z call_logs.py::save_call_transcript (Gemini Live/OpenAI
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

    # 2026-09-09 — USUNIĘTE: wcześniej dopisywało tu analysis.transcript_summary (wbudowane
    # streszczenie ElevenLabs) jako dodatkowy wiersz call_transcripts z rolą "summary". Panel
    # (firm/[id]/page.tsx) nie ma osobnej obsługi tej roli — renderuje WSZYSTKO co nie jest
    # "user" jako "🤖 Asystent", więc to streszczenie wyglądało jak dodatkowa wypowiedź bota
    # na końcu rozmowy, choć nigdy nie padło na żywo (złapane na żywo, zgłoszone przez
    # użytkownika). Prawdziwy raport z rozmowy leci mailem niżej (summarize_conversation_lines,
    # NASZE podsumowanie z kontekstem firmy) — to tu było tylko duplikatem/śmieciem w
    # transkrypcie, nigdy nie czytanym przez żaden inny kod.

    return saved


NO_CONTENT_SUMMARY = "Brak treści rozmowy."
FAILED_SUMMARY = "Nie udało się wygenerować streszczenia."


@dataclass(frozen=True)
class PostCallPayload:
    """Dane rozmowy z webhooka post-call.

    Numer firmy i id połączenia wracają w dynamic_variables, które sami wysłaliśmy na
    starcie rozmowy (ElevenLabs odsyła je bez zmian). system__call_sid ma pierwszeństwo:
    przy SIP direct nadpisujemy go uuid Vonage (nagłówek X-CALL-ID), pod którym jest
    wpis w call_logs. phone_call.* to zapasowe źródło dla innych typów połączeń.
    """

    conversation_id: str
    called_number: str
    caller_phone: str
    call_sid: str
    duration: int
    status: str | None
    transcript: list
    analysis: dict
    dynamic_variables: dict
    keys: list

    @classmethod
    def from_body(cls, body: dict) -> PostCallPayload:
        data = body.get("data") or {}
        dyn_vars = (data.get("conversation_initiation_client_data") or {}).get("dynamic_variables") or {}
        phone_call = data.get("phone_call") or {}
        return cls(
            conversation_id=data.get("conversation_id") or "",
            called_number=dyn_vars.get("called_number") or phone_call.get("agent_number") or "",
            caller_phone=dyn_vars.get("caller_phone") or phone_call.get("external_number") or "",
            call_sid=(
                dyn_vars.get("system__call_sid")
                or dyn_vars.get("call_sid")
                or dyn_vars.get("twilio_call_sid")
                or phone_call.get("call_sid")
                or ""
            ),
            duration=int((data.get("metadata") or {}).get("call_duration_secs") or 0),
            status=data.get("status"),
            transcript=data.get("transcript") or [],
            analysis=data.get("analysis") or {},
            dynamic_variables=dyn_vars,
            keys=list(data.keys()),
        )


def _caller_display(caller_phone: str) -> str:
    if caller_phone and caller_phone.lower() not in ("nieznany", "unknown", ""):
        return caller_phone
    return "numer zastrzeżony"


def _conversation_lines(transcript: list) -> list[str]:
    """Transkrypt ElevenLabs w formacie linii wspólnym dla podsumowań wszystkich silników."""
    lines = []
    for turn in transcript:
        content = (turn.get("message") or "").strip()
        if len(content) > 2:
            label = "Asystent" if turn.get("role") == "agent" else "Klient"
            lines.append(f"{label}: {content[:200]}")
    return lines


async def _already_processed_elsewhere(tenant: dict, call_sid: str) -> bool:
    """Retry webhooka potrafi trafić na inną replikę/proces — pamięć procesu nie wystarcza,
    więc sprawdzamy, czy transkrypt tej rozmowy jest już w bazie."""
    target_db = saas_db if tenant.get("id", "").startswith("firm_") else db
    try:
        return bool(await target_db.execute("SELECT id FROM call_transcripts WHERE call_sid = ? LIMIT 1", [call_sid]))
    except Exception as e:
        logger.error(f"⚠️ [ELEVENLABS AGENT] Post-call: DB dedup check error: {e}")
        return False


def _choose_summary(
    summary: str, payload: PostCallPayload, tenant: dict, pending_contact_owner: dict | None
) -> tuple[str, bool]:
    """(tekst raportu, czy rozmowa miała realną treść — od tego zależy push)."""
    has_real_content = summary != NO_CONTENT_SUMMARY
    if summary in (NO_CONTENT_SUMMARY, FAILED_SUMMARY):
        # Wbudowane streszczenie ElevenLabs jest lepsze niż nic.
        summary = payload.analysis.get("transcript_summary") or ""
        if summary:
            has_real_content = True
    if not summary and pending_contact_owner:
        # Klient zostawił wiadomość — to prawdziwe zgłoszenie, nawet bez streszczenia.
        return "Streszczenie rozmowy niedostępne — szczegóły w zgłoszeniu powyżej.", True
    if not summary and int(tenant.get("report_empty_calls") or 0):
        summary = (
            f"Połączenie odebrane od: {_caller_display(payload.caller_phone)}. Rozmowa się nie odbyła — "
            "rozmówca nic nie powiedział lub rozłączył się bez zostawienia wiadomości."
        )
    return summary, has_real_content


@router.post("/elevenlabs/post-call")
async def elevenlabs_post_call(request: Request):
    """Po rozmowie ElevenLabs: transkrypt, podsumowanie, raport e-mail, CRM i push.

    Minut NIE naliczamy — robi to webhook statusu operatora (/twilio/status,
    /vonage/events) jak dla pozostałych silników; tutaj oznaczałoby to podwójne obciążenie.
    Webhook bywa ponawiany, więc duplikaty są odrzucane.
    """
    raw_body = await request.body()
    try:
        body = json.loads(raw_body)
    except Exception:
        logger.error(f"❌ [ELEVENLABS AGENT] Post-call: nie mogę sparsować JSON: {raw_body[:500]!r}")
        return {"status": "ignored"}

    payload = PostCallPayload.from_body(body)
    logger.info(
        f"📊 [ELEVENLABS AGENT] Post-call: {payload.called_number} ({payload.call_sid}, {payload.duration}s, "
        f"status={payload.status}, {len(payload.transcript)} tur) "
        f"| signature={request.headers.get('elevenlabs-signature', '')!r}"
    )

    # Rozmowa skończona — sprzątamy jej stan; odłożona wiadomość contact_owner trafi do raportu.
    pending_contact_owner = (_elevenlabs_call_states.pop(payload.conversation_id, None) or {}).get(
        "pending_contact_owner"
    )

    if not payload.call_sid or not payload.called_number:
        logger.warning(
            f"⚠️ [ELEVENLABS AGENT] Post-call: brak call_sid/called_number w payloadzie, pomijam. "
            f"data.keys()={payload.keys} dynamic_variables={payload.dynamic_variables}"
        )
        return {"status": "ignored"}
    if payload.call_sid in _processed_post_call_sids:
        logger.warning(
            f"⚠️ [ELEVENLABS AGENT] Post-call: {payload.call_sid} już przetworzony, pomijam duplikat webhooka"
        )
        return {"status": "duplicate_ignored"}
    _processed_post_call_sids.add(payload.call_sid)

    tenant = await get_tenant_by_phone(payload.called_number)
    if not tenant:
        logger.warning(f"⚠️ [ELEVENLABS AGENT] Post-call: nie znaleziono tenanta dla {payload.called_number}")
        return {"status": "ignored"}
    if await _already_processed_elsewhere(tenant, payload.call_sid):
        logger.warning(
            f"⚠️ [ELEVENLABS AGENT] Post-call: {payload.call_sid} ma już transkrypt w DB, "
            "pomijam duplikat webhooka (inna replika/restart)"
        )
        return {"status": "duplicate_ignored"}

    saved = await save_elevenlabs_transcript(tenant, payload.call_sid, payload.transcript, payload.analysis)
    logger.info(f"📝 [ELEVENLABS AGENT] Transcript saved: {saved} wiadomości ({payload.call_sid})")

    conversation_lines = _conversation_lines(payload.transcript)
    summary, has_real_content = _choose_summary(
        await summarize_conversation_lines(conversation_lines, tenant), payload, tenant, pending_contact_owner
    )
    await _deliver_report(tenant, payload, summary, has_real_content, conversation_lines, pending_contact_owner)
    return {"status": "ok"}


async def _deliver_report(
    tenant: dict,
    payload: PostCallPayload,
    summary: str,
    has_real_content: bool,
    conversation_lines: list[str],
    pending_contact_owner: dict | None,
) -> None:
    caller = payload.caller_phone or "nieznany"
    lead_email_enabled = int(tenant.get("lead_email_enabled") or 0)
    to_email = tenant.get("lead_email") or tenant.get("notification_email") or tenant.get("email")
    report_sent = bool(lead_email_enabled and to_email and summary)

    if summary:
        await persist_call_summary(tenant, payload.call_sid, summary)
    if report_sent:
        ok = await send_call_summary_email(
            tenant,
            caller,
            summary,
            to_email,
            pending_message=pending_contact_owner,
            transcript_lines=conversation_lines if int(tenant.get("transcript_email_enabled") or 0) else None,
        )
        logger.info(f"📧 [ELEVENLABS AGENT] Raport z rozmowy: {'wysłany' if ok else 'błąd wysyłki'} do {to_email}")
    if summary and is_crm_test_tenant(tenant):
        await maybe_send_to_crm(tenant, caller, summary)
    if not report_sent and pending_contact_owner:
        # Wiadomość odłożyliśmy do raportu, który jednak nie poszedł — wysyłamy ją osobno.
        fallback_to = tenant.get("notification_email") or tenant.get("email")
        if fallback_to:
            await send_message_email(
                tenant,
                pending_contact_owner.get("customer_name") or "Nieznany",
                pending_contact_owner.get("message") or "",
                caller,
                fallback_to,
            )
            logger.warning("📧 [ELEVENLABS AGENT] pending_contact_owner: raport nie poleciał, wysłano awaryjnie osobno")
    if has_real_content and summary:
        await send_push_notifications(
            tenant, title="📞 Nowe zgłoszenie", body=f"{_caller_display(payload.caller_phone)}: {summary}"
        )
