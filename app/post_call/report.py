"""Obsługa końca rozmowy w silnikach Pipecat: podsumowanie, zapis, e-mail, CRM, push."""

from pipecat.processors.aggregators.llm_context import LLMContext

from app.call_logs import persist_call_summary
from app.notifications.email import send_call_summary_email
from app.notifications.push import crm_call_url, send_push_notifications
from app.post_call.crm_sync import is_crm_test_tenant, maybe_send_to_crm
from app.post_call.summary import extract_conversation_lines, generate_conversation_summary


async def maybe_send_call_summary(
    tenant: dict,
    caller_phone: str,
    context: LLMContext,
    call_state: dict | None = None,
    call_sid: str | None = None,
) -> None:
    """Woła się w finally: bloku websocket handlera — PO KAŻDEJ rozmowie, niezależnie jak się
    skończyła (cisza, limit czasu, contact_owner, end_conversation, zwykłe rozłączenie).
    Streszczenie liczy się ZAWSZE (patrz 2026-09-23 niżej) — mail/webhook CRM zostają
    bramkowane jak wcześniej ustawieniami panelu.

    call_state: gdy handle_contact_owner (tej samej rozmowy) odłożył wiadomość dla właściciela
    (patrz komentarz 2026-09-09 tam) bo raport i "email do powiadomień" wskazują na TEN SAM
    adres — doklejamy ją tu do JEDNEGO maila zamiast wysyłać osobno. Gdy call_state=None albo
    bez odłożonej wiadomości — zachowanie identyczne jak wcześniej.

    call_sid: 2026-09-23 — gdy podane, streszczenie + priorytet zapisują się też do wiersza
    call_logs (portal /crm dla klienta, patrz call_logs.py::persist_call_summary) — WYMAGA żeby
    save_call_transcript() (tworzy wiersz call_logs) wykonało się PRZED tym wywołaniem, inaczej
    UPDATE trafia w pustkę (patrz kolejność w engines/common.py::finalize_call)."""
    lead_email_enabled = int(tenant.get("lead_email_enabled") or 0)
    to_email = tenant.get("lead_email") or tenant.get("notification_email") or tenant.get("email")
    pending = (call_state or {}).get("pending_contact_owner")
    crm_enabled = is_crm_test_tenant(tenant)
    # 2026-09-23 — USUNIĘTE: wczesny return gdy ani lead_email_enabled ani crm_enabled (Pipedrive
    # test tenant) nie są włączone. Streszczenie zasila teraz TEŻ portal /crm (call_logs.summary/
    # priority) niezależnie od tych dwóch przełączników, więc musi liczyć się zawsze — koszt
    # jednego dodatkowego wywołania GPT-4.1-mini na rozmowę, akceptowalny (call_logs to dziś
    # główne źródło danych dla klienckiego CRM, nie tylko dodatek do maila).
    summary = await generate_conversation_summary(context, tenant)
    # 2026-09-25 — zapamiętaj PRZED podmianą summary niżej na tekst pustej rozmowy: push
    # leci TYLKO dla rozmów z realną treścią (GPT faktycznie coś streścił, LUB jest
    # odłożona wiadomość z contact_owner — patrz komentarz "pending" wyżej, to też realny
    # lead mimo że transkrypt był za krótki dla GPT), NIE za każde ciche rozłączenie —
    # świadoma decyzja, żeby nie zalewać telefonu właściciela szumem.
    has_real_content = (summary != "Brak treści rozmowy.") or bool(pending)
    if summary == "Brak treści rozmowy.":
        # 2026-09-09 — domyślnie nadal pomijamy (większość firm nie chce maila za KAŻDE
        # rozłączenie bez słowa). Nowy przełącznik per-firma (QFX Group, na żądanie
        # klienta: "chciałbym otrzymywać informację o KAŻDYM połączeniu przychodzącym,
        # również wtedy, gdy rozmówca niczego nie pozostawi") — gdy włączony, wysyłamy
        # krótki raport zamiast całkiem pomijać. caller_phone bywa pusty/"nieznany" dla
        # połączeń z zastrzeżonym numerem — pokazujemy to jawnie, nie fałszywy numer.
        # Wyjątek: jest odłożona wiadomość z contact_owner — to NIE jest pusta rozmowa,
        # transkrypt po prostu nie zawierał wystarczająco treści dla GPT, ale realny lead
        # istnieje i musi trafić do właściciela.
        if not pending and not int(tenant.get("report_empty_calls") or 0):
            return
        if not pending:
            caller_display = (
                caller_phone
                if caller_phone and caller_phone.lower() not in ("nieznany", "unknown", "")
                else "numer zastrzeżony"
            )
            summary = f"Połączenie odebrane od: {caller_display}. Rozmowa się nie odbyła — rozmówca nic nie powiedział lub rozłączył się bez zostawienia wiadomości."
        else:
            summary = "Streszczenie rozmowy niedostępne — szczegóły w zgłoszeniu powyżej."
    if call_sid:
        await persist_call_summary(tenant, call_sid, summary)
    if lead_email_enabled and to_email:
        transcript_lines = (
            extract_conversation_lines(context) if int(tenant.get("transcript_email_enabled") or 0) else None
        )
        await send_call_summary_email(
            tenant, caller_phone, summary, to_email, pending_message=pending, transcript_lines=transcript_lines
        )
    if crm_enabled:
        await maybe_send_to_crm(tenant, caller_phone, summary)
    if has_real_content:
        caller_display = (
            caller_phone
            if caller_phone and caller_phone.lower() not in ("nieznany", "unknown", "")
            else "numer zastrzeżony"
        )
        await send_push_notifications(
            tenant,
            title="📞 Nowe zgłoszenie",
            body=f"{caller_display}: {summary}",
            url=crm_call_url(call_sid),
        )
