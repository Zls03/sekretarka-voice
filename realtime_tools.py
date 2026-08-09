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
"""

import os
import asyncio

from loguru import logger

from pipecat.frames.frames import EndFrame
from pipecat.pipeline.task import PipelineTask
from pipecat.services.llm_service import FunctionCallParams
from pipecat.adapters.schemas.function_schema import FunctionSchema


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
    w cascade, tylko z dodatkowymi wzorcami z realnego, obserwowanego przypadku."""
    m = message.lower().strip()
    if len(m) < 10:
        return True
    return any(m.startswith(p) for p in _VAGUE_MESSAGE_STARTS)


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
NAJPIERW powiedz krótkie, naturalne pożegnanie (np. "Dziękuję za telefon, do usłyszenia!",
"Miłego dnia, do widzenia!") — RÓŻNE za każdym razem, NIE to samo co powitanie —
DOPIERO POTEM wywołaj tę funkcję.""",
        properties={},
        required=[],
        handler=handle_end_conversation,
    )
