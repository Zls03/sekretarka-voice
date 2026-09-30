"""Powiadomienia web push do portalu /crm (PWA) o nowych zgłoszeniach."""

import json
import os

from loguru import logger

from app.db import saas_db


async def _send_push_notifications(tenant: dict, title: str, body: str, url: str = "/crm") -> None:
    """Web push do wszystkich subskrypcji portalu /crm tej firmy (crm_push_subscriptions —
    zapisywane przez bizvoice-panel po zgodzie właściciela w przeglądarce, patrz
    src/app/crm/(dashboard)/PushNotifications.tsx). Wołana WYŁĄCZNIE dla rozmów z realną
    treścią (patrz warunek w maybe_send_call_summary) — świadomie NIE za każde puste
    połączenie, żeby nie zalewać telefonu właściciela szumem.

    Osobna, w pełni nieblokująca funkcja (własny try/except na poziomie całej funkcji i
    per-subskrypcja) — brak kluczy VAPID, padnięty request do jednego urządzenia, czy
    cokolwiek innego tutaj NIGDY nie może wywrócić reszty maybe_send_call_summary (mail/
    CRM muszą polecieć niezależnie). Wygasłe subskrypcje (404/410 — użytkownik
    odinstalował PWA albo wyczyścił dane przeglądarki) są od razu kasowane z bazy, żeby
    nie próbować ich bez końca przy każdej kolejnej rozmowie."""
    vapid_private_key = os.getenv("VAPID_PRIVATE_KEY")
    firm_id = tenant.get("id")
    if not vapid_private_key or not firm_id:
        logger.warning(f"📲 [PUSH] Pomijam — brak VAPID_PRIVATE_KEY lub firm_id (tenant={tenant.get('id')})")
        return
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        logger.error("📲 [PUSH] Pomijam — pywebpush niezainstalowany")
        return

    try:
        rows = await saas_db.execute(
            "SELECT endpoint, p256dh, auth FROM crm_push_subscriptions WHERE firm_id = ?",
            [firm_id],
        )
    except Exception as e:
        logger.error(f"📲 [PUSH] Nie udało się pobrać subskrypcji: {e}")
        return
    if not rows:
        # 2026-09-26 — dodane po żywym zgłoszeniu ("dlaczego nie przyszło powiadomienie?")
        # gdzie ta funkcja nie zostawiała ŻADNEGO śladu w logach — nie dało się odróżnić
        # "nic nie wysłałem bo brak subskrypcji" od "coś poszło nie tak po cichu".
        logger.info(f"📲 [PUSH] Brak zapisanych subskrypcji dla firm_id={firm_id} — nic do wysłania")
        return

    payload = json.dumps({"title": title, "body": body[:180], "url": url})
    sent, expired = 0, 0
    for row in rows:
        subscription_info = {
            "endpoint": row.get("endpoint"),
            "keys": {"p256dh": row.get("p256dh"), "auth": row.get("auth")},
        }
        try:
            webpush(
                subscription_info=subscription_info,
                data=payload,
                vapid_private_key=vapid_private_key,
                vapid_claims={"sub": "mailto:kontakt@bizvoice.pl"},
            )
        except WebPushException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (404, 410):
                expired += 1
                try:
                    await saas_db.execute(
                        "DELETE FROM crm_push_subscriptions WHERE endpoint = ?",
                        [subscription_info["endpoint"]],
                    )
                except Exception:
                    pass
            else:
                logger.error(f"📲 [PUSH] Błąd wysyłki (status={status}): {e}")
        except Exception as e:
            logger.error(f"📲 [PUSH] Nieoczekiwany błąd wysyłki: {e}")
        else:
            sent += 1
    logger.info(f"📲 [PUSH] firm_id={firm_id}: {sent}/{len(rows)} wysłane, {expired} wygasłych usuniętych")
