"""
tts_factory.py
==============
Factory: inicjalizacja serwisu TTS na podstawie konfiguracji tenanta.

Obsługiwane providery:
  - elevenlabs (domyślny)
  - cartesia
  - openai
  - azure
  - google  (Gemini 2.5 Flash TTS)
"""

import os
import json
import re

from loguru import logger
from pipecat.transcriptions.language import Language
from constants import TTSProvider
from pipecat.services.azure.tts import AzureTTSService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.openai.tts import OpenAITTSService


DEFAULT_ELEVENLABS_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"

# ---------------------------------------------------------------------------
# Pomocnicze: konwersja liczb na tekst polski (dla TTS)
# ---------------------------------------------------------------------------

def _number_to_polish(n: int) -> str:
    if n == 0:
        return "zero"
    ones = ["", "jeden", "dwa", "trzy", "cztery", "pięć",
            "sześć", "siedem", "osiem", "dziewięć"]
    teens = ["dziesięć", "jedenaście", "dwanaście", "trzynaście",
             "czternaście", "piętnaście", "szesnaście", "siedemnaście",
             "osiemnaście", "dziewiętnaście"]
    tens = ["", "dziesięć", "dwadzieścia", "trzydzieści",
            "czterdzieści", "pięćdziesiąt", "sześćdziesiąt",
            "siedemdziesiąt", "osiemdziesiąt", "dziewięćdziesiąt"]
    hundreds = ["", "sto", "dwieście", "trzysta", "czterysta",
                "pięćset", "sześćset", "siedemset", "osiemset", "dziewięćset"]
    parts = []
    if n >= 1000:
        t = n // 1000
        if t == 1:
            parts.append("tysiąc")
        elif t in [2, 3, 4]:
            parts.append(ones[t] + " tysiące")
        else:
            parts.append(ones[t] + " tysięcy")
        n %= 1000
    if n >= 100:
        parts.append(hundreds[n // 100])
        n %= 100
    if n >= 20:
        parts.append(tens[n // 10])
        n %= 10
        if n > 0:
            parts.append(ones[n])
    elif n >= 10:
        parts.append(teens[n - 10])
    elif n > 0:
        parts.append(ones[n])
    return " ".join(parts)


def _zloty_form(n: int) -> str:
    if n == 1:
        return "złoty"
    last_digit = n % 10
    last_two = n % 100
    if last_digit == 1 and last_two != 11:
        return "złoty"
    if last_digit in [2, 3, 4] and last_two not in [12, 13, 14]:
        return "złote"
    return "złotych"


def _replace_number(match) -> str:
    num = int(match.group(1))
    if num > 9999:
        return match.group(0)
    return _number_to_polish(num) + " " + _zloty_form(num)


async def _expand_abbreviations(text: str, aggregation_type=None) -> str:
    """Rozwijanie skrótów przed syntezą mowy: zł → złotych, ul. → ulicy, itd."""
    text = re.sub(r'^otych\b\s*', '', text)
    text = text.replace('złotychotych', 'złotych')
    text = text.replace('złotyotych', 'złoty')
    text = text.replace('złoteotych', 'złote')
    text = re.sub(r'(\d+)\s*złotych\b', _replace_number, text)
    text = re.sub(r'(\d+)\s*zł\b', _replace_number, text)
    text = re.sub(r'\bul\.', 'ulicy', text)
    text = re.sub(r'\bnr\b', 'numer', text)
    text = re.sub(r'\btel\.', 'telefon', text)
    text = re.sub(r'\bgodz\.', 'godzina', text)
    return text


# ---------------------------------------------------------------------------
# Główna fabryka
# ---------------------------------------------------------------------------

def create_tts_service(tenant: dict):
    """
    Zwraca zainicjalizowany serwis TTS dla danego tenanta.

    Wybór providera na podstawie pola tenant['tts_provider'].
    Domyślnie: ElevenLabs.
    """
    tts_provider = tenant.get('tts_provider', 'elevenlabs')

    if tts_provider == TTSProvider.CARTESIA:
        cartesia_voice = tenant.get('azure_voice_id') or '575a5d29-1fdc-4d4e-9afa-5a9a71759864'
        logger.info(f"🎙️ Using Cartesia TTS | voice: {cartesia_voice}")
        tts = CartesiaTTSService(
            api_key=os.getenv("CARTESIA_API_KEY"),
            voice_id=cartesia_voice,
            model_id="sonic-2",
            language="pl",
            sample_rate=8000,
            speed=1.0,
            pitch=0.0,
        )
        tts.add_text_transformer(_expand_abbreviations)
        return tts

    if tts_provider == TTSProvider.OPENAI:
        logger.info("🎙️ Using OpenAI TTS | voice: alloy")
        tts = OpenAITTSService(
            api_key=os.getenv("OPENAI_API_KEY"),
            model="tts-1",
            voice="alloy",
            sample_rate=24000,
        )
        tts.add_text_transformer(_expand_abbreviations)
        return tts

    if tts_provider == TTSProvider.AZURE:
        azure_voice = tenant.get('azure_voice_id') or 'pl-PL-AgnieszkaNeural'
        logger.info(f"🎙️ Using Azure TTS | voice: {azure_voice}")
        tts = AzureTTSService(
            api_key=os.getenv("AZURE_SPEECH_KEY"),
            region=os.getenv("AZURE_SPEECH_REGION", "westeurope"),
            voice=azure_voice,
            sample_rate=8000,
            params=AzureTTSService.InputParams(
                language=Language.PL,
                rate="1.04",
            ),
        )
        tts.add_text_transformer(_expand_abbreviations)
        return tts

    if tts_provider == TTSProvider.GOOGLE:
        from pipecat.services.google.tts import GeminiTTSService
        raw_voice = tenant.get('azure_voice_id') or 'pl-PL-Chirp3-HD-Aoede'
        # strip old Chirp3-HD prefix if still stored in DB
        google_voice = raw_voice.split('Chirp3-HD-')[-1] if 'Chirp3-HD-' in raw_voice else raw_voice
        logger.info(f"🎙️ Using Gemini Flash TTS | voice: {google_voice}")
        tts = GeminiTTSService(
            credentials=os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON"),
            model="gemini-2.5-flash-tts",
            voice_id=google_voice,
            params=GeminiTTSService.InputParams(
                language=Language.PL_PL,
                prompt="Mów naturalnie i uprzejmie po polsku.",
            ),
        )
        tts.add_text_transformer(_expand_abbreviations)
        return tts

    # ElevenLabs (domyślny)
    voice_id = tenant.get('elevenlabs_voice_id') or DEFAULT_ELEVENLABS_VOICE_ID
    speaking_rate = float(tenant.get('speaking_rate') or 1.1)
    logger.info(f"🎙️ Using ElevenLabs TTS (quality mode) | voice: {voice_id} | speed: {speaking_rate}")
    tts = ElevenLabsTTSService(
        api_key=os.getenv("ELEVENLABS_API_KEY"),
        voice_id=voice_id,
        model="eleven_turbo_v2_5",
        output_format="pcm_16000",
        stability=0.6,
        similarity_boost=0.75,
        speed=speaking_rate,
    )
    tts.add_text_transformer(_expand_abbreviations)
    return tts
