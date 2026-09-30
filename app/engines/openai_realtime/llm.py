"""Konfiguracja usługi OpenAI Realtime i dosyłanie promptu po spóźnionym odczycie CRM."""

import asyncio
import os

from loguru import logger
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.services.openai.realtime.events import (
    AudioConfiguration,
    AudioInput,
    AudioOutput,
    InputAudioTranscription,
    SessionProperties,
    SessionUpdateEvent,
)
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

from app.crm_contacts import get_crm_contact_name
from app.prompt.instructions import append_known_caller_hint, build_realtime_instructions

OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1-mini")


# OpenAI Realtime nie ma osobnych głosów per-język (jak Google pl-PL-...) — to
# uniwersalne persony głosowe, które mówią w języku z tekstu/instrukcji. "marin" to
# obecnie flagowy, najbardziej naturalny głos OpenAI Realtime (stan na moją wiedzę).
OPENAI_REALTIME_VOICE = os.getenv("OPENAI_REALTIME_VOICE", "cedar")


def build_realtime_llm(
    system_prompt: str,
    tools: list | None = None,
    voice: str | None = None,
    speed: float | None = None,
):
    """Buduje OpenAIRealtimeLLMService + parę context aggregatorów.

    tools: lista FunctionSchema (z handler ustawionym na schemacie — LLMService
    rejestruje je automatycznie z LLMContext, bez osobnego register_function).

    voice/speed: per-tenant, NIE globalne — patrz wywołanie w websocket handlerach
    (czytane z tenant.get("realtime_voice")/tenant.get("speaking_rate"), z fallbackiem
    na OPENAI_REALTIME_VOICE/domyślne API gdy tenant jeszcze nic nie ustawił — to
    pozwala docelowo wybierać głos/tempo w panelu per-firma, tak jak już działa dla
    cascade, zamiast na sztywno w kodzie/zmiennej środowiskowej dla całego serwisu)."""
    resolved_voice = voice or OPENAI_REALTIME_VOICE
    logger.info(f"🧠 OpenAI Realtime, model={OPENAI_REALTIME_MODEL}, voice={resolved_voice}, speed={speed or 'domyślne API'}")
    llm = OpenAIRealtimeLLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        settings=OpenAIRealtimeLLMService.Settings(
            model=OPENAI_REALTIME_MODEL,
            system_instruction=system_prompt,
            # Niżej niż domyślne (OpenAI: 0.8) — mniej "kreatywnych" dopowiedzeń/wstawek
            # konwersacyjnych, bardziej dosłowne trzymanie się instrukcji z promptu.
            # 0.6 to udokumentowane minimum dla tego API (niżej API i tak by przycięło).
            temperature=0.6,
            session_properties=SessionProperties(
                # Twardy sufit na długość JEDNEJ odpowiedzi — zabezpieczenie przed rozgadaniem
                # się modelu (obserwowane wcześniej: 12s odpowiedź, patrz bug z idle timerem)
                # i przed kosztem pojedynczej odpowiedzi wymykającej się spod kontroli.
                # ~600 tokenów to z zapasem więcej niż jakakolwiek sensowna odpowiedź głosowa.
                max_output_tokens=600,
                audio=AudioConfiguration(
                    output=AudioOutput(voice=resolved_voice, speed=speed),
                    # Transkrypcja usera domyślnie WYŁĄCZONA w OpenAI — bez tego
                    # UserTranscriptMonitor nigdy nie widzi TranscriptionFrame.
                    # language="pl" wymuszony, bo auto-detekcja na krótkich,
                    # telefonicznych próbkach potrafi rozpoznać zupełnie inny język.
                    input=AudioInput(transcription=InputAudioTranscription(language="pl")),
                )
            ),
        ),
    )

    context = LLMContext(tools=tools or [])
    # realtime_service_mode=True: usługa realtime emituje inaczej UserStarted/StoppedSpeakingFrame,
    # więc zapisy do kontekstu muszą iść w trybie "trailing" zamiast czekać na te ramki.
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context, realtime_service_mode=True
    )
    # context zwracany też osobno — potrzebny na końcu rozmowy do raportu
    # (realtime_tools.py::maybe_send_call_summary czyta context.get_messages()).
    return llm, user_aggregator, assistant_aggregator, context


async def apply_crm_when_ready(
    llm: OpenAIRealtimeLLMService, tenant: dict, client_profile_task: asyncio.Task,
    caller_phone: str = "", has_booking: bool = False, has_contact_owner: bool = True,
) -> dict | None:
    """Powitanie leci OD RAZU z generycznym promptem (bez czekania na CRM, ~2-3s HTTP
    do panelu) — ta funkcja czeka na wynik w tle i, jeśli okaże się że dzwoni znany
    klient, dosyła zaktualizowany prompt (session.update) w trakcie rozmowy, żeby
    dane CRM (historia wizyt) były dostępne gdy klient o nie zapyta. include_greeting=False
    (patrz realtime_prompt.py::build_realtime_instructions) — bez tego model mógłby
    zrozumieć aktualizację jako polecenie przywitania się jeszcze raz.

    has_booking: MUSI być przekazane z tego samego booking_available co przy budowie
    tools/system_prompt na starcie połączenia — bez tego ta aktualizacja w trakcie
    rozmowy nadpisałaby prompt z powrotem na "rezerwacje jeszcze w budowie", mimo że
    book_appointment cały czas jest zarejestrowane (dokładnie ten sam błąd co wcześniej
    znaleziony przy transfer_to_owner/has_transfer, patrz historia tego pliku)."""
    client_profile = await client_profile_task
    if client_profile:
        logger.info(f"👤 [REALTIME TEST] CRM (spóźniony): {client_profile.get('name')} (wizyty: {client_profile.get('visit_count', 0)})")
        updated_prompt = build_realtime_instructions(
            tenant, client_profile, include_greeting=False, has_booking=has_booking,
            has_contact_owner=has_contact_owner,
        )
        known_name = await get_crm_contact_name(tenant.get("id", ""), caller_phone)
        if known_name:
            updated_prompt = append_known_caller_hint(updated_prompt, known_name, has_contact_owner=has_contact_owner)
        await llm.send_client_event(SessionUpdateEvent(session=SessionProperties(instructions=updated_prompt)))
    return client_profile
