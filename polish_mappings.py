# polish_mappings.py - Mapowania dla polskiego języka (voice AI)
"""
Kompleksowe mapowania dla STT w języku polskim.
Obsługuje różne formy gramatyczne i błędy transkrypcji.

Używane przez: parse_time(), fuzzy_match_staff(), parse_polish_date()
"""

# ==========================================
# GODZINY - wszystkie formy
# ==========================================

HOUR_TO_NUMBER = {
    # 6
    "szósta": 6, "szostej": 6, "szóstej": 6, "szoста": 6, "szesc": 6, "sześć": 6,
    
    # 7
    "siódma": 7, "siodma": 7, "siódmej": 7, "siodmej": 7, 
    "siedem": 7, "siódmą": 7, "siodmą": 7,
    
    # 8
    "ósma": 8, "osma": 8, "ósmej": 8, "osmej": 8, 
    "osiem": 8, "ósmą": 8, "osmą": 8,
    
    # 9
    "dziewiąta": 9, "dziewiata": 9, "dziewiątej": 9, "dziewiatej": 9,
    "dziewięć": 9, "dziewiec": 9, "dziewiątą": 9,
    
    # 10
    "dziesiąta": 10, "dziesiata": 10, "dziesiątej": 10, "dziesiatej": 10,
    "dziesięć": 10, "dziesiec": 10, "dziesiątą": 10,
    
    # 11
    "jedenasta": 11, "jedenastej": 11, "jedenaście": 11, "jedenascie": 11,
    "jedenastą": 11,
    
    # 12
    "dwunasta": 12, "dwunastej": 12, "dwanaście": 12, "dwanascie": 12,
    "dwunastą": 12, "w południe": 12, "południe": 12,
    
    # 13
    "trzynasta": 13, "trzynastej": 13, "trzynaście": 13, "trzynascie": 13,
    "trzynastą": 13, "pierwsza po południu": 13,
    
    # 14
    "czternasta": 14, "czternastej": 14, "czternaście": 14, "czternascie": 14,
    "czternastą": 14, "druga po południu": 14,
    
    # 15
    "piętnasta": 15, "pietnasta": 15, "piętnastej": 15, "pietnastej": 15,
    "piętnaście": 15, "pietnascie": 15, "piętnastą": 15,
    "trzecia po południu": 15,
    
    # 16
    "szesnasta": 16, "szesnastej": 16, "szesnaście": 16, "szesnascie": 16,
    "szesnastą": 16, "czwarta po południu": 16,
    
    # 17
    "siedemnasta": 17, "siedemnastej": 17, "siedemnaście": 17, "siedemnascie": 17,
    "siedemnastą": 17, "piąta po południu": 17,
    
    # 18
    "osiemnasta": 18, "osiemnastej": 18, "osiemnaście": 18, "osiemnascie": 18,
    "osiemnastą": 18, "szósta po południu": 18,
    
    # 19
    "dziewiętnasta": 19, "dziewietnasta": 19, "dziewiętnastej": 19, "dziewietnastej": 19,
    "dziewiętnaście": 19, "dziewietnascie": 19,
    
    # 20
    "dwudziesta": 20, "dwudziestej": 20, "dwadzieścia": 20, "dwadziescia": 20,
    "dwudziestą": 20, "ósma wieczorem": 20,
    
    # 21
    "dwudziesta pierwsza": 21, "dwudziestej pierwszej": 21,
    "dziewiąta wieczorem": 21,
    
    # 22
    "dwudziesta druga": 22, "dwudziestej drugiej": 22,
    "dziesiąta wieczorem": 22,
    
    # PÓŁGODZINY - słownie
    "dziewiąta trzydzieści": 9, "dziewiatej trzydzieści": 9,
    "dziesiąta trzydzieści": 10, "dziesiatej trzydzieści": 10,
    "jedenasta trzydzieści": 11, "jedenastej trzydzieści": 11,
    "dwunasta trzydzieści": 12, "dwunastej trzydzieści": 12,
    "trzynasta trzydzieści": 13, "trzynastej trzydzieści": 13,
    "czternasta trzydzieści": 14, "czternastej trzydzieści": 14,
    "piętnasta trzydzieści": 15, "pietnasta trzydzieści": 15,
    "szesnasta trzydzieści": 16, "szesnastej trzydzieści": 16,
    "siedemnasta trzydzieści": 17, "siedemnastej trzydzieści": 17,
    "osiemnasta trzydzieści": 18, "osiemnastej trzydzieści": 18,
    
    # WPÓŁ DO - mapuje na godzinę PRZED (wpół do dziesiątej = 9:30)
    "wpół do dziesiątej": 9, "wpol do dziesiatej": 9,
    "wpół do jedenastej": 10, "wpol do jedenastej": 10,
    "wpół do dwunastej": 11, "wpol do dwunastej": 11,
    "wpół do pierwszej": 12, "wpol do pierwszej": 12,
    "wpół do drugiej": 13, "wpol do drugiej": 13,
    "wpół do trzeciej": 14, "wpol do trzeciej": 14,
    "wpół do czwartej": 15, "wpol do czwartej": 15,
    "wpół do piątej": 16, "wpol do piatej": 16,
    "wpół do szóstej": 17, "wpol do szostej": 17,
}

# Odwrotne mapowanie - liczba na słowo (do TTS)
NUMBER_TO_HOUR_WORD = {
    6: "szóstej", 7: "siódmej", 8: "ósmej", 9: "dziewiątej", 10: "dziesiątej",
    11: "jedenastej", 12: "dwunastej", 13: "trzynastej", 14: "czternastej",
    15: "piętnastej", 16: "szesnastej", 17: "siedemnastej", 18: "osiemnastej",
    19: "dziewiętnastej", 20: "dwudziestej", 21: "dwudziestej pierwszej",
    22: "dwudziestej drugiej",
}

# ==========================================
# IMIONA - zdrobnienia i warianty
# ==========================================

NAME_ALIASES = {
    # Kobiece - zdrobnienia → pełne imię
    "ania": "anna", "ani": "anna", "aneczka": "anna", "anka": "anna",
    "kasia": "katarzyna", "kaśka": "katarzyna", "kasieńka": "katarzyna", "kacha": "katarzyna",
    "asia": "joanna", "joasia": "joanna", "aśka": "joanna",
    "basia": "barbara", "baśka": "barbara",
    "gosia": "małgorzata", "gośka": "małgorzata", "małgosia": "małgorzata",
    "ela": "elżbieta", "elka": "elżbieta", "elusia": "elżbieta",
    "ola": "aleksandra", "olka": "aleksandra", "oleńka": "aleksandra",
    "ewka": "ewa", "ewunia": "ewa",
    "magda": "magdalena", "magdzia": "magdalena",
    "wika": "wiktoria", "wiki": "wiktoria",
    "monia": "monika", "moniczka": "monika",
    "daria": "daria", "darka": "daria",
    "natka": "natalia", "natalka": "natalia",
    "aga": "agnieszka", "agniesia": "agnieszka",
    "iza": "izabela", "izka": "izabela",
    "kinga": "kinga",
    "sylwia": "sylwia", "sylwka": "sylwia",
    "marta": "marta", "marcia": "marta",
    "beata": "beata", "beatka": "beata",
    "dorota": "dorota", "dorcia": "dorota",
    "paulina": "paulina", "paula": "paulina",
    
    # Męskie - zdrobnienia → pełne imię
    "tomek": "tomasz", "tomcio": "tomasz",
    "bartek": "bartłomiej", "bartuś": "bartłomiej", "bartosz": "bartłomiej",
    "krzysiek": "krzysztof", "krzyś": "krzysztof",
    "piotrek": "piotr", "piotruś": "piotr",
    "marcin": "marcin", "marciniek": "marcin",
    "michałek": "michał", "michał": "michał",
    "janek": "jan", "jasiek": "jan", "jaś": "jan",
    "maciek": "maciej", "maciuś": "maciej",
    "witek": "wiktor", "wicio": "wiktor",
    "wojtek": "wojciech", "wojtuś": "wojciech",
    "arek": "arkadiusz", "aruś": "arkadiusz",
    "darek": "dariusz", "daruś": "dariusz",
    "łukasz": "łukasz", "łuki": "łukasz",
    "paweł": "paweł", "pawełek": "paweł",
    "adam": "adam", "adaś": "adam",
    "rafał": "rafał", "rafcio": "rafał",
    "kamil": "kamil", "kamilek": "kamil",
    "sebastian": "sebastian", "seba": "sebastian",
    "grzesiek": "grzegorz", "grześ": "grzegorz",
    "daniel": "daniel", "danek": "daniel",
}

# Odwrotne - pełne imię → możliwe zdrobnienia (do rozpoznawania)
# Używane gdy w bazie mamy "Ania" a klient mówi "Anna"
FULL_NAME_TO_ALIASES = {}
for alias, full in NAME_ALIASES.items():
    if full not in FULL_NAME_TO_ALIASES:
        FULL_NAME_TO_ALIASES[full] = []
    FULL_NAME_TO_ALIASES[full].append(alias)

# ==========================================
# DNI TYGODNIA - wszystkie formy
# ==========================================

DAY_TO_NUMBER = {
    # Poniedziałek (0)
    "poniedziałek": 0, "poniedzialek": 0, "poniedziałku": 0, "poniedzialku": 0,
    "w poniedziałek": 0, "w poniedzialek": 0,
    
    # Wtorek (1)
    "wtorek": 1, "wtorku": 1, "we wtorek": 1,
    
    # Środa (2)
    "środa": 2, "sroda": 2, "środę": 2, "srode": 2,
    "w środę": 2, "w srode": 2,
    
    # Czwartek (3)
    "czwartek": 3, "czwartku": 3, "w czwartek": 3,
    
    # Piątek (4)
    "piątek": 4, "piatek": 4, "piątku": 4, "piatku": 4,
    "w piątek": 4, "w piatek": 4,
    
    # Sobota (5)
    "sobota": 5, "sobotę": 5, "sobote": 5, "sobocie": 5,
    "w sobotę": 5, "w sobote": 5,
    
    # Niedziela (6)
    "niedziela": 6, "niedzielę": 6, "niedziele": 6, "niedzieli": 6,
    "w niedzielę": 6, "w niedziele": 6,
}

NUMBER_TO_DAY = {
    0: "poniedziałek", 1: "wtorek", 2: "środa", 3: "czwartek",
    4: "piątek", 5: "sobota", 6: "niedziela",
}

# ==========================================
# MIESIĄCE
# ==========================================

MONTH_TO_NUMBER = {
    "styczeń": 1, "styczen": 1, "stycznia": 1,
    "luty": 2, "lutego": 2,
    "marzec": 3, "marca": 3,
    "kwiecień": 4, "kwiecien": 4, "kwietnia": 4,
    "maj": 5, "maja": 5,
    "czerwiec": 6, "czerwca": 6,
    "lipiec": 7, "lipca": 7,
    "sierpień": 8, "sierpien": 8, "sierpnia": 8,
    "wrzesień": 9, "wrzesien": 9, "września": 9, "wrzesnia": 9,
    "październik": 10, "pazdziernik": 10, "października": 10, "pazdziernika": 10,
    "listopad": 11, "listopada": 11,
    "grudzień": 12, "grudzien": 12, "grudnia": 12,
}

# ==========================================
# CZĘSTE BŁĘDY STT (Deepgram)
# ==========================================

STT_CORRECTIONS = {
    # Godziny - częste błędy
    "siódmy": "siódma",
    "ósmy": "ósma", 
    "osmy": "ósma",
    "dziesiąty": "dziesiąta",
    "jedenasty": "jedenasta",
    
    # Imiona - częste błędy Deepgram
    "anka": "ania",
    "wiktór": "wiktor",
    "viktora": "wiktora",
    "wiktorach": "wiktora",  # z logów!
    "wiktor wiktor": "wiktor",  # podwójne z logów!
    
    # Inne
    "okej": "ok",
    "okey": "ok",
}

# ==========================================
# HELPER FUNCTIONS
# ==========================================

def normalize_polish_text(text: str) -> str:
    """Normalizuje polski tekst - usuwa polskie znaki dla porównań."""
    if not text:
        return ""
    
    replacements = {
        "ą": "a", "ć": "c", "ę": "e", "ł": "l", "ń": "n",
        "ó": "o", "ś": "s", "ź": "z", "ż": "z",
        "Ą": "A", "Ć": "C", "Ę": "E", "Ł": "L", "Ń": "N",
        "Ó": "O", "Ś": "S", "Ź": "Z", "Ż": "Z",
    }
    
    result = text
    for pl_char, ascii_char in replacements.items():
        result = result.replace(pl_char, ascii_char)
    
    return result


def apply_stt_corrections(text: str) -> str:
    """Stosuje znane korekty błędów STT z granicami słów."""
    import re
    
    if not text:
        return text
    
    text_lower = text.lower().strip()
    
    # Sprawdź dokładne dopasowania najpierw
    if text_lower in STT_CORRECTIONS:
        return STT_CORRECTIONS[text_lower]
    
    # Stosuj korekty tylko na granicach słów (żeby "anka" nie zmieniło "bankomat")
    for error, correction in STT_CORRECTIONS.items():
        text_lower = re.sub(rf'\b{re.escape(error)}\b', correction, text_lower)
    
    return text_lower

def parse_hour_from_text(text: str) -> int | None:
    """
    Parsuje godzinę z tekstu - używa wszystkich mapowań.
    
    Przykłady:
    - "siódma" → 7
    - "na 15" → 15  
    - "o ósmej" → 8
    - "siedemnastą" → 17
    """
    import re
    
    if not text:
        return None
    
    text = text.lower().strip()
    text = apply_stt_corrections(text)
    
    # 1. Dokładne dopasowanie
    if text in HOUR_TO_NUMBER:
        return HOUR_TO_NUMBER[text]
    
    # 2. Sprawdź frazy z kontekstem NAJPIERW (dłuższe frazy mają priorytet)
    #    Sortuj po długości malejąco żeby "szósta po południu" było przed "szósta"
    for word, hour in sorted(HOUR_TO_NUMBER.items(), key=lambda x: -len(x[0])):
        # Użyj granic słów żeby "sześć" nie złapało się w "szesnasta"
        if re.search(rf'\b{re.escape(word)}\b', text):
            return hour
    
    # 3. Wyciągnij liczbę
    numbers = re.findall(r'\d+', text)
    if numbers:
        hour = int(numbers[0])
        if 0 <= hour <= 23:
            return hour
    
    # 4. Fallback - porównaj bez polskich znaków (ostatnia deska ratunku)
    text_normalized = normalize_polish_text(text)
    for word, hour in sorted(HOUR_TO_NUMBER.items(), key=lambda x: -len(x[0])):
        word_normalized = normalize_polish_text(word)
        if re.search(rf'\b{re.escape(word_normalized)}\b', text_normalized):
            return hour
    
    return None


def match_staff_name(query: str, staff_list: list) -> dict | None:
    """
    Dopasowuje imię pracownika z tolerancją na zdrobnienia i błędy.
    
    Przykłady:
    - "Ania" → staff z name="Anna" ✓
    - "wiktorach" → staff z name="Wiktor" ✓
    - "Lukasz" → staff z name="Łukasz" ✓
    """
    if not query or not staff_list:
        return None
    
    query = query.lower().strip()
    query = apply_stt_corrections(query)
    query_normalized = normalize_polish_text(query)
    
    # Normalizuj query przez aliasy
    alias_query = NAME_ALIASES.get(query, query)
    alias_query_normalized = normalize_polish_text(alias_query)
    
    for staff in staff_list:
        staff_name = staff["name"].lower().strip()
        staff_name_normalized = normalize_polish_text(staff_name)
        staff_first_name = staff_name.split()[0] if " " in staff_name else staff_name
        staff_first_normalized = normalize_polish_text(staff_first_name)
        
        # Normalizuj imię pracownika przez aliasy
        staff_alias = NAME_ALIASES.get(staff_first_name, staff_first_name)
        staff_alias_normalized = normalize_polish_text(staff_alias)
        
        # Porównania - z i bez polskich znaków
        checks = [
            query == staff_name,
            query == staff_first_name,
            query_normalized == staff_name_normalized,
            query_normalized == staff_first_normalized,
            alias_query == staff_name,
            alias_query == staff_first_name,
            alias_query_normalized == staff_alias_normalized,
        ]
        
        if any(checks):
            return staff
        
        # Sprawdź czy query to alias imienia pracownika
        if staff_first_name in FULL_NAME_TO_ALIASES:    
            aliases = FULL_NAME_TO_ALIASES[staff_first_name]
            if query in aliases or query_normalized in [normalize_polish_text(a) for a in aliases]:
                return staff
    
    return None
# ==========================================
# ALIASY DLA KOMPATYBILNOŚCI
# ==========================================

POLISH_DAYS = NUMBER_TO_DAY
POLISH_DAYS_REVERSE = {v: k for k, v in NUMBER_TO_DAY.items()}