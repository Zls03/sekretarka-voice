# polish_mappings.py - Mapowania dla polskiego języka (voice AI)
# WERSJA 2.0 - Rozszerzona o odmianę imion, wykrywanie płci, naturalne listy
"""
Kompleksowe mapowania dla STT/TTS w języku polskim.
Obsługuje różne formy gramatyczne i błędy transkrypcji.

NOWE W V2:
- 150+ imion z odmianą (dopełniacz)
- Wykrywanie płci po imieniu
- Naturalne listy ("A, B i C")
- Lepsze reguły automatyczne

Używane przez: flows_booking_simple.py, parse_time(), fuzzy_match_staff()
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
# IMIONA - zdrobnienia i warianty (dla STT)
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
    "zuzia": "zuzanna", "zuza": "zuzanna",
    "hania": "hanna", "hanka": "hanna",
    "jola": "jolanta", "jolka": "jolanta",
    "madzia": "magdalena",
    "krysia": "krystyna", "kryśka": "krystyna",
    "bożenka": "bożena",
    "grażynka": "grażyna",
    "danusia": "danuta", "danka": "danuta",
    "renata": "renata", "renia": "renata",
    "aldona": "aldona", "aldonka": "aldona",
    "maja": "maja",
    "lena": "lena", "lenka": "lena",
    "julia": "julia", "julka": "julia",
    "weronika": "weronika", "werka": "weronika",
    "dominika": "dominika",
    "patrycja": "patrycja",
    "sandra": "aleksandra",
    "ola": "aleksandra",
    
    # Męskie - zdrobnienia → pełne imię
    "tomek": "tomasz", "tomcio": "tomasz",
    "bartek": "bartłomiej", "bartuś": "bartłomiej", "bartosz": "bartłomiej",
    "krzysiek": "krzysztof", "krzyś": "krzysztof",
    "piotrek": "piotr", "piotruś": "piotr",
    "marcin": "marcin", "marciniek": "marcin",
    "michałek": "michał",
    "janek": "jan", "jasiek": "jan", "jaś": "jan",
    "maciek": "maciej", "maciuś": "maciej",
    "witek": "wiktor", "wicio": "wiktor",
    "wojtek": "wojciech", "wojtuś": "wojciech",
    "arek": "arkadiusz", "aruś": "arkadiusz",
    "darek": "dariusz", "daruś": "dariusz",
    "łukasz": "łukasz", "łuki": "łukasz",
    "pawełek": "paweł",
    "adaś": "adam",
    "rafał": "rafał", "rafcio": "rafał",
    "kamil": "kamil", "kamilek": "kamil",
    "sebastian": "sebastian", "seba": "sebastian",
    "grzesiek": "grzegorz", "grześ": "grzegorz",
    "daniel": "daniel", "danek": "daniel",
    "kuba": "jakub", "kubuś": "jakub",
    "staszek": "stanisław", "staś": "stanisław",
    "stefek": "stefan",
    "józek": "józef", "józio": "józef",
    "zbyszek": "zbigniew", "zbysio": "zbigniew",
    "rysiek": "ryszard", "rysio": "ryszard",
    "leszek": "leszek",
    "heniek": "henryk", "henio": "henryk",
    "władek": "władysław",
    "bogdan": "bogdan", "bogdanek": "bogdan",
    "mateusz": "mateusz", "mati": "mateusz",
    "damian": "damian",
    "dawid": "dawid",
    "hubert": "hubert",
    "filip": "filip",
    "oskar": "oskar",
    "szymon": "szymon", "szymek": "szymon",
    "kacper": "kacper",
    "dominik": "dominik",
    "patryk": "patryk",
    "adrian": "adrian",
    "przemek": "przemysław", "przemcio": "przemysław",
    "mirek": "mirosław", "miruś": "mirosław",
    "jacek": "jacek",
    "mariusz": "mariusz",
    "robert": "robert",
}

# Odwrotne - pełne imię → możliwe zdrobnienia (do rozpoznawania)
FULL_NAME_TO_ALIASES = {}
for alias, full in NAME_ALIASES.items():
    if full not in FULL_NAME_TO_ALIASES:
        FULL_NAME_TO_ALIASES[full] = []
    FULL_NAME_TO_ALIASES[full].append(alias)


# ==========================================
# ODMIANA IMION (dopełniacz) - NOWE W V2!
# ==========================================

# Słownik: mianownik → dopełniacz (150+ imion)
IMIE_DOPELNIACZ = {
    # ==========================================
    # ŻEŃSKIE - popularne (TOP 50)
    # ==========================================
    "anna": "anny",
    "maria": "marii",
    "katarzyna": "katarzyny",
    "małgorzata": "małgorzaty",
    "agnieszka": "agnieszki",
    "barbara": "barbary",
    "krystyna": "krystyny",
    "elżbieta": "elżbiety",
    "ewa": "ewy",
    "teresa": "teresy",
    "joanna": "joanny",
    "magdalena": "magdaleny",
    "monika": "moniki",
    "danuta": "danuty",
    "zofia": "zofii",
    "grażyna": "grażyny",
    "bożena": "bożeny",
    "aleksandra": "aleksandry",
    "janina": "janiny",
    "marta": "marty",
    "dorota": "doroty",
    "beata": "beaty",
    "jolanta": "jolanty",
    "renata": "renaty",
    "iwona": "iwony",
    "halina": "haliny",
    "izabela": "izabeli",
    "karolina": "karoliny",
    "natalia": "natalii",
    "justyna": "justyny",
    "sylwia": "sylwii",
    "wiktoria": "wiktorii",
    "paulina": "pauliny",
    "kinga": "kingi",
    "patrycja": "patrycji",
    "dominika": "dominiki",
    "weronika": "weroniki",
    "julia": "julii",
    "zuzanna": "zuzanny",
    "hanna": "hanny",
    "alicja": "alicji",
    "daria": "darii",
    "aldona": "aldony",
    "edyta": "edyty",
    "aneta": "anety",
    "cecylia": "cecylii",
    "emilia": "emilii",
    "gabriela": "gabrieli",
    "helena": "heleny",
    "irena": "ireny",
    "jadwiga": "jadwigi",
    "lidia": "lidii",
    "lucyna": "lucyny",
    "łucja": "łucji",
    "marianna": "marianny",
    "marlena": "marleny",
    "milena": "mileny",
    "nina": "niny",
    "olga": "olgi",
    "róża": "róży",
    "sabina": "sabiny",
    "urszula": "urszuli",
    "wanda": "wandy",
    "żaneta": "żanety",
    "maja": "mai",
    "lena": "leny",
    "oliwia": "oliwii",
    "amelia": "amelii",
    "laura": "laury",
    "klaudia": "klaudii",
    "nicole": "nicole",  # nieodmienne
    "nikola": "nikoli",
    
    # ==========================================
    # ŻEŃSKIE - zdrobnienia
    # ==========================================
    "ania": "ani",
    "kasia": "kasi",
    "basia": "basi",
    "gosia": "gosi",
    "asia": "asi",
    "ola": "oli",
    "ela": "eli",
    "magda": "magdy",
    "aga": "agi",
    "iza": "izy",
    "ewa": "ewy",
    "monia": "moni",
    "daria": "dari",
    "darka": "darki",
    "natka": "natki",
    "kinga": "kingi",
    "sylwia": "sylwii",
    "marta": "marty",
    "beata": "beaty",
    "dorota": "doroty",
    "jola": "joli",
    "krysia": "krysi",
    "hania": "hani",
    "zuzia": "zuzi",
    "julka": "julki",
    "lenka": "lenki",
    "wika": "wiki",
    "renia": "reni",
    "danusia": "danusi",
    "madzia": "madzi",
    "grażynka": "grażynki",
    "bożenka": "bożenki",
    "werka": "werki",
    "aldonka": "aldonki",
    
    # ==========================================
    # MĘSKIE - popularne (TOP 60)
    # ==========================================
    "jan": "jana",
    "andrzej": "andrzeja",
    "piotr": "piotra",
    "krzysztof": "krzysztofa",
    "stanisław": "stanisława",
    "tomasz": "tomasza",
    "paweł": "pawła",
    "józef": "józefa",
    "marcin": "marcina",
    "marek": "marka",
    "michał": "michała",
    "grzegorz": "grzegorza",
    "jerzy": "jerzego",
    "tadeusz": "tadeusza",
    "adam": "adama",
    "łukasz": "łukasza",
    "zbigniew": "zbigniewa",
    "ryszard": "ryszarda",
    "dariusz": "dariusza",
    "henryk": "henryka",
    "mariusz": "mariusza",
    "kazimierz": "kazimierza",
    "wojciech": "wojciecha",
    "robert": "roberta",
    "mateusz": "mateusza",
    "rafał": "rafała",
    "jacek": "jacka",
    "janusz": "janusza",
    "maciej": "macieja",
    "sławomir": "sławomira",
    "jarosław": "jarosława",
    "kamil": "kamila",
    "wiesław": "wiesława",
    "roman": "romana",
    "władysław": "władysława",
    "arkadiusz": "arkadiusza",
    "przemysław": "przemysława",
    "sebastian": "sebastiana",
    "mirosław": "mirosława",
    "leszek": "leszka",
    "daniel": "daniela",
    "dawid": "dawida",
    "damian": "damiana",
    "szymon": "szymona",
    "kacper": "kacpra",
    "filip": "filipa",
    "hubert": "huberta",
    "oskar": "oskara",
    "wiktor": "wiktora",
    "dominik": "dominika",
    "patryk": "patryka",
    "adrian": "adriana",
    "jakub": "jakuba",
    "bartłomiej": "bartłomieja",
    "bartosz": "bartosza",
    "bogdan": "bogdana",
    "stefan": "stefana",
    "edward": "edwarda",
    "mieczysław": "mieczysława",
    "zygmunt": "zygmunta",
    "bogusław": "bogusława",
    "bernard": "bernarda",
    "cezary": "cezarego",
    "emil": "emila",
    "franciszek": "franciszka",
    "igor": "igora",
    "karol": "karola",
    "leon": "leona",
    "maksymilian": "maksymiliana",
    "nikodem": "nikodema",
    "oliwier": "oliwiera",
    "samuel": "samuela",
    "tymoteusz": "tymoteusza",
    "błażej": "błażeja",
    "borys": "borysa",
    "bruno": "bruna",
    "gustaw": "gustawa",
    "konrad": "konrada",
    "leonard": "leonarda",
    "marcel": "marcela",
    "norbert": "norberta",
    "olaf": "olafa",
    "oleg": "olega",
    "radosław": "radosława",
    "sylwester": "sylwestra",
    "waldemar": "waldemara",
    "witold": "witolda",
    
    # ==========================================
    # MĘSKIE - zdrobnienia
    # ==========================================
    "tomek": "tomka",
    "bartek": "bartka",
    "krzysiek": "krzyśka",
    "piotrek": "piotrka",
    "janek": "janka",
    "jasiek": "jaśka",
    "maciek": "maćka",
    "witek": "witka",
    "wojtek": "wojtka",
    "arek": "arka",
    "darek": "darka",
    "grzesiek": "grześka",
    "staszek": "staśka",
    "józek": "józka",
    "zbyszek": "zbyszka",
    "rysiek": "ryśka",
    "heniek": "heńka",
    "władek": "władka",
    "kuba": "kuby",
    "szymon": "szymona",
    "szymek": "szymka",
    "kacper": "kacpra",
    "mati": "matiego",
    "seba": "seby",
    "przemek": "przemka",
    "mirek": "mirka",
    "rafcio": "rafcia",
    "pawełek": "pawełka",
    "adaś": "adasia",
    "bogdanek": "bogdanka",
    "stefek": "stefka",
    "leszek": "leszka",
    "jacek": "jacka",
}

# Imiona męskie kończące się na 'a' (wyjątki)
MESKIE_NA_A = {
    "kuba", "barnaba", "bonawentura", "kosma", "dyzma", 
    "jarema", "saba", "boryna",  # literackie/rzadkie
}


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
    "wiktorach": "wiktora",
    "wiktor wiktor": "wiktor",
    
    # Inne
    "okej": "ok",
    "okey": "ok",
}


# ==========================================
# FUNKCJE POMOCNICZE - PODSTAWOWE
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
    
    # Stosuj korekty tylko na granicach słów
    for error, correction in STT_CORRECTIONS.items():
        text_lower = re.sub(rf'\b{re.escape(error)}\b', correction, text_lower)
    
    return text_lower


# ==========================================
# FUNKCJE ODMIANY - NOWE W V2!
# ==========================================

def odmien_imie(imie: str, przypadek: str = "dopelniacz") -> str:
    """
    Odmienia imię przez przypadki.
    
    Args:
        imie: Imię w mianowniku (np. "Ania", "Paweł")
        przypadek: "dopelniacz" (u Ani), "biernik" (widzę Anię), etc.
    
    Returns:
        Odmienione imię
    
    Przykłady:
        odmien_imie("Ania") → "Ani"
        odmien_imie("Paweł") → "Pawła"
        odmien_imie("Wiktor") → "Wiktora"
        odmien_imie("Katarzyna") → "Katarzyny"
    """
    if not imie:
        return imie
    
    imie_clean = imie.strip()
    imie_lower = imie_clean.lower()
    original_case = imie_clean[0].isupper() if imie_clean else False
    
    # 1. Sprawdź słownik (najdokładniejsze)
    if imie_lower in IMIE_DOPELNIACZ:
        result = IMIE_DOPELNIACZ[imie_lower]
        return result.title() if original_case else result
    
    # 2. Sprawdź alias → pełne imię → słownik
    if imie_lower in NAME_ALIASES:
        full_name = NAME_ALIASES[imie_lower]
        if full_name in IMIE_DOPELNIACZ:
            # Ale zwróć odmienione zdrobnienie, nie pełne imię
            # np. "Ania" → "Ani", nie "Anny"
            pass  # użyj reguł poniżej
    
    # 3. Reguły automatyczne (dla nieznanych imion)
    result = _odmien_reguly(imie_lower)
    
    return result.title() if original_case else result


def _odmien_reguly(imie: str) -> str:
    """Automatyczne reguły odmiany przez dopełniacz."""
    
    # Żeńskie kończące się na -ia → -i
    if imie.endswith("ia"):
        return imie[:-1]  # Ania → Ani, Maria → Mari, Kasia → Kasi
    
    # Żeńskie kończące się na -ja → -i (Maja → Mai)
    if imie.endswith("ja"):
        return imie[:-2] + "i"  # Maja → Mai
    
    # Żeńskie kończące się na -a (ale nie -ia, -ja) → -y
    if imie.endswith("a") and imie not in MESKIE_NA_A:
        # Sprawdź czy spółgłoska miękka przed 'a' → wtedy -i
        if len(imie) > 2:
            przedostatnia = imie[-2]
            # Po k, g → -i (Kinga → Kingi, Olga → Olgi)
            if przedostatnia in "kg":
                return imie[:-1] + "i"
            # Po innych → -y (Marta → Marty, Beata → Beaty)
            else:
                return imie[:-1] + "y"
        return imie[:-1] + "y"
    
    # Męskie na -eł → -ła (Paweł → Pawła)
    if imie.endswith("eł"):
        return imie[:-2] + "ła"
    
    # Męskie na -ał → -ała (Michał → Michała)
    if imie.endswith("ał"):
        return imie[:-2] + "ała"
    
    # Męskie na -ek → -ka (Tomek → Tomka, Marek → Marka)
    if imie.endswith("ek"):
        return imie[:-2] + "ka"
    
    # Męskie na -ec → -ca (tylko niektóre, np. Tadeusz)
    # To rzadkie, pomijam
    
    # Męskie na spółgłoskę → +a (Piotr → Piotra, Adam → Adama)
    if imie[-1] not in "aeiouyąęó":
        return imie + "a"
    
    # Męskie na -y → -ego (Jerzy → Jerzego) - WAŻNE!
    if imie.endswith("y"):
        return imie[:-1] + "ego"
    
    # Męskie na -i/-o → -ego/-a (rzadkie)
    if imie.endswith("i"):
        return imie + "ego"
    if imie.endswith("o"):
        return imie[:-1] + "a"  # Bruno → Bruna
    
    # Fallback - zwróć bez zmian
    return imie


def detect_gender(imie: str) -> str:
    """
    Wykrywa płeć na podstawie imienia.
    
    Returns:
        "Pana" lub "Pani"
    
    Przykłady:
        detect_gender("Paweł") → "Pana"
        detect_gender("Anna") → "Pani"
        detect_gender("Kuba") → "Pana"  # wyjątek
        detect_gender("Jerzy") → "Pana"
    """
    if not imie:
        return "Pana"  # default męski
    
    imie_lower = imie.lower().strip()
    
    # Wyjątki - męskie kończące się na 'a'
    if imie_lower in MESKIE_NA_A:
        return "Pana"
    
    # Sprawdź alias
    if imie_lower in NAME_ALIASES:
        full = NAME_ALIASES[imie_lower]
        if full.endswith("a"):
            return "Pani"
        return "Pana"
    
    # Standardowa reguła
    if imie_lower.endswith("a"):
        return "Pani"
    else:
        return "Pana"


def detect_gender_verb(imie: str) -> str:
    """
    Zwraca końcówkę czasownika w czasie przeszłym.
    
    Returns:
        "a" (żeńska) lub "" (męska)
    
    Przykłady:
        f"Zapisał{detect_gender_verb('Paweł')}m" → "Zapisałem" (jako bot-kobieta mówi o kliencie)
        
    Ale dla bota-kobiety mówiącej o sobie:
        "Zapisałam" (zawsze żeńska)
    """
    gender = detect_gender(imie)
    return "" if gender == "Pana" else "a"


# ==========================================
# WOŁACZ - powitanie "Dzień dobry Wiktorze"
# ==========================================

_VOCATIVE = {
    # Męskie
    "Adam": "Adamie", "Andrzej": "Andrzeju", "Artur": "Arturze",
    "Bartosz": "Bartoszu", "Bartłomiej": "Bartłomieju",
    "Damian": "Damianie", "Daniel": "Danielu", "Dariusz": "Dariuszu",
    "Dawid": "Dawidzie", "Dominik": "Dominiku",
    "Filip": "Filipie", "Grzegorz": "Grzegorzu",
    "Igor": "Igorze", "Jakub": "Jakubie", "Jan": "Janie",
    "Jarek": "Jarku", "Jarosław": "Jarosławie",
    "Kamil": "Kamilu", "Karol": "Karolu", "Konrad": "Konradzie",
    "Krystian": "Krystianie", "Krzysztof": "Krzysztofie",
    "Łukasz": "Łukaszu", "Maciej": "Macieju", "Marcin": "Marcinie",
    "Marek": "Marku", "Mariusz": "Mariuszu", "Mateusz": "Mateuszu",
    "Michał": "Michale", "Mikołaj": "Mikołaju",
    "Patryk": "Patryku", "Paweł": "Pawle", "Piotr": "Piotrze",
    "Przemysław": "Przemysławie", "Radosław": "Radosławie",
    "Rafał": "Rafale", "Robert": "Robercie",
    "Sebastian": "Sebastianie", "Sławomir": "Sławomirze",
    "Stanisław": "Stanisławie", "Szymon": "Szymonie",
    "Tomasz": "Tomaszu", "Waldemar": "Waldemarze",
    "Wiktor": "Wiktorze", "Wiesław": "Wiesławie",
    "Wojciech": "Wojciechu", "Zbigniew": "Zbigniewie",
    # Żeńskie
    "Agnieszka": "Agnieszko", "Aleksandra": "Aleksandro",
    "Ania": "Aniu", "Anna": "Anno", "Asia": "Asiu",
    "Barbara": "Barbaro", "Basia": "Basiu", "Beata": "Beato",
    "Celina": "Celino", "Dominika": "Dominiko", "Dorota": "Doroto",
    "Ewa": "Ewo", "Gosia": "Gosiu", "Halina": "Halino",
    "Izabela": "Izabelo", "Iwona": "Iwono",
    "Joanna": "Joanno", "Justyna": "Justyno",
    "Karolina": "Karolino", "Kasia": "Kasiu", "Katarzyna": "Katarzyno",
    "Magda": "Magdo", "Magdalena": "Magdaleno",
    "Małgorzata": "Małgorzato", "Marta": "Marto", "Monika": "Moniko",
    "Nadia": "Nadiu", "Natalia": "Natalio",
    "Ola": "Olu", "Patrycja": "Patrycjo", "Paulina": "Paulino",
    "Sylwia": "Sylwio", "Teresa": "Tereso", "Weronika": "Weronico",
    "Zofia": "Zofio", "Zuzanna": "Zuzanno", "Zuzia": "Zuziu",
}

def vocative_imie(name: str) -> str:
    """Zwraca wołacz imienia. Fallback do mianownika gdy nieznane."""
    if not name:
        return name
    name_cap = name.strip().capitalize()
    if name_cap in _VOCATIVE:
        return _VOCATIVE[name_cap]
    lower = name_cap.lower()
    # Żeńskie zdrobnienia: -sia, -zia, -cia, -nia, -bia → -iu
    if lower.endswith(('sia', 'zia', 'cia', 'nia', 'bia')):
        return name_cap[:-2] + 'u'
    # Żeńskie: kończy na -a → -o
    if lower.endswith('a'):
        return name_cap[:-1] + 'o'
    # Męskie: -sz, -cz → -u
    if lower.endswith(('sz', 'cz')):
        return name_cap + 'u'
    # Męskie: -l, -j, -k → -u
    if lower.endswith(('l', 'j', 'k')):
        return name_cap + 'u'
    # Męskie: -r → -rze
    if lower.endswith('r'):
        return name_cap + 'ze'
    # Męskie: -ł → -le
    if lower.endswith('ł'):
        return name_cap[:-1] + 'le'
    # Nieznane → mianownik
    return name_cap


# ==========================================
# FUNKCJE LISTY - NOWE W V2!
# ==========================================

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


def format_staff_list(staff_list: list, with_services: bool = False) -> str:
    """
    Formatuje listę pracowników naturalnie.
    
    Przykłady:
        format_staff_list([{"name": "Ania"}, {"name": "Wiktor"}]) 
        → "Ania i Wiktor"
    """
    if not staff_list:
        return "brak pracowników"
    
    names = [s.get("name", "?") for s in staff_list]
    return natural_list(names)


def format_service_list(services: list) -> str:
    """
    Formatuje listę usług naturalnie.
    
    Przykłady:
        format_service_list([{"name": "Strzyżenie"}, {"name": "Farbowanie"}])
        → "strzyżenie i farbowanie"
    """
    if not services:
        return "brak usług"
    
    names = [s.get("name", "?").lower() for s in services]
    return natural_list(names)


def format_slots_list(slots: list, max_items: int = 5) -> str:
    """
    Formatuje listę godzin naturalnie.
    
    Args:
        slots: Lista godzin (np. ["9:00", "10:00", "11:00"])
        max_items: Maksymalna liczba do wyświetlenia
    
    Przykłady:
        format_slots_list(["9:00", "10:00", "11:00"])
        → "dziewiąta, dziesiąta i jedenasta"
    """
    if not slots:
        return "brak wolnych terminów"
    
    # Importuj helper do formatowania godzin
    try:
        from flows_helpers import format_hour_polish
    except ImportError:
        # Fallback
        def format_hour_polish(h):
            if isinstance(h, str) and ":" in h:
                hour = int(h.split(":")[0])
            else:
                hour = int(h)
            return NUMBER_TO_HOUR_WORD.get(hour, f"{hour}:00")
    
    # Ogranicz i sformatuj
    limited = slots[:max_items]
    formatted = [format_hour_polish(s) for s in limited]
    
    result = natural_list(formatted)
    
    if len(slots) > max_items:
        result += f" (i {len(slots) - max_items} więcej)"
    
    return result


# ==========================================
# FUNKCJE PARSOWANIA - ROZSZERZONE
# ==========================================

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
    
    # 2. Sprawdź frazy z kontekstem (dłuższe frazy mają priorytet)
    for word, hour in sorted(HOUR_TO_NUMBER.items(), key=lambda x: -len(x[0])):
        if re.search(rf'\b{re.escape(word)}\b', text):
            return hour
    
    # 3. Wyciągnij liczbę
    numbers = re.findall(r'\d+', text)
    if numbers:
        hour = int(numbers[0])
        if 0 <= hour <= 23:
            return hour
    
    # 4. Fallback - bez polskich znaków
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
        
        # Porównania
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


# ==========================================
# TESTY (uruchom: python polish_mappings.py)
# ==========================================

if __name__ == "__main__":
    print("=" * 60)
    print("TESTY POLISH_MAPPINGS V2")
    print("=" * 60)
    
    # Test odmiany imion
    print("\n📝 ODMIANA IMION (dopełniacz):")
    test_names = [
        "Ania", "Anna", "Kasia", "Katarzyna", "Magda", "Marta", "Kinga", "Olga",
        "Paweł", "Wiktor", "Tomek", "Tomasz", "Jan", "Michał", "Jerzy", "Adam",
        "Kuba", "Maja", "Julia",
    ]
    for name in test_names:
        declined = odmien_imie(name)
        gender = detect_gender(name)
        print(f"  {name:15} → {declined:15} ({gender})")
    
    # Test naturalnych list
    print("\n📋 NATURALNE LISTY:")
    print(f"  1 element:  {natural_list(['Ania'])}")
    print(f"  2 elementy: {natural_list(['Ania', 'Wiktor'])}")
    print(f"  3 elementy: {natural_list(['Ania', 'Wiktor', 'Kasia'])}")
    print(f"  z 'lub':    {natural_list(['poniedziałek', 'wtorek'], 'lub')}")
    
    # Test formatowania godzin
    print("\n🕐 FORMATOWANIE GODZIN:")
    test_slots = ["9:00", "10:00", "11:00", "12:00", "13:00", "14:00"]
    print(f"  {format_slots_list(test_slots, 3)}")
    print(f"  {format_slots_list(test_slots, 6)}")
    
    print("\n✅ Testy zakończone!")