"""Webhooki wołane przez ElevenLabs: personalizacja, narzędzia w trakcie rozmowy, post-call."""

import json
import uuid

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
    # realtime_tools.py (Gemini Live/OpenAI Realtime): TYLKO gdy dla tego numeru jeszcze nie
    # ma żadnego imienia, i tylko gdy klient sam wprost je podał (nie "Nieznany"/puste).
    if customer_name and customer_name != "Nieznany":
        spawn(maybe_save_contact_name(tenant.get("id", ""), caller_phone, customer_name))

    if tenant.get("contact_owner_enabled", 1) != 1:
        # Twardy blok — narzędzie w ElevenLabs jest statycznie przypięte do agenta
        # (nie da się go usunąć per-rozmowa jak w Gemini Live/OpenAI Realtime, patrz
        # has_contact_owner w realtime_prompt.py), więc nawet gdy model je i tak wywoła
        # wbrew instrukcji w prompcie, tu odmawiamy wysyłki — to jedyne miejsce gdzie
        # ustawienie tenanta jest faktycznie wymuszone, nie tylko sugerowane tekstem.
        logger.warning(
            f"🚫 [ELEVENLABS AGENT] contact_owner wywołany mimo contact_owner_enabled=0 dla {tenant.get('name')} — odmawiam wysyłki"
        )
        return {"status": "error", "reason": "disabled"}

    to_email = tenant.get("notification_email") or tenant.get("email")
    if not to_email:
        return {"status": "error", "reason": "no_notification_email"}

    # 2026-09-09 — 1:1 z handle_contact_owner w realtime_tools.py: gdy ta sama firma ma TEŻ
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
    Reużywa 1:1 book_appointment_step z realtime_booking.py (ta sama funkcja co
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

    # 2026-09-09 — USUNIĘTE: wcześniej dopisywało tu analysis.transcript_summary (wbudowane
    # streszczenie ElevenLabs) jako dodatkowy wiersz call_transcripts z rolą "summary". Panel
    # (firm/[id]/page.tsx) nie ma osobnej obsługi tej roli — renderuje WSZYSTKO co nie jest
    # "user" jako "🤖 Asystent", więc to streszczenie wyglądało jak dodatkowa wypowiedź bota
    # na końcu rozmowy, choć nigdy nie padło na żywo (złapane na żywo, zgłoszone przez
    # użytkownika). Prawdziwy raport z rozmowy leci mailem niżej (summarize_conversation_lines,
    # NASZE podsumowanie z kontekstem firmy) — to tu było tylko duplikatem/śmieciem w
    # transkrypcie, nigdy nie czytanym przez żaden inny kod.

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
    # 2026-09-10 — system__call_sid sprawdzane NAJPIERW: dla SIP direct (vonage_answer_
    # gemini_live) wstrzykujemy UUID Vonage jako nagłówek SIP X-CALL-ID (zarezerwowany przez
    # ElevenLabs, nadpisuje ich własny system__call_sid), żeby ID rozmowy w tym webhooku
    # zgadzało się z UUID pod którym /vonage/events zakłada wpis w call_logs — bez tego
    # transkrypt zapisywał się pod WEWNĘTRZNYM call_sid ElevenLabs (SCL_xxx), którego panel
    # nigdy nie znajdował przy wyświetlaniu historii rozmowy dla danego wpisu w logu połączeń.
    call_sid = (
        dyn_vars.get("system__call_sid")
        or dyn_vars.get("call_sid")
        or dyn_vars.get("twilio_call_sid")
        or phone_call.get("call_sid")
        or ""
    )
    duration = int(metadata.get("call_duration_secs") or 0)
    transcript = data.get("transcript") or []

    logger.info(
        f"📊 [ELEVENLABS AGENT] Post-call: {called_number} ({call_sid}, {duration}s, "
        f"status={data.get('status')}, {len(transcript)} tur) | signature={signature_header!r}"
    )

    # Sprzątanie stanu rezerwacji "w toku" (patrz _elevenlabs_call_states wyżej) — rozmowa
    # się skończyła, ewentualny niedokończony booking i tak trzeba by zaczynać od nowa.
    # Zanim posprzątamy: wyciągamy ewentualną odłożoną wiadomość z contact_owner (patrz
    # 2026-09-09 w elevenlabs_tool_contact_owner) — doklejamy ją do raportu niżej zamiast
    # wysyłać osobny mail.
    _pending_call_state = _elevenlabs_call_states.pop(data.get("conversation_id") or "", None)
    pending_contact_owner = (_pending_call_state or {}).get("pending_contact_owner")

    if not call_sid or not called_number:
        logger.warning(
            f"⚠️ [ELEVENLABS AGENT] Post-call: brak call_sid/called_number w payloadzie, pomijam. "
            f"data.keys()={list(data.keys())} dynamic_variables={dyn_vars}"
        )
        return {"status": "ignored"}

    if call_sid in _processed_post_call_sids:
        logger.warning(f"⚠️ [ELEVENLABS AGENT] Post-call: {call_sid} już przetworzony, pomijam duplikat webhooka")
        return {"status": "duplicate_ignored"}
    _processed_post_call_sids.add(call_sid)

    tenant = await get_tenant_by_phone(called_number)
    if not tenant:
        logger.warning(f"⚠️ [ELEVENLABS AGENT] Post-call: nie znaleziono tenanta dla {called_number}")
        return {"status": "ignored"}

    # 2026-09-18 — powyższy _processed_post_call_sids (in-memory) NIE wystarczał: potwierdzone
    # na żywo (3 realne testy, zawsze dokładnie 2 identyczne notatki w CRM) że retry webhooka
    # z ElevenLabs potrafi trafić na INNY proces/replikę Railway niż ten, który obsłużył
    # pierwsze wywołanie — założenie "to jeden długo działający proces" (patrz komentarz przy
    # _elevenlabs_call_states wyżej) okazało się fałszywe dla post-call, mimo że bezpieczne dla
    # stanu W TRAKCIE jednej rozmowy (tam most WebSocket faktycznie trzyma jeden proces).
    # Sprawdzamy więc DODATKOWO trwały stan w DB (call_transcripts przeżywa restart/inną
    # replikę) — jeśli transkrypt dla tego call_sid już istnieje, ktoś (inny proces) już to
    # przetworzył. Nieidealne (race dwóch request'ów w tej samej milisekundzie wciąż możliwy),
    # ale naprawia realny, powtarzalny przypadek z testów zamiast tylko teoretyczny.
    is_saas = tenant.get("id", "").startswith("firm_")
    target_db = saas_db if is_saas else db
    try:
        already_saved = await target_db.execute(
            "SELECT id FROM call_transcripts WHERE call_sid = ? LIMIT 1", [call_sid]
        )
    except Exception as e:
        logger.error(f"⚠️ [ELEVENLABS AGENT] Post-call: DB dedup check error: {e}")
        already_saved = None
    if already_saved:
        logger.warning(
            f"⚠️ [ELEVENLABS AGENT] Post-call: {call_sid} ma już transkrypt w DB, pomijam duplikat webhooka (inna replika/restart)"
        )
        return {"status": "duplicate_ignored"}

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
    # 2026-09-25 — patrz identyczny komentarz/flaga w realtime_tools.py::maybe_send_call_summary
    # (push leci TYLKO dla rozmów z realną treścią, nie za każde ciche połączenie). Zapamiętane
    # PRZED podmianami niżej, bo "Brak treści rozmowy." zaraz zostanie nadpisane fallbackiem.
    has_real_content = summary != "Brak treści rozmowy."
    if summary == "Brak treści rozmowy." or summary == "Nie udało się wygenerować streszczenia.":
        # Zapasowo — wbudowane streszczenie ElevenLabs lepsze niż nic, gdyby nasze zawiodło.
        summary = analysis.get("transcript_summary") or ""
        if summary:
            has_real_content = True
    if not summary and pending_contact_owner:
        # 2026-09-09 — odłożona wiadomość z contact_owner to NIE pusta rozmowa, transkrypt po
        # prostu nie dał GPT wystarczająco treści — realny lead istnieje, musi trafić do maila.
        summary = "Streszczenie rozmowy niedostępne — szczegóły w zgłoszeniu powyżej."
        has_real_content = True
    elif not summary and int(tenant.get("report_empty_calls") or 0):
        # 2026-09-09 — patrz identyczny komentarz w realtime_tools.py::maybe_send_call_summary
        # (QFX Group: raport nawet dla połączeń bez treści, zamiast pomijać całkiem).
        caller_display = (
            caller_phone
            if caller_phone and caller_phone.lower() not in ("nieznany", "unknown", "")
            else "numer zastrzeżony"
        )
        summary = f"Połączenie odebrane od: {caller_display}. Rozmowa się nie odbyła — rozmówca nic nie powiedział lub rozłączył się bez zostawienia wiadomości."
    if summary:
        await persist_call_summary(tenant, call_sid, summary)
    if lead_email_enabled and to_email and summary:
        ok = await send_call_summary_email(
            tenant,
            caller_phone or "nieznany",
            summary,
            to_email,
            pending_message=pending_contact_owner,
            transcript_lines=conversation_lines if int(tenant.get("transcript_email_enabled") or 0) else None,
        )
        logger.info(f"📧 [ELEVENLABS AGENT] Raport z rozmowy: {'wysłany' if ok else 'błąd wysyłki'} do {to_email}")
    if summary and is_crm_test_tenant(tenant):
        # Patrz CLAUDE.md "CRM Integration" i identyczny hook w
        # realtime_tools.py::maybe_send_call_summary — POC ograniczony do numeru
        # demo BizVoice, niezależny od lead_email_enabled.
        await maybe_send_to_crm(tenant, caller_phone or "nieznany", summary)
    if not (lead_email_enabled and to_email and summary) and pending_contact_owner:
        # Awaryjny fallback — odłożyliśmy wiadomość zakładając że poleci tu razem z raportem,
        # ale coś się zmieniło (np. lead_email_enabled wyłączone w trakcie rozmowy) — wyślij
        # ją osobno, żeby zgłoszenie nie przepadło.
        fallback_to = tenant.get("notification_email") or tenant.get("email")
        if fallback_to:
            await send_message_email(
                tenant,
                pending_contact_owner.get("customer_name") or "Nieznany",
                pending_contact_owner.get("message") or "",
                caller_phone or "nieznany",
                fallback_to,
            )
            logger.warning("📧 [ELEVENLABS AGENT] pending_contact_owner: raport nie poleciał, wysłano awaryjnie osobno")

    if has_real_content and summary:
        caller_display = (
            caller_phone
            if caller_phone and caller_phone.lower() not in ("nieznany", "unknown", "")
            else "numer zastrzeżony"
        )
        await send_push_notifications(
            tenant,
            title="📞 Nowe zgłoszenie",
            body=f"{caller_display}: {summary}",
        )

    return {"status": "ok"}
