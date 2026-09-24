# realtime_tools.py — function-calling tools dla OpenAI Realtime (Faza 4 planu
# migracji, patrz CLAUDE.md). Wydzielone z bot_gemini_test.py.
"""
CONTACT_OWNER — pierwsza funkcja Fazy 4 (kolejność Faz 3/4 odwrócona świadomie —
patrz docstring bot_gemini_test.py: booking jest ryzykowniejszy, więc zostaje na koniec).

TYLKO ścieżka "zostaw wiadomość" — działa dla Twilio I Vonage jednakowo (samo
wysłanie emaila nie zależy od dostawcy telefonii). Żywe przekierowanie rozmowy
(transfer) ŚWIADOMIE pominięte na razie:
  - w cascade transfer dla Twilio idzie przez dwuetapowy trik (zapis do
    transfer_requests + TwiML <Dial> w /twilio/after-stream), którego bot_gemini_test.py
    w ogóle nie ma (brak własnego /twilio/after-stream)
  - dla Vonage nie ma GOTOWEGO mechanizmu wcale — wymagałby osobnego wywołania
    Vonage REST API na żywym połączeniu (patrz docstring bot.py przy sekcji VONAGE)
  To jest dokładnie ta granica, którą plan w CLAUDE.md już wcześniej zaakceptował:
  "acceptable to ship 'leave a message only' for Vonage at first".

END_CONVERSATION — global-function odpowiednik end_conversation_function() z cascade
(flows.py) — bez tego bot nie ma ŻADNEGO sposobu żeby rozpoznać koniec rozmowy inaczej
niż przez ciszę (patrz bot_gemini_test.py::monitor_call_health) — klient mówiący
"dziękuję, to wszystko" po prostu wisiałby w rozmowie aż zadziała idle timeout.

send_message_email() jest SKOPIOWANA z flows.py, nie zaimportowana — ten sam powód co
reszta promptu (patrz docstring realtime_prompt.py): flows.py ciągnie pipecat_flows,
niekompatybilne z pipecat-ai==1.4.0 użytym w tym serwisie.

RAPORT Z ROZMOWY (Faza 5, pierwszy kawałek) — działa PO KAŻDEJ rozmowie, niezależnie
od tego czy klient czegoś konkretnego chciał (to różni się od contact_owner, który
wysyła tylko gdy klient WPROST poprosił o kontakt/wiadomość). Bramkowane DOKŁADNIE tym
samym polem co w cascade (bot.py, sekcja "Lead email po rozmowie"): `lead_email_enabled`
+ `lead_email` (lub `notification_email` jako fallback) — to jest to samo pole co
checkbox "Raport z rozmowy na email" w panelu, więc zero nowej konfiguracji potrzebne."""

import os
import re
import time
import uuid
import asyncio
from urllib.parse import quote

from loguru import logger

from pipecat.frames.frames import EndFrame
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.llm_service import FunctionCallParams
from pipecat.adapters.schemas.function_schema import FunctionSchema

# db/saas_db — bezpieczny import wprost, helpers.py nie ma zależności od pipecat
# (patrz docstring bot_gemini_test.py po pełne wyjaśnienie tego wzorca).
from helpers import db, saas_db

PRICE_PER_MINUTE = 0.39  # zł/min — MUSI być zsynchronizowane z bot.py::PRICE_PER_MINUTE


async def send_message_email(tenant: dict, customer_name: str, message: str, phone: str, to_email: str) -> bool:
    """Wyślij email z wiadomością do właściciela. Uproszczona kopia flows.py::send_message_email
    (bez GPT-streszczenia kontekstu rozmowy — bonus, nie rdzeń funkcji)."""
    resend_api_key = os.getenv("RESEND_API_KEY")
    if not resend_api_key:
        logger.warning("📧 [REALTIME TEST] RESEND_API_KEY nieskonfigurowany — nie wysyłam")
        return False

    business_name = tenant.get("name", "Firma")
    html_content = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px;">
        <h2 style="color: #333;">📞 Nowa wiadomość od klienta</h2>
        <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
            <tr><td style="padding: 8px; border-bottom: 1px solid #eee; width: 120px;"><strong>Firma:</strong></td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;">{business_name}</td></tr>
            <tr><td style="padding: 8px; border-bottom: 1px solid #eee;"><strong>Od:</strong></td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;">{customer_name}</td></tr>
            <tr><td style="padding: 8px; border-bottom: 1px solid #eee;"><strong>Telefon:</strong></td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;"><a href="tel:{phone}">{phone}</a></td></tr>
        </table>
        <p><strong>💬 Wiadomość:</strong></p>
        <p style="background: #f5f5f5; padding: 15px; border-radius: 5px;">{message}</p>
        <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
        <p style="color: #999; font-size: 12px;">Wiadomość przekazana przez asystenta głosowego (test Realtime) • {business_name}</p>
    </div>
    """
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {resend_api_key}", "Content-Type": "application/json"},
                json={
                    "from": "Voice AI <noreply@bizvoice.pl>",
                    "to": [to_email],
                    "subject": f"📞 Wiadomość od {customer_name} - {business_name}",
                    "html": html_content,
                },
                timeout=10.0,
            )
            if response.status_code == 200:
                logger.info("📧 [REALTIME TEST] Email wysłany")
                return True
            logger.error(f"📧 [REALTIME TEST] Resend error: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logger.error(f"📧 [REALTIME TEST] Send email error: {e}")
        return False


_VAGUE_MESSAGE_STARTS = (
    "klient chce", "klient prosi", "klient jest", "klient potrzebuje", "klient dzwoni",
    "proszę o kontakt", "proszę skontaktować się", "proszę zadzwonić", "proszę oddzwonić",
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
            remainder = m[len(p):].strip(" .,!?")
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


def build_contact_owner_tool(
    tenant: dict, caller_phone: str, task_box: dict, call_state: dict, has_transfer_tool: bool = False
) -> FunctionSchema:
    """FunctionSchema z handlerem przypiętym bezpośrednio — LLMContext rejestruje go
    automatycznie (patrz bot_gemini_test.py::build_realtime_llm), bez osobnego
    register_function.

    has_transfer_tool: True gdy w TEJ SAMEJ rozmowie jest też zarejestrowane
    transfer_to_owner (patrz build_transfer_tool niżej) — czyli Vonage +
    transfer_enabled=1. Zmienia opis: bez transfer_to_owner "połącz mnie z
    właścicielem" może oznaczać TYLKO wiadomość (to jedyna dostępna opcja), więc
    to jednoznaczne. Z transfer_to_owner dostępnym ta sama fraza jest DWUZNACZNA
    (żywe połączenie czy wiadomość?) — zgłoszone przez użytkownika po testach na
    żywo jako coś do doprecyzowania, więc opis w tym wariancie każe modelowi
    wprost zapytać którą opcję klient woli, zamiast zgadywać.

    task_box: {"task": None} wypełniane PO stworzeniu PipelineTask — w momencie budowy
    tego tool'a (przed pipeline'em, bo LLMContext potrzebuje tools już przy konstrukcji
    llm) `task` jeszcze nie istnieje. Handler czyta task_box["task"] dopiero przy
    faktycznym wywołaniu (w trakcie żywej rozmowy), więc do tego czasu jest już ustawiony.

    call_state: to samo co w bot_gemini_test.py::make_call_state() — TU ustawiamy
    call_state["ended"]=True od razu po udanym wysłaniu, żeby monitor_call_health
    przestał liczyć ciszę na kończącym się połączeniu. Bez tego (bug znaleziony na
    żywym telefonie): po EndFrame z tej funkcji monitor dalej działał, nie wiedział że
    rozmowa się kończy, i próbował rozłączyć DRUGI RAZ przez "brak odpowiedzi 20s"."""

    async def handle_contact_owner(params: FunctionCallParams):
        customer_name = (params.arguments.get("customer_name") or "").strip() or "Nieznany"
        message = (params.arguments.get("message") or "").strip()
        logger.info(f"📞 [REALTIME TEST] contact_owner: {customer_name} — {message[:60]!r}")

        owner_email = tenant.get("notification_email") or tenant.get("email")
        if not owner_email:
            logger.warning("📞 [REALTIME TEST] contact_owner: brak notification_email na tenancie")
            await params.result_callback({"status": "error", "reason": "no_owner_email"})
            return
        if not message:
            await params.result_callback({"status": "error", "reason": "empty_message"})
            return
        if _is_scripted_bot_phrase(message):
            logger.warning(f"📞 [REALTIME TEST] contact_owner: treść to zaszyta wypowiedź bota (nie klienta), odrzucam: {message[:60]!r}")
            await params.result_callback({"status": "error", "reason": "message_too_vague"})
            return
        if _looks_like_vague_meta_message(message):
            # Model czasem zamiast prawdziwej treści wpisuje własny, pokrętny opis sytuacji
            # (np. "Proszę o kontakt z Pawłem, bo ktoś nie skontaktował się" — bez sensu jako
            # wiadomość). 1:1 zabezpieczenie z cascade (flows_contact.py::handle_set_contact_message)
            # — odrzuć i każ dopytać, zamiast wysyłać śmieciowego emaila.
            logger.warning(f"📞 [REALTIME TEST] contact_owner: mętna wiadomość, odrzucam: {message[:60]!r}")
            await params.result_callback({"status": "error", "reason": "message_too_vague"})
            return

        # 2026-09-09 — jeśli ta sama firma MA TEŻ włączony raport z rozmowy (lead_email_enabled)
        # na TEN SAM adres — nie wysyłaj osobnego maila teraz. Zamiast tego odłóż treść do
        # call_state, a maybe_send_call_summary() (koniec rozmowy) doklei ją do JEDNEGO,
        # połączonego maila zamiast wysyłać dwa niemal jednoczesne maile z tego samego numeru.
        # Gdy adresy się różnią (rzadkie, ale panel na to pozwala) albo raport jest wyłączony —
        # wysyłamy jak dotychczas natychmiast, żeby nie zgubić gwarancji dostarczenia.
        report_to_email = tenant.get("lead_email") or tenant.get("notification_email") or tenant.get("email")
        defer_to_report = bool(int(tenant.get("lead_email_enabled") or 0) and report_to_email and report_to_email == owner_email)
        if defer_to_report:
            call_state["pending_contact_owner"] = {"customer_name": customer_name, "message": message}
            sent = True
            logger.info(f"📞 [REALTIME TEST] contact_owner: odłożone do połączonego raportu ({owner_email})")
        else:
            sent = await send_message_email(tenant, customer_name, message, caller_phone, owner_email)
        # 2026-09-09 — opcjonalny per-firmowy dokładny tekst pożegnania (panel: pole pod
        # checkboxem "Zbieranie wiadomości dla właściciela"). Bez tego model i tak formułował
        # jakieś pożegnanie, ale trzymał się przykładu WPISANEGO NA SZTYWNO w opis tego
        # narzędzia niżej — sugestia z DODATKOWYCH INFO (znacznie dalej w kontekście) ją
        # przegrywała, złapane na żywo (QFX Group). Puste pole = brak zmiany zachowania.
        closing_line = (tenant.get("contact_owner_closing_line") or "").strip()
        result = {"status": "ok" if sent else "error"}
        if sent and closing_line:
            result["say_exactly"] = closing_line
        await params.result_callback(result)

        if sent:
            call_state["ended"] = True
            # Zaplanuj rozłączenie po TTS — ta sama logika co bot.py::save_and_confirm_message
            # (sleep + EndFrame — nie czekamy na realny koniec audio, patrz komentarz przy say_now
            # w bot_gemini_test.py).
            async def auto_hangup():
                await asyncio.sleep(6.0)  # dłużej niż w say_now — tu bot jeszcze SAM formułuje potwierdzenie
                try:
                    t = task_box.get("task")
                    if t:
                        await t.queue_frame(EndFrame())
                        logger.info("🔚 [REALTIME TEST] EndFrame po contact_owner")
                except Exception as e:
                    logger.error(f"[REALTIME TEST] EndFrame po contact_owner error: {e}")
            asyncio.create_task(auto_hangup())

    if has_transfer_tool:
        trigger_block = """Klient chce KONTAKTU z właścicielem/firmą. Masz DWA sposoby: ta funkcja
(zostaw wiadomość, właściciel oddzwoni) ORAZ transfer_to_owner (żywe połączenie TERAZ).
- Jeśli klient WYRAŹNIE mówi że chce wiadomość/oddzwonienie ("zostawić wiadomość", "niech
  oddzwoni", "proszę o kontakt zwrotny") → użyj TEJ funkcji wprost, bez dopytywania.
- Jeśli klient WYRAŹNIE chce żywej rozmowy TERAZ ("połącz mnie", "chcę z kimś porozmawiać
  natychmiast") → NIE używaj tej funkcji, użyj transfer_to_owner.
- Jeśli NIE JEST JASNE które z dwóch klient ma na myśli (np. samo "czy mogę porozmawiać z
  właścicielem?") → ZAPYTAJ wprost, jednym zdaniem, BEZ formy "ty" (np. "Mogę połączyć od razu,
  albo zostawić wiadomość, żeby ktoś oddzwonił — co będzie wygodniejsze?") — i dopiero na
  podstawie odpowiedzi wybierz właściwą funkcję. NIE zgaduj.
- klient jest sfrustrowany i potrzebuje pomocy człowieka, a nie jest jasne który wariant → też dopytaj jak wyżej"""
    else:
        trigger_block = """Klient chce kontaktu z właścicielem/firmą — zostawić wiadomość. Użyj gdy:
- "chcę porozmawiać z właścicielem", "proszę o kontakt", "czy mogę zostawić wiadomość"
- "połącz mnie", "przekieruj mnie", "chcę rozmawiać z człowiekiem"
- klient jest sfrustrowany i potrzebuje pomocy człowieka
- nie możesz pomóc i klient potrzebuje właściciela"""

    return FunctionSchema(
        name="contact_owner",
        description=f"""{trigger_block}
Wywołaj DOPIERO gdy masz OBA pola (imię i treść wiadomości). Jeśli czegoś brakuje, zapytaj
klienta JEDNO krótkie pytanie wprost (np. "Czego dokładnie dotyczy sprawa?" / "Na jakie imię
mam zapisać?") — NIE zapowiadaj że o to zapytasz, po prostu zapytaj.
Jeśli wynik wywołania to status="error", reason="message_too_vague" — wiadomość była za ogólna
(np. samo "chce kontaktu" bez konkretu). Krótko przeproś i zadaj TO SAMO pytanie ponownie,
bez tłumaczenia dlaczego pytasz drugi raz.
⚠️ Jeśli wynik to status="ok" — połączenie zaraz automatycznie się rozłączy (kilka sekund po
Twojej odpowiedzi), więc Twoja odpowiedź MUSI być zamkniętym pożegnaniem, NIE pytaniem
otwartym. NIE pytaj "Czy mogę jeszcze w czymś pomóc?" — nikt nie zdąży odpowiedzieć.
⛔ Jeśli wynik zawiera pole "say_exactly" (niepuste) — Twoja odpowiedź MUSI być TĄ TREŚCIĄ
SŁOWO W SŁOWO, bez zmiany, dodania czy skrócenia — ma pierwszeństwo przed przykładem niżej.
Jeśli "say_exactly" NIE występuje w wyniku — sformułuj pożegnanie sam, np.:
✅ "Dobrze, przekazuję wiadomość właścicielowi. Dziękuję za telefon, do usłyszenia!"
❌ "Wiadomość została przekazana. Czy mogę jeszcze w czymś pomóc?\"""",
        properties={
            "customer_name": {"type": "string", "description": "Imię klienta"},
            "message": {
                "type": "string",
                "description": (
                    "DOKŁADNA treść tego czego klient chce/potrzebuje, jego słowami lub krótkim "
                    "rzeczowym streszczeniem KONKRETU sprawy (np. 'Chce przełożyć wizytę z piątku "
                    "na sobotę', 'Pyta o możliwość zniżki grupowej dla 5 osób'). "
                    "⛔ NIE formułuj tego jako opis o kliencie w trzeciej osobie i NIE jako meta-opis "
                    "sytuacji (np. NIE 'Klient prosi o kontakt', NIE 'Proszę o kontakt z klientem') — "
                    "to pole ma zawierać SAMĄ TREŚĆ sprawy, nie opis że wiadomość istnieje. "
                    "⚠️ To jest wewnętrzny opis PARAMETRU dla Ciebie — klient dzwoni, MÓWI, nie pisze. "
                    "NIGDY nie używaj słów 'napisz'/'pisz' w rozmowie z klientem (np. 'napisz proszę "
                    "ile minut potrzebujesz') — zamiast tego zapytaj głosowo, np. 'powiedz proszę' / "
                    "'ile minut mniej więcej potrzebujesz?'."
                ),
            },
        },
        required=["customer_name", "message"],
        handler=handle_contact_owner,
    )


def build_end_conversation_tool(task_box: dict, call_state: dict) -> FunctionSchema:
    """Global-function odpowiednik end_conversation_function() z cascade (flows.py) —
    tam było zawsze dostępne niezależnie od node'a. Bez tego bot nie miał ŻADNEGO
    sposobu żeby rozpoznać koniec rozmowy inaczej niż przez ciszę (10s/20s) — klient
    mówiący "dziękuję, to wszystko" po prostu wisiał w rozmowie aż zadziałał idle timeout.

    call_state: patrz komentarz w build_contact_owner_tool — ten sam fix (call_state["ended"]
    ustawiane od razu), żeby monitor_call_health nie próbował rozłączyć drugi raz."""

    async def handle_end_conversation(params: FunctionCallParams):
        logger.info("👋 [REALTIME TEST] end_conversation — rozłączam po pożegnaniu")
        call_state["ended"] = True
        await params.result_callback({"status": "ok"})

        async def auto_hangup():
            await asyncio.sleep(3.0)  # tyle samo co say_now — tu pożegnanie jest krótkie, z góry znane
            try:
                t = task_box.get("task")
                if t:
                    await t.queue_frame(EndFrame())
                    logger.info("🔚 [REALTIME TEST] EndFrame po end_conversation")
            except Exception as e:
                logger.error(f"[REALTIME TEST] EndFrame po end_conversation error: {e}")

        asyncio.create_task(auto_hangup())

    return FunctionSchema(
        name="end_conversation",
        description="""Klient KOŃCZY rozmowę — żegna się, dziękuje, mówi że to wszystko. Użyj gdy:
- "dziękuję, to wszystko", "do widzenia", "dzięki, pa", "nic więcej", "to na razie wszystko", "koniec"
⚠️ WAŻNA KOLEJNOŚĆ: wywołaj tę funkcję OD RAZU, NIC nie mówiąc przed nią — żadnego pożegnania,
żadnej zapowiedzi typu "już kończymy". Twoja odpowiedź po wyniku tej funkcji (przyjdzie
automatycznie) to jest to miejsce na krótkie, naturalne pożegnanie (np. "Dziękuję za telefon,
do usłyszenia!", "Miłego dnia!") — RÓŻNE za każdym razem. Jedno pożegnanie, nie dwa.""",
        properties={},
        required=[],
        handler=handle_end_conversation,
    )


# ==========================================
# RAPORT Z ROZMOWY — Faza 5, pierwszy kawałek (patrz docstring pliku)
# ==========================================

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
                block.get("text", "") for block in content
                if isinstance(block, dict) and block.get("text")
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
    Bez tenant (stare wywołania) zachowanie identyczne jak wcześniej — proste 2-3 zdania."""
    try:
        if not conversation:
            return "Brak treści rozmowy."

        conversation_text = "\n".join(conversation[-20:])

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
                "Pilność: JEDNO z: \"PILNE\" (rozmówca wprost mówi że sprawa jest "
                "pilna/ma krótki termin), \"OFERTA HANDLOWA\" (to telemarketing/"
                "sprzedaż/oferta współpracy), \"STANDARD\" (wszystko inne)\n\n"
                "DODATKOWO: jeśli rozmówca przedstawił się jako przedstawiciel sądu, "
                "prokuratury, Policji, komornika, urzędu, banku lub notariusza — "
                "zacznij podsumowanie linią \"PRIORYTETOWE\" i dopisz pod spodem: "
                "nazwę instytucji, wydział/jednostkę (jeśli podano), sygnaturę lub "
                "numer sprawy (jeśli podano), bezpośredni numer telefonu do rozmówcy "
                "(jeśli podano) — oprócz standardowych punktów powyżej.\n"
                "Pisz zwięźle, bez lania wody, bez dodatkowego nagłówka. Jeśli rozmowa "
                "była pusta/bez treści (np. sama cisza, natychmiastowe rozłączenie) — "
                "napisz jedno zdanie o tym zamiast reszty punktów."
                f"{context_block}"
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
                "- Priorytet: JEDNO z: \"🚨 PILNE\" (klient opisuje awarię/usterkę/coś nie działa "
                "i chce naprawy szybko), \"🔥 GORĄCY LEAD\" (klient ma konkretną, sprecyzowaną "
                "potrzebę — wie czego chce, podał konkrety typu lokalizacja/ilość/budżet/termin, "
                "brzmi na zdecydowanego), \"🟡 STANDARDOWE\" (dopiero się rozgląda, pyta ogólnie, "
                "brak konkretów) lub \"—\" (rozmowa nie dotyczyła żadnej sprawy — samo pytanie o "
                "godziny/adres/FAQ bez intencji zakupowej). Wybierz jedno, nie tłumacz wyboru.\n"
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
            )
            max_tokens = 350
        else:
            system_content = (
                "Streść poniższą rozmowę telefoniczną w 2-3 zdaniach po polsku. "
                "Napisz: czego klient szukał/pytał, czy zostawił dane kontaktowe "
                "lub opisał konkretną sprawę, i jaki był wynik rozmowy. Pisz zwięźle."
            )
            max_tokens = 150

        import openai
        client = openai.AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        response = await client.chat.completions.create(
            model="gpt-4.1-mini",  # ten sam model co flows.py::send_message_email w cascade
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": conversation_text},
            ],
            max_tokens=max_tokens,
            temperature=0.3,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"📋 [REALTIME TEST] Summary generation error: {e}")
        return "Nie udało się wygenerować streszczenia."


# CRM Integration (n8n + dowolny CRM klienta) — patrz CLAUDE.md, sekcja "CRM Integration".
# 2026-09-13: POC ograniczony wyłącznie do numeru demo/sprzedażowego BizVoice
# (+48459050542) przez sztywną listę numerów. 2026-09-18: zastąpione przełącznikiem
# per-tenant z panelu (firms.crm_enabled/crm_provider/crm_domain/crm_api_key,
# patrz bizvoice-panel/src/app/firm/[id]/page.tsx sekcja "Integracja CRM") — każda
# firma sama decyduje i sama podaje swój klucz, zero sztywnych numerów w kodzie.
# _CRM_TEST_PHONE_NUMBERS zostaje jako fallback WYŁĄCZNIE na wypadek starych tenantów
# sprzed migracji których ktoś zapomniał przełączyć w panelu — docelowo martwy kod.
_CRM_TEST_PHONE_NUMBERS = {"+48459050542"}
N8N_CRM_WEBHOOK_URL = os.getenv(
    "N8N_CRM_WEBHOOK_URL", "https://magnus1503.app.n8n.cloud/webhook/call-summary"
)


def _is_crm_test_tenant(tenant: dict) -> bool:
    """Nazwa zostaje z czasów POC (patrz komentarz wyżej) żeby nie zmieniać nazwy w
    wywołaniach w bot_elevenlabs_agent.py/realtime_tools.py — dziś sprawdza realny
    przełącznik per-firma, nie tylko testowy numer."""
    if int(tenant.get("crm_enabled") or 0) == 1 and (tenant.get("crm_api_key") or "").strip():
        return True
    phone = (tenant.get("phone_number") or "").replace(" ", "").replace("-", "")
    return phone in _CRM_TEST_PHONE_NUMBERS


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


async def estimate_deal_value(summary: str, tenant: dict) -> int | None:
    """Osobne, dodatkowe wywołanie GPT WYŁĄCZNIE do oszacowania wartości transakcji dla
    CRM (pole Deal.value) — woła się tylko dla testowego tenanta CRM i tylko gdy
    rozmowa wygląda na gorący lead (patrz maybe_send_to_crm). Celowo CAŁKOWICIE
    odizolowane od summarize_conversation_lines/generate_conversation_summary (funkcja
    mailowa) — nowa, osobna funkcja, nie modyfikacja tamtego promptu — więc nie ma
    żadnego ryzyka dla raportów mailowych innych firm."""
    try:
        additional_info = (tenant.get("additional_info") or "").strip()
        system_content = (
            "Na podstawie poniższego streszczenia rozmowy telefonicznej i cennika/oferty "
            "firmy oszacuj miesięczną wartość tej transakcji w złotych, jeśli da się to "
            "wywnioskować z rozmowy (np. klient wspomniał konkretny pakiet/usługę z "
            "cennika). Odpowiedz WYŁĄCZNIE samą liczbą całkowitą (bez \"zł\", bez spacji, "
            "bez opisu), albo słowem \"brak\" jeśli nie da się tego ocenić.\n"
            f"Cennik/kontekst firmy: {additional_info}"
        )
        import openai
        client = openai.AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        response = await client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": summary},
            ],
            max_tokens=20,
            temperature=0,
        )
        digits = "".join(ch for ch in response.choices[0].message.content if ch.isdigit())
        return int(digits) if digits else None
    except Exception as e:
        logger.error(f"📋 [CRM] Estymacja wartości deala error: {e}")
        return None


async def maybe_send_to_crm(tenant: dict, caller_phone: str, summary: str) -> None:
    """Wysyła streszczenie rozmowy do n8n → CRM klienta. Nieblokująca — błąd tutaj
    (n8n padł, timeout) nigdy nie może wywrócić resztę maybe_send_call_summary; mail
    idzie niezależnie od tego czy to się uda.

    tenant_phone (NUMER FIRMY, przypisany numer BizVoice — nie mylić z caller_phone,
    czyli numerem DZWONIĄCEGO) jedzie w payloadzie właśnie po to, żeby n8n miał
    stabilny, unikalny klucz do routingu "która firma → który CRM/credential", gdy
    dojdzie kolejny klient z własnym Pipedrive/Bitrix24 (patrz CLAUDE.md).

    2026-09-14 — dodane pola reason/details/outcome (parsowane z summary, patrz
    _parse_summary_fields) i estimated_value (patrz estimate_deal_value, tylko dla
    🔥 GORĄCY LEAD) — dają n8n materiał do ustawienia osobnych pól w Pipedrive (Deal
    custom fields + Deal.value) zamiast tylko jednego bloku tekstu w notatce.

    2026-09-17 — dodane pole `priority` (parsowane z linii "Priorytet: <emoji> <etykieta>"
    w summary, patrz _parse_summary_fields). WAŻNE: priorytet w summary NIE jest na
    początku całego tekstu — to jeden z wypunktowań w środku ("- Priorytet: 🔥 GORĄCY
    LEAD\n- Kto dzwonił: ...", patrz prompt w summarize_conversation_lines()). Test na
    żywym telefonie (2026-09-17) pokazał że zarówno ten hook, jak i n8n Code node,
    błędnie zakładały że summary.startswith(emoji) — to nigdy nie mogło zadziałać na
    prawdziwej rozmowie, tylko na ręcznie spreparowanych testowych payloadach gdzie emoji
    wstawialiśmy na starcie tekstu. Stąd `priority` jako osobne, czyste pole zamiast
    każenia n8n zgadywać format z surowego summary.

    2026-09-17 — dodane pola `name`/`organization` (z nowego punktu "Firma" w prompcie,
    patrz summarize_conversation_lines) — n8n używa ich żeby nazwać kontakt w Pipedrive
    imieniem/firmą klienta zamiast samym numerem telefonu, gdy klient je poda. Jedyna
    zmiana w SAMYM prompcie (nie tylko w parsowaniu) w tym pliku — dodaje jeden,
    opcjonalny punkt do streszczenia używanego też przez mail, ale GPT pomija go gdy
    brak danych, więc raporty innych firm wyglądają tak jak wcześniej."""
    try:
        fields = _parse_summary_fields(summary)
        priority = fields.get("Priorytet") or ""
        estimated_value = None
        if "🔥" in priority:
            estimated_value = await estimate_deal_value(summary, tenant)
        import httpx
        async with httpx.AsyncClient() as client:
            await client.post(
                N8N_CRM_WEBHOOK_URL,
                json={
                    "business_name": tenant.get("name") or "",
                    "tenant_phone": tenant.get("phone_number") or "",
                    "caller_phone": caller_phone or "",
                    # 2026-09-18 — crm_provider/crm_domain/crm_api_key per-firma (panel, patrz
                    # ensureColumns() w bizvoice-panel/api/firms/[id]/route.ts) — n8n routuje po
                    # crm_provider i używa TEGO klucza/domeny zamiast sztywnego credentiala.
                    # Puste dla starego testowego tenanta (fallback po numerze w
                    # _is_crm_test_tenant) — n8n dla niego dalej używa własnego, zapisanego
                    # credentiala Pipedrive dopóki nie zostanie przełączony w panelu.
                    "crm_provider": tenant.get("crm_provider") or "",
                    "crm_domain": tenant.get("crm_domain") or "",
                    "crm_api_key": tenant.get("crm_api_key") or "",
                    "summary": summary,
                    "priority": priority,
                    "name": fields.get("Kto dzwonił") or "",
                    "organization": fields.get("Firma") or "",
                    "reason": fields.get("Powód kontaktu") or "",
                    "details": fields.get("Szczegóły") or "",
                    "outcome": fields.get("Wynik rozmowy") or "",
                    "estimated_value": estimated_value,
                },
                timeout=8.0,
            )
    except Exception as e:
        logger.error(f"📋 [CRM] n8n webhook error: {e}")


async def send_call_summary_email(
    tenant: dict, caller_phone: str, summary: str, to_email: str, pending_message: dict | None = None,
    transcript_lines: list[str] | None = None,
) -> bool:
    """Email z raportem PO KAŻDEJ rozmowie. Jedyny mechanizm "zgłoszeniowy" od 2026-09-03
    (submit_lead usunięty — patrz docstring generate_conversation_summary) — summary jest
    teraz strukturalnym, punktowym podsumowaniem per-firma (nie prostym 2-3-zdaniowym),
    stąd white-space:pre-line żeby punkty "-" z GPT renderowały się jako osobne linie.

    pending_message: 2026-09-09 — gdy contact_owner odłożył wiadomość dla właściciela (patrz
    handle_contact_owner) bo firma ma raport włączony na TEN SAM adres, treść trafia tu jako
    osobna, wyróżniona sekcja NAD podsumowaniem — dosłowna (nie przepuszczona przez GPT),
    żeby nie zgubić/nie sparafrazować tego co klient faktycznie powiedział.

    transcript_lines: 2026-09-23 — pełny zapis rozmowy ("Klient: .../Asystent: ..."), ta sama
    lista co idzie do GPT na podsumowanie (extract_conversation_lines/conversation_lines).
    Renderowany jako "dymki" (osobny blok na turę, kolor per rozmówca) pod podsumowaniem —
    zero kosztu (te dane i tak już mamy w pamięci po zakończeniu rozmowy, żadnego dodatkowego
    wywołania). Zastępuje nagranie audio dla klientów którzy chcą zweryfikować co dokładnie
    padło w rozmowie, bez kwestii RODO związanych z nagraniem głosu (to sam tekst, ten sam co
    i tak widać w zakładce "Logi" w panelu). ŚWIADOMIE bez <details>/zwijania — sprawdzone na
    żywo (Gmail web) że klienci poczty ignorują ten tag i renderują zawartość zawsze rozwiniętą,
    więc lepiej postawić na czytelne, zawsze widoczne dymki niż pozorne zwijanie."""
    resend_api_key = os.getenv("RESEND_API_KEY")
    if not resend_api_key:
        logger.warning("📋 [REALTIME TEST] RESEND_API_KEY nieskonfigurowany — nie wysyłam raportu")
        return False

    import html as _html
    from datetime import datetime as _datetime
    from zoneinfo import ZoneInfo as _ZoneInfo

    call_time_str = _datetime.now(_ZoneInfo("Europe/Warsaw")).strftime("%d.%m.%Y, %H:%M")

    transcript_block = ""
    if transcript_lines:
        turns_html = ""
        for line in transcript_lines:
            is_client = line.startswith("Klient:")
            speaker = "Klient" if is_client else "Asystent"
            text = _html.escape(line.split(":", 1)[1].strip() if ":" in line else line)
            bg, border, label_color, indent = (
                ("#e8f4fd", "#2196F3", "#1565c0", "24px") if is_client
                else ("#f2f2f2", "#9e9e9e", "#616161", "0")
            )
            turns_html += f"""
            <div style="background: {bg}; border-left: 3px solid {border}; border-radius: 6px; padding: 8px 12px; margin: 6px 0; margin-left: {indent}; font-size: 13px; line-height: 1.5;">
                <strong style="color: {label_color}; font-size: 11px; text-transform: uppercase;">{speaker}</strong><br>{text}
            </div>
            """
        transcript_block = f"""
        <div style="margin: 20px 0;">
            <p style="font-weight: bold; color: #333; margin-bottom: 8px;">📝 Pełny zapis rozmowy</p>
            {turns_html}
        </div>
        """

    business_name = tenant.get("name", "Firma")
    lead_block = ""
    subject = f"📞 Raport z rozmowy — {business_name}"
    if pending_message:
        cn = pending_message.get("customer_name") or "Nieznany"
        msg = pending_message.get("message") or ""
        lead_block = f"""
        <p><strong>📨 Zgłoszenie dla właściciela:</strong></p>
        <p style="background: #fff8e1; padding: 15px; border-radius: 5px; border-left: 4px solid #ffc107;">
            <strong>{cn}</strong><br>{msg}
        </p>
        """
        subject = f"📨 Zgłoszenie + raport z rozmowy — {business_name}"
    html_content = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px;">
        <h2 style="color: #333;">📞 Raport z rozmowy</h2>
        <p style="color: #666; margin-top: -10px;">{business_name}</p>
        {lead_block}
        <p><strong>📋 Podsumowanie:</strong></p>
        <p style="background: #e8f4fd; padding: 15px; border-radius: 5px; border-left: 4px solid #2196F3; white-space: pre-line;">{summary}</p>
        <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
            <tr><td style="padding: 8px; border-bottom: 1px solid #eee; width: 120px;"><strong>Telefon:</strong></td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;"><a href="tel:{caller_phone}">{caller_phone}</a></td></tr>
            <tr><td style="padding: 8px; border-bottom: 1px solid #eee; width: 120px;"><strong>Data i godzina:</strong></td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;">{call_time_str}</td></tr>
        </table>
        {transcript_block}
        <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
        <p style="color: #999; font-size: 12px;">Automatyczny raport rozmowy — asystent głosowy (test Realtime) • {business_name}</p>
    </div>
    """
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {resend_api_key}", "Content-Type": "application/json"},
                json={
                    "from": "Voice AI <noreply@bizvoice.pl>",
                    "to": [to_email],
                    "subject": subject,
                    "html": html_content,
                },
                timeout=10.0,
            )
            if response.status_code == 200:
                logger.info("📋 [REALTIME TEST] Raport z rozmowy wysłany")
                return True
            logger.error(f"📋 [REALTIME TEST] Resend error: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logger.error(f"📋 [REALTIME TEST] Send summary email error: {e}")
        return False


async def maybe_send_call_summary(
    tenant: dict, caller_phone: str, context: LLMContext, call_state: dict | None = None,
    call_sid: str | None = None,
) -> None:
    """Woła się w finally: bloku websocket handlera — PO KAŻDEJ rozmowie, niezależnie jak się
    skończyła (cisza, limit czasu, contact_owner, end_conversation, zwykłe rozłączenie).
    Streszczenie liczy się ZAWSZE (patrz 2026-09-23 niżej) — mail/webhook CRM zostają
    bramkowane jak wcześniej ustawieniami panelu.

    call_state: gdy handle_contact_owner (tej samej rozmowy) odłożył wiadomość dla właściciela
    (patrz komentarz 2026-09-09 tam) bo raport i "email do powiadomień" wskazują na TEN SAM
    adres — doklejamy ją tu do JEDNEGO maila zamiast wysyłać osobno. Gdy call_state=None albo
    bez odłożonej wiadomości — zachowanie identyczne jak wcześniej.

    call_sid: 2026-09-23 — gdy podane, streszczenie + priorytet zapisują się też do wiersza
    call_logs (portal /crm dla klienta, patrz helpers.py::persist_call_summary) — WYMAGA żeby
    save_call_transcript() (tworzy wiersz call_logs) wykonało się PRZED tym wywołaniem, inaczej
    UPDATE trafia w pustkę (patrz kolejność w bot_gemini_test.py/bot_openai_realtime.py)."""
    lead_email_enabled = int(tenant.get("lead_email_enabled") or 0)
    to_email = tenant.get("lead_email") or tenant.get("notification_email") or tenant.get("email")
    pending = (call_state or {}).get("pending_contact_owner")
    crm_enabled = _is_crm_test_tenant(tenant)
    # 2026-09-23 — USUNIĘTE: wczesny return gdy ani lead_email_enabled ani crm_enabled (Pipedrive
    # test tenant) nie są włączone. Streszczenie zasila teraz TEŻ portal /crm (call_logs.summary/
    # priority) niezależnie od tych dwóch przełączników, więc musi liczyć się zawsze — koszt
    # jednego dodatkowego wywołania GPT-4.1-mini na rozmowę, akceptowalny (call_logs to dziś
    # główne źródło danych dla klienckiego CRM, nie tylko dodatek do maila).
    summary = await generate_conversation_summary(context, tenant)
    if summary == "Brak treści rozmowy.":
        # 2026-09-09 — domyślnie nadal pomijamy (większość firm nie chce maila za KAŻDE
        # rozłączenie bez słowa). Nowy przełącznik per-firma (QFX Group, na żądanie
        # klienta: "chciałbym otrzymywać informację o KAŻDYM połączeniu przychodzącym,
        # również wtedy, gdy rozmówca niczego nie pozostawi") — gdy włączony, wysyłamy
        # krótki raport zamiast całkiem pomijać. caller_phone bywa pusty/"nieznany" dla
        # połączeń z zastrzeżonym numerem — pokazujemy to jawnie, nie fałszywy numer.
        # Wyjątek: jest odłożona wiadomość z contact_owner — to NIE jest pusta rozmowa,
        # transkrypt po prostu nie zawierał wystarczająco treści dla GPT, ale realny lead
        # istnieje i musi trafić do właściciela.
        if not pending and not int(tenant.get("report_empty_calls") or 0):
            return
        if not pending:
            caller_display = caller_phone if caller_phone and caller_phone.lower() not in ("nieznany", "unknown", "") else "numer zastrzeżony"
            summary = f"Połączenie odebrane od: {caller_display}. Rozmowa się nie odbyła — rozmówca nic nie powiedział lub rozłączył się bez zostawienia wiadomości."
        else:
            summary = "Streszczenie rozmowy niedostępne — szczegóły w zgłoszeniu powyżej."
    if call_sid:
        await persist_call_summary(tenant, call_sid, summary)
    if lead_email_enabled and to_email:
        transcript_lines = extract_conversation_lines(context) if int(tenant.get("transcript_email_enabled") or 0) else None
        await send_call_summary_email(
            tenant, caller_phone, summary, to_email, pending_message=pending, transcript_lines=transcript_lines
        )
    if crm_enabled:
        await maybe_send_to_crm(tenant, caller_phone, summary)


# ==========================================
# TRANSKRYPT + NALICZANIE MINUT (reszta Fazy 5)
# ==========================================
#
# 1:1 z cascade (bot.py::save_call_log + bot.py::apply_call_charge) — te same tabele
# (call_logs, call_transcripts), te same kolumny, ta sama logika naliczania. Dzięki temu
# zakładka "Logi połączeń" w panelu pokazuje rozmowy Realtime BEZ ŻADNYCH zmian w UI —
# panel nie wie i nie musi wiedzieć że to inny silnik pod spodem.
#
# Dwie fazy zapisu, tak jak w cascade:
#   1. save_call_transcript() — wołane w finally: websocket handlera (ma dostęp do
#      LLMContext z pełną rozmową). Tworzy wiersz call_logs (duration=0, status='in_progress')
#      + wszystkie wiersze call_transcripts.
#   2. apply_call_charge() — wołane z /vonage/events gdy Vonage potwierdzi realny czas
#      trwania połączenia (UPDATE tego samego wiersza call_logs + odjęcie kredytów/minut).
#   Rozdzielone bo to dwa różne, niezależne od siebie w czasie zdarzenia (koniec pipeline'u
#   vs. webhook od Vonage) — dokładnie tak jak w cascade, nie uproszczenie.

_call_logs_columns_ensured = False


async def _ensure_call_logs_columns() -> None:
    """Jednorazowo (per proces/cold start) dokłada kolumny summary/priority/seen do call_logs
    w SaaS DB — te same rozmowy co dziś idą do maila teraz zasilają też portal /crm (bizvoice-panel).
    Wzorzec identyczny jak ensureColumns() w bizvoice-panel/api/firms/[id]/route.ts (ALTER w
    try/except, bezpieczne do powtarzania — TursoDB.execute i tak łyka błąd i loguje go, nie
    podnosi wyjątku, ale global flag oszczędza redundantne wywołania po pierwszym udanym/nieudanym
    razie w życiu procesu)."""
    global _call_logs_columns_ensured
    if _call_logs_columns_ensured:
        return
    _call_logs_columns_ensured = True
    for sql in (
        "ALTER TABLE call_logs ADD COLUMN summary TEXT",
        "ALTER TABLE call_logs ADD COLUMN priority TEXT",
        "ALTER TABLE call_logs ADD COLUMN seen INTEGER DEFAULT 0",
    ):
        await saas_db.execute(sql)


async def persist_call_summary(tenant: dict, call_sid: str, summary: str) -> None:
    """Zapisuje streszczenie+priorytet do wiersza call_logs (musi już istnieć — patrz
    save_call_transcript/save_elevenlabs_transcript, wołane WCZEŚNIEJ w tym samym finally: bloku).
    Tylko SaaS (portal /crm dotyczy firm_ tenantów) — dla starych tenantów admina to no-op.
    Best-effort: błąd nie może wywrócić wysyłki maila/webhooka, które dzieją się zaraz po tym.
    Priorytet parsowany tu (nie przez wywołujących) żeby WSZYSTKIE 3 silniki (Gemini Live,
    OpenAI Realtime, ElevenLabs) zapisywały identycznie, jednym wspólnym kodem.

    2026-09-24 — tu też, przy okazji KAŻDEJ rozmowy (nie tylko gdy admin akurat otworzy
    zakładkę Statystyki/Logi danej firmy — poprzedni, niepewny wyzwalacz w
    bizvoice-panel/api/firms/[id]/stats/route.ts), czyścimy stare call_transcripts (surowy,
    słowo-w-słowo zapis — bardziej wrażliwe dane niż samo streszczenie, więc krótsza retencja
    ma sens). call_logs (lekki rekord: telefon/streszczenie/priorytet — realny rekord CRM,
    portal /crm i "stały klient" na nim polegają) NIE jest tu kasowany — dłuższa retencja
    ustawiona osobno w panelu (365 dni zamiast 30, patrz stats/route.ts)."""
    tenant_id = tenant.get("id", "")
    if not tenant_id.startswith("firm_") or not call_sid:
        return
    try:
        priority = _parse_summary_fields(summary).get("Priorytet") or ""
        await _ensure_call_logs_columns()
        await saas_db.execute(
            "UPDATE call_logs SET summary = ?, priority = ? WHERE call_sid = ?",
            [summary, priority, call_sid],
        )
        await saas_db.execute(
            "DELETE FROM call_transcripts WHERE tenant_id = ? AND created_at < datetime('now', '-30 days')",
            [tenant_id],
        )
    except Exception as e:
        logger.error(f"[CRM] persist_call_summary error: {e}")


async def save_call_transcript(tenant: dict, call_sid: str, caller_phone: str, context: LLMContext) -> None:
    """Zapisuje wiersz call_logs (in_progress) + transkrypt do call_transcripts.
    1:1 z bot.py::save_call_log, tylko czyta LLMContext zamiast flow_manager.get_current_context()."""
    if not call_sid:
        logger.warning("📊 [REALTIME TEST] Brak call_sid — pomijam zapis transkryptu/logu")
        return
    tenant_id = tenant.get("id", "")
    if not tenant_id:
        return

    is_saas = tenant_id.startswith("firm_")
    target_db = saas_db if is_saas else db

    try:
        existing = await target_db.execute("SELECT id FROM call_logs WHERE call_sid = ?", [call_sid])
        if not existing:
            await target_db.execute(
                """INSERT INTO call_logs
                   (id, tenant_id, call_sid, caller_phone, duration_seconds, status, created_at)
                   VALUES (?, ?, ?, ?, 0, 'in_progress', datetime('now'))""",
                [f"call_{int(time.time())}", tenant_id, call_sid, caller_phone],
            )
            logger.info(f"📊 [REALTIME TEST] Call log created: {call_sid} ({'saas' if is_saas else 'admin'})")
    except Exception as e:
        logger.error(f"[REALTIME TEST] Call log create error: {e}")
        return

    try:
        messages = context.get_messages() if context else []
        saved_contents = set()
        saved_count = 0
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")
            if role not in ("user", "assistant") or not content:
                continue
            if isinstance(content, list):
                content = " ".join(
                    b.get("text", "") for b in content if isinstance(b, dict) and b.get("text")
                )
            content = (content or "").strip()
            if len(content) < 2:
                continue
            content_key = f"{role}:{content[:100]}"
            if content_key in saved_contents:
                continue
            saved_contents.add(content_key)

            transcript_id = f"tr_{uuid.uuid4().hex[:12]}"
            await target_db.execute(
                """INSERT INTO call_transcripts
                   (id, tenant_id, call_sid, role, content, created_at)
                   VALUES (?, ?, ?, ?, ?, datetime('now'))""",
                [transcript_id, tenant_id, call_sid, role, content[:500]],
            )
            saved_count += 1
        logger.info(f"📝 [REALTIME TEST] Transcript saved: {saved_count} messages")
    except Exception as e:
        logger.error(f"[REALTIME TEST] Transcript save error: {e}")


async def apply_call_charge(tenant_id: str, is_saas_tenant: bool, call_sid: str, call_status: str, duration: int) -> None:
    """Nalicza minuty/kredyty za zakończoną rozmowę. 1:1 port bot.py::apply_call_charge
    (sama logika finansowa, bez zmian) — wołane z /vonage/events poniżej."""
    duration_minutes = duration / 60.0

    if call_status != "completed" or duration <= 0:
        return

    if is_saas_tenant:
        saas_row = await saas_db.execute("SELECT user_id FROM firms WHERE id = ?", [tenant_id])
        saas_user_id = saas_row[0]["user_id"] if saas_row else None
        if not saas_user_id:
            logger.warning(f"⚠️ [REALTIME TEST] No user_id for SaaS firm {tenant_id} — can't charge")
            return

        cost = round(duration_minutes * PRICE_PER_MINUTE, 4)

        await saas_db.execute(
            "UPDATE firms SET minutes_used = minutes_used + ? WHERE id = ?",
            [duration_minutes, tenant_id],
        )
        await saas_db.execute(
            """UPDATE credits
               SET balance = balance - ?,
                   total_spent = total_spent + ?
               WHERE user_id = ?""",
            [cost, cost, saas_user_id],
        )
        logger.info(f"📊 [REALTIME TEST] SaaS: -{cost:.4f} zł ({duration_minutes:.2f} min) for user {saas_user_id}")

        credits = await saas_db.execute("SELECT balance FROM credits WHERE user_id = ?", [saas_user_id])
        if credits:
            balance = float(credits[0].get("balance") or 0)
            if balance < PRICE_PER_MINUTE:
                await saas_db.execute("UPDATE firms SET is_blocked = 1 WHERE id = ?", [tenant_id])
                logger.warning(f"⚠️ [REALTIME TEST] SaaS firm {tenant_id} BLOCKED — balance too low: {balance:.2f} zł")

        firm_data = await saas_db.execute(
            "SELECT minutes_used, minutes_limit FROM firms WHERE id = ?", [tenant_id]
        )
        if firm_data:
            used = float(firm_data[0].get("minutes_used") or 0)
            limit = int(firm_data[0].get("minutes_limit") or 0)
            if limit > 0 and used >= limit * 0.99:
                await saas_db.execute("UPDATE firms SET is_blocked = 1 WHERE id = ?", [tenant_id])
                logger.warning(f"⚠️ [REALTIME TEST] SaaS firm {tenant_id} BLOCKED — minutes limit reached: {used:.1f}/{limit} min")

        await saas_db.execute(
            """INSERT INTO transactions
               (id, user_id, type, amount, description, created_at)
               VALUES (?, ?, 'usage', ?, ?, datetime('now'))""",
            [
                f"tx_{call_sid[:12]}",
                saas_user_id,
                -cost,
                f"Rozmowa {duration}s ({duration_minutes:.2f} min) [Realtime test]",
            ],
        )
    else:
        await db.execute(
            "UPDATE tenants SET minutes_used = minutes_used + ? WHERE id = ?",
            [duration_minutes, tenant_id],
        )
        logger.info(f"📊 [REALTIME TEST] Admin: +{duration_minutes:.2f} min for {tenant_id}")

        tenant_data = await db.execute(
            "SELECT minutes_used, minutes_limit FROM tenants WHERE id = ?", [tenant_id]
        )
        if tenant_data:
            used = float(tenant_data[0].get("minutes_used", 0))
            limit = int(tenant_data[0].get("minutes_limit", 100))
            if used >= limit * 0.99:
                await db.execute("UPDATE tenants SET is_blocked = 1 WHERE id = ?", [tenant_id])
                logger.warning(f"⚠️ [REALTIME TEST] Admin tenant {tenant_id} BLOCKED - limit reached")


# ==========================================
# ŻYWE PRZEKIEROWANIE (transfer) — Vonage REST API, tylko Vonage
# ==========================================
#
# Twilio ma OSOBNY, już istniejący mechanizm (transfer_requests table + TwiML <Dial>
# w /twilio/after-stream, patrz flows_contact.py) — ten kod go NIE zastępuje ani nie
# dotyka, jest wyłącznie dla Vonage, który (w odróżnieniu od Twilio) nie ma
# dwuetapowego triku dostępnego w tym serwisie (brak /twilio/after-stream w
# bot_gemini_test.py — patrz CLAUDE.md, ta granica była tam już wcześniej opisana).
#
# Mechanizm: Vonage Voice REST API, PUT /v1/calls/{call_uuid} z action="transfer" +
# nowe NCCO — to PODMIENIA bieżący "leg" połączenia (który dotąd był podłączony do
# NASZEGO websocketu) na nowy (talk + connect do telefonu właściciela). Efekt:
# Vonage sam zamyka nasz websocket gdy nowe NCCO przejmuje kontrolę — NIE wysyłamy
# własnego EndFrame po udanym transferze (w odróżnieniu od contact_owner/end_conversation),
# żeby nie ścigać się z tym zamknięciem od strony Vonage. call_state["ended"]=True
# nadal ustawiane, żeby monitor_gemini_call_health przestał liczyć ciszę.
#
# Autoryzacja: JWT RS256 podpisany kluczem prywatnym Aplikacji Vonage (VONAGE_APPLICATION_ID
# + VONAGE_PRIVATE_KEY) — to INNE poświadczenie niż to co obsługuje Answer/Event URL
# (te w ogóle nie wymagają autoryzacji, są tylko webhookami). Ten mechanizm jest
# NIEPRZETESTOWANY na żywym połączeniu w momencie napisania — pierwszy telefon z
# transfer_enabled=1 to pierwszy prawdziwy test.

VONAGE_API_BASE = "https://api.nexmo.com"

# Ile dzwoni telefon właściciela zanim uznamy że nikt nie odbiera. Domyślny timeout
# Vonage dla akcji "connect" to 60s (sprawdzone w ncco-reference) — zbyt długo, klient
# wisiałby w martwej ciszy prawie minutę. 15s (~4 sygnały) to dolna typowa wartość używana
# w call center/IVR zanim leci fallback na pocztę głosową/wiadomość.
TRANSFER_RING_TIMEOUT = 15


async def send_missed_transfer_email(business_name: str, caller_phone: str, to_email: str) -> bool:
    """Email gdy próba żywego przekierowania (transfer_to_owner) skończyła się timeout/busy/
    rejected/failed/unanswered — patrz /vonage/transfer-fallback w bot_gemini_test.py, które
    woła tę funkcję. Klient w tym samym momencie słyszy zapowiedź (NCCO zwrócone z tego
    webhooka) że wiadomość zostanie przekazana — to jest ta wiadomość, więc właściciel i tak
    się dowiaduje mimo nieodebrania. Przyjmuje same stringi (nie tenant dict) — webhook nie ma
    dostępu do obiektu tenanta, tylko do tego co sami wpisaliśmy w query string eventUrl."""
    resend_api_key = os.getenv("RESEND_API_KEY")
    if not resend_api_key:
        logger.warning("📧 [TRANSFER] RESEND_API_KEY nieskonfigurowany — nie wysyłam")
        return False
    html_content = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px;">
        <h2 style="color: #333;">📞 Nieodebrane przekierowanie połączenia</h2>
        <p style="color: #666; margin-top: -10px;">{business_name}</p>
        <p>Klient chciał porozmawiać na żywo, ale połączenie nie zostało odebrane w ciągu
        {TRANSFER_RING_TIMEOUT}s. Oddzwoń, gdy będziesz mógł:</p>
        <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
            <tr><td style="padding: 8px; border-bottom: 1px solid #eee; width: 120px;"><strong>Telefon:</strong></td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;"><a href="tel:{caller_phone}">{caller_phone}</a></td></tr>
        </table>
        <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
        <p style="color: #999; font-size: 12px;">Automatyczne powiadomienie — asystent głosowy (test Realtime) • {business_name}</p>
    </div>
    """
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {resend_api_key}", "Content-Type": "application/json"},
                json={
                    "from": "Voice AI <noreply@bizvoice.pl>",
                    "to": [to_email],
                    "subject": f"📞 Nieodebrane przekierowanie — {business_name}",
                    "html": html_content,
                },
                timeout=10.0,
            )
            if response.status_code == 200:
                logger.info("📧 [TRANSFER] Email o nieodebranym przekierowaniu wysłany")
                return True
            logger.error(f"📧 [TRANSFER] Resend error: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logger.error(f"📧 [TRANSFER] Send email error: {e}")
        return False


def _generate_vonage_jwt() -> str | None:
    """JWT krótkożyjący (60s) do jednego wywołania REST API — Vonage wymaga nowego
    tokenu per-request (albo bardzo krótkiego TTL), nie długożyjącego API key jak
    część innych dostawców."""
    app_id = os.getenv("VONAGE_APPLICATION_ID")
    private_key = os.getenv("VONAGE_PRIVATE_KEY")
    if not app_id or not private_key:
        logger.warning("📞 [TRANSFER] Brak VONAGE_APPLICATION_ID/VONAGE_PRIVATE_KEY — transfer niedostępny")
        return None
    # Railway env vars są jednolinijkowe — klucz PEM wklejony z dosłownymi "\n"
    # zamiast prawdziwych złamań linii trzeba odtworzyć, inaczej podpis RS256 nie zweryfikuje się.
    private_key = private_key.replace("\\n", "\n")
    import jwt as pyjwt
    now = int(time.time())
    payload = {
        "iat": now,
        "exp": now + 60,
        "jti": str(uuid.uuid4()),
        "application_id": app_id,
    }
    return pyjwt.encode(payload, private_key, algorithm="RS256")


def _format_transfer_number(raw: str) -> str:
    """Normalizacja numeru pod Vonage connect/phone endpoint.

    Bug znaleziony na żywym telefonie: pierwsza wersja kopiowała 1:1 normalizację z
    flows_contact.py (cascade, Twilio transfer), która KOŃCZY numer znakiem "+"
    (+48XXXXXXXXX) — bo tego wymaga Twilio. Vonage wymaga czegoś innego: sprawdzone
    wprost w dokumentacji NCCO (developer.vonage.com/en/voice/voice-api/ncco-reference,
    akcja connect/phone) — przykład tam to "447700900001", BEZ znaku "+". Ten sam "+"
    który u Twilio jest obowiązkowy, u Vonage powodował 400 Bad Request."""
    number = (raw or "").replace(" ", "").replace("-", "").replace("(", "").replace(")", "").lstrip("+")
    if number.startswith("0048"):
        number = number[4:]
    elif number.startswith("48") and len(number) == 11:
        number = number[2:]
    return f"48{number}"


async def transfer_vonage_call(
    call_uuid: str, destination_number: str, from_number: str, announce_text: str,
    api_base: str | None = None, fallback_url: str | None = None,
) -> bool:
    """PUT /v1/calls/{uuid} — podmienia NCCO trwającego połączenia. `announce_text`
    leci jako "talk" ZANIM Vonage podłączy telefon właściciela (natywny mechanizm
    Vonage, nie nasz pipeline — unika wyścigu z Gemini Live próbującym powiedzieć
    to samo przez nasze audio, które i tak zaraz zostanie odcięte).

    from_number: bug znaleziony na żywym telefonie (400 Bad Request) — akcja "connect"
    formalnie ma pole "from" jako opcjonalne w schemacie, ale sprawdzone wprost w
    dokumentacji Vonage (ncco-reference#connect): "This must be one of your Vonage
    virtual numbers if you're connecting to a real phone, as the call won't connect
    otherwise" — w praktyce wymagane. To musi być NASZ numer Vonage (tenant['phone_number']),
    nie numer docelowy (właściciela).

    api_base: DRUGI bug za 400 (po naprawie "from") — potwierdzone przez Vonage API
    Support: każde połączenie jest przypisane do konkretnego REGIONALNEGO centrum
    danych (np. api-eu-3.vonage.com), przekazywanego jako "region_url" TYLKO w evencie
    Answer. "Jeśli dostajesz 400/404 przy modyfikacji aktywnego połączenia... Twoje
    połączenie prawdopodobnie siedzi w innym Data Center" — request wysłany na sztywne
    api.nexmo.com trafia w złe centrum dla połączeń spoza jego regionu. Fallback na
    VONAGE_API_BASE gdy region_url nie zostało przechwycone (np. jakiś stary/inny call)."""
    token = _generate_vonage_jwt()
    if not token:
        return False
    if not call_uuid:
        logger.warning("📞 [TRANSFER] Brak call_uuid — nie mogę przekierować")
        return False

    base = (api_base or VONAGE_API_BASE).rstrip("/")

    connect_action = {
        "action": "connect",
        "from": from_number,
        "timeout": TRANSFER_RING_TIMEOUT,
        "endpoint": [{"type": "phone", "number": destination_number}],
    }
    if fallback_url:
        # eventType=synchronous — sprawdzone wprost w dokumentacji Vonage (ncco-reference#connect):
        # gdy połączenie z endpointem skończy się timeout/busy/rejected/failed/unanswered, Vonage
        # odpytuje eventUrl i oczekuje NOWEJ NCCO w odpowiedzi, która ZASTĘPUJE bieżącą. Bez tego
        # (bug zaobserwowany na żywym telefonie): po ring_timeout cała rozmowa po prostu się urywa,
        # klient słyszy ciszę i rozłączenie zamiast jakiegokolwiek wyjaśnienia.
        connect_action["eventType"] = "synchronous"
        connect_action["eventUrl"] = [fallback_url]  # MUSI być lista, nawet z jednym URL-em (schemat Vonage)
    ncco = [
        {"action": "talk", "text": announce_text, "language": "pl-PL"},
        connect_action,
    ]
    body = {"action": "transfer", "destination": {"type": "ncco", "ncco": ncco}}
    logger.info(f"📞 [TRANSFER] Wysyłam do Vonage ({base}): uuid={call_uuid} body={body}")
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            response = await client.put(
                f"{base}/v1/calls/{call_uuid}",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=body,
                timeout=10.0,
            )
            if response.status_code in (200, 204):
                logger.info(f"📞 [TRANSFER] Vonage transfer OK: {call_uuid} → {destination_number}")
                return True
            logger.error(f"📞 [TRANSFER] Vonage API error: {response.status_code} — {response.text}")
            return False
    except Exception as e:
        logger.error(f"📞 [TRANSFER] Vonage API exception: {e}")
        return False


def build_transfer_tool(
    tenant: dict, call_sid: str, call_state: dict, region_url: str | None = None,
    caller_phone: str = "", host: str | None = None,
) -> FunctionSchema:
    """FunctionSchema dla żywego przekierowania — WARUNKOWO dołączane z bot_gemini_test.py
    tylko gdy tenant.get("transfer_enabled")==1 I połączenie idzie przez Vonage (call_sid
    to wtedy prawdziwy Vonage call uuid, przechwycony w /vonage/answer-gemini-live, patrz
    tam). Ten sam checkbox/pole co dla Twilio (transfer_number) — bez nowej konfiguracji
    w panelu.

    region_url: regionalny host centrum danych Vonage dla TEGO konkretnego połączenia
    (patrz docstring transfer_vonage_call — bez tego 400 Bad Request dla połączeń poza
    domyślnym regionem).

    host: nasz WŁASNY publiczny host (Railway), potrzebny żeby zbudować pełny URL do
    /vonage/transfer-fallback — TO Vonage odpytuje ten adres z zewnątrz gdy właściciel nie
    odbierze, więc musi to być URL dostępny z internetu, nie region_url (który jest hostem
    PO STRONIE VONAGE, kompletnie inna rzecz)."""

    async def handle_transfer(params: FunctionCallParams):
        if call_state.get("suppress_idle_reset"):
            # Okno wymuszonej wypowiedzi (gemini_say_now — dopytanie o ciszę/limit czasu/pożegnanie,
            # patrz bot_gemini_test.py). W odróżnieniu od OpenAI Realtime (say_now ma tool_choice="none"),
            # Gemini Live NIE MA odpowiednika — potwierdzone czytaniem źródła pipecat 1.4.0
            # (_create_single_response wysyła przez send_client_content bez żadnej opcji per-turn
            # wyłączającej narzędzia). contact_owner/submit_lead łapią to przez _is_scripted_bot_phrase
            # (treść wiadomości), ale transfer_to_owner nie przyjmuje żadnych argumentów — nie ma
            # czego sprawdzić, więc bez tej flagi nic by nie złapało przypadkowego wywołania transferu
            # w trakcie np. "Nie słyszę odpowiedzi. Dziękuję za kontakt, do widzenia!".
            logger.warning("📞 [TRANSFER] Wywołanie w trakcie wymuszonej wypowiedzi systemowej — odrzucam jako prawdopodobnie przypadkowe")
            await params.result_callback({"status": "error", "reason": "suppressed"})
            return
        raw_number = (tenant.get("transfer_number") or "").strip()
        if not raw_number:
            logger.warning(f"📞 [TRANSFER] tenant {tenant.get('id')} ma transfer_enabled ale brak transfer_number")
            await params.result_callback({"status": "error", "reason": "no_transfer_number"})
            return

        destination = _format_transfer_number(raw_number)
        if len(destination) < 11:
            logger.error(f"📞 [TRANSFER] Nieprawidłowy numer po normalizacji: {destination!r}")
            await params.result_callback({"status": "error", "reason": "invalid_number"})
            return

        fallback_url = None
        if host:
            owner_email = tenant.get("notification_email") or tenant.get("email") or ""
            fallback_url = (
                f"https://{host}/vonage/transfer-fallback"
                f"?businessName={quote(tenant.get('name') or 'Firma', safe='')}"
                f"&callerPhone={quote(caller_phone or '', safe='')}"
                f"&ownerEmail={quote(owner_email, safe='')}"
            )

        from_number = _format_transfer_number(tenant.get("phone_number") or "")
        ok = await transfer_vonage_call(
            call_sid, destination, from_number,
            announce_text="Już łączę z osobą odpowiedzialną, chwileczkę.",
            api_base=region_url,
            fallback_url=fallback_url,
        )
        await params.result_callback({"status": "ok" if ok else "error"})
        if ok:
            call_state["ended"] = True  # zatrzymaj monitor ciszy — patrz docstring sekcji wyżej

    return FunctionSchema(
        name="transfer_to_owner",
        description="""Klient WYRAŹNIE żąda połączenia NA ŻYWO z człowiekiem/właścicielem
(nie samej wiadomości) — np. "połącz mnie z kimś", "chcę porozmawiać z człowiekiem TERAZ",
"przełącz mnie". Użyj TYLKO gdy klient chce żywej rozmowy w TEJ chwili — jeśli chce
zostawić wiadomość/kontakt zwrotny, użyj zamiast tego contact_owner.
Wywołaj OD RAZU, NIC nie mówiąc przed nią (żadnej zapowiedzi typu "już przełączam") —
zapowiedź o przekierowaniu leci automatycznie z systemu telefonii, nie od Ciebie.
Jeśli wynik to status="error" — przekierowanie się nie udało (brak numeru/błąd), krótko
przeproś i zaproponuj zamiast tego zostawienie wiadomości przez contact_owner.""",
        properties={},
        required=[],
        handler=handle_transfer,
    )


async def is_call_allowed(tenant: dict) -> bool:
    """Pre-call guard, 1:1 z bot.py (sprawdzane PRZED startem pipeline'u, w /twilio/incoming-gemini-test
    i /vonage/answer poniżej). Bez tego zablokowany/bez-środków tenant i tak dostawałby pełne, płatne
    połączenie z OpenAI Realtime — apply_call_charge() ustawia is_blocked DOPIERO PO zakończonej rozmowie,
    więc to jedyne miejsce które faktycznie zapobiega rozpoczęciu kosztownej sesji.

    Dla SaaS get_tenant_by_phone() i tak już filtruje is_blocked=0 w SQL (patrz helpers.py), więc ten
    check tu to głównie: (1) obrona przed niespójnością (is_blocked jeszcze nie ustawione, a saldo już
    zeszło poniżej progu), (2) jedyny check dla tenantów admina, gdzie SQL filtruje tylko is_active."""
    if tenant.get("is_blocked"):
        logger.warning(f"🚫 [REALTIME TEST] Tenant {tenant.get('id')} BLOCKED — odrzucam połączenie")
        return False
    if tenant.get("source") == "saas":
        user_id = tenant.get("user_id", "")
        rows = await saas_db.execute("SELECT balance FROM credits WHERE user_id = ?", [user_id])
        balance = float(rows[0].get("balance") or 0) if rows else 0
        if balance < PRICE_PER_MINUTE:
            logger.warning(f"🚫 [REALTIME TEST] SaaS {tenant.get('id')} — brak kredytów: {balance:.2f} zł")
            return False
    return True
