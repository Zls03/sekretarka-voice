"""Rozliczanie rozmów: blokada przed startem (brak środków) i naliczanie minut/kredytów po zakończeniu."""

from loguru import logger

from app.db import db, saas_db

PRICE_PER_MINUTE = 0.39  # zł/min — MUSI być zsynchronizowane z bot.py::PRICE_PER_MINUTE


async def apply_call_charge(tenant_id: str, is_saas_tenant: bool, call_sid: str, call_status: str, duration: int) -> None:
    """Nalicza minuty/kredyty za zakończoną rozmowę. 1:1 port bot.py::apply_call_charge
    (sama logika finansowa, bez zmian) — wołane z /vonage/events poniżej."""
    duration_minutes = duration / 60.0

    if call_status != "completed" or duration <= 0:
        return

    if is_saas_tenant:
        saas_row = await saas_db.execute("SELECT user_id FROM firms WHERE id = ?", [tenant_id])
        saas_user_id = saas_row[0]["user_id"] if saas_row else None
        if not saas_user_id:
            logger.warning(f"⚠️ [REALTIME TEST] No user_id for SaaS firm {tenant_id} — can't charge")
            return

        cost = round(duration_minutes * PRICE_PER_MINUTE, 4)

        await saas_db.execute(
            "UPDATE firms SET minutes_used = minutes_used + ? WHERE id = ?",
            [duration_minutes, tenant_id],
        )
        await saas_db.execute(
            """UPDATE credits
               SET balance = balance - ?,
                   total_spent = total_spent + ?
               WHERE user_id = ?""",
            [cost, cost, saas_user_id],
        )
        logger.info(f"📊 [REALTIME TEST] SaaS: -{cost:.4f} zł ({duration_minutes:.2f} min) for user {saas_user_id}")

        credits = await saas_db.execute("SELECT balance FROM credits WHERE user_id = ?", [saas_user_id])
        if credits:
            balance = float(credits[0].get("balance") or 0)
            if balance < PRICE_PER_MINUTE:
                await saas_db.execute("UPDATE firms SET is_blocked = 1 WHERE id = ?", [tenant_id])
                logger.warning(f"⚠️ [REALTIME TEST] SaaS firm {tenant_id} BLOCKED — balance too low: {balance:.2f} zł")

        firm_data = await saas_db.execute(
            "SELECT minutes_used, minutes_limit FROM firms WHERE id = ?", [tenant_id]
        )
        if firm_data:
            used = float(firm_data[0].get("minutes_used") or 0)
            limit = int(firm_data[0].get("minutes_limit") or 0)
            if limit > 0 and used >= limit * 0.99:
                await saas_db.execute("UPDATE firms SET is_blocked = 1 WHERE id = ?", [tenant_id])
                logger.warning(f"⚠️ [REALTIME TEST] SaaS firm {tenant_id} BLOCKED — minutes limit reached: {used:.1f}/{limit} min")

        await saas_db.execute(
            """INSERT INTO transactions
               (id, user_id, type, amount, description, created_at)
               VALUES (?, ?, 'usage', ?, ?, datetime('now'))""",
            [
                f"tx_{call_sid[:12]}",
                saas_user_id,
                -cost,
                f"Rozmowa {duration}s ({duration_minutes:.2f} min) [Realtime test]",
            ],
        )
    else:
        await db.execute(
            "UPDATE tenants SET minutes_used = minutes_used + ? WHERE id = ?",
            [duration_minutes, tenant_id],
        )
        logger.info(f"📊 [REALTIME TEST] Admin: +{duration_minutes:.2f} min for {tenant_id}")

        tenant_data = await db.execute(
            "SELECT minutes_used, minutes_limit FROM tenants WHERE id = ?", [tenant_id]
        )
        if tenant_data:
            used = float(tenant_data[0].get("minutes_used", 0))
            limit = int(tenant_data[0].get("minutes_limit", 100))
            if used >= limit * 0.99:
                await db.execute("UPDATE tenants SET is_blocked = 1 WHERE id = ?", [tenant_id])
                logger.warning(f"⚠️ [REALTIME TEST] Admin tenant {tenant_id} BLOCKED - limit reached")


async def is_call_allowed(tenant: dict) -> bool:
    """Pre-call guard, 1:1 z bot.py (sprawdzane PRZED startem pipeline'u, w /twilio/incoming-gemini-test
    i /vonage/answer poniżej). Bez tego zablokowany/bez-środków tenant i tak dostawałby pełne, płatne
    połączenie z OpenAI Realtime — apply_call_charge() ustawia is_blocked DOPIERO PO zakończonej rozmowie,
    więc to jedyne miejsce które faktycznie zapobiega rozpoczęciu kosztownej sesji.

    Dla SaaS get_tenant_by_phone() i tak już filtruje is_blocked=0 w SQL (patrz helpers.py), więc ten
    check tu to głównie: (1) obrona przed niespójnością (is_blocked jeszcze nie ustawione, a saldo już
    zeszło poniżej progu), (2) jedyny check dla tenantów admina, gdzie SQL filtruje tylko is_active."""
    if tenant.get("is_blocked"):
        logger.warning(f"🚫 [REALTIME TEST] Tenant {tenant.get('id')} BLOCKED — odrzucam połączenie")
        return False
    if tenant.get("source") == "saas":
        user_id = tenant.get("user_id", "")
        rows = await saas_db.execute("SELECT balance FROM credits WHERE user_id = ?", [user_id])
        balance = float(rows[0].get("balance") or 0) if rows else 0
        if balance < PRICE_PER_MINUTE:
            logger.warning(f"🚫 [REALTIME TEST] SaaS {tenant.get('id')} — brak kredytów: {balance:.2f} zł")
            return False
    return True
