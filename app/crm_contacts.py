"""Kartoteka kontaktów portalu /crm (tabela crm_contacts) — imię znanego rozmówcy."""

import os
from datetime import datetime

from loguru import logger

from app.db import saas_db


# Świadomie NIEZALEŻNE od get_client_profile/PANEL_URL niżej — to jest booking CRM
# (clients/visits, tylko dla firm z booking_enabled i historią wizyt przez internal API
# panelu). crm_contacts to prostsza, uniwersalna kartoteka portalu /crm (zakładka
# "Klienci") — działa dla KAŻDEJ firmy niezależnie od booking, i to jedyne miejsce gdzie
# właściciel ręcznie wpisuje imię kontaktu. Zapytanie idzie wprost do SaaS DB, bez
# pośrednictwa panelu.
async def get_crm_contact_name(firm_id: str, phone: str) -> str:
    """Imię zapisane w portalu /crm (zakładka Klienci) dla tego numeru, jeśli jest.
    Dopasowanie po ostatnich 9 cyfrach — te same numery bywają zapisane z/bez "+48",
    identyczny wzorzec co api/crm/leads/route.ts po stronie panelu."""
    if not saas_db.is_configured or not firm_id or not phone:
        return ""
    try:
        rows = await saas_db.execute(
            "SELECT name FROM crm_contacts WHERE firm_id = ? AND substr(phone, -9) = substr(?, -9) LIMIT 1",
            [firm_id, phone],
        )
    except Exception as e:
        logger.error(f"[crm_contacts] get_crm_contact_name error: {e}")
        return ""
    if not rows:
        return ""
    return (rows[0].get("name") or "").strip()


async def maybe_save_contact_name(firm_id: str, phone: str, name: str) -> None:
    """Zapisuje imię do crm_contacts TYLKO gdy dla tego numeru jeszcze nie ma żadnego
    imienia — nigdy nie nadpisuje tego co właściciel już ręcznie wpisał w portalu /crm.
    Wołane po udanym contact_owner (patrz realtime_tools.py/bot_elevenlabs_agent.py) —
    tam customer_name to coś co klient SAM wprost podał (model musiał o to zapytać, żeby
    w ogóle wypełnić ten parametr narzędzia), nie zgadywanie z transkryptu, więc ryzyko
    zapisania złego imienia jest niskie. Błędy połykane — to poboczny, nieblokujący zapis,
    nie może wywrócić zakończenia rozmowy."""
    if not saas_db.is_configured or not firm_id or not phone or not name:
        return
    try:
        existing = await saas_db.execute(
            "SELECT id, name FROM crm_contacts WHERE firm_id = ? AND substr(phone, -9) = substr(?, -9) LIMIT 1",
            [firm_id, phone],
        )
        if existing:
            if (existing[0].get("name") or "").strip():
                return  # już jest jakieś imię — nie nadpisujemy
            await saas_db.execute(
                "UPDATE crm_contacts SET name = ?, updated_at = datetime('now') WHERE id = ?",
                [name, existing[0]["id"]],
            )
        else:
            contact_id = f"contact_{int(datetime.now().timestamp() * 1000)}_{os.urandom(3).hex()}"
            await saas_db.execute(
                "INSERT INTO crm_contacts (id, firm_id, phone, name, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))",
                [contact_id, firm_id, phone, name],
            )
        logger.info(f"📇 [crm_contacts] Zapisano imię '{name}' dla {phone} (firm_id={firm_id})")
    except Exception as e:
        logger.error(f"[crm_contacts] maybe_save_contact_name error: {e}")
