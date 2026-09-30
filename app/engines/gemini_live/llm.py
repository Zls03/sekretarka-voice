"""Konfiguracja usługi Gemini Live (model, głos, język, thinking)."""

from google.genai.types import ThinkingConfig
from loguru import logger
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
from pipecat.transcriptions.language import Language

from app.config import settings

GEMINI_LIVE_MODEL = "gemini-3.1-flash-live-preview"  # zweryfikowane w docs.ai.google.dev (sierpień 2026)
# Próba użycia gemini-2.5-flash-native-audio-preview-12-2025 (2026-09-03) odrzucona natychmiast
# przez samo Gemini: "1007 None. Unsupported language code 'pl' for model
# models/gemini-2.5-flash-native-audio-preview-12-2025" — ten wariant w ogóle nie obsługuje
# polskiego, więc nie nadaje się jako zamiennik niezależnie od TTFB. Zostajemy przy 3.1 preview
# (jedyny Live API model ze wsparciem 'pl' jaki mamy) i traktujemy jego epizody podwyższonego
# TTFB (3-11s, potwierdzone A/B testem) jako coś po stronie Google — patrz OpenAI Realtime
# jako sprawdzony fallback (realtime_engine='openai' w panelu, zakładka "Realtime GPT").


def build_gemini_live_llm(system_prompt: str, tools: list | None = None, voice: str | None = None):
    """Analogiczne do build_realtime_llm() wyżej, ale dla Gemini Live.

    tools: lista FunctionSchema — DOKŁADNIE ten sam format co dla OpenAI Realtime
    (pipecat.adapters.schemas.function_schema.FunctionSchema jest zamierzenie
    provider-agnostic, GeminiLiveLLMService konwertuje ją pod spodem przez
    GeminiLLMAdapter — sprawdzone w źródle pipecat), więc realtime_tools.py::build_*_tool
    są reużywane WPROST, bez żadnej gemini-specyficznej wersji.

    voice: per-tenant (tenant.get("gemini_voice"), NA RAZIE bez UI w panelu — jak
    "realtime_voice" dla OpenAI zanim dostał zakładkę). Fallback "Kore" — #1 damski
    głos PL wg rankingu użytkownika (11.08.2026). Osobne pole nazwy od "realtime_voice",
    bo zestawy nazw głosów OpenAI i Gemini się NIE pokrywają (np. "cedar" nic nie znaczy
    dla Gemini, "Kore" nic nie znaczy dla OpenAI) — wspólne pole ryzykowałoby wysłaniem
    złej nazwy do złego dostawcy.

    ⚠️ BRAK kontroli tempa/prędkości mówienia — sprawdzone w źródle pipecat:
    GeminiLiveLLMService.Settings nie ma odpowiednika OpenAI Realtime AudioOutput(speed=...).
    To ograniczenie samego Gemini Live API, nie brak wpięcia z naszej strony — nie da się
    obecnie tego podpiąć pod "speaking_rate" tak jak działa to dla OpenAI Realtime/cascade.

    settings=... (zamiast przestarzałych kwargs model=/voice_id=) — świadomie żeby móc
    ustawić language=Language.PL. Bug znaleziony na żywym telefonie: bez tego domyślny
    język transkrypcji to EN_US (patrz InputParams w źródle pipecat), więc pierwsze
    tury rozmowy transkrybowały się jako bełkot w losowych językach (niemiecki,
    hiszpański, portugalski) zanim model jakoś "złapał" polski w dalszej części rozmowy.
    ⚠️ To ustawia język TYLKO dla generowania odpowiedzi (speech_config.language_code) —
    sprawdzone w źródle: input_audio_transcription (rozpoznawanie mowy KLIENTA) jest
    tworzone przez pipecat 1.4.0 BEZ żadnej podpowiedzi językowej (pusty
    AudioTranscriptionConfig()), mimo że google-genai SDK wspiera pole `language_codes`
    właśnie do tego. Pipecat 1.4.0 tego pola jeszcze nie przekazuje — to prawdopodobnie
    prawdziwa przyczyna sporadycznego bełkotu w obcym języku w transkrypcji KLIENTA,
    obserwowanego na żywych telefonach nawet po ustawieniu language=Language.PL. Naprawa
    wymagałaby nadpisania wewnętrznej metody connect() biblioteki (fragile, może się
    zepsuć przy update pipecat) — świadomie NIE zrobione teraz, bo problem wystąpił
    rzadko (0 razy w 2 ostatnich pełnych testach) i ryzyko łatki nie jest tego warte.

    HYBRYDA GŁOSU (dodana i usunięta 2026-09-01): próba podmiany natywnego głosu
    Gemini na ElevenLabs (przez blokowanie własnego audio Gemini filtrem i syntezę
    transkryptu przez zewnętrzny TTS) — po pomiarze na żywych połączeniach usunięta:
    ~4.1s śr. user->bot audio z ElevenLabs vs ~2.4s natywnie (SimpleTextAggregator
    czeka na całe zdanie z Gemini zanim wyśle tekst do TTS), różnica w jakości głosu
    nie rekompensowała tej różnicy w opóźnieniu. Zostajemy przy DOMYŚLNYM
    modalities=AUDIO (nie ustawiane tu jawnie) i natywnym głosie Gemini zawsze.

    thinking=ThinkingConfig(thinking_level="minimal") — POPRAWKA 2026-08-22, analiza
    latencji po testowych rozmowach (TTFB 1.3-2.8s na zwykłych turach, nie ~0.6s jak
    sugerowała wcześniejsza notatka w CLAUDE.md o samym powitaniu). Sprawdzone w źródle
    pipecat 1.4.0 (services/google/gemini_live/llm.py): gdy `thinking` zostaje None (co
    było tu domyślnie), pipecat NIE wysyła w ogóle thinking_config do Gemini — a
    ThinkingConfig w google-genai SDK dokumentuje że dla modeli Gemini 3 (nasz
    gemini-3.1-flash-live-preview się łapie) brak jawnego ustawienia oznacza domyślne
    "high". Dla porównania: zwykły (nie-Live) GoogleLLMService w tej samej wersji pipecat
    MA wbudowaną automatyczną optymalizację ustawiającą minimalny thinking dla modeli
    flash właśnie w scenariuszach realtime — wariant Live tej automatyki nie ma, trzeba
    ręcznie. Kompromis: "minimal" to maksymalna szybkość kosztem najmniejszego namysłu
    modelu — jeśli po wdrożeniu pogorszy się trzymanie sztywnych reguł promptu (np.
    dosłowne say_exactly przy rezerwacji, wybór właściwej funkcji kontaktowej) rozważyć
    podniesienie do "low"."""
    resolved_voice = voice or "Kore"
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
    # realtime_service_mode=True — patrz docstring GeminiLiveLLMService: usługa nie
    # emituje UserStarted/StoppedSpeakingFrame, więc zapisy do kontekstu muszą iść
    # w trybie "trailing" (tak samo jak dla OpenAI Realtime w bot_openai_realtime.py).
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context, realtime_service_mode=True)
    # context zwracany osobno — ten sam powód co w build_realtime_llm (raport rozmowy
    # + transkrypt czytają context.get_messages() po zakończeniu połączenia).
    return llm, user_aggregator, assistant_aggregator, context
