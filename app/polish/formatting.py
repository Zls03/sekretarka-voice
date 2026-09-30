"""Formatowanie po polsku: godziny i daty słownie, dni tygodnia, listy wyliczeniowe."""

from datetime import datetime, timedelta

# Odwrotne mapowanie - liczba na słowo (do TTS)
NUMBER_TO_HOUR_WORD = {
    6: "szóstej",
    7: "siódmej",
    8: "ósmej",
    9: "dziewiątej",
    10: "dziesiątej",
    11: "jedenastej",
    12: "dwunastej",
    13: "trzynastej",
    14: "czternastej",
    15: "piętnastej",
    16: "szesnastej",
    17: "siedemnastej",
    18: "osiemnastej",
    19: "dziewiętnastej",
    20: "dwudziestej",
    21: "dwudziestej pierwszej",
    22: "dwudziestej drugiej",
}


NUMBER_TO_DAY = {
    0: "poniedziałek",
    1: "wtorek",
    2: "środa",
    3: "czwartek",
    4: "piątek",
    5: "sobota",
    6: "niedziela",
}


def natural_list(items: list, connector: str = "i") -> str:
    """
    Tworzy naturalną listę po polsku.

    Args:
        items: Lista elementów
        connector: Łącznik (domyślnie "i", może być "lub", "albo")

    Przykłady:
        natural_list(["Ania"]) → "Ania"
        natural_list(["Ania", "Wiktor"]) → "Ania i Wiktor"
        natural_list(["9:00", "10:00", "11:00"]) → "9:00, 10:00 i 11:00"
        natural_list(["A", "B"], "lub") → "A lub B"
    """
    if not items:
        return ""

    # Konwertuj wszystko na stringi
    str_items = [str(i) for i in items]

    if len(str_items) == 1:
        return str_items[0]
    elif len(str_items) == 2:
        return f"{str_items[0]} {connector} {str_items[1]}"
    else:
        return ", ".join(str_items[:-1]) + f" {connector} " + str_items[-1]


POLISH_DAYS = NUMBER_TO_DAY


def format_hour_polish(hour) -> str:
    """Formatuj godzinę po polsku słownie (obsługuje H:MM i int)"""

    # Jeśli string "14:30" lub "14:00"
    if isinstance(hour, str) and ":" in hour:
        parts = hour.split(":")
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0

        hour_word = NUMBER_TO_HOUR_WORD.get(h, str(h))
        if m == 30:
            return f"{hour_word} trzydzieści"
        elif m == 0:
            return hour_word
        else:
            return f"{hour_word} {m:02d}"

    # Jeśli int
    if isinstance(hour, int):
        return NUMBER_TO_HOUR_WORD.get(hour, str(hour))

    return str(hour)


def format_date_polish(date: datetime) -> str:
    """Formatuj datę po polsku - naturalnie słownie"""
    today = datetime.now().date()
    target = date.date()

    if target == today:
        return "dziś"
    elif target == today + timedelta(days=1):
        return "jutro"
    elif target == today + timedelta(days=2):
        return "pojutrze"
    else:
        # Biernik po "w" (w poniedziałek, w środę, w sobotę...)
        DAYS_ACCUSATIVE = {
            0: "poniedziałek",
            1: "wtorek",
            2: "środę",
            3: "czwartek",
            4: "piątek",
            5: "sobotę",
            6: "niedzielę",
        }
        day_name = DAYS_ACCUSATIVE[target.weekday()]

        # Miesiące po polsku w dopełniaczu
        POLISH_MONTHS = {
            1: "stycznia",
            2: "lutego",
            3: "marca",
            4: "kwietnia",
            5: "maja",
            6: "czerwca",
            7: "lipca",
            8: "sierpnia",
            9: "września",
            10: "października",
            11: "listopada",
            12: "grudnia",
        }

        month_name = POLISH_MONTHS.get(target.month, str(target.month))

        preposition = "we" if target.weekday() == 1 else "w"  # we wtorek, w środę
        return f"{preposition} {day_name}, {target.day} {month_name}"
