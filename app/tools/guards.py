"""Heurystyki odrzucające bezwartościowe wywołania narzędzi (puste/meta wiadomości, frazy bota)."""

_VAGUE_MESSAGE_STARTS = (
    "klient chce",
    "klient prosi",
    "klient jest",
    "klient potrzebuje",
    "klient dzwoni",
    "proszę o kontakt",
    "proszę skontaktować się",
    "proszę zadzwonić",
    "proszę oddzwonić",
)


def _looks_like_vague_meta_message(message: str) -> bool:
    """Wykrywa dwa warianty śmieciowej wiadomości: (1) GPT pisze O kliencie w trzeciej
    osobie zamiast treści OD klienta (meta-opis), (2) wiadomość jest za krótka/pusta
    żeby cokolwiek znaczyć. Nie jest to dowód matematyczny — heurystyka, tak jak
    w cascade, tylko z dodatkowymi wzorcami z realnego, obserwowanego przypadku.

    ⚠️ TYLKO dla contact_owner (pole `message` = treść DO przekazania). NIE używać dla
    submit_lead (`problem` = opis SPRAWY klienta, gdzie "Klient chce X" jest normalną,
    poprawną frazą, nie oznaką pustki) — patrz _looks_too_short() niżej. Pomylenie tych
    dwóch odrzucało w praktyce poprawne, konkretne zgłoszenia (obserwowane na żywym
    telefonie: "Klient chce pomocy w sprawie legalizacji pobytu, dotyczącej wizy."
    zostało odrzucone tylko dlatego że zaczynało się od "Klient chce").

    Bug znaleziony na żywym telefonie (drugi przypadek, w samym contact_owner tym
    razem): sprawdzanie SAMEGO POCZĄTKU zdania (startswith) odrzucało "Klient prosi
    o kontakt telefoniczny od właściciela" (ma konkret: "telefoniczny", "od
    właściciela"), a identyczna treść bez słowa "Klient" na początku ("Prosi o
    kontakt telefoniczny od właściciela") przechodziła bez problemu — czysty
    przypadek składni, nie różnica w jakości treści. Fix: liczy się nie TO że zdanie
    zaczyna się od podejrzanej frazy, tylko czy PO NIEJ zostaje realny konkret."""
    m = message.lower().strip()
    if len(m) < 10:
        return True
    for p in _VAGUE_MESSAGE_STARTS:
        if m.startswith(p):
            remainder = m[len(p) :].strip(" .,!?")
            return len(remainder) < 15
    return False


def _looks_too_short(text: str) -> bool:
    """Łagodniejszy filtr dla submit_lead::problem — tylko długość, bez czarnej listy
    fraz (te są legalne we frazowaniu opisu sprawy w trzeciej osobie)."""
    return len((text or "").strip()) < 10


# Wymuszone, zaszyte w kodzie wypowiedzi bota (patrz bot_gemini_test.py::say_now) — dopytanie
# o ciszę, ostrzeżenie o limicie czasu, pożegnania. Zaobserwowany na żywym telefonie bug:
# jedna z tych wypowiedzi wylądowała jako `message`/`problem` w contact_owner (model wywołał
# funkcję z DOKŁADNIE tym tekstem, zamiast treścią od klienta), wysyłając śmieciowy email do
# właściciela. Główny fix to tool_choice="none" na tej wymuszonej odpowiedzi (say_now), TU
# to tylko druga linia obrony — gdyby mimo wszystko coś podobnego się powtórzyło.
_SCRIPTED_BOT_PHRASES = (
    "czy nadal jesteśmy połączeni",
    "nie słyszę odpowiedzi",
    "za chwilę będę kończyć",
    "przepraszam, czas rozmowy się skończył",
)


def _is_scripted_bot_phrase(text: str) -> bool:
    t = (text or "").lower().strip()
    return any(p in t for p in _SCRIPTED_BOT_PHRASES)
