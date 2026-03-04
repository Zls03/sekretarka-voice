# flows_manage.py - Zarządzanie istniejącymi wizytami
# WERSJA 2.0 - PRZEPISANE wg wzorca flows_booking_simple
"""
ARCHITEKTURA (tak samo jak booking_simple):
- GPT rozpoznaje TYLKO: action, booking_code, date_text, time_text, spoken_choice, confirmation
- GPT NIE decyduje o niczym — tylko wyciąga słowa klienta
- KOD decyduje o wszystkim: walidacja, przejścia, komunikaty TTS

OBSŁUGUJE:
- Wyszukiwanie wizyty po numerze telefonu (caller_phone)
- Fallback na kod wizyty z SMS
- Anulowanie wizyty (z potwierdzeniem)
- Zmianę daty/godziny (z walidacją slotów)
- Wybór z listy wielu wizyt
- Fallback: przekierowanie lub wiadomość do właściciela
"""

import os
import httpx
import dateparser
from datetime import datetime, timedelta
from typing import Optional, Dict, Tuple, List
from loguru import logger
from pipecat_flows import FlowManager, FlowsFunctionSchema
from pipecat.frames.frames import TTSSpeakFrame

from flows_helpers import (
    format_hour_polish, format_date_polish,
    get_available_slots_from_api,
    validate_date_constraints,
    POLISH_DAYS,
)
from flows_booking_simple import (
    get_next_available_days,
    format_availability_message,
    _slots_summary,
    validate_slot_available,
    preprocess_date_text,
    _parse_time,
    DATEPARSER_SETTINGS,
)
from polish_mappings import odmien_imie

PANEL_API_URL = os.getenv("PANEL_API_URL", "http://localhost:3000")


# ============================================================================
# API - WYSZUKIWANIE I EDYCJA WIZYT
# ============================================================================

async def find_bookings_by_phone(tenant: dict, phone: str) -> List[Dict]:
    """Szuka wizyt po numerze telefonu"""
    slug = tenant.get("slug", "")
    if not slug or not phone:
        return []

    phone_clean = phone.replace(" ", "").replace("-", "")
    if not phone_clean.startswith("+"):
        phone_clean = f"+48{phone_clean[-9:]}"

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(
                f"{PANEL_API_URL}/api/panel/{slug}/bookings",
                params={"phone": phone_clean, "upcoming": "true"}
            )
            if response.status_code == 200:
                data = response.json()
                bookings = data.get("bookings", [])
                logger.info(f"📋 Found {len(bookings)} bookings for {phone_clean}")
                return bookings
    except Exception as e:
        logger.error(f"❌ find_bookings_by_phone error: {e}")
    return []


async def find_booking_by_code(tenant: dict, code: str) -> Optional[Dict]:
    """Szuka wizyty po kodzie z SMS"""
    slug = tenant.get("slug", "")
    if not slug or not code:
        return None

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(
                f"{PANEL_API_URL}/api/panel/{slug}/bookings/{code.strip()}"
            )
            if response.status_code == 200:
                data = response.json()
                booking = data.get("booking")
                if booking:
                    logger.info(f"📋 Found booking by code {code}: {booking.get('id')}")
                return booking
    except Exception as e:
        logger.error(f"❌ find_booking_by_code error: {e}")
    return None


async def cancel_booking_api(tenant: dict, booking_id: str) -> bool:
    """Anuluje wizytę przez API"""
    slug = tenant.get("slug", "")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.delete(
                f"{PANEL_API_URL}/api/panel/{slug}/bookings/{booking_id}"
            )
            if response.status_code in [200, 204]:
                logger.info(f"✅ Booking {booking_id} cancelled")
                return True
            logger.warning(f"⚠️ Cancel failed: {response.status_code}")
    except Exception as e:
        logger.error(f"❌ cancel_booking_api error: {e}")
    return False


async def reschedule_booking_api(
    tenant: dict, booking_id: str, new_date: str, new_time: str
) -> bool:
    """Przesuwa wizytę przez API"""
    slug = tenant.get("slug", "")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.patch(
                f"{PANEL_API_URL}/api/panel/{slug}/bookings/{booking_id}",
                json={"date": new_date, "time": new_time}
            )
            if response.status_code in [200, 201]:
                logger.info(f"✅ Booking {booking_id} rescheduled to {new_date} {new_time}")
                return True
            logger.warning(f"⚠️ Reschedule failed: {response.status_code}: {response.text[:200]}")
    except Exception as e:
        logger.error(f"❌ reschedule_booking_api error: {e}")
    return False


def format_booking_summary(booking: Dict) -> str:
    """Formatuje wizytę do wypowiedzi TTS"""
    service = booking.get("service_name", "wizyta")
    staff = booking.get("staff_name", "")
    date_str = booking.get("booking_date") or booking.get("date", "")
    time_str = booking.get("booking_time") or booking.get("time", "")

    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        date_formatted = format_date_polish(date_obj)
    except Exception:
        date_formatted = date_str

    try:
        time_formatted = format_hour_polish(time_str)
    except Exception:
        time_formatted = time_str

    staff_part = f" u {odmien_imie(staff)}" if staff else ""
    return f"{service}{staff_part}, {date_formatted} o {time_formatted}"


# ============================================================================
# POMOCNICZE - ekstrakcja staff/service z wizyty
# ============================================================================

def _booking_to_staff(booking: Dict, tenant: Dict) -> Dict:
    """Wyciąga obiekt staff z danych wizyty"""
    staff_id = booking.get("staff_id")
    staff_list = tenant.get("staff", [])
    found = next((s for s in staff_list if str(s.get("id")) == str(staff_id)), None)
    if found:
        return found
    return {
        "id": staff_id,
        "name": booking.get("staff_name", ""),
        "max_booking_days": 14,
        "min_advance_hours": 1,
    }


def _booking_to_service(booking: Dict, tenant: Dict) -> Dict:
    """Wyciąga obiekt service z danych wizyty"""
    service_id = booking.get("service_id")
    services = tenant.get("services", [])
    found = next((s for s in services if str(s.get("id")) == str(service_id)), None)
    if found:
        return found
    return {
        "id": service_id,
        "name": booking.get("service_name", ""),
        "duration_minutes": booking.get("duration_minutes", 60),
    }


# ============================================================================
# GŁÓWNA FUNKCJA GPT
# GPT wyciąga TYLKO te pola - nic więcej
# ============================================================================

def manage_appointment_function(tenant: Dict) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="manage_appointment",
        description="Wywołuj przy KAŻDEJ odpowiedzi klienta dotyczącej zarządzania wizytą.",
        properties={
            "action": {
                "type": "string",
                "enum": ["cancel", "reschedule", "none"],
                "description": (
                    "cancel=klient chce anulować wizytę, "
                    "reschedule=klient chce zmienić termin, "
                    "none=niejasne lub klient odpowiada na pytanie"
                )
            },
            "booking_code": {
                "type": "string",
                "description": "Kod wizyty podany przez klienta (cyfry z SMS) lub null"
            },
            "date_text": {
                "type": "string",
                "description": "Nowa data DOKŁADNIE jak klient powiedział ('jutro', 'w piątek') lub null"
            },
            "time_text": {
                "type": "string",
                "description": (
                    "Nowa godzina w formacie HH:MM. "
                    "Zamień słowa na cyfry: 'na czternastą' → '14:00', "
                    "'wpół do dwunastej' → '11:30'. Null jeśli klient nie podał."
                )
            },
            "spoken_choice": {
                "type": "string",
                "description": (
                    "Gdy klient wybiera wizytę z listy: "
                    "'pierwsza', 'druga', 'trzecia', 'ostatnia', '1', '2', '3' lub null"
                )
            },
            "confirmation": {
                "type": "string",
                "enum": ["yes", "no", "none"],
                "description": "yes=potwierdza, no=rezygnuje/anuluje, none=nic z tych"
            },
        },
        required=["action", "confirmation"],
        handler=lambda args, fm: handle_manage_appointment(args, fm, tenant),
    )


# ============================================================================
# GŁÓWNY HANDLER
# Logika: kod decyduje o wszystkim, GPT tylko wyciąga dane
# ============================================================================

async def handle_manage_appointment(
    args: Dict, flow_manager: FlowManager, tenant: Dict
) -> Tuple:

    # Pobierz dane z args
    action = args.get("action", "none")
    booking_code = args.get("booking_code")
    date_text = args.get("date_text")
    time_text = args.get("time_text")
    spoken_choice = args.get("spoken_choice", "")
    confirmation = args.get("confirmation", "none")

    # Pobierz stan
    state = flow_manager.state.get("manage", {})
    caller_phone = flow_manager.state.get("caller_phone", "")

    logger.info(
        f"📥 MANAGE: action={action}, code={booking_code}, "
        f"date={date_text}, time={time_text}, choice={spoken_choice}, confirm={confirmation}"
    )

    # === REZYGNACJA (globalnie — zawsze działa) ===
    if confirmation == "no" and "booking" not in state and "bookings_list" not in state:
        flow_manager.state["manage"] = {}
        from flows import create_anything_else_node
        await flow_manager.task.queue_frame(TTSSpeakFrame(
            text="Dobrze, nie wprowadzam żadnych zmian. Czy mogę jeszcze w czymś pomóc?"
        ))
        return (None, create_anything_else_node(tenant))

    # ==========================================================================
    # KROK 1: ZNAJDŹ WIZYTĘ
    # ==========================================================================

    if "booking" not in state:

        # 1a. Wiele wizyt w kolejce — wybór przez klienta
        if "bookings_list" in state:
            return await _handle_list_choice(
                spoken_choice, date_text, time_text, booking_code,
                state, flow_manager, tenant
            )

        booking = None

        # 1b. Szukaj po numerze telefonu
        if caller_phone:
            bookings = await find_bookings_by_phone(tenant, caller_phone)

            if len(bookings) == 1:
                booking = bookings[0]
                logger.info(f"✅ Found 1 booking by phone")

            elif len(bookings) > 1:
                # Kilka wizyt — zapytaj o którą
                state["bookings_list"] = bookings[:3]
                flow_manager.state["manage"] = state
                lines = [f"{i}. {format_booking_summary(b)}" for i, b in enumerate(bookings[:3], 1)]
                return await _respond(
                    f"Znalazłam kilka wizyt: {', '.join(lines)}. O której wizycie chce Pan porozmawiać?",
                    flow_manager, tenant, state
                )

        # 1c. Szukaj po kodzie z SMS
        if not booking and booking_code:
            code_attempts = state.get("code_attempts", 0)

            if code_attempts >= 3:
                logger.warning("⚠️ Too many code attempts")
                return await _fallback_no_booking(flow_manager, tenant, state)

            booking = await find_booking_by_code(tenant, booking_code.strip())

            if not booking:
                state["code_attempts"] = code_attempts + 1
                flow_manager.state["manage"] = state
                remaining = 3 - state["code_attempts"]
                if remaining > 0:
                    return await _respond(
                        f"Nie znalazłam wizyty o kodzie {booking_code}. Proszę spróbować jeszcze raz.",
                        flow_manager, tenant, state
                    )
                else:
                    return await _fallback_no_booking(flow_manager, tenant, state)
            else:
                state["code_attempts"] = 0
                logger.info(f"✅ Found booking by code: {booking_code}")

        # 1d. Nic nie znaleziono — zapytaj o kod
        if not booking and not booking_code:
            return await _respond(
                "Nie znalazłam rezerwacji na ten numer telefonu. Czy ma Pan kod wizyty z SMS-a?",
                flow_manager, tenant, state
            )

        # 1e. Nadal brak — fallback
        if not booking:
            return await _fallback_no_booking(flow_manager, tenant, state)

        # 1f. Sprawdź limit czasu na zmiany
        can_modify, limit_msg = _check_modify_time_limit(booking, tenant)
        if not can_modify:
            return await _respond(limit_msg, flow_manager, tenant, state)

        state["booking"] = booking
        flow_manager.state["manage"] = state

        summary = format_booking_summary(booking)
        return await _respond(
            f"Znalazłam wizytę: {summary}. Co chce Pan zrobić — anulować czy zmienić termin?",
            flow_manager, tenant, state
        )

    # ==========================================================================
    # KROK 2: MAMY WIZYTĘ — obsługa akcji
    # ==========================================================================

    booking = state["booking"]

    # --- REZYGNACJA z akcji (mamy wizytę) ---
    if confirmation == "no":
        flow_manager.state["manage"] = {}
        from flows import create_anything_else_node
        await flow_manager.task.queue_frame(TTSSpeakFrame(
            text="Dobrze, nie wprowadzam żadnych zmian. Czy mogę jeszcze w czymś pomóc?"
        ))
        return (None, create_anything_else_node(tenant))

    # ==========================================================================
    # ANULOWANIE
    # ==========================================================================

    if action == "cancel" or "cancel_confirmed" in state or state.get("pending_action") == "cancel":

        state["pending_action"] = "cancel"

        # Krok A: poczekaj na potwierdzenie
        if "cancel_confirmed" not in state:
            if confirmation == "yes":
                state["cancel_confirmed"] = True
            else:
                summary = format_booking_summary(booking)
                return await _respond(
                    f"Czy na pewno chce Pan anulować: {summary}?",
                    flow_manager, tenant, state
                )

        # Krok B: wykonaj anulowanie
        booking_id = booking.get("id") or booking.get("bookingId")
        success = await cancel_booking_api(tenant, booking_id)

        if success:
            await _notify_owner_cancel(tenant, booking, caller_phone)
            flow_manager.state["manage"] = {}
            from flows import create_anything_else_node
            await flow_manager.task.queue_frame(TTSSpeakFrame(
                text="Wizyta została anulowana. Czy mogę jeszcze w czymś pomóc?"
            ))
            return (None, create_anything_else_node(tenant))
        else:
            return await _fallback_api_error(flow_manager, tenant, state)

    # ==========================================================================
    # ZMIANA TERMINU
    # ==========================================================================

    if action == "reschedule" or "new_date" in state or "new_time" in state or state.get("pending_action") == "reschedule":

        state["pending_action"] = "reschedule"
        staff = _booking_to_staff(booking, tenant)
        service = _booking_to_service(booking, tenant)

        # --- Krok A: zbierz nową datę ---
        if date_text and "new_date" not in state:
            date_clean = preprocess_date_text(date_text)
            logger.info(f"📅 Reschedule date preprocessing: '{date_text}' → '{date_clean}'")

            parsed_date = dateparser.parse(
                date_clean, languages=["pl"], settings=DATEPARSER_SETTINGS
            )

            if not parsed_date:
                return await _respond(
                    f"Nie rozumiem daty '{date_text}'. Proszę powiedzieć np. 'jutro', 'w piątek', '15 marca'.",
                    flow_manager, tenant, state
                )

            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            if parsed_date.date() < today.date():
                return await _respond(
                    "Ta data już minęła. Proszę podać przyszłą datę.",
                    flow_manager, tenant, state
                )

            # Walidacja ograniczeń pracownika
            is_valid, msg = validate_date_constraints(parsed_date, tenant, staff)
            if not is_valid:
                return await _respond(msg, flow_manager, tenant, state)

            # Sprawdź sloty na nowy dzień
            try:
                from flows import play_snippet
                await play_snippet(flow_manager, "checking")
            except Exception:
                pass

            slots = await get_available_slots_from_api(tenant, staff, service, parsed_date)
            logger.info(f"📅 Slots for {parsed_date.strftime('%Y-%m-%d')}: {slots}")

            if not slots:
                available_days = await get_next_available_days(
                    tenant, staff, service,
                    max_days=int(staff.get("max_booking_days") or 14), limit=3
                )
                if available_days:
                    suggestion = format_availability_message(available_days)
                    return await _respond(
                        f"Na {format_date_polish(parsed_date)} nie ma wolnych terminów. {suggestion}",
                        flow_manager, tenant, state
                    )
                else:
                    max_days = int(staff.get("max_booking_days") or 14)
                    return await _respond(
                        f"Na {format_date_polish(parsed_date)} nie ma wolnych terminów "
                        f"i w najbliższych {max_days} dniach grafik jest pełny. "
                        f"Nowe terminy pojawiają się codziennie — proszę spróbować jutro.",
                        flow_manager, tenant, state
                    )

            state["new_date"] = parsed_date
            state["available_slots"] = slots
            flow_manager.state["manage"] = state

            slots_text = _slots_summary(slots)
            return await _respond(
                f"Na {format_date_polish(parsed_date)} wolne są: {slots_text}. Którą godzinę Pan wybiera?",
                flow_manager, tenant, state
            )

        if "new_date" not in state:
            # Brak daty — zaproponuj najbliższe terminy
            try:
                from flows import play_snippet
                await play_snippet(flow_manager, "checking")
            except Exception:
                pass

            available_days = await get_next_available_days(
                tenant, staff, service,
                max_days=int(staff.get("max_booking_days") or 14), limit=3
            )

            if available_days:
                suggestion = format_availability_message(available_days)
                return await _respond(
                    f"Na jaki dzień chce Pan przełożyć wizytę? {suggestion}",
                    flow_manager, tenant, state
                )
            else:
                return await _respond(
                    "Na jaki dzień chce Pan przełożyć wizytę?",
                    flow_manager, tenant, state
                )

        # --- Krok B: zbierz nową godzinę ---
        if time_text and "new_time" not in state:
            parsed_time = _parse_time(time_text)

            if not parsed_time:
                slots_text = _slots_summary(state.get("available_slots", []))
                return await _respond(
                    f"Nie rozumiem godziny '{time_text}'. Wolne są: {slots_text}.",
                    flow_manager, tenant, state
                )

            # Waliduj slot — świeże dane z API
            is_available, current_slots = await validate_slot_available(
                tenant, staff, service, state["new_date"], parsed_time
            )

            if not is_available:
                if current_slots:
                    state["available_slots"] = current_slots
                    slots_text = _slots_summary(current_slots)
                    return await _respond(
                        f"Niestety {format_hour_polish(parsed_time)} jest już zajęta. Wolne są: {slots_text}.",
                        flow_manager, tenant, state
                    )
                else:
                    state.pop("new_date", None)
                    state.pop("available_slots", None)
                    return await _respond(
                        "Ten dzień właśnie się zapełnił. Proszę wybrać inny dzień.",
                        flow_manager, tenant, state
                    )

            state["new_time"] = parsed_time
            state["available_slots"] = current_slots
            flow_manager.state["manage"] = state

        if "new_time" not in state:
            slots_text = _slots_summary(state.get("available_slots", []))
            return await _respond(
                f"Którą godzinę Pan wybiera? Wolne są: {slots_text}.",
                flow_manager, tenant, state
            )

        # --- Krok C: potwierdzenie zmiany ---
        if "reschedule_confirmed" not in state:
            if confirmation == "yes":
                state["reschedule_confirmed"] = True
            else:
                old_summary = format_booking_summary(booking)
                new_date_str = format_date_polish(state["new_date"])
                new_time_str = format_hour_polish(state["new_time"])
                return await _respond(
                    f"Zmieniam: {old_summary} → {new_date_str} o {new_time_str}. Czy potwierdzam?",
                    flow_manager, tenant, state
                )

        # --- Krok D: podwójna walidacja przed zapisem ---
        is_available, _ = await validate_slot_available(
            tenant, staff, service, state["new_date"], state["new_time"]
        )

        if not is_available:
            logger.warning("❌ Slot taken between confirmation and save!")
            state.pop("new_time", None)
            slots = await get_available_slots_from_api(
                tenant, staff, service, state["new_date"]
            )
            if slots:
                state["available_slots"] = slots
                slots_text = _slots_summary(slots)
                return await _respond(
                    f"Przepraszam, ta godzina właśnie została zajęta. Wolne są: {slots_text}.",
                    flow_manager, tenant, state
                )
            else:
                state.pop("new_date", None)
                state.pop("available_slots", None)
                return await _respond(
                    "Przepraszam, ten dzień właśnie się zapełnił. Proszę wybrać inny dzień.",
                    flow_manager, tenant, state
                )

        # --- Krok E: wykonaj zmianę ---
        booking_id = booking.get("id") or booking.get("bookingId")
        new_date_api = state["new_date"].strftime("%Y-%m-%d")
        new_time_api = state["new_time"]

        success = await reschedule_booking_api(tenant, booking_id, new_date_api, new_time_api)

        if success:
            await _notify_owner_reschedule(tenant, booking, new_date_api, new_time_api, caller_phone)
            flow_manager.state["manage"] = {}
            from flows import create_anything_else_node
            new_date_formatted = format_date_polish(state["new_date"])
            new_time_formatted = format_hour_polish(state["new_time"])
            await flow_manager.task.queue_frame(TTSSpeakFrame(
                text=f"Gotowe! Wizyta przełożona na {new_date_formatted} o {new_time_formatted}. "
                     f"Czy mogę jeszcze w czymś pomóc?"
            ))
            return (None, create_anything_else_node(tenant))
        else:
            return await _fallback_api_error(flow_manager, tenant, state)

    # ==========================================================================
    # NIEJASNA INTENCJA — mamy wizytę, ale nie wiemy co klient chce
    # ==========================================================================
    return await _respond(
        "Czy chce Pan anulować wizytę czy zmienić termin?",
        flow_manager, tenant, state
    )


# ============================================================================
# OBSŁUGA WYBORU Z LISTY WIZYT
# ============================================================================

async def _handle_list_choice(
    spoken_choice: str,
    date_text: str,
    time_text: str,
    booking_code: str,
    state: Dict,
    flow_manager: FlowManager,
    tenant: Dict,
) -> Tuple:
    """Klient wybiera wizytę z listy (gdy ma ich kilka)"""
    bookings = state["bookings_list"]
    selected = None

    # Zbierz wszystkie wskazówki od klienta
    hint = " ".join(filter(None, [spoken_choice, date_text, time_text, booking_code])).lower()

    if any(kw in hint for kw in ["ostatni", "ostatnia", "ostatnie", "końcow"]):
        selected = bookings[-1]
    else:
        for kw, idx in [
            ("pierwsz", 0), ("jedyn", 0), ("1", 0),
            ("drug", 1), ("dwie", 1), ("2", 1),
            ("trzec", 2), ("trzy", 2), ("3", 2),
        ]:
            if kw in hint:
                if idx < len(bookings):
                    selected = bookings[idx]
                break

    if selected:
        # Sprawdź limit czasu
        can_modify, limit_msg = _check_modify_time_limit(selected, tenant)
        if not can_modify:
            state.pop("bookings_list", None)
            flow_manager.state["manage"] = state
            return await _respond(limit_msg, flow_manager, tenant, state)

        state["booking"] = selected
        state.pop("bookings_list", None)
        flow_manager.state["manage"] = state
        summary = format_booking_summary(selected)
        return await _respond(
            f"Dobrze, wizyta: {summary}. Co chce Pan zrobić — anulować czy zmienić termin?",
            flow_manager, tenant, state
        )
    else:
        return await _respond(
            "Proszę powiedzieć która wizyta: pierwsza, druga lub trzecia.",
            flow_manager, tenant, state
        )


# ============================================================================
# SPRAWDZENIE LIMITU CZASU NA ZMIANY
# ============================================================================

def _check_modify_time_limit(booking: Dict, tenant: Dict) -> Tuple[bool, str]:
    """
    Sprawdza czy wizyta jest wystarczająco daleko w przyszłości,
    żeby można ją było modyfikować.
    
    Returns:
        (can_modify: bool, message: str)
    """
    try:
        date_str = booking.get("booking_date") or booking.get("date", "")
        time_str = booking.get("booking_time") or booking.get("time", "")
        booking_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except Exception as e:
        logger.warning(f"⚠️ Cannot parse booking datetime: {e}")
        return (True, "")  # Nie blokuj jeśli nie możemy sparsować

    min_cancel_hours = int(tenant.get("min_cancel_hours") or 24)
    min_cancel_time = datetime.now() + timedelta(hours=min_cancel_hours)

    if booking_dt < min_cancel_time:
        hours_left = max(0, int((booking_dt - datetime.now()).total_seconds() / 3600))
        msg = (
            f"Przepraszam, wizytę można zmienić lub anulować minimum {min_cancel_hours} "
            f"godziny przed terminem. Do wizyty pozostało tylko {hours_left} godzin. "
            f"Proszę skontaktować się z salonem bezpośrednio."
        )
        return (False, msg)

    return (True, "")


# ============================================================================
# FUNKCJE POMOCNICZE
# ============================================================================

async def _respond(
    text: str,
    flow_manager: FlowManager,
    tenant: Dict,
    state: Dict,
) -> Tuple:
    """Wysyła odpowiedź TTS i wraca do manage node"""
    flow_manager.state["manage"] = state
    await flow_manager.task.queue_frame(TTSSpeakFrame(text=text))
    logger.info(f"🎤 MANAGE RESPONSE: {text[:80]}...")
    return (None, create_manage_node(tenant))


async def _fallback_no_booking(
    flow_manager: FlowManager, tenant: Dict, state: Dict
) -> Tuple:
    """Nie znaleziono wizyty — przekieruj lub wyślij wiadomość"""
    transfer_enabled = tenant.get("transfer_enabled", False)
    transfer_number = tenant.get("transfer_number", "")

    if transfer_enabled and transfer_number:
        logger.info("📞 Manage fallback → transfer")
        await flow_manager.task.queue_frame(TTSSpeakFrame(
            text="Nie znalazłam tej wizyty w systemie. Połączę Pana z salonem bezpośrednio."
        ))
        from flows_contact import handle_transfer_request
        return await handle_transfer_request({}, flow_manager)
    else:
        logger.info("📧 Manage fallback → message to owner")
        return await _respond(
            "Nie znalazłam tej wizyty w systemie. "
            "Mogę przekazać wiadomość do właściciela — oddzwonią do Pana. Czy mam to zrobić?",
            flow_manager, tenant, state
        )


async def _fallback_api_error(
    flow_manager: FlowManager, tenant: Dict, state: Dict
) -> Tuple:
    """Błąd API przy zapisie"""
    transfer_enabled = tenant.get("transfer_enabled", False)
    transfer_number = tenant.get("transfer_number", "")

    if transfer_enabled and transfer_number:
        await flow_manager.task.queue_frame(TTSSpeakFrame(
            text="Przepraszam, wystąpił problem techniczny. Połączę Pana z salonem bezpośrednio."
        ))
        from flows_contact import handle_transfer_request
        return await handle_transfer_request({}, flow_manager)
    else:
        return await _respond(
            "Przepraszam, wystąpił problem techniczny. "
            "Proszę spróbować za chwilę lub skontaktować się z salonem bezpośrednio.",
            flow_manager, tenant, state
        )


async def _notify_owner_cancel(tenant: Dict, booking: Dict, caller_phone: str):
    """Powiadomienie do właściciela o anulowaniu"""
    try:
        from flows_helpers import send_owner_notification
        summary = format_booking_summary(booking)
        await send_owner_notification(
            tenant=tenant,
            subject="Anulowanie wizyty przez telefon",
            body=(
                f"Klient ({caller_phone}) anulował wizytę przez bota.\n\n"
                f"Wizyta: {summary}\n"
                f"ID: {booking.get('id')}"
            )
        )
    except Exception as e:
        logger.warning(f"⚠️ Owner notify (cancel) failed: {e}")


async def _notify_owner_reschedule(
    tenant: Dict, booking: Dict,
    new_date: str, new_time: str, caller_phone: str
):
    """Powiadomienie do właściciela o zmianie terminu"""
    try:
        from flows_helpers import send_owner_notification
        old_summary = format_booking_summary(booking)
        await send_owner_notification(
            tenant=tenant,
            subject="Zmiana terminu wizyty przez telefon",
            body=(
                f"Klient ({caller_phone}) zmienił termin wizyty przez bota.\n\n"
                f"Poprzedni termin: {old_summary}\n"
                f"Nowy termin: {new_date} o {new_time}\n"
                f"ID: {booking.get('id')}"
            )
        )
    except Exception as e:
        logger.warning(f"⚠️ Owner notify (reschedule) failed: {e}")


# ============================================================================
# NODE
# GPT tylko wyciąga dane z wypowiedzi — tak samo jak booking_simple
# ============================================================================

def create_manage_node(tenant: Dict) -> Dict:
    return {
        "name": "manage_appointment",
        "respond_immediately": False,

        "role_messages": [{
            "role": "system",
            "content": (
                "Jesteś asystentką pomagającą zarządzać istniejącą wizytą. "
                "Używaj formy 'Pan/Pani'. Mów krótko i naturalnie."
            )
        }],

        "task_messages": [{
            "role": "system",
            "content": """ZAWSZE wywołuj manage_appointment z tym co klient powiedział.

Przykłady dopasowania:
- "chcę anulować" → action="cancel", confirmation="none"
- "tak, anuluj" → action="cancel", confirmation="yes"
- "nie, nie anuluj" → action="cancel", confirmation="no"
- "chcę przełożyć" → action="reschedule", confirmation="none"
- "chcę przełożyć na piątek" → action="reschedule", date_text="piątek", confirmation="none"
- "na czternastą" → action="reschedule", time_text="14:00", confirmation="none"
- "na wpół do dwunastej" → action="reschedule", time_text="11:30", confirmation="none"
- "mój kod to 1234" → action="none", booking_code="1234", confirmation="none"
- "tak" → action="none", confirmation="yes"
- "nie" → action="none", confirmation="no"
- "tę pierwszą" → action="none", spoken_choice="pierwsza", confirmation="none"
- "drugą wizytę" → action="none", spoken_choice="druga", confirmation="none"
- "o tej ostatniej" → action="none", spoken_choice="ostatnia", confirmation="none"
"""
        }],

        "functions": [
            manage_appointment_function(tenant),
        ]
    }


# ============================================================================
# START
# ============================================================================

def start_manage_function() -> FlowsFunctionSchema:
    """Funkcja startowa — klient chce zarządzać wizytą"""
    return FlowsFunctionSchema(
        name="manage_booking",
        description=(
            "Klient chce zmienić, przełożyć lub anulować istniejącą wizytę. "
            "Używaj gdy klient mówi: 'chcę odwołać', 'chcę przełożyć', "
            "'mam wizytę i chcę zmienić', 'anuluj moją wizytę'."
        ),
        properties={},
        required=[],
        handler=handle_start_manage,
    )


async def handle_start_manage(args: Dict, flow_manager: FlowManager) -> Tuple:
    """Handler startowy — resetuje stan i szuka wizyty od razu"""
    tenant = flow_manager.state.get("tenant", {})
    flow_manager.state["manage"] = {}

    logger.info("🔧 MANAGE START")

    await flow_manager.task.queue_frame(TTSSpeakFrame(
        text="Pomogę Panu zmienić lub anulować wizytę. Chwileczkę, sprawdzam."
    ))

    return (None, create_manage_node(tenant))


# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    "start_manage_function",
    "manage_appointment_function",
    "create_manage_node",
]