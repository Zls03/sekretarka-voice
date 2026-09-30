"""Wewnętrzne API panelu: profil klienta (historia wizyt) i zapis wizyty."""

import httpx
from loguru import logger

from app.config import settings

PANEL_URL = settings.panel_url
INTERNAL_API_SECRET = settings.internal_api_secret


async def get_client_profile(firm_id: str, phone: str) -> dict | None:
    """Pobiera profil dzwoniącego klienta z panelu (CRM)."""
    if not PANEL_URL or not INTERNAL_API_SECRET:
        return None
    url = f"{PANEL_URL}/api/internal/client"
    headers = {"x-internal-secret": INTERNAL_API_SECRET}
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            res = await client.get(url, params={"firm_id": firm_id, "phone": phone}, headers=headers)
            if res.status_code == 200:
                return res.json()
    except Exception as e:
        logger.warning(f"CRM lookup failed: {e}")
    return None


async def save_client_visit(
    firm_id: str, phone: str, name: str, service: str, staff: str, scheduled_at: str, notes: str = ""
):
    """Zapisuje/aktualizuje klienta i wizytę w panelu (CRM). Nie blokuje przy błędzie."""
    if not PANEL_URL or not INTERNAL_API_SECRET:
        logger.warning("CRM save_client_visit: PANEL_URL lub INTERNAL_API_SECRET nie ustawione — pomijam")
        return
    url = f"{PANEL_URL}/api/internal/client"
    headers = {
        "x-internal-secret": INTERNAL_API_SECRET,
        "Content-Type": "application/json",
    }
    payload = {
        "firm_id": firm_id,
        "phone": phone,
        "name": name,
        "service": service,
        "staff": staff,
        "scheduled_at": scheduled_at,
    }
    if notes:
        payload["notes"] = notes
    logger.info(f"📋 CRM save: {name} ({phone}) → {service} @ {scheduled_at}")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.post(url, json=payload, headers=headers)
            logger.info(f"📋 CRM response: {res.status_code}")
    except Exception as e:
        logger.warning(f"CRM save failed: {e}")
