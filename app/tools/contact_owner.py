"""Narzędzie contact_owner — przekazanie wiadomości od klienta właścicielowi firmy."""

import asyncio

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.frames.frames import EndFrame
from pipecat.services.llm_service import FunctionCallParams

from app.background import spawn
from app.crm_contacts import maybe_save_contact_name
from app.notifications.email import send_message_email
from app.tools.guards import is_scripted_bot_phrase, looks_like_vague_meta_message


def build_contact_owner_tool(
    tenant: dict, caller_phone: str, task_box: dict, call_state: dict, has_transfer_tool: bool = False
) -> FunctionSchema:
    """FunctionSchema z handlerem przypiętym bezpośrednio — LLMContext rejestruje go
    automatycznie (patrz engines/openai_realtime/llm.py::build_realtime_llm), bez osobnego
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

    call_state: to samo co w engines/openai_realtime/monitors.py::make_call_state() — TU ustawiamy
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
        if is_scripted_bot_phrase(message):
            logger.warning(
                f"📞 [REALTIME TEST] contact_owner: treść to zaszyta wypowiedź bota (nie klienta), odrzucam: {message[:60]!r}"
            )
            await params.result_callback({"status": "error", "reason": "message_too_vague"})
            return
        if looks_like_vague_meta_message(message):
            # Model czasem zamiast prawdziwej treści wpisuje własny, pokrętny opis sytuacji
            # (np. "Proszę o kontakt z Pawłem, bo ktoś nie skontaktował się" — bez sensu jako
            # wiadomość). 1:1 zabezpieczenie z cascade (cascade::handle_set_contact_message)
            # — odrzuć i każ dopytać, zamiast wysyłać śmieciowego emaila.
            logger.warning(f"📞 [REALTIME TEST] contact_owner: mętna wiadomość, odrzucam: {message[:60]!r}")
            await params.result_callback({"status": "error", "reason": "message_too_vague"})
            return

        # Auto-zapis imienia do portalu /crm (zakładka Klienci) — TYLKO gdy tam jeszcze nic
        # nie ma (patrz maybe_save_contact_name). customer_name tu to coś co klient SAM
        # wprost podał (model musiał o to zapytać, żeby w ogóle wypełnić ten parametr) —
        # nie zgadywanie z transkryptu, więc bezpieczne do auto-zapisu. Poboczny, nieblokujący
        # zapis — błąd nie może wywrócić wysyłki wiadomości do właściciela.
        if customer_name != "Nieznany":
            spawn(maybe_save_contact_name(tenant.get("id", ""), caller_phone, customer_name))

        # 2026-09-09 — jeśli ta sama firma MA TEŻ włączony raport z rozmowy (lead_email_enabled)
        # na TEN SAM adres — nie wysyłaj osobnego maila teraz. Zamiast tego odłóż treść do
        # call_state, a maybe_send_call_summary() (koniec rozmowy) doklei ją do JEDNEGO,
        # połączonego maila zamiast wysyłać dwa niemal jednoczesne maile z tego samego numeru.
        # Gdy adresy się różnią (rzadkie, ale panel na to pozwala) albo raport jest wyłączony —
        # wysyłamy jak dotychczas natychmiast, żeby nie zgubić gwarancji dostarczenia.
        report_to_email = tenant.get("lead_email") or tenant.get("notification_email") or tenant.get("email")
        defer_to_report = bool(
            int(tenant.get("lead_email_enabled") or 0) and report_to_email and report_to_email == owner_email
        )
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

            # Rozłączenie po potwierdzeniu: stałe opóźnienie zamiast czekania na koniec audio
            # (usługi realtime nie sygnalizują go w porę).
            async def auto_hangup():
                await asyncio.sleep(6.0)  # dłużej niż w say_now — tu bot jeszcze SAM formułuje potwierdzenie
                try:
                    t = task_box.get("task")
                    if t:
                        await t.queue_frame(EndFrame())
                        logger.info("🔚 [REALTIME TEST] EndFrame po contact_owner")
                except Exception as e:
                    logger.error(f"[REALTIME TEST] EndFrame po contact_owner error: {e}")

            spawn(auto_hangup())

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
