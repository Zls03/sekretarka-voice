"""E-maile do właściciela firmy (Resend): wiadomość od klienta, raport z rozmowy, nieodebrany transfer."""

from loguru import logger

from app.config import settings
from app.telephony.vonage_api import TRANSFER_RING_TIMEOUT


async def send_message_email(tenant: dict, customer_name: str, message: str, phone: str, to_email: str) -> bool:
    """Wyślij email z wiadomością do właściciela. Uproszczona kopia cascade::send_message_email
    (bez GPT-streszczenia kontekstu rozmowy — bonus, nie rdzeń funkcji)."""
    resend_api_key = settings.resend_api_key
    if not resend_api_key:
        logger.warning("📧 [REALTIME TEST] RESEND_API_KEY nieskonfigurowany — nie wysyłam")
        return False

    business_name = tenant.get("name", "Firma")
    html_content = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px;">
        <h2 style="color: #333;">📞 Nowa wiadomość od klienta</h2>
        <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
            <tr><td style="padding: 8px; border-bottom: 1px solid #eee; width: 120px;"><strong>Firma:</strong></td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;">{business_name}</td></tr>
            <tr><td style="padding: 8px; border-bottom: 1px solid #eee;"><strong>Od:</strong></td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;">{customer_name}</td></tr>
            <tr><td style="padding: 8px; border-bottom: 1px solid #eee;"><strong>Telefon:</strong></td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;"><a href="tel:{phone}">{phone}</a></td></tr>
        </table>
        <p><strong>💬 Wiadomość:</strong></p>
        <p style="background: #f5f5f5; padding: 15px; border-radius: 5px;">{message}</p>
        <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
        <p style="color: #999; font-size: 12px;">Wiadomość przekazana przez asystenta głosowego (test Realtime) • {business_name}</p>
    </div>
    """
    try:
        import httpx

        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {resend_api_key}", "Content-Type": "application/json"},
                json={
                    "from": "Voice AI <noreply@bizvoice.pl>",
                    "to": [to_email],
                    "subject": f"📞 Wiadomość od {customer_name} - {business_name}",
                    "html": html_content,
                },
                timeout=10.0,
            )
            if response.status_code == 200:
                logger.info("📧 [REALTIME TEST] Email wysłany")
                return True
            logger.error(f"📧 [REALTIME TEST] Resend error: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logger.error(f"📧 [REALTIME TEST] Send email error: {e}")
        return False


async def send_call_summary_email(
    tenant: dict,
    caller_phone: str,
    summary: str,
    to_email: str,
    pending_message: dict | None = None,
    transcript_lines: list[str] | None = None,
) -> bool:
    """Email z raportem PO KAŻDEJ rozmowie. Jedyny mechanizm "zgłoszeniowy" od 2026-09-03
    (submit_lead usunięty — patrz docstring generate_conversation_summary) — summary jest
    teraz strukturalnym, punktowym podsumowaniem per-firma (nie prostym 2-3-zdaniowym),
    stąd white-space:pre-line żeby punkty "-" z GPT renderowały się jako osobne linie.

    pending_message: 2026-09-09 — gdy contact_owner odłożył wiadomość dla właściciela (patrz
    handle_contact_owner) bo firma ma raport włączony na TEN SAM adres, treść trafia tu jako
    osobna, wyróżniona sekcja NAD podsumowaniem — dosłowna (nie przepuszczona przez GPT),
    żeby nie zgubić/nie sparafrazować tego co klient faktycznie powiedział.

    transcript_lines: 2026-09-23 — pełny zapis rozmowy ("Klient: .../Asystent: ..."), ta sama
    lista co idzie do GPT na podsumowanie (extract_conversation_lines/conversation_lines).
    Renderowany jako "dymki" (osobny blok na turę, kolor per rozmówca) pod podsumowaniem —
    zero kosztu (te dane i tak już mamy w pamięci po zakończeniu rozmowy, żadnego dodatkowego
    wywołania). Zastępuje nagranie audio dla klientów którzy chcą zweryfikować co dokładnie
    padło w rozmowie, bez kwestii RODO związanych z nagraniem głosu (to sam tekst, ten sam co
    i tak widać w zakładce "Logi" w panelu). ŚWIADOMIE bez <details>/zwijania — sprawdzone na
    żywo (Gmail web) że klienci poczty ignorują ten tag i renderują zawartość zawsze rozwiniętą,
    więc lepiej postawić na czytelne, zawsze widoczne dymki niż pozorne zwijanie."""
    resend_api_key = settings.resend_api_key
    if not resend_api_key:
        logger.warning("📋 [REALTIME TEST] RESEND_API_KEY nieskonfigurowany — nie wysyłam raportu")
        return False

    import html as _html
    from datetime import datetime as _datetime
    from zoneinfo import ZoneInfo as _ZoneInfo

    call_time_str = _datetime.now(_ZoneInfo("Europe/Warsaw")).strftime("%d.%m.%Y, %H:%M")

    transcript_block = ""
    if transcript_lines:
        turns_html = ""
        for line in transcript_lines:
            is_client = line.startswith("Klient:")
            speaker = "Klient" if is_client else "Asystent"
            text = _html.escape(line.split(":", 1)[1].strip() if ":" in line else line)
            bg, border, label_color, indent = (
                ("#e8f4fd", "#2196F3", "#1565c0", "24px") if is_client else ("#f2f2f2", "#9e9e9e", "#616161", "0")
            )
            turns_html += f"""
            <div style="background: {bg}; border-left: 3px solid {border}; border-radius: 6px; padding: 8px 12px; margin: 6px 0; margin-left: {indent}; font-size: 13px; line-height: 1.5;">
                <strong style="color: {label_color}; font-size: 11px; text-transform: uppercase;">{speaker}</strong><br>{text}
            </div>
            """
        transcript_block = f"""
        <div style="margin: 20px 0;">
            <p style="font-weight: bold; color: #333; margin-bottom: 8px;">📝 Pełny zapis rozmowy</p>
            {turns_html}
        </div>
        """

    business_name = tenant.get("name", "Firma")
    lead_block = ""
    subject = f"📞 Raport z rozmowy — {business_name}"
    if pending_message:
        cn = pending_message.get("customer_name") or "Nieznany"
        msg = pending_message.get("message") or ""
        lead_block = f"""
        <p><strong>📨 Zgłoszenie dla właściciela:</strong></p>
        <p style="background: #fff8e1; padding: 15px; border-radius: 5px; border-left: 4px solid #ffc107;">
            <strong>{cn}</strong><br>{msg}
        </p>
        """
        subject = f"📨 Zgłoszenie + raport z rozmowy — {business_name}"
    html_content = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px;">
        <h2 style="color: #333;">📞 Raport z rozmowy</h2>
        <p style="color: #666; margin-top: -10px;">{business_name}</p>
        {lead_block}
        <p><strong>📋 Podsumowanie:</strong></p>
        <p style="background: #e8f4fd; padding: 15px; border-radius: 5px; border-left: 4px solid #2196F3; white-space: pre-line;">{summary}</p>
        <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
            <tr><td style="padding: 8px; border-bottom: 1px solid #eee; width: 120px;"><strong>Telefon:</strong></td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;"><a href="tel:{caller_phone}">{caller_phone}</a></td></tr>
            <tr><td style="padding: 8px; border-bottom: 1px solid #eee; width: 120px;"><strong>Data i godzina:</strong></td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;">{call_time_str}</td></tr>
        </table>
        {transcript_block}
        <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
        <p style="color: #999; font-size: 12px;">Automatyczny raport rozmowy — asystent głosowy (test Realtime) • {business_name}</p>
    </div>
    """
    try:
        import httpx

        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {resend_api_key}", "Content-Type": "application/json"},
                json={
                    "from": "Voice AI <noreply@bizvoice.pl>",
                    "to": [to_email],
                    "subject": subject,
                    "html": html_content,
                },
                timeout=10.0,
            )
            if response.status_code == 200:
                logger.info("📋 [REALTIME TEST] Raport z rozmowy wysłany")
                return True
            logger.error(f"📋 [REALTIME TEST] Resend error: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logger.error(f"📋 [REALTIME TEST] Send summary email error: {e}")
        return False


async def send_missed_transfer_email(business_name: str, caller_phone: str, to_email: str) -> bool:
    """Email gdy próba żywego przekierowania (transfer_to_owner) skończyła się timeout/busy/
    rejected/failed/unanswered — patrz /vonage/transfer-fallback (telephony/vonage.py), które
    woła tę funkcję. Klient w tym samym momencie słyszy zapowiedź (NCCO zwrócone z tego
    webhooka) że wiadomość zostanie przekazana — to jest ta wiadomość, więc właściciel i tak
    się dowiaduje mimo nieodebrania. Przyjmuje same stringi (nie tenant dict) — webhook nie ma
    dostępu do obiektu tenanta, tylko do tego co sami wpisaliśmy w query string eventUrl."""
    resend_api_key = settings.resend_api_key
    if not resend_api_key:
        logger.warning("📧 [TRANSFER] RESEND_API_KEY nieskonfigurowany — nie wysyłam")
        return False
    html_content = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px;">
        <h2 style="color: #333;">📞 Nieodebrane przekierowanie połączenia</h2>
        <p style="color: #666; margin-top: -10px;">{business_name}</p>
        <p>Klient chciał porozmawiać na żywo, ale połączenie nie zostało odebrane w ciągu
        {TRANSFER_RING_TIMEOUT}s. Oddzwoń, gdy będziesz mógł:</p>
        <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
            <tr><td style="padding: 8px; border-bottom: 1px solid #eee; width: 120px;"><strong>Telefon:</strong></td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;"><a href="tel:{caller_phone}">{caller_phone}</a></td></tr>
        </table>
        <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
        <p style="color: #999; font-size: 12px;">Automatyczne powiadomienie — asystent głosowy (test Realtime) • {business_name}</p>
    </div>
    """
    try:
        import httpx

        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {resend_api_key}", "Content-Type": "application/json"},
                json={
                    "from": "Voice AI <noreply@bizvoice.pl>",
                    "to": [to_email],
                    "subject": f"📞 Nieodebrane przekierowanie — {business_name}",
                    "html": html_content,
                },
                timeout=10.0,
            )
            if response.status_code == 200:
                logger.info("📧 [TRANSFER] Email o nieodebranym przekierowaniu wysłany")
                return True
            logger.error(f"📧 [TRANSFER] Resend error: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logger.error(f"📧 [TRANSFER] Send email error: {e}")
        return False
