"""Gramatyka polskich imion: odmiana (dopełniacz, wołacz), rodzaj, normalizacja tekstu."""

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
