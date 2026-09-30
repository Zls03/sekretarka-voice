"""Opis firmy dla modelu: usługi, cennik, godziny, adres, FAQ, forma gramatyczna asystenta."""

from app.polish.formatting import POLISH_DAYS


def assistant_gender_forms(assistant_name: str) -> dict:
    """
    Zwraca słownik z formami gramatycznymi na podstawie imienia asystenta.
    Imiona kończące się na 'a' = żeńskie, z wyjątkami dla imion męskich (Kuba, Barnaba...).
    """
    MESKIE_NA_A = {"kuba", "barnaba", "saba", "kosma", "bonawentura"}
    name_lower = (assistant_name or "").lower().strip()
    is_female = name_lower.endswith("a") and name_lower not in MESKIE_NA_A

    if is_female:
        return {
            "role_noun": "wirtualną asystentką (sekretarką)",
            "role_noun_short": "wirtualna asystentka",
            "role_booking": "asystentką rezerwacji",
            "gender_line": "Jesteś kobietą - mów w rodzaju żeńskim (zrobiłam, powiedziałam, zapisałam, pomogę)",
            "self_intro": f"Jestem {assistant_name}, wirtualna asystentka",
            "self_ai": "Jestem wirtualną asystentką, ale chętnie pomogę",
            "gender_short": "w rodzaju żeńskim (jestem asystentką)",
            "nie_dosłyszałam": "Nie dosłyszałam",
        }
    else:
        return {
            "role_noun": "wirtualnym asystentem (sekretarzem)",
            "role_noun_short": "wirtualny asystent",
            "role_booking": "asystentem rezerwacji",
            "gender_line": "Jesteś mężczyzną - mów w rodzaju męskim (zrobiłem, powiedziałem, zapisałem, pomogę)",
            "self_intro": f"Jestem {assistant_name}, wirtualny asystent",
            "self_ai": "Jestem wirtualnym asystentem, ale chętnie pomogę",
            "gender_short": "w rodzaju męskim (jestem asystentem)",
            "nie_dosłyszałam": "Nie dosłyszałem",
        }


def format_time_for_tts(time_str: str) -> str:
    """Usuwa zero wiodące z godziny: 08:00 → 8:00"""
    if not time_str:
        return time_str
    if time_str.startswith("0") and len(time_str) >= 2 and time_str[1].isdigit():
        return time_str[1:]
    return time_str


def build_business_context(tenant: dict) -> str:
    """Buduje kontekst o firmie dla GPT"""
    parts = []
    booking_enabled = tenant.get("booking_enabled", 1) == 1

    # Branża
    industry = tenant.get("industry", "").strip()
    if industry:
        parts.append(f"BRANŻA: {industry}")

    # Godziny pracy
    working_hours = tenant.get("working_hours", [])
    if working_hours:
        hours_text = []
        for wh in working_hours:
            day_num = wh.get("day_of_week", 0)
            if wh.get("open_time"):
                day_name = POLISH_DAYS.get(day_num, str(day_num))
                open_t = format_time_for_tts(wh["open_time"])
                close_t = format_time_for_tts(wh["close_time"])
                hours_text.append(f"{day_name}: {open_t}-{close_t}")
        if hours_text:
            parts.append(f"GODZINY PRACY: {', '.join(hours_text)}")

    # Usługi/Cennik - różne źródło w zależności od trybu
    if booking_enabled:
        # Tryb z rezerwacjami - usługi z kalendarza
        services = tenant.get("services", [])
        if services:
            svc_lines = []
            for s in services:
                price_text = (s.get("price_text") or "").strip()
                duration_text = (s.get("duration_text") or "").strip()
                price = s.get("price", "")
                duration = s.get("duration_minutes", 30)
                description = s.get("description", "").strip() if s.get("description") else ""
                price_display = price_text if price_text else (f"{price} zł" if price else "cena do uzgodnienia")
                duration_display = duration_text if duration_text else f"{duration} min"
                line = f"• {s['name']} = {price_display} ({duration_display})"
                if description:
                    line += f". Opis: {description}"
                svc_lines.append(line)
            cennik_text = "CENNIK (DOKŁADNE CENY - PODAWAJ DOKŁADNIE!):\n" + "\n".join(svc_lines)
            parts.append(cennik_text)
        else:
            parts.append("CENNIK: NIE SKONFIGUROWANY — nie znasz usług ani cen, nie podawaj żadnych")
    else:
        # Tryb informacyjny - usługi z info_services
        info_services = tenant.get("info_services", [])
        if info_services:
            svc_lines = []
            for s in info_services:
                name = s.get("name", "")
                price_text = (s.get("price_text") or "").strip()
                duration_text = (s.get("duration_text") or "").strip()
                price = s.get("price", "")
                duration = s.get("duration_minutes", "")
                description = s.get("description", "").strip() if s.get("description") else ""

                line = f"• {name}"
                if price_text:
                    line += f" {price_text}"
                elif price:
                    line += f" = {price} zł"
                if duration_text:
                    line += f". Czas: {duration_text}"
                elif duration:
                    line += f" (Trwa {duration} min)"
                if description:
                    line += f". Opis: {description}"
                svc_lines.append(line)
            cennik_text = "CENNIK (DOKŁADNE CENY - PODAWAJ DOKŁADNIE!):\n" + "\n".join(svc_lines)
            parts.append(cennik_text)
        else:
            parts.append("CENNIK: NIE SKONFIGUROWANY — nie znasz usług ani cen, nie podawaj żadnych")

        # Dodaj informację że rezerwacje są wyłączone
        parts.append(
            "UWAGA: Rezerwacje telefoniczne są WYŁĄCZONE. Jeśli klient pyta o rezerwację, poinformuj że nie jest dostępna przez telefon."
        )

    # Adres - formatuj ładnie dla wymowy TTS
    address = tenant.get("address", "").strip()
    if address:
        import re

        # Zamień skróty
        address = address.replace("ul.", "ulica").replace("ul ", "ulica ")
        address = address.replace("al.", "aleja").replace("al ", "aleja ")
        address = address.replace("pl.", "plac").replace("pl ", "plac ")

        # Dodaj "numer" przed liczbą w adresie (np. "Kwiatowa 15" → "Kwiatowa numer 15")
        # Szuka: spacja + cyfry + (koniec lub przecinek lub spacja)
        address = re.sub(r" (\d+)([,\s]|$)", r" numer \1\2", address)

        parts.append(f"ADRES: {address}")
    else:
        parts.append("ADRES: NIE SKONFIGUROWANY — nie znasz adresu firmy, nie podawaj żadnego adresu")

    # FAQ
    faq = tenant.get("faq", [])
    if faq:
        faq_text = []
        for f in faq:
            q = f.get("question", "")
            a = f.get("answer", "")
            if q and a:
                faq_text.append(f"Pytanie: {q} → Odpowiedź: {a}")
        if faq_text:
            parts.append("FAQ:\n" + "\n".join(faq_text))

    # Dodatkowe info
    additional = tenant.get("additional_info", "")
    if additional:
        parts.append(f"DODATKOWE INFO: {additional}")

    # Godziny pracy pracowników (dla trybu z rezerwacjami)
    if booking_enabled:
        staff = tenant.get("staff", [])
        if staff:
            staff_hours = []
            for s in staff:
                wh_json = s.get("working_hours_json", "")
                if wh_json and wh_json != "'{}'":
                    try:
                        import json

                        wh = json.loads(wh_json) if isinstance(wh_json, str) else wh_json
                        days_pl = {
                            "mon": "pon",
                            "tue": "wt",
                            "wed": "śr",
                            "thu": "czw",
                            "fri": "pt",
                            "sat": "sob",
                            "sun": "niedz",
                        }
                        hours_list = []
                        for day_en, day_pl in days_pl.items():
                            day_data = wh.get(day_en, {})
                            if day_data and not day_data.get("closed", False) and day_data.get("open"):
                                open_t = format_time_for_tts(day_data["open"])
                                close_t = format_time_for_tts(day_data["close"])
                                hours_list.append(f"{day_pl}: {open_t}-{close_t}")
                        if hours_list:
                            position = s.get("position", "").strip()
                            name_part = f"{s['name']} ({position})" if position else s["name"]
                            staff_hours.append(f"{name_part}: {', '.join(hours_list)}")
                    except Exception:
                        pass
            if staff_hours:
                parts.append("GODZINY PRACY PRACOWNIKÓW:\n" + "\n".join(staff_hours))

    # Ostrzeżenie na końcu
    has_additional_info = bool(tenant.get("additional_info", "").strip())

    # POPRAWKA 2026-08-24: booking_enabled sprawdzany PIERWSZY, niezależnie od
    # has_additional_info. Wcześniej gdy additional_info było niepuste (nawet jeśli to
    # tylko ogólny opis produktu, np. marketingowe "umawia wizytę do Google Calendar"),
    # model dostawał wprost instrukcję żeby to traktować jako źródło prawdy o sposobach
    # rezerwacji — CAŁKOWICIE z pominięciem booking_enabled. Złapane na żywym telefonie
    # 24.08.2026: tenant z wyłączonym bookingiem, ale niepustym additional_info opisującym
    # produkt ogólnie — model mieszał opis produktu z realnym stanem tej linii.
    if not booking_enabled:
        reservation_rule = (
            "- NIE mów że można rezerwować online/telefonicznie jeśli nie ma takiej informacji "
            "w DODATKOWYCH INFO. DODATKOWE INFO może opisywać usługę ogólnie (np. jako część "
            "opisu produktu) — to NIE oznacza że rezerwacja jest dostępna na TEJ linii. "
            "Kieruj się instrukcją REZERWACJE dalej w prompcie."
        )
    elif has_additional_info:
        reservation_rule = (
            "- Sposoby rezerwacji podawaj TYLKO na podstawie DODATKOWYCH INFO powyżej — nie wymyślaj innych"
        )
    else:
        reservation_rule = (
            "- Rezerwacje przyjmuj PRZEZ AGENTA — nie odsyłaj do innych miejsc jeśli nie ma takiej informacji"
        )

    parts.append(f"""⚠️ WAŻNE ZASADY:
- Jeśli powyżej NIE MA jakiejś informacji - powiedz że nie masz tej informacji
- NIGDY NIE WYMYŚLAJ cen, godzin ani innych faktów
- Podawaj ceny DOKŁADNIE tak jak są napisane powyżej
- NIGDY nie podawaj informacji spoza tego co masz powyżej (cennik, FAQ, godziny, adres, dodatkowe info)
{reservation_rule}""")

    return "\n\n".join(parts)
