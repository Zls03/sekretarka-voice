"""API panelu dla rezerwacji: wolne terminy, zapis, odwołanie i przełożenie wizyty."""

import asyncio
from datetime import datetime

import httpx
from loguru import logger

from app.config import settings

# URL do panelu Next.js
PANEL_API_URL = settings.panel_api_url
ADMIN_PANEL_API_URL = settings.admin_panel_api_url
PANEL_SLUG = settings.panel_slug


async def get_available_slots_from_api(tenant: dict, staff: dict, service: dict, date: datetime) -> list[str]:
    """
    Pobiera wolne sloty z API panelu (Google Calendar) - BEZ CACHE.
    Zwraca świeże dane bezpośrednio z API.
    """
    staff_id = staff.get("id")
    service_id = service.get("id")
    date_str = date.strftime("%Y-%m-%d")
    slug = tenant.get("slug") or PANEL_SLUG

    if not slug:
        logger.warning("⚠️ No panel slug configured")
        return []

    base_url = ADMIN_PANEL_API_URL if tenant.get("source") == "admin" else PANEL_API_URL
    logger.info(f"📅 Fetching fresh slots from API: slug={slug}, staff={staff_id}, date={date_str}")

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(
                f"{base_url}/api/panel/{slug}/calendar/slots",
                params={"staffId": staff_id, "serviceId": service_id, "date": date_str},
            )

            if response.status_code == 200:
                data = response.json()
                slots = data.get("slots", [])

                # Zwracaj jako stringi "H:MM" żeby zachować minuty
                result = []
                for slot in slots:
                    if isinstance(slot, str) and ":" in slot:
                        # Normalizuj: "09:30" → "9:30"
                        parts = slot.split(":")
                        h = int(parts[0])
                        m = parts[1] if len(parts) > 1 else "00"
                        result.append(f"{h}:{m}")
                    elif isinstance(slot, int):
                        result.append(f"{slot}:00")

                logger.info(f"📅 API returned {len(result)} slots for {date_str}: {result[:5]}...")
                return result
            else:
                logger.warning(f"⚠️ Calendar API returned {response.status_code}: {response.text[:200]}")

    except httpx.TimeoutException:
        logger.error(f"❌ Calendar API timeout for {date_str}")
    except Exception as e:
        logger.error(f"❌ Calendar API error: {e}")

    return []


async def save_booking_in_panel(
    tenant: dict,
    staff: dict,
    service: dict,
    date: datetime,
    time_str: str,
    customer_name: str,
    customer_phone: str,
    notes: str = "",
) -> tuple[str, dict]:
    """POST /api/panel/{slug}/bookings. 409 (nowy unique index w bizvoice-panel na
    staff_id+booking_date+booking_time, dodany w tej samej sesji) NIE jest retry'owany —
    slot jest definitywnie zajęty, ponawianie nic nie da. Inne błędy retry'owane ×3 z
    0.5s odstępem, tak jak flows_helpers.save_booking_to_api.

    Returns: (outcome, data) — outcome to "ok" | "slot_taken" | "error"."""
    slug = tenant.get("slug") or PANEL_SLUG
    if not slug:
        logger.warning("⚠️ [BOOKING] Brak panel slug — nie mogę zapisać")
        return ("error", {})

    date_str = date.strftime("%Y-%m-%d")
    base_url = ADMIN_PANEL_API_URL if tenant.get("source") == "admin" else PANEL_API_URL
    payload = {
        "staff_id": staff.get("id"),
        "service_id": service.get("id"),
        "date": date_str,
        "time": time_str,
        "client_name": customer_name,
        "client_phone": customer_phone,
    }
    if notes:
        payload["notes"] = notes

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(f"{base_url}/api/panel/{slug}/bookings", json=payload)
                if response.status_code in (200, 201):
                    data = response.json()
                    data["booking_code"] = data.get("visitCode") or data.get("booking_code") or ""
                    logger.info(f"✅ [BOOKING] Zapisano: {data.get('bookingId')} (kod {data['booking_code']})")
                    return ("ok", data)
                if response.status_code == 409:
                    logger.warning(f"⚠️ [BOOKING] 409 slot_taken: {date_str} {time_str}")
                    return ("slot_taken", {})
                logger.warning(f"⚠️ [BOOKING] API error {response.status_code} (próba {attempt + 1}/3)")
        except Exception as e:
            logger.error(f"❌ [BOOKING] API exception (próba {attempt + 1}/3): {e}")
        if attempt < 2:
            await asyncio.sleep(0.5)

    logger.error("❌ [BOOKING] Zapis nie powiódł się po 3 próbach")
    return ("error", {})


async def cancel_booking_in_panel(tenant: dict, booking_id: str) -> bool:
    slug = tenant.get("slug") or PANEL_SLUG
    if not slug or not booking_id:
        return False
    base_url = ADMIN_PANEL_API_URL if tenant.get("source") == "admin" else PANEL_API_URL
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.delete(f"{base_url}/api/panel/{slug}/bookings/{booking_id}")
            return response.status_code in (200, 201)
    except Exception as e:
        logger.error(f"❌ [MANAGE_BOOKING] Cancel error: {e}")
        return False


async def reschedule_booking_in_panel(tenant: dict, booking_id: str, date: datetime, time_str: str) -> bool:
    slug = tenant.get("slug") or PANEL_SLUG
    if not slug or not booking_id:
        return False
    base_url = ADMIN_PANEL_API_URL if tenant.get("source") == "admin" else PANEL_API_URL
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.patch(
                f"{base_url}/api/panel/{slug}/bookings/{booking_id}",
                json={"date": date.strftime("%Y-%m-%d"), "time": time_str},
            )
            return response.status_code in (200, 201)
    except Exception as e:
        logger.error(f"❌ [MANAGE_BOOKING] Reschedule error: {e}")
        return False
