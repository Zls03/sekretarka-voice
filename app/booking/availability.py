"""Dostępność terminów: godziny otwarcia, grafik pracownika, wolne sloty i reguły wyprzedzenia."""

import asyncio
from datetime import datetime, timedelta

from loguru import logger

from app.booking.panel_api import get_available_slots_from_api
from app.booking.parsing import _normalize_time
from app.polish.formatting import format_date_polish, format_hour_polish


def get_opening_hours(tenant: dict, weekday: int) -> tuple[int, int] | None:
    """Pobierz godziny otwarcia dla danego dnia tygodnia"""
    default_hours = {
        0: (9, 18), 1: (9, 18), 2: (9, 18), 3: (9, 18), 4: (9, 18),
        5: (9, 14), 6: None,
    }

    working_hours = tenant.get("working_hours", [])
    for wh in working_hours:
        if wh.get("day_of_week") == weekday:
            open_time = wh.get("open_time")
            close_time = wh.get("close_time")
            if open_time and close_time:
                open_hour = int(open_time.split(":")[0])
                close_hour = int(close_time.split(":")[0])
                return (open_hour, close_hour)
            return None

    return default_hours.get(weekday)


def validate_max_days_ahead(date: datetime, tenant: dict, staff: dict) -> tuple[bool, str]:
    """Sprawdza TYLKO max dni w przód — do użycia gdy znamy jeszcze tylko datę (bez godziny wizyty)."""
    now = datetime.now()
    try:
        max_days_ahead = int(staff.get("max_days_ahead") or staff.get("max_booking_days") or 14)
    except (ValueError, TypeError):
        max_days_ahead = 14

    max_date = now + timedelta(days=max_days_ahead)

    if date > max_date:
        return (False, f"Rezerwacje można składać maksymalnie {max_days_ahead} dni w przód.")

    return (True, "")


def validate_min_advance_hours(date_with_time: datetime, tenant: dict, staff: dict) -> tuple[bool, str]:
    """Sprawdza min. wyprzedzenie godzinowe — wywołuj gdy mamy już PEŁNY datetime (data + godzina wizyty)."""
    now = datetime.now()
    try:
        min_advance_hours = int(staff.get("min_advance_hours") or staff.get("min_booking_hours") or 12)
    except (ValueError, TypeError):
        min_advance_hours = 12

    min_booking_time = now + timedelta(hours=min_advance_hours)

    if date_with_time < min_booking_time:
        return (False, f"Rezerwacje przyjmujemy z minimum {min_advance_hours} godzinnym wyprzedzeniem.")

    return (True, "")


def get_staff_working_hours(staff: dict, weekday: int) -> tuple[int, int] | None:
    """Pobierz godziny pracy pracownika dla danego dnia"""
    import json

    wh_json = staff.get("working_hours_json", "")
    if not wh_json or wh_json == "'{}'":
        return None

    try:
        wh = json.loads(wh_json) if isinstance(wh_json, str) else wh_json

        # Mapowanie weekday (0=pon) na klucze JSON
        day_keys = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}
        day_key = day_keys.get(weekday)

        if not day_key or day_key not in wh:
            return None

        day_data = wh[day_key]

        # Sprawdź czy pracownik pracuje tego dnia
        if day_data.get("closed", False):
            return None

        open_time = day_data.get("open", "")
        close_time = day_data.get("close", "")

        if open_time and close_time:
            open_hour = int(open_time.split(":")[0])
            close_hour = int(close_time.split(":")[0])
            return (open_hour, close_hour)

    except Exception as e:
        logger.warning(f"⚠️ Error parsing staff working hours: {e}")

    return None


async def get_available_slots_from_working_hours(
    tenant: dict, staff: dict, service: dict, date: datetime
) -> list[str]:
    """Fallback: generuje sloty co 30 min z godzin pracy"""
    weekday = date.weekday()

    # Najpierw sprawdź godziny pracownika
    staff_hours = get_staff_working_hours(staff, weekday)

    if staff_hours:
        open_hour, close_hour = staff_hours
    else:
        # Brak godzin pracownika = brak terminów, nie fallback na salon
        logger.info(f"ℹ️ Staff {staff.get('name')} has no hours for weekday {weekday} - no slots")
        return []

    service_duration = service.get("duration_minutes", 60)

    slots = []
    current_minutes = open_hour * 60  # Pracuj w minutach
    close_minutes = close_hour * 60

    while current_minutes + service_duration <= close_minutes:
        h = current_minutes // 60
        m = current_minutes % 60
        slots.append(f"{h}:{m:02d}")
        current_minutes += 30  # Co 30 minut

    # Filtruj przeszłe sloty jeśli dziś
    # Filtruj przeszłe sloty i respektuj min_booking_hours
    #
    # POPRAWKA 2026-08-24: ujednolicone z validate_min_advance_hours() (autorytatywna
    # walidacja przy zapisie, patrz wyżej w tym pliku) — ta funkcja miała inną kolejność
    # kluczy I inny default (1h zamiast 12h). Ta funkcja to lokalny fallback (używany tylko
    # gdy Calendar API zwróci błąd, patrz get_available_slots()/validate_slot_available()),
    # ale realnie osiągalny — bez ujednolicenia mógł zaproponować klientowi termin za ~1h,
    # który finalna walidacja przy zapisie i tak odrzuciłaby (wymagając 12h), dając mylący,
    # niespójny komunikat.
    now = datetime.now()
    try:
        min_advance_hours = int(staff.get("min_advance_hours") or staff.get("min_booking_hours") or 12)
    except (ValueError, TypeError):
        min_advance_hours = 12

    min_time = now + timedelta(hours=min_advance_hours)
    min_minutes_from_midnight = min_time.hour * 60 + min_time.minute

    if date.date() == now.date():
        slots = [s for s in slots if _slot_to_minutes(s) >= min_minutes_from_midnight]
    elif date.date() == (now + timedelta(hours=min_advance_hours)).date():
        # min_booking_hours przekracza północ - filtruj też następny dzień
        slots = [s for s in slots if _slot_to_minutes(s) >= min_minutes_from_midnight]

    logger.info(f"📅 Generated {len(slots)} slots from working hours (min_advance={min_advance_hours}h)")
    return slots


def _slot_to_minutes(slot: str) -> int:
    """Helper: '14:30' → 870"""
    parts = slot.split(":")
    return int(parts[0]) * 60 + int(parts[1])


# Cache dla slotów (używany tylko przez get_available_slots, nie przez _from_api)
_slots_cache = {}


_slots_cache_lock = asyncio.Lock()


async def get_available_slots(
    tenant: dict, staff: dict, service: dict, date: datetime
) -> list[str]:
    """Główna funkcja - z cache 60s (używaj get_available_slots_from_api dla świeżych danych)"""
    cache_key = f"{staff.get('id')}_{date.strftime('%Y-%m-%d')}"

    # Szybki odczyt bez locka (optymistyczny)
    if cache_key in _slots_cache:
        cached_time, cached_slots = _slots_cache[cache_key]
        if (datetime.now() - cached_time).seconds < 60:
            logger.info(f"📅 Cache hit for {cache_key}: {len(cached_slots)} slots")
            return cached_slots

    # Lock tylko gdy trzeba odpytać API
    async with _slots_cache_lock:
        # Sprawdź ponownie po wejściu w lock (inny coroutine mógł już uzupełnić)
        if cache_key in _slots_cache:
            cached_time, cached_slots = _slots_cache[cache_key]
            if (datetime.now() - cached_time).seconds < 60:
                logger.info(f"📅 Cache hit (post-lock) for {cache_key}: {len(cached_slots)} slots")
                return cached_slots

        # Pobierz z API lub working hours
        calendar_connected = staff.get("google_calendar_id") or staff.get("google_connected")

        if calendar_connected:
            logger.info(f"📅 Staff {staff.get('name')} has calendar, using API")
            slots = await get_available_slots_from_api(tenant, staff, service, date)
            if slots:
                _slots_cache[cache_key] = (datetime.now(), slots)
                return slots
            logger.warning("⚠️ API returned no slots, falling back")

        slots = await get_available_slots_from_working_hours(tenant, staff, service, date)
        _slots_cache[cache_key] = (datetime.now(), slots)
        return slots


def staff_can_do_service(staff: dict, service: dict) -> bool:
    """
    Sprawdź czy pracownik wykonuje daną usługę.
    Pusta lista usług = pracownik robi wszystko.
    """
    if not service:
        return True

    staff_service_ids = [svc.get("id") for svc in staff.get("services", [])]

    # Pusta lista = wszystkie usługi
    if not staff_service_ids:
        return True

    return service.get("id") in staff_service_ids


async def get_next_available_days(
    tenant: dict, staff: dict, service: dict, max_days: int = 14, limit: int = 3
) -> list[dict]:
    """Znajduje najbliższe dni z wolnymi terminami.

    Sprawdza dni w PACZKACH równolegle (asyncio.gather), nie jeden po drugim — każde zapytanie
    do panelu (kalendarz Google) to ~0.7-1.3s HTTP round-trip, a typowy przypadek to "dziś już nic
    nie ma, jutro jest" — sekwencyjnie to 2 round-tripy z rzędu (widoczne na żywym telefonie jako
    🔴 2-5s w logach user->bot latency). Paczka po `_BATCH` dni naraz sprowadza to do ~1 round-tripu.
    Kolejność wyniku zostaje chronologiczna mimo równoległości — gather() zwraca w kolejności
    argumentów, nie ukończenia, więc "najbliższy termin" nadal znaczy najbliższy kalendarzowo.

    Returns: [{"date": datetime, "slots": ["10:00", ...], "slots_count": N}, ...]
    """
    _BATCH = 4
    results = []
    today = datetime.now()

    async def _check(check_date: datetime) -> tuple[datetime, list[str]]:
        try:
            return check_date, await get_available_slots_from_api(tenant, staff, service, check_date)
        except Exception as e:
            logger.warning(f"⚠️ [BOOKING] Error checking date {check_date}: {e}")
            return check_date, []

    for batch_start in range(0, max_days, _BATCH):
        batch_dates = [today + timedelta(days=d) for d in range(batch_start, min(batch_start + _BATCH, max_days))]
        for check_date, slots in await asyncio.gather(*[_check(d) for d in batch_dates]):
            if slots:
                results.append({"date": check_date, "slots": slots, "slots_count": len(slots)})
                if len(results) >= limit:
                    return results

    return results


def _slots_summary(slots: list[str]) -> str:
    """Podsumowanie slotów: max 2 przykłady (voice-friendly)"""
    if not slots:
        return "brak wolnych terminów"
    if len(slots) == 1:
        return format_hour_polish(slots[0])
    if len(slots) == 2:
        return f"{format_hour_polish(slots[0])} lub {format_hour_polish(slots[1])}"
    first = slots[0]
    mid = slots[len(slots) // 2]
    return f"{format_hour_polish(first)}, {format_hour_polish(mid)} i inne"


def format_availability_message(available_days: list[dict]) -> str:
    """Formatuje wiadomość o dostępnych terminach — KRÓTKO (voice-friendly)"""
    if not available_days:
        return "Niestety, w najbliższych dniach nie ma wolnych terminów."
    first = available_days[0]
    date_str = format_date_polish(first["date"])
    first_slot = format_hour_polish(first["slots"][0])
    return f"Najbliższy wolny termin to {date_str} o {first_slot}. Zapisać, czy wolisz inny termin?"


async def validate_slot_available(
    tenant: dict, staff: dict, service: dict, date: datetime, time_str: str
) -> tuple[bool, list[str]]:
    """Sprawdza czy konkretny slot jest dostępny. Pobiera ŚWIEŻE dane z API (bez cache)."""
    logger.info(f"🔍 [BOOKING] Validating slot: {date.strftime('%Y-%m-%d')} at {time_str}")

    try:
        current_slots = await get_available_slots_from_api(tenant, staff, service, date)
    except Exception as e:
        logger.error(f"❌ [BOOKING] API error during validation: {e}")
        current_slots = await get_available_slots(tenant, staff, service, date)

    time_normalized = _normalize_time(time_str)
    slots_normalized = [_normalize_time(s) for s in current_slots]
    is_available = time_normalized in slots_normalized

    if is_available:
        logger.info(f"✅ [BOOKING] Slot {time_str} is AVAILABLE")
    else:
        logger.warning(f"❌ [BOOKING] Slot {time_str} is NOT available! Available: {current_slots[:5]}")

    return (is_available, current_slots)
