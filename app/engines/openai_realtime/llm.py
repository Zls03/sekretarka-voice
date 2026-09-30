"""Konfiguracja usługi OpenAI Realtime i dosyłanie promptu po spóźnionym odczycie CRM."""

import asyncio

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

from app.config import settings
from app.engines.common import CallFeatures, build_call_prompt

OPENAI_REALTIME_MODEL = settings.openai_realtime_model
# Głosy OpenAI są wielojęzyczne — mówią w języku instrukcji; firma może wybrać własny.
OPENAI_REALTIME_VOICE = settings.openai_realtime_voice


def build_realtime_llm(
    system_prompt: str,
    tools: list | None = None,
    voice: str | None = None,
    speed: float | None = None,
):
    """Usługa OpenAI Realtime + para agregatorów kontekstu.

    Narzędzia rejestrują się same z kontekstu (handler jest w FunctionSchema).
    Głos i tempo są ustawieniami firmy; brak wartości = domyślny głos i tempo API.
    Kontekst jest zwracany osobno — po rozmowie czytają go transkrypt i podsumowanie.
    """
    resolved_voice = voice or OPENAI_REALTIME_VOICE
    logger.info(
        f"🧠 OpenAI Realtime, model={OPENAI_REALTIME_MODEL}, voice={resolved_voice}, speed={speed or 'domyślne API'}"
    )
    llm = OpenAIRealtimeLLMService(
        api_key=settings.openai_api_key,
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
                ),
            ),
        ),
    )

    context = LLMContext(tools=tools or [])
    # realtime_service_mode=True: usługa realtime emituje inaczej UserStarted/StoppedSpeakingFrame,
    # więc zapisy do kontekstu muszą iść w trybie "trailing" zamiast czekać na te ramki.
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context, realtime_service_mode=True)
    # context zwracany też osobno — potrzebny na końcu rozmowy do raportu
    # (post_call/report.py::maybe_send_call_summary czyta context.get_messages()).
    return llm, user_aggregator, assistant_aggregator, context


async def apply_crm_when_ready(
    llm: OpenAIRealtimeLLMService,
    tenant: dict,
    client_profile_task: asyncio.Task,
    caller_phone: str,
    features: CallFeatures,
) -> dict | None:
    """Dosyła prompt z profilem klienta (historia wizyt), gdy odpowiedź panelu dotrze.

    Powitanie leci od razu z ogólnym promptem — nie czekamy 2-3 s na panel. Aktualizacja
    idzie bez bloku powitania (inaczej model przywitałby się drugi raz) i z tymi samymi
    `features` co na starcie, żeby prompt dalej pasował do zarejestrowanych narzędzi.
    """
    client_profile = await client_profile_task
    if client_profile:
        logger.info(
            f"👤 [REALTIME TEST] CRM (spóźniony): {client_profile.get('name')} (wizyty: {client_profile.get('visit_count', 0)})"
        )
        updated_prompt = await build_call_prompt(
            tenant,
            caller_phone,
            features,
            client_profile=client_profile,
            include_greeting=False,
        )
        await llm.send_client_event(SessionUpdateEvent(session=SessionProperties(instructions=updated_prompt)))
    return client_profile
