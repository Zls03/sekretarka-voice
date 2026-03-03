# flows_manage.py - Zarządzanie istniejącymi wizytami
# WERSJA 1.0
"""
Obsługuje:
- Wyszukiwanie wizyty po numerze telefonu (caller_phone)
- Fallback na kod wizyty z SMS
- Anulowanie wizyty
- Zmianę daty/godziny
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
    save_booking_to_api,
    send_booking_sms,
    increment_sms_count,
    POLISH_DAYS,
)
from flows_booking_simple import (
    get_next_available_days,
    format_availability_message,
    _slots_summary,
    validate_slot_available,
    preprocess_date_text,
    _parse_time,
    _normalize_time,
    DATEPARSER_SETTINGS,
)
from polish_mappings import odmien_imie, detect_gender

PANEL_API_URL = os.getenv("PANEL_API_URL", "http://localhost:3000")


# ============================================================================
# API - WYSZUKIWANIE I EDYCJA WIZYT
# ============================================================================

async def find_bookings_by_phone(tenant: dict, phone: str) -> List[Dict]:
    """Szuka wizyt po numerze telefonu w bazie systemu"""
    slug = tenant.get("slug", "")
    if not slug or not phone:
        return []

    # Normalizuj numer
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
    """Przesuwa wizytę na nowy termin przez API"""
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
    date_str = booking.get("date", "")
    time_str = booking.get("time", "")
    code = booking.get("visit_code") or booking.get("booking_code", "")

    # Formatuj datę
    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        date_formatted = format_date_polish(date_obj)
    except Exception:
        date_formatted = date_str

    # Formatuj godzinę
    try:
        time_formatted = format_hour_polish(time_str)
    except Exception:
        time_formatted = time_str

    staff_part = f" u {odmien_imie(staff)}" if staff else ""
    return f"{service}{staff_part}, {date_formatted} o {time_formatted}"


# ============================================================================
# GŁÓWNA FUNKCJA GPT
# ============================================================================

def manage_appointment_function(tenant: Dict) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name="manage_appointment",
        description="Zarządzaj istniejącą wizytą: anulowanie lub zmiana terminu.",
        properties={
            "action": {
                "type": "string",
                "enum": ["cancel", "reschedule", "ask_code", "none"],
                "description": (
                    "cancel=anuluj wizytę, reschedule=zmień termin, "
                    "ask_code=klient podaje kod wizyty, none=niejasne"
                )
            },
            "booking_code": {
                "type": "string",
                "description": "Kod wizyty podany przez klienta (4 cyfry z SMS) lub null"
            },
            "date_text": {
                "type": "string",
                "description": "Nowa data podana przez klienta ('jutro', 'w piątek') lub null"
            },
            "time_text": {
                "type": "string",
                "description": "Nowa godzina w formacie HH:MM lub null"
            },
            "confirmation": {
                "type": "string",
                "enum": ["yes", "no", "none"],
                "description": "yes=potwierdza akcję, no=rezygnuje, none=nic z tych"
            },
        },
        required=["action", "confirmation"],
        handler=lambda args, fm: handle_manage_appointment(args, fm, tenant),
    )


# ============================================================================
# GŁÓWNY HANDLER
# ============================================================================

async def handle_manage_appointment(
    args: Dict, flow_manager: FlowManager, tenant: Dict
) -> Tuple:
    action = args.get("action", "none")
    booking_code = args.get("booking_code")
    date_text = args.get("date_text")
    time_text = args.get("time_text")
    confirmation = args.get("confirmation", "none")

    state = flow_manager.state.get("manage", {})
    caller_phone = flow_manager.state.get("caller_phone", "")

    logger.info(
        f"📥 MANAGE: action={action}, code={booking_code}, "
        f"date={date_text}, time={time_text}, confirm={confirmation}"
    )

    # === KROK 1: Znajdź wizytę jeśli jeszcze nie mamy ===
    if "booking" not in state:
        booking = None

        # 1a. Szukaj po numerze telefonu
        if caller_phone:
            bookings = await find_bookings_by_phone(tenant, caller_phone)
            if len(bookings) == 1:
                booking = bookings[0]
                logger.info(f"✅ Found booking by phone: {booking.get('id')}")
            elif len(bookings) > 1:
                # Wiele wizyt — przedstaw i zapytaj o którą
                state["bookings_list"] = bookings
                flow_manager.state["manage"] = state
                lines = []
                for i, b in enumerate(bookings[:3], 1):
                    lines.append(f"{i}. {format_booking_summary(b)}")
                summary = ", ".join(lines)
                return await _respond_manage(
                    f"Znalazłam kilka wizyt na Pana numer: {summary}. "
                    f"O której wizycie chce Pan porozmawiać?",
                    flow_manager, tenant, state
                )

        # 1b. Klient podał kod
        if not booking and booking_code:
            booking = await find_booking_by_code(tenant, booking_code.strip())
            if booking:
                logger.info(f"✅ Found booking by code: {booking_code}")

        # 1c. Nie znaleziono — zapytaj o kod
        if not booking and not booking_code:
            return await _respond_manage(
                "Nie znalazłam rezerwacji przypisanej do tego numeru telefonu. "
                "Czy ma Pan kod wizyty z SMS-a?",
                flow_manager, tenant, state
            )

        # 1d. Nadal nic — fallback
        if not booking:
            return await _fallback_no_booking(flow_manager, tenant, state)

        state["booking"] = booking
        flow_manager.state["manage"] = state

        # Przedstaw wizytę
        summary = format_booking_summary(booking)
        return await _respond_manage(
            f"Znalazłam Pana wizytę: {summary}. "
            f"Co chce Pan zrobić — anulować czy zmienić termin?",
            flow_manager, tenant, state
        )

    # === Wybór z listy wielu wizyt ===
    if "bookings_list" in state and "booking" not in state:
        bookings = state["bookings_list"]
        # Spróbuj dopasować po numerze porządkowym lub dacie
        # Uproszczenie: jeśli klient powiedział "pierwsza" lub "1" itp.
        selected = None
        for kw, idx in [
            ("pierwsz", 0), ("jedna", 0), ("1", 0),
            ("drug", 1), ("dwie", 1), ("2", 1),
            ("trzec", 2), ("trzy", 2), ("3", 2),
        ]:
            spoken = (date_text or "") + (time_text or "") + (booking_code or "")
            if kw in spoken.lower():
                if idx < len(bookings):
                    selected = bookings[idx]
                break

        if selected:
            state["booking"] = selected
            state.pop("bookings_list", None)
            flow_manager.state["manage"] = state
            summary = format_booking_summary(selected)
            return await _respond_manage(
                f"Dobrze, wizyta: {summary}. Co chce Pan zrobić?",
                flow_manager, tenant, state
            )
        else:
            return await _respond_manage(
                "Proszę powiedzieć która wizyta: pierwsza, druga lub trzecia.",
                flow_manager, tenant, state
            )

    booking = state["booking"]

    # === KROK 2: Anulowanie ===
    if action == "cancel":
        if "cancel_confirmed" not in state:
            if confirmation == "yes":
                state["cancel_confirmed"] = True
            else:
                summary = format_booking_summary(booking)
                return await _respond_manage(
                    f"Czy na pewno chce Pan anulować: {summary}?",
                    flow_manager, tenant, state
                )

        # Wykonaj anulowanie
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

    # === KROK 3: Zmiana terminu ===
    if action == "reschedule":

        # 3a. Zbierz nową datę
        if date_text and "new_date" not in state:
            date_clean = preprocess_date_text(date_text)
            parsed_date = dateparser.parse(
                date_clean, languages=["pl"], settings=DATEPARSER_SETTINGS
            )

            if not parsed_date:
                return await _respond_manage(
                    f"Nie rozumiem daty '{date_text}'. Proszę powiedzieć np. 'jutro', 'w piątek'.",
                    flow_manager, tenant, state
                )

            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            if parsed_date.date() < today.date():
                return await _respond_manage(
                    "Ta data już minęła. Proszę podać przyszłą datę.",
                    flow_manager, tenant, state
                )

            # Pobierz dane pracownika z wizyty
            staff = _booking_to_staff(booking, tenant)
            service = _booking_to_service(booking, tenant)

            is_valid, msg = validate_date_constraints(parsed_date, tenant, staff)
            if not is_valid:
                return await _respond_manage(msg, flow_manager, tenant, state)

            # Sprawdź sloty
            slots = await get_available_slots_from_api(tenant, staff, service, parsed_date)
            if not slots:
                available_days = await get_next_available_days(
                    tenant, staff, service,
                    max_days=int(staff.get("max_booking_days") or 14), limit=3
                )
                if available_days:
                    suggestion = format_availability_message(available_days)
                    return await _respond_manage(
                        f"Na {format_date_polish(parsed_date)} nie ma wolnych terminów. {suggestion}",
                        flow_manager, tenant, state
                    )
                else:
                    max_days = int(staff.get("max_booking_days") or 14)
                    return await _respond_manage(
                        f"Na {format_date_polish(parsed_date)} nie ma wolnych terminów "
                        f"i w najbliższych {max_days} dniach grafik jest pełny. "
                        f"Nowe terminy pojawiają się codziennie — proszę spróbować jutro.",
                        flow_manager, tenant, state
                    )

            state["new_date"] = parsed_date
            state["available_slots"] = slots
            flow_manager.state["manage"] = state

            slots_text = _slots_summary(slots)
            return await _respond_manage(
                f"Na {format_date_polish(parsed_date)} wolne są: {slots_text}. Którą godzinę Pan wybiera?",
                flow_manager, tenant, state
            )

        if "new_date" not in state:
            return await _respond_manage(
                "Na jaki dzień chce Pan przełożyć wizytę?",
                flow_manager, tenant, state
            )

        # 3b. Zbierz nową godzinę
        if time_text and "new_time" not in state:
            parsed_time = _parse_time(time_text)
            if not parsed_time:
                slots_text = _slots_summary(state.get("available_slots", []))
                return await _respond_manage(
                    f"Nie rozumiem godziny '{time_text}'. Wolne są: {slots_text}.",
                    flow_manager, tenant, state
                )

            staff = _booking_to_staff(booking, tenant)
            service = _booking_to_service(booking, tenant)

            is_available, current_slots = await validate_slot_available(
                tenant, staff, service, state["new_date"], parsed_time
            )

            if not is_available:
                if current_slots:
                    slots_text = _slots_summary(current_slots)
                    return await _respond_manage(
                        f"Niestety {format_hour_polish(parsed_time)} jest już zajęta. "
                        f"Wolne są: {slots_text}.",
                        flow_manager, tenant, state
                    )
                else:
                    state.pop("new_date", None)
                    return await _respond_manage(
                        "Ten dzień właśnie się zapełnił. Proszę wybrać inny dzień.",
                        flow_manager, tenant, state
                    )

            state["new_time"] = parsed_time
            state["available_slots"] = current_slots
            flow_manager.state["manage"] = state

        if "new_time" not in state:
            slots_text = _slots_summary(state.get("available_slots", []))
            return await _respond_manage(
                f"Którą godzinę Pan wybiera? Wolne są: {slots_text}.",
                flow_manager, tenant, state
            )

        # 3c. Potwierdzenie zmiany
        if "reschedule_confirmed" not in state:
            if confirmation == "yes":
                state["reschedule_confirmed"] = True
            else:
                old_summary = format_booking_summary(booking)
                new_date_str = format_date_polish(state["new_date"])
                new_time_str = format_hour_polish(state["new_time"])
                return await _respond_manage(
                    f"Zmieniam wizytę z: {old_summary} "
                    f"na {new_date_str} o {new_time_str}. Czy potwierdzam?",
                    flow_manager, tenant, state
                )

        # Wykonaj zmianę
        booking_id = booking.get("id") or booking.get("bookingId")
        new_date_str = state["new_date"].strftime("%Y-%m-%d")
        new_time_str = state["new_time"]

        success = await reschedule_booking_api(tenant, booking_id, new_date_str, new_time_str)

        if success:
            await _notify_owner_reschedule(tenant, booking, new_date_str, new_time_str, caller_phone)
            flow_manager.state["manage"] = {}
            from flows import create_anything_else_node
            new_date_formatted = format_date_polish(state["new_date"])
            new_time_formatted = format_hour_polish(state["new_time"])
            await flow_manager.task.queue_frame(TTSSpeakFrame(
                text=f"Gotowe! Wizyta przełożona na {new_date_formatted} "
                     f"o {new_time_formatted}. Czy mogę jeszcze w czymś pomóc?"
            ))
            return (None, create_anything_else_node(tenant))
        else:
            return await _fallback_api_error(flow_manager, tenant, state)

    # === Rezygnacja ===
    if confirmation == "no":
        flow_manager.state["manage"] = {}
        from flows import create_anything_else_node
        await flow_manager.task.queue_frame(TTSSpeakFrame(
            text="Dobrze, nie wprowadzam żadnych zmian. Czy mogę jeszcze w czymś pomóc?"
        ))
        return (None, create_anything_else_node(tenant))

    # === Niejasna intencja ===
    return await _respond_manage(
        "Czy chce Pan anulować wizytę czy zmienić termin?",
        flow_manager, tenant, state
    )


# ============================================================================
# POMOCNICZE
# ============================================================================

def _booking_to_staff(booking: Dict, tenant: Dict) -> Dict:
    """Wyciąga obiekt staff z danych wizyty"""
    staff_id = booking.get("staff_id")
    staff_list = tenant.get("staff", [])
    found = next((s for s in staff_list if str(s.get("id")) == str(staff_id)), None)
    if found:
        return found
    # Fallback — zwróć minimalny obiekt żeby API działało
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


async def _respond_manage(
    text: str,
    flow_manager: FlowManager,
    tenant: Dict,
    state: Dict,
) -> Tuple:
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
        from flows_contact import handle_transfer_request
        logger.info("📞 Manage fallback → transfer")
        await flow_manager.task.queue_frame(TTSSpeakFrame(
            text="Nie znalazłam tej wizyty w systemie. "
                 "Połączę Pana z salonem bezpośrednio."
        ))
        # Użyj istniejącego mechanizmu transferu
        fake_args = {}
        return await handle_transfer_request(fake_args, flow_manager)
    else:
        logger.info("📧 Manage fallback → message to owner")
        await flow_manager.task.queue_frame(TTSSpeakFrame(
            text="Nie znalazłam tej wizyty w systemie. "
                 "Mogę przekazać wiadomość do właściciela — "
                 "oddzwonią do Pana. Czy mam to zrobić?"
        ))
        flow_manager.state["manage"] = {**state, "fallback_message": True}
        return (None, create_manage_node(tenant))


async def _fallback_api_error(
    flow_manager: FlowManager, tenant: Dict, state: Dict
) -> Tuple:
    """Błąd API — nie udało się wykonać operacji"""
    transfer_enabled = tenant.get("transfer_enabled", False)
    transfer_number = tenant.get("transfer_number", "")

    if transfer_enabled and transfer_number:
        await flow_manager.task.queue_frame(TTSSpeakFrame(
            text="Przepraszam, wystąpił problem techniczny. "
                 "Połączę Pana z salonem bezpośrednio."
        ))
        from flows_contact import handle_transfer_request
        return await handle_transfer_request({}, flow_manager)
    else:
        return await _respond_manage(
            "Przepraszam, wystąpił problem techniczny. "
            "Proszę spróbować za chwilę lub skontaktować się z salonem bezpośrednio.",
            flow_manager, tenant, state
        )


async def _notify_owner_cancel(tenant: Dict, booking: Dict, caller_phone: str):
    """Wysyła email do właściciela o anulowaniu"""
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
    """Wysyła email do właściciela o zmianie terminu"""
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
# ============================================================================

def create_manage_node(tenant: Dict) -> Dict:
    return {
        "name": "manage_appointment",
        "respond_immediately": False,

        "role_messages": [{
            "role": "system",
            "content": """Jesteś asystentką pomagającą zarządzać istniejącą wizytą.
Klient może chcieć anulować wizytę lub zmienić jej termin.
Używaj formy 'Pan/Pani'. Mów krótko i naturalnie."""
        }],

        "task_messages": [{
            "role": "system",
            "content": """ZAWSZE wywołuj manage_appointment.

Przykłady:
- "chcę anulować" → action="cancel", confirmation="none"
- "tak, anuluj" → action="cancel", confirmation="yes"
- "chcę przełożyć na piątek" → action="reschedule", date_text="piątek"
- "na czternastą" → action="reschedule", time_text="14:00"
- "mój kod to 1234" → action="ask_code", booking_code="1234"
- "tak" → confirmation="yes"
- "nie" → confirmation="no"
- "nie, dziękuję" → confirmation="no"
"""
        }],

        "functions": [
            manage_appointment_function(tenant),
        ]
    }


def start_manage_function() -> FlowsFunctionSchema:
    """Funkcja startowa - wykrywa że klient chce zarządzać wizytą"""
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
    tenant = flow_manager.state.get("tenant", {})
    flow_manager.state["manage"] = {}

    logger.info("🔧 MANAGE START")

    await flow_manager.task.queue_frame(TTSSpeakFrame(
        text="Pomogę Panu zmienić lub anulować wizytę. Chwileczkę, sprawdzam."
    ))

    # Od razu szukaj po telefonie w tle — wynik pojawi się w pierwszym wywołaniu handlera
    return (None, create_manage_node(tenant))


# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    "start_manage_function",
    "manage_appointment_function",
    "create_manage_node",
]