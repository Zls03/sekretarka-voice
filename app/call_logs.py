"""Zapis rozmów w bazie: transkrypt (call_transcripts) i podsumowanie (call_logs)."""

import time
import uuid

from loguru import logger
from pipecat.processors.aggregators.llm_context import LLMContext

from app.billing import apply_call_charge
from app.db import db, saas_db
from app.post_call.summary import _parse_summary_fields

#
# 1:1 z cascade (bot.py::save_call_log + bot.py::apply_call_charge) — te same tabele
# (call_logs, call_transcripts), te same kolumny, ta sama logika naliczania. Dzięki temu
# zakładka "Logi połączeń" w panelu pokazuje rozmowy Realtime BEZ ŻADNYCH zmian w UI —
# panel nie wie i nie musi wiedzieć że to inny silnik pod spodem.
#
# Dwie fazy zapisu, tak jak w cascade:
#   1. save_call_transcript() — wołane w finally: websocket handlera (ma dostęp do
#      LLMContext z pełną rozmową). Tworzy wiersz call_logs (duration=0, status='in_progress')
#      + wszystkie wiersze call_transcripts.
#   2. apply_call_charge() — wołane z /vonage/events gdy Vonage potwierdzi realny czas
#      trwania połączenia (UPDATE tego samego wiersza call_logs + odjęcie kredytów/minut).
#   Rozdzielone bo to dwa różne, niezależne od siebie w czasie zdarzenia (koniec pipeline'u
#   vs. webhook od Vonage) — dokładnie tak jak w cascade, nie uproszczenie.
_call_logs_columns_ensured = False


async def _ensure_call_logs_columns() -> None:
    """Jednorazowo (per proces/cold start) dokłada kolumny summary/priority/seen do call_logs
    w SaaS DB — te same rozmowy co dziś idą do maila teraz zasilają też portal /crm (bizvoice-panel).
    Wzorzec identyczny jak ensureColumns() w bizvoice-panel/api/firms/[id]/route.ts (ALTER w
    try/except, bezpieczne do powtarzania — TursoDB.execute i tak łyka błąd i loguje go, nie
    podnosi wyjątku, ale global flag oszczędza redundantne wywołania po pierwszym udanym/nieudanym
    razie w życiu procesu)."""
    global _call_logs_columns_ensured
    if _call_logs_columns_ensured:
        return
    _call_logs_columns_ensured = True
    for sql in (
        "ALTER TABLE call_logs ADD COLUMN summary TEXT",
        "ALTER TABLE call_logs ADD COLUMN priority TEXT",
        "ALTER TABLE call_logs ADD COLUMN seen INTEGER DEFAULT 0",
        # 2026-09-28 — "najpierw dzwoni do właściciela" v2: 'owner' gdy rozmowę odebrał
        # osobiście właściciel (przez Siperb, patrz process_human_first_recording), NULL/brak
        # dla zwykłych rozmów z sekretarką AI. Steruje znaczkiem w portalu /crm.
        "ALTER TABLE call_logs ADD COLUMN answered_by TEXT",
    ):
        await saas_db.execute(sql)


async def persist_call_summary(tenant: dict, call_sid: str, summary: str) -> None:
    """Zapisuje streszczenie+priorytet do wiersza call_logs (musi już istnieć — patrz
    save_call_transcript/save_elevenlabs_transcript, wołane WCZEŚNIEJ w tym samym finally: bloku).
    Tylko SaaS (portal /crm dotyczy firm_ tenantów) — dla starych tenantów admina to no-op.
    Best-effort: błąd nie może wywrócić wysyłki maila/webhooka, które dzieją się zaraz po tym.
    Priorytet parsowany tu (nie przez wywołujących) żeby WSZYSTKIE 3 silniki (Gemini Live,
    OpenAI Realtime, ElevenLabs) zapisywały identycznie, jednym wspólnym kodem.

    2026-09-24 — tu też, przy okazji KAŻDEJ rozmowy (nie tylko gdy admin akurat otworzy
    zakładkę Statystyki/Logi danej firmy — poprzedni, niepewny wyzwalacz w
    bizvoice-panel/api/firms/[id]/stats/route.ts), czyścimy stare call_transcripts (surowy,
    słowo-w-słowo zapis — bardziej wrażliwe dane niż samo streszczenie, więc krótsza retencja
    ma sens). call_logs (lekki rekord: telefon/streszczenie/priorytet — realny rekord CRM,
    portal /crm i "stały klient" na nim polegają) NIE jest tu kasowany — dłuższa retencja
    ustawiona osobno w panelu (365 dni zamiast 30, patrz stats/route.ts)."""
    tenant_id = tenant.get("id", "")
    if not tenant_id.startswith("firm_") or not call_sid:
        return
    try:
        priority = _parse_summary_fields(summary).get("Priorytet") or ""
        await _ensure_call_logs_columns()
        await saas_db.execute(
            "UPDATE call_logs SET summary = ?, priority = ? WHERE call_sid = ?",
            [summary, priority, call_sid],
        )
        await saas_db.execute(
            "DELETE FROM call_transcripts WHERE tenant_id = ? AND created_at < datetime('now', '-30 days')",
            [tenant_id],
        )
    except Exception as e:
        logger.error(f"[CRM] persist_call_summary error: {e}")


async def save_call_transcript(tenant: dict, call_sid: str, caller_phone: str, context: LLMContext) -> None:
    """Zapisuje wiersz call_logs (in_progress) + transkrypt do call_transcripts.
    1:1 z bot.py::save_call_log, tylko czyta LLMContext zamiast flow_manager.get_current_context()."""
    if not call_sid:
        logger.warning("📊 [REALTIME TEST] Brak call_sid — pomijam zapis transkryptu/logu")
        return
    tenant_id = tenant.get("id", "")
    if not tenant_id:
        return

    is_saas = tenant_id.startswith("firm_")
    target_db = saas_db if is_saas else db

    try:
        existing = await target_db.execute("SELECT id FROM call_logs WHERE call_sid = ?", [call_sid])
        if not existing:
            await target_db.execute(
                """INSERT INTO call_logs
                   (id, tenant_id, call_sid, caller_phone, duration_seconds, status, created_at)
                   VALUES (?, ?, ?, ?, 0, 'in_progress', datetime('now'))""",
                [f"call_{int(time.time())}", tenant_id, call_sid, caller_phone],
            )
            logger.info(f"📊 [REALTIME TEST] Call log created: {call_sid} ({'saas' if is_saas else 'admin'})")
    except Exception as e:
        logger.error(f"[REALTIME TEST] Call log create error: {e}")
        return

    try:
        messages = context.get_messages() if context else []
        saved_contents = set()
        saved_count = 0
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")
            if role not in ("user", "assistant") or not content:
                continue
            if isinstance(content, list):
                content = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("text"))
            content = (content or "").strip()
            if len(content) < 2:
                continue
            content_key = f"{role}:{content[:100]}"
            if content_key in saved_contents:
                continue
            saved_contents.add(content_key)

            transcript_id = f"tr_{uuid.uuid4().hex[:12]}"
            await target_db.execute(
                """INSERT INTO call_transcripts
                   (id, tenant_id, call_sid, role, content, created_at)
                   VALUES (?, ?, ?, ?, ?, datetime('now'))""",
                [transcript_id, tenant_id, call_sid, role, content[:500]],
            )
            saved_count += 1
        logger.info(f"📝 [REALTIME TEST] Transcript saved: {saved_count} messages")
    except Exception as e:
        logger.error(f"[REALTIME TEST] Transcript save error: {e}")


async def record_call_status(
    tenant: dict,
    call_sid: str,
    caller_phone: str,
    duration: int,
    status: str,
    log_tag: str,
) -> None:
    """Zapisuje czas trwania i status zakończonej rozmowy (webhook statusu operatora) i ją rozlicza.

    Webhook statusu i save_call_transcript() (koniec websocketu) przychodzą w dowolnej
    kolejności — kto pierwszy, ten tworzy wiersz call_logs, drugi go tylko uzupełnia.
    Dlatego tu zapisujemy prawdziwy numer dzwoniącego, a nie zaślepkę.
    """
    tenant_id = tenant["id"]
    is_saas_tenant = tenant.get("source") == "saas"
    target_db = saas_db if is_saas_tenant else db

    existing = await target_db.execute("SELECT id FROM call_logs WHERE call_sid = ?", [call_sid])
    if existing:
        await target_db.execute(
            "UPDATE call_logs SET duration_seconds = ?, status = ? WHERE call_sid = ?",
            [duration, status, call_sid],
        )
        logger.info(f"📊 [{log_tag}] Updated call log: {call_sid} → {duration}s")
    else:
        await target_db.execute(
            """INSERT INTO call_logs
               (id, tenant_id, call_sid, caller_phone, duration_seconds, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
            [f"call_{int(time.time())}", tenant_id, call_sid, caller_phone, duration, status],
        )
        logger.info(f"📊 [{log_tag}] Created call log: {call_sid} → {duration}s")

    await apply_call_charge(tenant_id, is_saas_tenant, call_sid, status, duration)
