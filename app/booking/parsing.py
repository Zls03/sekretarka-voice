"""Parsowanie dat i godzin podawanych przez klienta w języku naturalnym."""

import re
from datetime import datetime

DATEPARSER_SETTINGS = {
    'PREFER_DATES_FROM': 'future',
    'PREFER_DAY_OF_MONTH': 'first',
    'RETURN_AS_TIMEZONE_AWARE': False,
}


def preprocess_date_text(date_text: str) -> str:
    """Czyści tekst daty przed przekazaniem do dateparser — usuwa polskie przyimki i
    modyfikatory czasowe."""
    if not date_text:
        return date_text

    text = date_text.lower().strip()

    time_modifiers = [
        " po południu", " popołudniu", " popoludniu",
        " rano", " wieczorem", " przed południem",
        " po poludniu",
    ]
    for mod in time_modifiers:
        text = text.replace(mod, "")

    prefixes_to_remove = ["na ", "w dniu ", "dnia ", "w ", "we "]
    for prefix in prefixes_to_remove:
        if text.startswith(prefix):
            text = text[len(prefix):]
            break

    day_mappings = {
        "poniedziałek": "poniedziałek", "wtorek": "wtorek",
        "środę": "środa", "środe": "środa",
        "czwartek": "czwartek", "piątek": "piątek",
        "sobotę": "sobota", "sobote": "sobota",
        "niedzielę": "niedziela", "niedziele": "niedziela",
    }
    for wrong, correct in day_mappings.items():
        if text == wrong or text.startswith(wrong + " "):
            text = text.replace(wrong, correct, 1)
            break

    return text.strip()


def _parse_time(text: str) -> str | None:
    """Parsuje godzinę z tekstu polskiego"""
    if not text:
        return None

    text = text.lower().strip()

    stt_time_fixes = {
        "siedem zer zero": "7:00", "siedem zero zero": "7:00", "siedem zero": "7:00",
        "osiem zer zero": "8:00", "osiem zero zero": "8:00", "osiem zero": "8:00",
        "dziewięć zer zero": "9:00", "dziewięć zero": "9:00",
    }
    for wrong, correct in stt_time_fixes.items():
        if wrong in text:
            return correct

    if "wpół do" in text or "w pół do" in text:
        wpol_mappings = {
            "siódmej": "6:30", "siedmej": "6:30",
            "ósmej": "7:30", "osmej": "7:30",
            "dziewiątej": "8:30", "dziewiatej": "8:30",
            "dziesiątej": "9:30", "dziesiatej": "9:30",
            "jedenastej": "10:30", "dwunastej": "11:30",
            "trzynastej": "12:30", "czternastej": "13:30",
            "piętnastej": "14:30", "pietnastej": "14:30",
            "szesnastej": "15:30", "siedemnastej": "16:30",
            "osiemnastej": "17:30",
        }
        for word, time in wpol_mappings.items():
            if word in text:
                return time

    has_thirty = any(x in text for x in ["trzydzieści", "trzydziesci", "30", ":30"])
    word_to_hour = {
        "dziewiąt": 9, "dziesiąt": 10, "jedenast": 11, "dwunast": 12,
        "trzynast": 13, "czternast": 14, "piętnast": 15, "szesnast": 16,
        "siedemnast": 17, "osiemnast": 18, "dziewiętnast": 19, "dwudziest": 20,
        "ósm": 8, "siódm": 7,
    }
    for word, hour in word_to_hour.items():
        if word in text:
            minutes = "30" if has_thirty else "00"
            return f"{hour}:{minutes}"

    match = re.search(r'(\d{1,2})[:\.](\d{2})', text)
    if match:
        return f"{int(match.group(1))}:{match.group(2)}"

    match = re.search(r'(?:o|na|godzin[aeę]?)\s*(\d{1,2})', text)
    if match:
        return f"{int(match.group(1))}:00"

    match = re.search(r'\b(\d{1,2})\b', text)
    if match:
        hour = int(match.group(1))
        if 7 <= hour <= 21:
            return f"{hour}:00"

    return None


def _normalize_time(time_val) -> str:
    """Normalizuje czas do formatu H:MM dla porównań"""
    if isinstance(time_val, str):
        if ":" in time_val:
            parts = time_val.split(":")
            h = int(parts[0])
            m = parts[1].zfill(2)
            return f"{h}:{m}"
        return f"{int(time_val)}:00"
    elif isinstance(time_val, int):
        return f"{time_val}:00"
    return str(time_val)


# ODWOŁYWANIE / PRZEKŁADANIE ISTNIEJĄCEJ WIZYTY — manage_booking
#
# W przeciwieństwie do book_appointment (rezerwacja W TOKU tej rozmowy, trzymana w
# call_state["booking"]) ten tool operuje na wizytach zapisanych WCZEŚNIEJ, w innych
# rozmowach — znalezionych po numerze dzwoniącego przez panel CRM (ten sam
# get_client_profile() co karmi CRM hint w system prompcie, patrz realtime_prompt.py).
# Klient NIE podaje kodu wizyty — identyfikacja jest wyłącznie po caller_phone, dokładnie
# jak przy book_appointment (klient też nie zna żadnych wewnętrznych ID).
#
# Realny automatyczny cancel/reschedule (nie "zostawię wiadomość właścicielowi" jak w
# cascade — patrz flows.py::handle_manage_booking) jest możliwy bo panel ma już gotowe
# PATCH/DELETE /api/panel/{slug}/bookings/{id} (usuwa/odtwarza wydarzenie w Google
# Calendar, wysyła maila do pracownika) — tylko nikt wcześniej nie podłączył tego pod
# telefon. Fallback na "brak wizyty" gdy get_client_profile nie widzi nic z booking_id
# (np. wizyta wpisana ręcznie do CRM bez odpowiadającego rekordu w `bookings`, albo panel
# offline) — wtedy model ma w opisie narzędzia instrukcję żeby zaproponować contact_owner.
def _parse_iso_dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)
