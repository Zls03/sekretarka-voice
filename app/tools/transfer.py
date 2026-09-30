"""Narzędzie transfer_to_owner — żywe przekierowanie rozmowy na numer właściciela (tylko Vonage)."""

from urllib.parse import quote

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.services.llm_service import FunctionCallParams

from app.telephony.vonage_api import transfer_vonage_call


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
