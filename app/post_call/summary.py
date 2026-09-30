"""Inteligentne podsumowanie rozmowy (GPT-4.1-mini) — wspólne dla wszystkich silników głosowych."""

import re

from loguru import logger
from pipecat.processors.aggregators.llm_context import LLMContext

from app.config import settings


def extract_conversation_lines(context: LLMContext) -> list[str]:
    """Wydzielone z generate_conversation_summary() (2026-09-23) żeby maybe_send_call_summary
    mogło dołączyć pełny zapis rozmowy do maila (rozwijana sekcja), nie tylko streszczenie —
    ta sama lista wejściowa co idzie do GPT, tylko bez wołania summarize_conversation_lines()."""
    messages = context.get_messages()
    conversation = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role not in ("user", "assistant") or not content:
            continue
        if isinstance(content, list):
            # Content czasem przychodzi jako lista bloków (np. [{"type": "text", "text": "..."}])
            content = " ".join(
                block.get("text", "") for block in content if isinstance(block, dict) and block.get("text")
            )
        content = (content or "").strip()
        if len(content) > 2:
            label = "Klient" if role == "user" else "Asystent"
            conversation.append(f"{label}: {content[:200]}")
    return conversation


async def generate_conversation_summary(context: LLMContext, tenant: dict | None = None) -> str:
    """Streszcza rozmowę przez szybkie wywołanie GPT. Ten sam pomysł co
    flows.py::generate_conversation_summary, tylko czyta uniwersalny LLMContext
    (context.get_messages(), format OpenAI: {"role": ..., "content": ...}) zamiast
    flow_manager.get_current_context() z pipecat_flows.

    Ekstrakcja z LLMContext -> lista "Klient: .../Asystent: ..." (extract_conversation_lines)
    -> delegacja do summarize_conversation_lines() (wspólnej z bot_elevenlabs_agent.py, patrz tam)."""
    conversation = extract_conversation_lines(context)
    return await summarize_conversation_lines(conversation, tenant)


async def summarize_conversation_lines(conversation: list[str], tenant: dict | None = None) -> str:
    """Właściwe wywołanie GPT-4.1-mini generujące podsumowanie — wydzielone
    2026-09-04 z generate_conversation_summary() żeby ElevenLabs (który ma
    transkrypt w zupełnie innym formacie, data.transcript[] z rolami "agent"/"user",
    nie LLMContext) mógł korzystać z DOKŁADNIE tej samej logiki podsumowania/ekstrakcji
    zamiast polegać na wbudowanym streszczeniu ElevenLabs (analysis.transcript_summary)
    — patrz bot_elevenlabs_agent.py::elevenlabs_post_call, gdzie transkrypt jest
    konwertowany do tego samego formatu list stringów "Klient: .../Asystent: ..."
    przed wywołaniem tej funkcji.

    tenant: OPCJONALNE (2026-09-03, zastępuje usunięte submit_lead) — gdy podane, wstrzykuje
    tenant["additional_info"] (to samo pole "Dodatkowe info dla agenta" z panelu, już użyte
    w głównym system_instruction rozmowy) jako kontekst branży do JEDNEJ, uniwersalnej
    instrukcji ekstrakcji poniżej. Świadomie NIE per-firmowy prompt "jak mnie podsumowywać"
    (dokładanie meta-promptowania nietechnicznemu userowi) — ta sama instrukcja działa dla
    każdej branży, a kontekst firmy (co robi, czego może dotyczyć rozmowa) wystarcza żeby
    model wiedział co jest istotne (np. że "montaż" u firmy klimatyzacyjnej = montaż klimy).
    Bez tenant (stare wywołania) zachowanie identyczne jak wcześniej — proste 2-3 zdania.

    ⚠️ ANTY-HALUCYNACJA (2026-09-25, potwierdzony żywy przypadek — QFX Group, klient bez
    słowa się rozłączył po powitaniu, GPT-4.1-mini i tak wymyślił fikcyjnego rozmówcę
    "Anna Nowak" z telefonem/emailem, mimo jawnej instrukcji "napisz jedno zdanie jeśli
    pusto"). Trzy niezależne warstwy, żadna nie polega wyłącznie na tym że model
    "zachowa się rozsądnie":
      1. TWARDY WARUNEK WEJŚCIOWY — dawniej sprawdzaliśmy tylko `not conversation` (pusta
         lista), ale samo powitanie bota to już 1 element (niepusta lista!), więc ten
         przypadek przechodził dalej do GPT. Teraz wymagamy co najmniej jednej REALNEJ
         linii "Klient: ..." o sensownej długości — bez tego GPT w ogóle nie jest wołany,
         więc fizycznie nie ma jak halucynować.
      2. temperature=0 (było 0.3) + jawny zakaz wymyślania danych dopisany do KAŻDEGO
         wariantu promptu niżej (ANTI_HALLUCINATION).
      3. Weryfikacja po fakcie (_contains_unverified_contact_details) — jeśli GPT mimo
         wszystko wpisze numer telefonu/email którego nie ma w prawdziwym transkrypcie,
         całe podsumowanie jest odrzucane (nie da się "zgadnąć" cudzego numeru, więc to
         twardy dowód konfabulacji — a skoro model zmyślił jeden fakt, nie ufamy reszcie)."""
    try:
        client_lines = [
            line for line in conversation if line.startswith("Klient: ") and len(line) > len("Klient: ") + 3
        ]
        if not client_lines:
            return "Brak treści rozmowy."

        conversation_text = "\n".join(conversation[-20:])

        ANTI_HALLUCINATION = (
            " ⛔ KRYTYCZNE: NIGDY nie wymyślaj imion, numerów telefonu, adresów e-mail, "
            "dat ani żadnych innych faktów, których nie ma DOSŁOWNIE w rozmowie poniżej — "
            "nawet jeśli oczekiwana struktura punktów tego wymaga. Brakującą informację "
            "po prostu pomiń, nie zgaduj i nie dopowiadaj. Lepiej krótsze i niepełne "
            "podsumowanie niż jedno wymyślone słowo."
        )

        if tenant and int(tenant.get("custom_report_format") or 0) == 1:
            # 2026-09-09 — format raportu na życzenie konkretnego klienta (kancelaria
            # prawna QFX Group, patrz historia sesji), włączany per-firma przełącznikiem
            # w panelu ("📋 Format prawniczy raportu", firms.custom_report_format).
            # Struktura i kategorie pilności celowo INNE niż uniwersalny format niżej —
            # to świadomy wyjątek od zasady "jeden format dla wszystkich" z myślą o
            # firmach które potrzebują dokładnie takiego układu (np. do dalszego
            # przetwarzania/segregacji ręcznej). Domyślnie wyłączone dla każdej firmy.
            business_name = tenant.get("name") or "firma"
            additional_info = (tenant.get("additional_info") or "").strip()
            context_block = f'\nKontekst firmy ("{business_name}"): {additional_info}' if additional_info else ""
            system_content = (
                "Podsumuj poniższą rozmowę telefoniczną dla kancelarii, po polsku, w "
                "DOKŁADNIE tej strukturze punktów (pomiń punkt jeśli danej informacji nie "
                "było w rozmowie):\n"
                "Kto: [imię i nazwisko dzwoniącego]\n"
                "Firma / instytucja: [nazwa firmy lub instytucji, jeśli podano]\n"
                "Numer telefonu: [jeśli dzwoniący podał go na głos w rozmowie]\n"
                "Sprawa: [konkretnie czego dotyczy]\n"
                "Czego oczekuje: [co konkretnie chce od kancelarii/adresata]\n"
                "Termin: [jeśli sprawa jest związana z konkretnym terminem]\n"
                'Pilność: JEDNO z: "PILNE" (rozmówca wprost mówi że sprawa jest '
                'pilna/ma krótki termin), "OFERTA HANDLOWA" (to telemarketing/'
                'sprzedaż/oferta współpracy), "STANDARD" (wszystko inne)\n\n'
                "DODATKOWO: jeśli rozmówca przedstawił się jako przedstawiciel sądu, "
                "prokuratury, Policji, komornika, urzędu, banku lub notariusza — "
                'zacznij podsumowanie linią "PRIORYTETOWE" i dopisz pod spodem: '
                "nazwę instytucji, wydział/jednostkę (jeśli podano), sygnaturę lub "
                "numer sprawy (jeśli podano), bezpośredni numer telefonu do rozmówcy "
                "(jeśli podano) — oprócz standardowych punktów powyżej.\n"
                "Pisz zwięźle, bez lania wody, bez dodatkowego nagłówka. Jeśli rozmowa "
                "była pusta/bez treści (np. sama cisza, natychmiastowe rozłączenie) — "
                "napisz jedno zdanie o tym zamiast reszty punktów."
                f"{context_block}"
                f"{ANTI_HALLUCINATION}"
            )
            max_tokens = 400
        elif tenant:
            business_name = tenant.get("name") or "firma"
            additional_info = (tenant.get("additional_info") or "").strip()
            context_block = f'\nKontekst firmy ("{business_name}"): {additional_info}' if additional_info else ""
            system_content = (
                "Podsumuj poniższą rozmowę telefoniczną dla właściciela firmy, po polsku, "
                "krótkimi punktami — NIE jednym akapitem. Wypisz TYLKO to, co realnie padło "
                "w rozmowie (pomiń punkt jeśli danej informacji nie było):\n"
                '- Priorytet: JEDNO z: "🚨 PILNE" (klient opisuje awarię/usterkę/coś nie działa '
                'i chce naprawy szybko), "🔥 GORĄCY LEAD" (klient ma konkretną, sprecyzowaną '
                "potrzebę — wie czego chce, podał konkrety typu lokalizacja/ilość/budżet/termin, "
                'brzmi na zdecydowanego), "🟡 STANDARDOWE" (dopiero się rozgląda, pyta ogólnie, '
                "brak konkretów). Jeśli rozmowa nie dotyczyła żadnej sprawy (samo pytanie o "
                "godziny/adres/FAQ bez intencji zakupowej) — POMIŃ CAŁY ten punkt (nie pisz "
                '"Priorytet: —" ani samego "—", po prostu go nie wypisuj). Wybierz jedno, nie '
                "tłumacz wyboru.\n"
                "- Kto dzwonił: imię/nazwisko jeśli klient je podał (inaczej pomiń punkt)\n"
                "- Firma: nazwa firmy dzwoniącego, TYLKO jeśli klient ją wprost podał (inaczej pomiń "
                "punkt — nie zgaduj i nie wpisuj nazwy firmy do której dzwoni, chodzi o firmę KLIENTA)\n"
                "- Powód kontaktu: konkretnie czego klient chciał/szukał/o co pytał\n"
                "- Szczegóły: wszystko dodatkowe co klient podał i co ma znaczenie dla TEJ "
                "konkretnej firmy (np. lokalizacja, rodzaj usługi/produktu, termin, pilność, "
                "budżet, marka/model urządzenia, kod błędu) — użyj kontekstu firmy poniżej żeby "
                "wiedzieć co jest istotne\n"
                "- Wynik rozmowy: czy sprawa została załatwiona, czy klient czeka na kontakt, "
                "czy przekierowano/odmówiono itp.\n"
                "Pisz zwięźle, bez lania wody, bez nagłówka. Jeśli rozmowa była pusta/bez treści "
                "(np. sama cisza, natychmiastowe rozłączenie) — pomiń Priorytet i napisz jedno "
                "zdanie o tym zamiast reszty punktów."
                f"{context_block}"
                f"{ANTI_HALLUCINATION}"
            )
            max_tokens = 350
        else:
            system_content = (
                "Streść poniższą rozmowę telefoniczną w 2-3 zdaniach po polsku. "
                "Napisz: czego klient szukał/pytał, czy zostawił dane kontaktowe "
                "lub opisał konkretną sprawę, i jaki był wynik rozmowy. Pisz zwięźle."
                f"{ANTI_HALLUCINATION}"
            )
            max_tokens = 150

        import openai

        client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
        response = await client.chat.completions.create(
            model="gpt-4.1-mini",  # ten sam model co flows.py::send_message_email w cascade
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": conversation_text},
            ],
            max_tokens=max_tokens,
            temperature=0,  # było 0.3 — zero losowości, patrz duży komentarz ANTY-HALUCYNACJA wyżej
        )
        result = response.choices[0].message.content.strip()

        if _contains_unverified_contact_details(result, conversation_text):
            logger.error(
                "📋 [SUMMARY] Odrzucono wygenerowane podsumowanie — zawiera numer telefonu "
                f"lub email spoza transkryptu (prawdopodobna halucynacja GPT). Surowe "
                f"wyjście modelu: {result!r} | Źródłowy transkrypt: {conversation_text!r}"
            )
            return "Rozmowa zawierała ograniczoną treść — pełny zapis dostępny osobno w panelu."

        return result
    except Exception as e:
        logger.error(f"📋 [REALTIME TEST] Summary generation error: {e}")
        return "Nie udało się wygenerować streszczenia."


_PHONE_CANDIDATE_RE = re.compile(r"\+?\d[\d\s\-]{5,}\d")
_EMAIL_CANDIDATE_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _contains_unverified_contact_details(summary: str, source_text: str) -> bool:
    """Warstwa 3 anty-halucynacyjnej ochrony (patrz duży komentarz w
    summarize_conversation_lines()) — True gdy podsumowanie zawiera numer telefonu lub
    adres e-mail, którego nie ma (choćby w innym formatowaniu) w prawdziwym transkrypcie
    rozmowy. Numer porównujemy po wyciągnięciu samych cyfr (klient mógł podać go ze
    spacjami/myślnikami, GPT mógł sformatować inaczej niż w rozmowie), email dosłownie
    (case-insensitive, dokładny string) — oba typy danych da się jednoznacznie
    zweryfikować, bo model nie ma jak "zgadnąć" cudzego prawdziwego numeru/emaila."""
    source_digits = re.sub(r"\D", "", source_text)
    for match in _PHONE_CANDIDATE_RE.findall(summary):
        digits = re.sub(r"\D", "", match)
        if len(digits) >= 7 and digits not in source_digits:
            return True
    source_lower = source_text.lower()
    return any(match.lower() not in source_lower for match in _EMAIL_CANDIDATE_RE.findall(summary))


_SUMMARY_FIELD_LABELS = ["Priorytet", "Kto dzwonił", "Firma", "Powód kontaktu", "Szczegóły", "Wynik rozmowy"]


def _parse_summary_fields(summary: str) -> dict:
    """Wyciąga pojedyncze pola (Powód kontaktu/Szczegóły/Wynik rozmowy/Kto dzwonił) z
    tekstu streszczenia — WYŁĄCZNIE do wzbogacenia CRM (osobne pola zamiast jednego
    bloku tekstu). Celowo parsuje istniejący tekst zamiast zmieniać prompt w
    summarize_conversation_lines() — GPT nie zawsze trzyma się ściśle jednej linii na
    punkt (bywa że pisze wszystko jednym ciągiem bez \\n), więc kotwiczymy się na
    samych etykietach ("Powód kontaktu:" itd.), nie na podziale linii — działa
    niezależnie od tego jak GPT akurat sformatował odpowiedź. Zero zmian w funkcji
    generującej tekst do maila = zero ryzyka dla raportów innych firm."""
    fields: dict[str, str] = {}
    pattern = "|".join(re.escape(label) for label in _SUMMARY_FIELD_LABELS)
    matches = list(re.finditer(rf"(?:{pattern}):\s*", summary))
    for i, m in enumerate(matches):
        label = m.group(0).rstrip(": \t").strip("- ").strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(summary)
        fields[label] = summary[start:end].strip(" -\n\t")
    return fields
