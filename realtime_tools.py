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
import time
import uuid
import asyncio

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
    zostało odrzucone tylko dlatego że zaczynało się od "Klient chce")."""
    m = message.lower().strip()
    if len(m) < 10:
        return True
    return any(m.startswith(p) for p in _VAGUE_MESSAGE_STARTS)


def _looks_too_short(text: str) -> bool:
    """Łagodniejszy filtr dla submit_lead::problem — tylko długość, bez czarnej listy
    fraz (te są legalne we frazowaniu opisu sprawy w trzeciej osobie)."""
    return len((text or "").strip()) < 10


def build_contact_owner_tool(tenant: dict, caller_phone: str, task_box: dict, call_state: dict) -> FunctionSchema:
    """FunctionSchema z handlerem przypiętym bezpośrednio — LLMContext rejestruje go
    automatycznie (patrz bot_gemini_test.py::build_realtime_llm), bez osobnego
    register_function.

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
        if _looks_like_vague_meta_message(message):
            # Model czasem zamiast prawdziwej treści wpisuje własny, pokrętny opis sytuacji
            # (np. "Proszę o kontakt z Pawłem, bo ktoś nie skontaktował się" — bez sensu jako
            # wiadomość). 1:1 zabezpieczenie z cascade (flows_contact.py::handle_set_contact_message)
            # — odrzuć i każ dopytać, zamiast wysyłać śmieciowego emaila.
            logger.warning(f"📞 [REALTIME TEST] contact_owner: mętna wiadomość, odrzucam: {message[:60]!r}")
            await params.result_callback({"status": "error", "reason": "message_too_vague"})
            return

        sent = await send_message_email(tenant, customer_name, message, caller_phone, owner_email)
        await params.result_callback({"status": "ok" if sent else "error"})

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

    return FunctionSchema(
        name="contact_owner",
        description="""Klient chce kontaktu z właścicielem/firmą — zostawić wiadomość. Użyj gdy:
- "chcę porozmawiać z właścicielem", "proszę o kontakt", "czy mogę zostawić wiadomość"
- "połącz mnie", "przekieruj mnie", "chcę rozmawiać z człowiekiem"
- klient jest sfrustrowany i potrzebuje pomocy człowieka
- nie możesz pomóc i klient potrzebuje właściciela
Wywołaj DOPIERO gdy masz OBA pola (imię i treść wiadomości). Jeśli czegoś brakuje, zapytaj
klienta JEDNO krótkie pytanie wprost (np. "Czego dokładnie dotyczy sprawa?" / "Na jakie imię
mam zapisać?") — NIE zapowiadaj że o to zapytasz, po prostu zapytaj.
Jeśli wynik wywołania to status="error", reason="message_too_vague" — wiadomość była za ogólna
(np. samo "chce kontaktu" bez konkretu). Krótko przeproś i zadaj TO SAMO pytanie ponownie,
bez tłumaczenia dlaczego pytasz drugi raz.""",
        properties={
            "customer_name": {"type": "string", "description": "Imię klienta"},
            "message": {
                "type": "string",
                "description": (
                    "DOKŁADNA treść tego czego klient chce/potrzebuje, jego słowami lub krótkim "
                    "rzeczowym streszczeniem KONKRETU sprawy (np. 'Chce przełożyć wizytę z piątku "
                    "na sobotę', 'Pyta o możliwość zniżki grupowej dla 5 osób'). "
                    "⛔ NIE pisz o kliencie w trzeciej osobie i NIE pisz meta-opisu sytuacji "
                    "(np. NIE 'Klient prosi o kontakt', NIE 'Proszę o kontakt z klientem') — "
                    "to nie jest wiadomość, to opis że wiadomość istnieje. Napisz SAMĄ TREŚĆ sprawy."
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

async def generate_conversation_summary(context: LLMContext) -> str:
    """Streszcza rozmowę przez szybkie wywołanie GPT. Ten sam pomysł co
    flows.py::generate_conversation_summary, tylko czyta uniwersalny LLMContext
    (context.get_messages(), format OpenAI: {"role": ..., "content": ...}) zamiast
    flow_manager.get_current_context() z pipecat_flows."""
    try:
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

        if not conversation:
            return "Brak treści rozmowy."

        conversation_text = "\n".join(conversation[-20:])

        import openai
        client = openai.AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        response = await client.chat.completions.create(
            model="gpt-4.1-mini",  # ten sam model co flows.py::send_message_email w cascade
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Streść poniższą rozmowę telefoniczną w 2-3 zdaniach po polsku. "
                        "Napisz: czego klient szukał/pytał, czy zostawił dane kontaktowe "
                        "lub opisał konkretną sprawę, i jaki był wynik rozmowy. Pisz zwięźle."
                    ),
                },
                {"role": "user", "content": conversation_text},
            ],
            max_tokens=150,
            temperature=0.3,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"📋 [REALTIME TEST] Summary generation error: {e}")
        return "Nie udało się wygenerować streszczenia."


async def send_call_summary_email(tenant: dict, caller_phone: str, summary: str, to_email: str) -> bool:
    """Email z raportem PO KAŻDEJ rozmowie. Uproszczona kopia flows.py::send_lead_email
    (bez rozróżniania urgency itp. — to dopiero przyszły submit_lead, osobny krok)."""
    resend_api_key = os.getenv("RESEND_API_KEY")
    if not resend_api_key:
        logger.warning("📋 [REALTIME TEST] RESEND_API_KEY nieskonfigurowany — nie wysyłam raportu")
        return False

    business_name = tenant.get("name", "Firma")
    html_content = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px;">
        <h2 style="color: #333;">📞 Raport z rozmowy</h2>
        <p style="color: #666; margin-top: -10px;">{business_name}</p>
        <p><strong>📋 Podsumowanie:</strong></p>
        <p style="background: #e8f4fd; padding: 15px; border-radius: 5px; border-left: 4px solid #2196F3; font-style: italic;">{summary}</p>
        <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
            <tr><td style="padding: 8px; border-bottom: 1px solid #eee; width: 120px;"><strong>Telefon:</strong></td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;"><a href="tel:{caller_phone}">{caller_phone}</a></td></tr>
        </table>
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
                    "subject": f"📞 Raport z rozmowy — {business_name}",
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


async def maybe_send_call_summary(tenant: dict, caller_phone: str, context: LLMContext) -> None:
    """Woła się w finally: bloku websocket handlera — PO KAŻDEJ rozmowie, niezależnie jak się
    skończyła (cisza, limit czasu, contact_owner, end_conversation, zwykłe rozłączenie).
    Bramkowane tym samym polem co cascade (patrz docstring pliku) — jeśli tenant nie ma
    włączonego raportu w panelu, nic się nie wysyła."""
    lead_email_enabled = int(tenant.get("lead_email_enabled") or 0)
    to_email = tenant.get("lead_email") or tenant.get("notification_email") or tenant.get("email")
    if not lead_email_enabled or not to_email:
        return
    summary = await generate_conversation_summary(context)
    if summary == "Brak treści rozmowy.":
        # Rozmowa się nie odbyła / rozłączono natychmiast — nie ma czego raportować
        return
    await send_call_summary_email(tenant, caller_phone, summary, to_email)


# ==========================================
# SUBMIT_LEAD — "zbieranie zgłoszeń" (reszta Fazy 4)
# ==========================================
#
# 1:1 z cascade pod względem WYZWALACZA i DANYCH (patrz flows_contact.py::submit_lead_function/
# start_lead_collection_function) — klient OPISUJE sprawę/problem, model sam to rozpoznaje,
# BEZ proszenia wprost o kontakt (to jest domena contact_owner). Różni się mechaniką: cascade
# robił to dwuetapowo (start_lead_collection przełączał FlowManager na dedykowany node z TYLKO
# submit_lead+end_conversation dostępnymi — twarda bariera przed pomyleniem narzędzi). Realtime
# nie ma przełącznika node'ów — wszystkie tools są zawsze dostępne naraz — więc zamiast tego:
# JEDNO narzędzie (jak contact_owner, już sprawdzony wzorzec), z ostrym opisem różnicującym
# je od contact_owner/start_booking/FAQ, plus ta sama ochrona przed mętnym/pustym opisem.
#
# NIE rozłącza po zgłoszeniu (w przeciwieństwie do contact_owner) — klient może pytać dalej,
# tak jak w cascade (submit_lead wraca do zwykłej rozmowy, nie do pożegnania).
#
# Włączane WARUNKOWO — tylko gdy tenant.get("lead_mode")==1 (ten sam checkbox "Zbieranie
# zgłoszeń" w panelu), dokładnie jak w cascade.

_URGENCY_LABELS = {"high": "🔴 PILNE", "normal": "Standardowe"}


async def send_lead_email(
    tenant: dict, caller_phone: str, customer_name: str, problem: str, details: str, urgency: str,
    to_email: str, conversation_summary: str = "",
) -> bool:
    """Strukturalny email ze zgłoszeniem. Uniwersalny szablon (nie branżowy — pasuje pod
    kancelarię tak samo jak pod hydraulika), 1:1 z flows_contact.py::_send_lead_report_email
    pod względem treści/pól."""
    resend_api_key = os.getenv("RESEND_API_KEY")
    if not resend_api_key:
        logger.warning("📋 [REALTIME TEST] RESEND_API_KEY nieskonfigurowany — nie wysyłam zgłoszenia")
        return False

    business_name = tenant.get("name", "Firma")
    urgency_label = _URGENCY_LABELS.get(urgency, urgency)
    name_row = f"""
            <tr><td style="padding: 8px; border-bottom: 1px solid #eee; width: 120px;"><strong>Od:</strong></td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;">{customer_name}</td></tr>
    """ if customer_name else ""
    details_html = f"""
        <p><strong>Szczegóły:</strong></p>
        <p style="background: #f5f5f5; padding: 15px; border-radius: 5px;">{details}</p>
    """ if details else ""
    summary_html = f"""
        <p><strong>Kontekst rozmowy:</strong></p>
        <p style="background: #e8f4fd; padding: 15px; border-radius: 5px; border-left: 4px solid #2196F3; font-style: italic;">{conversation_summary}</p>
    """ if conversation_summary else ""

    html_content = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px;">
        <h2 style="color: #333;">📨 Nowe zgłoszenie — {urgency_label}</h2>
        <p style="color: #666; margin-top: -10px;">{business_name}</p>
        <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
            {name_row}
            <tr><td style="padding: 8px; border-bottom: 1px solid #eee; width: 120px;"><strong>Telefon:</strong></td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;"><a href="tel:{caller_phone}">{caller_phone}</a></td></tr>
        </table>
        <p><strong>Opis sprawy:</strong></p>
        <p style="background: #fff3e0; padding: 15px; border-radius: 5px;">{problem}</p>
        {details_html}
        {summary_html}
        <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
        <p style="color: #999; font-size: 12px;">Zgłoszenie przekazane przez asystenta głosowego (test Realtime) • {business_name}</p>
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
                    "subject": f"Nowe zgłoszenie ({urgency_label}) — {business_name}",
                    "html": html_content,
                },
                timeout=10.0,
            )
            if response.status_code == 200:
                logger.info("📋 [REALTIME TEST] Email ze zgłoszeniem wysłany")
                return True
            logger.error(f"📋 [REALTIME TEST] Resend error: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logger.error(f"📋 [REALTIME TEST] Send lead email error: {e}")
        return False


def build_submit_lead_tool(tenant: dict, caller_phone: str, context_box: dict) -> FunctionSchema:
    """FunctionSchema dla zbierania zgłoszeń. Wywoływane WARUNKOWO z bot_gemini_test.py —
    tylko gdy tenant.get("lead_mode")==1 — więc description niżej może bez obaw zakładać
    że lead_mode jest włączony (funkcja w ogóle nie istnieje w tools gdy jest wyłączony).

    context_box: {"context": None}, ten sam trik co task_box w build_contact_owner_tool —
    LLMContext powstaje DOPIERO wewnątrz build_realtime_llm(), już PO zbudowaniu listy tools
    (bo LLMContext(tools=...) potrzebuje ich na wejściu), więc w momencie budowy tego tool'a
    kontekst jeszcze nie istnieje. Handler czyta context_box["context"] dopiero przy
    faktycznym wywołaniu, gdy jest już ustawiony."""
    lead_triggers = (tenant.get("lead_triggers") or "").strip()
    triggers_text = lead_triggers or "klient opisuje problem, dolegliwość, usterkę, awarię, reklamację lub pyta o wycenę niestandardowej sprawy"

    lead_collection = (tenant.get("lead_collection") or "").strip()
    collection_text = lead_collection or "opis sprawy i ewentualne szczegóły (od kiedy, okoliczności)"

    lead_urgency_mode = tenant.get("lead_urgency_mode", 0) == 1
    urgency_rule = ""
    if lead_urgency_mode:
        urgency_kw = (tenant.get("lead_urgency_text") or "").strip() or "awaria, nie działa, pilne, jak najszybciej"
        urgency_rule = f'\nPilność HIGH gdy klient mówi: {urgency_kw}. W innym wypadku NORMAL.'

    async def handle_submit_lead(params: FunctionCallParams):
        customer_name = (params.arguments.get("customer_name") or "").strip()
        problem = (params.arguments.get("problem") or "").strip()
        details = (params.arguments.get("details") or "").strip()
        urgency = params.arguments.get("urgency") or "normal"
        logger.info(f"🛠️ [REALTIME TEST] submit_lead: urgency={urgency} — {problem[:60]!r}")

        if _looks_too_short(problem):
            logger.warning(f"🛠️ [REALTIME TEST] submit_lead: za mało konkretu, odrzucam: {problem[:60]!r}")
            await params.result_callback({"status": "error", "reason": "problem_too_vague"})
            return

        owner_email = tenant.get("notification_email") or tenant.get("email")
        if not owner_email:
            logger.warning("🛠️ [REALTIME TEST] submit_lead: brak notification_email na tenancie")
            await params.result_callback({"status": "error", "reason": "no_owner_email"})
            return

        # Streszczenie rozmowy W TLE — nie blokuj potwierdzenia dla klienta na tym
        async def send_with_summary():
            ctx = context_box.get("context")
            summary = await generate_conversation_summary(ctx) if ctx else ""
            if summary == "Brak treści rozmowy.":
                summary = ""
            await send_lead_email(tenant, caller_phone, customer_name, problem, details, urgency, owner_email, summary)

        asyncio.create_task(send_with_summary())

        # NIE rozłącza (w przeciwieństwie do contact_owner) — klient może pytać dalej, 1:1 z cascade.
        await params.result_callback({"status": "ok", "urgency": urgency})

    return FunctionSchema(
        name="submit_lead",
        description=f"""Klient OPISUJE sprawę/problem wymagający kontaktu ze specjalistą — BEZ
proszenia wprost o rozmowę z kimś (to byłoby contact_owner). Użyj gdy: {triggers_text}.{urgency_rule}
1. Dopytaj naturalnie o brakujące szczegóły (JEDNO pytanie na turę): {collection_text}
2. Gdy masz przynajmniej konkretny opis sprawy (nie musisz mieć wszystkiego) → wywołaj submit_lead
3. Po wywołaniu krótko potwierdź że zgłoszenie trafiło do specjalisty i zapytaj czy możesz
   jeszcze w czymś pomóc — rozmowa NIE kończy się automatycznie po tym wywołaniu
⛔ NIE używaj gdy klient chce standardowej rezerwacji z cennika
⛔ NIE używaj gdy klient WPROST prosi o rozmowę z człowiekiem/właścicielem → wtedy contact_owner
Jeśli wynik to status="error", reason="problem_too_vague" — opis był za krótki/pusty, dopytaj
klienta WPROST o konkret sprawy i wywołaj ponownie.""",
        properties={
            "customer_name": {"type": "string", "description": "Imię klienta, jeśli je podał (opcjonalne)"},
            "problem": {"type": "string", "description": "Konkretny opis sprawy/problemu klienta (1-2 zdania) — naturalne sformułowanie w trzeciej osobie, np. 'Klient chce X' jest OK"},
            "details": {"type": "string", "description": "Dodatkowe szczegóły zebrane od klienta (opcjonalne)"},
            "urgency": {"type": "string", "enum": ["high", "normal"], "description": "high = pilne/awaria, normal = standardowe"},
        },
        required=["problem"],
        handler=handle_submit_lead,
    )


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
