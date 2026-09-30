"""Konfiguracja usługi Gemini Live (model, głos, język, thinking)."""

from google.genai.types import ThinkingConfig
from loguru import logger
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
from pipecat.transcriptions.language import Language

from app.config import settings

# Jedyny model Live API z obsługą języka polskiego (gemini-2.5-flash-native-audio
# odrzuca "pl"). Epizody wyższego opóźnienia są po stronie Google — sprawdzonym
# zamiennikiem jest silnik OpenAI Realtime (realtime_engine="openai").
GEMINI_LIVE_MODEL = "gemini-3.1-flash-live-preview"

# Najwyżej oceniony damski głos PL; firma może wybrać inny (pole gemini_voice).
DEFAULT_GEMINI_VOICE = "Kore"


def build_gemini_live_llm(system_prompt: str, tools: list | None = None, voice: str | None = None):
    """Usługa Gemini Live + para agregatorów kontekstu.

    - tools: te same FunctionSchema co dla OpenAI Realtime (pipecat konwertuje je sam).
    - voice: nazwy głosów Gemini i OpenAI się nie pokrywają, stąd osobne pole firmy.
    - language=PL: bez tego pierwsze tury klienta transkrybowały się jako inne języki.
      Uwaga: pipecat 1.4 nie przekazuje podpowiedzi języka do transkrypcji wejścia,
      stąd sporadyczny "bełkot" w transkrypcie klienta — świadomie nie łatamy biblioteki.
    - thinking="minimal": domyślnie Gemini 3 myśli na poziomie "high", co wydłużało
      odpowiedzi o 1-2 s. Jeśli model gorzej trzyma się reguł promptu, podnieść do "low".
    - Brak regulacji tempa mowy — Gemini Live API tego nie udostępnia.
    - Zawsze natywny głos Gemini: hybryda z głosem ElevenLabs była o ~1.7 s wolniejsza.

    Kontekst jest zwracany osobno — po rozmowie czytają go transkrypt i podsumowanie.
    """
    resolved_voice = voice or DEFAULT_GEMINI_VOICE
    logger.info(
        f"🧠 Gemini Live, model={GEMINI_LIVE_MODEL}, voice={resolved_voice}, tools={[t.name for t in (tools or [])]}"
    )
    llm = GeminiLiveLLMService(
        api_key=settings.google_api_key,
        settings=GeminiLiveLLMService.Settings(
            model=GEMINI_LIVE_MODEL,
            voice=resolved_voice,
            language=Language.PL,
            thinking=ThinkingConfig(thinking_level="minimal"),
        ),
        system_instruction=system_prompt,
    )
    context = LLMContext(tools=tools or [])
    # Usługa realtime nie emituje UserStarted/StoppedSpeakingFrame — kontekst zapisujemy
    # w trybie "trailing".
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context, realtime_service_mode=True)
    return llm, user_aggregator, assistant_aggregator, context
