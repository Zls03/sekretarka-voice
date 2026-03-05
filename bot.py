# bot.py - Pipecat Voice AI dla salonów
"""
PIPECAT FLOWS MIGRATION v1.3
============================
Dodano wybór TTS provider (ElevenLabs / Cartesia) per tenant
"""
import time 
import os
import sys
import json
from pipecat.frames.frames import EndFrame, TTSSpeakFrame
import asyncio
import random
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.frames.frames import TranscriptionFrame
from datetime import datetime
from loguru import logger
from dotenv import load_dotenv
load_dotenv()

from flows import end_conversation_function
from flows_contact import contact_owner_function
# FastAPI
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import Response

# Pipecat core
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask, PipelineParams
from pipecat.services.azure.tts import AzureTTSService
from pipecat.transcriptions.language import Language

# Pipecat transports
from pipecat.transports.websocket.fastapi import FastAPIWebsocketTransport, FastAPIWebsocketParams
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.openai.base_llm import BaseOpenAILLMService

# Pipecat services
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.groq.llm import GroqLLMService
from pipecat.services.cerebras.llm import CerebrasLLMService
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext

# Pipecat Flows
from pipecat_flows import FlowManager

# Idle timeout processor
from pipecat.processors.user_idle_processor import UserIdleProcessor

# Nasze moduły
from helpers import get_tenant_by_phone, db
import uuid
from flows import create_initial_node

# Konfiguracja logowania
logger.remove()
logger.add(sys.stdout, level="DEBUG", format="{time:HH:mm:ss} | {level} | {message}")

app = FastAPI()

@app.on_event("startup")
async def startup_warmup():
    """Pre-warm OpenAI connection przy starcie serwera"""
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        await client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[{"role": "user", "content": "OK"}],
            max_tokens=1,
        )
        logger.info("🔥 Server startup: OpenAI model pre-warmed")
    except Exception as e:
        logger.warning(f"Startup warm failed: {e}")


# ==========================================
# 🔥 WARM-UP FUNCTIONS - rozgrzewanie serwisów
# ==========================================

async def warmup_llm(llm):
    """Rozgrzewa OpenAI - otwiera połączenie, ładuje model."""
    try:
        logger.info("🔥 LLM warm-up start")
        model_name = getattr(llm, 'model', None) or getattr(llm, 'model_name', None) or getattr(llm, '_model', None) or "gpt-4.1-mini"
        await llm._client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": "OK"}],
            max_tokens=1,
        )
        logger.info("🔥 LLM warm-up done")
    except Exception as e:
        logger.warning(f"LLM warm-up failed (non-critical): {e}")


async def warmup_tts(task):
    try:
        logger.info("🔥 TTS warm-up start")
        await task.queue_frame(TTSSpeakFrame(text="."))
        await asyncio.sleep(0.5)
        logger.info("🔥 TTS warm-up done")
    except Exception as e:
        logger.warning(f"TTS warm-up failed (non-critical): {e}")
# ==========================================
# KEYTERMS BUILDER - dynamiczne słowa per firma
# ==========================================

def build_keyterms(tenant: dict) -> list:
    """
    Buduje listę keyterms dla Deepgram na podstawie danych firmy.
    Automatycznie wyciąga słowa z: nazwy, usług, pracowników, FAQ, adresu.
    """
    keyterms = set()
    
    base_terms = [
        "dziewiąta", "dziesiąta", "jedenasta", "dwunasta",
        "trzynasta", "czternasta", "piętnasta", "szesnasta",
        "siedemnasta", "osiemnasta", "dziewiętnasta", "dwudziesta",
        "trzydzieści", "wpół",
        "tak", "nie", "dobrze", "okej", "dziękuję",
        "poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela",
        "wizyta", "termin", "rezerwacja", "umówić", "zapisać", "odwołać",
        "cennik", "cena", "ceny", "ile kosztuje", "koszt",
    ]
    keyterms.update(base_terms)
    
    name = tenant.get("name", "")
    if name:
        keyterms.add(name)
        for word in name.split():
            if len(word) > 2:
                keyterms.add(word)
    
    services = tenant.get("services", []) or tenant.get("info_services", [])
    for svc in services:
        svc_name = svc.get("name", "")
        if svc_name:
            keyterms.add(svc_name.lower())
            for word in svc_name.split():
                if len(word) > 3:
                    keyterms.add(word.lower())
    
    staff = tenant.get("staff", [])
    for s in staff:
        staff_name = s.get("name", "")
        if staff_name:
            keyterms.add(staff_name)
            first_name = staff_name.split()[0] if " " in staff_name else staff_name
            keyterms.add(first_name)
    
    faq = tenant.get("faq", [])
    important_patterns = [
        "multisport", "benefit", "medicover", "luxmed", "karnet", "karta",
        "rejestracja", "online", "strona", "parking", "płatność", "gotówka",
        "blik", "przelew", "faktura", "vat",
    ]
    for f in faq:
        question = f.get("question", "").lower()
        answer = f.get("answer", "").lower()
        full_text = question + " " + answer
        for pattern in important_patterns:
            if pattern in full_text:
                keyterms.add(pattern)
        for word in f.get("answer", "").split():
            if len(word) > 3 and word[0].isupper() and word.isalpha():
                keyterms.add(word)
    
    address = tenant.get("address", "")
    if address:
        for prefix in ["ul.", "ul ", "al.", "al ", "pl.", "pl "]:
            address = address.replace(prefix, " ")
        for word in address.split():
            clean_word = word.strip(",.;:")
            if len(clean_word) > 3 and clean_word.isalpha():
                keyterms.add(clean_word)
    
    custom = tenant.get("stt_keywords", "")
    if custom:
        for word in custom.split(","):
            word = word.strip()
            if word:
                keyterms.add(word)
    
    result = list(keyterms)
    if len(result) > 200:
        logger.warning(f"⚠️ Too many keyterms ({len(result)}), truncating to 200")
        result = result[:200]
    
    logger.info(f"🎤 Built {len(result)} keyterms for {tenant.get('name', 'unknown')}")
    logger.debug(f"🎤 Keyterms sample: {result[:15]}...")
    return result


# ==========================================
# TWILIO INCOMING
# ==========================================

@app.post("/twilio/incoming")
async def twilio_incoming(request: Request):
    """Obsługa połączenia przychodzącego z Twilio"""
    form = await request.form()
    
    called = form.get("Called", form.get("To", ""))
    caller = form.get("From", "")
    call_sid = form.get("CallSid", "")
    
    logger.info(f"📞 Incoming call: {caller} → {called} (CallSid: {call_sid})")
    
    tenant = await get_tenant_by_phone(called)
    
    if not tenant:
        logger.warning(f"❌ No tenant for {called}")
        return Response(
            content='<?xml version="1.0"?><Response><Say language="pl-PL">Przepraszamy, ten numer nie jest aktywny.</Say></Response>',
            media_type="application/xml"
        )
    
    if tenant.get("is_blocked"):
        logger.warning(f"🚫 Tenant {tenant['id']} BLOCKED")
        return Response(
            content='<?xml version="1.0"?><Response><Say language="pl-PL">Przepraszamy, linia jest chwilowo niedostępna.</Say><Hangup/></Response>',
            media_type="application/xml"
        )

    # === RATE LIMIT - max 3 połączenia z jednego numeru na godzinę ===
    if caller and caller != "unknown":
        try:
            recent = await db.execute(
                """SELECT COUNT(*) as cnt FROM call_logs 
                   WHERE caller_phone = ? 
                   AND created_at > datetime('now', '-1 hour')""",
                [caller]
            )
            call_count = recent[0]["cnt"] if recent else 0

            if call_count >= 3:
                logger.warning(f"🚫 Rate limit: {caller} ({call_count} calls/hour)")
                return Response(
                    content='''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say language="pl-PL" voice="Google.pl-PL-Standard-E">
        Przepraszamy, zbyt wiele połączeń z tego numeru. Proszę spróbować za godzinę.
    </Say>
    <Hangup/>
</Response>''',
                    media_type="application/xml"
                )
        except Exception as e:
            logger.warning(f"⚠️ Rate limit check failed (non-critical): {e}")
    logger.info(f"✅ Tenant: {tenant.get('name')}")
    host = request.headers.get("host", "localhost")
    first_message = tenant.get("first_message") or f"Dzień dobry, tu {tenant.get('name')}. W czym mogę pomóc?"
    
    greeting_twiml = ""
    logger.info(f"🔊 Bot will greet via TTS after WebSocket")
    
    twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    {greeting_twiml}
    <Connect action="https://{host}/twilio/after-stream?callSid={call_sid}">
        <Stream url="wss://{host}/ws">
            <Parameter name="callSid" value="{call_sid}" />
            <Parameter name="tenantId" value="{tenant['id']}" />
            <Parameter name="greetingPlayed" value="false" />
            <Parameter name="callerPhone" value="{caller}" />
            <Parameter name="playStartedAt" value="{int(time.time() * 1000)}" />
        </Stream>
    </Connect>
    <Say language="pl-PL" voice="Google.pl-PL-Standard-E">Do widzenia.</Say>
</Response>'''
    
    return Response(content=twiml, media_type="application/xml")


# ==========================================
# GREETING AUDIO
# ==========================================

@app.get("/greeting-audio/{tenant_id}")
async def get_greeting_audio(tenant_id: str):
    """Zwraca pre-generowane MP3 powitania dla Twilio <Play>"""
    import base64
    try:
        rows = await db.execute(
            "SELECT greeting_audio FROM tenants WHERE id = ?", 
            [tenant_id]
        )
        if rows and rows[0].get("greeting_audio"):
            audio_base64 = rows[0]["greeting_audio"]
            audio_bytes = base64.b64decode(audio_base64)
            logger.info(f"🎵 Serving greeting audio for {tenant_id}: {len(audio_bytes)} bytes")
            return Response(
                content=audio_bytes,
                media_type="audio/mpeg",
                headers={
                    "Content-Length": str(len(audio_bytes)),
                    "Cache-Control": "public, max-age=3600"
                }
            )
        else:
            logger.warning(f"⚠️ No greeting audio for {tenant_id}")
            return Response(status_code=404)
    except Exception as e:
        logger.error(f"Greeting audio error: {e}")
        return Response(status_code=500)


# ==========================================
# TRANSCRIPT LOGGING
# ==========================================

async def save_transcript(tenant_id: str, call_sid: str, role: str, content: str):
    try:
        transcript_id = f"tr_{uuid.uuid4().hex[:12]}"
        await db.execute(
            """INSERT INTO call_transcripts 
               (id, tenant_id, call_sid, role, content, created_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'))""",
            [transcript_id, tenant_id, call_sid, role, content]
        )
    except Exception as e:
        logger.error(f"Failed to save transcript: {e}")


# ==========================================
# ERROR LOGGING
# ==========================================

async def log_error(tenant_id: str, call_sid: str, error_type: str, error_message: str, context: str = None):
    try:
        error_id = f"err_{uuid.uuid4().hex[:12]}"
        await db.execute(
            """INSERT INTO error_logs 
               (id, tenant_id, call_sid, error_type, error_message, context, created_at)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
            [error_id, tenant_id, call_sid, error_type, error_message, context]
        )
        logger.info(f"📝 Error logged: {error_type}")
    except Exception as e:
        logger.error(f"Failed to log error: {e}")


# ==========================================
# TTS PROVIDER FACTORY
# ==========================================

DEFAULT_ELEVENLABS_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"


def create_tts_service(tenant: dict):
    tts_provider = tenant.get('tts_provider', 'elevenlabs')

    import re

    def number_to_polish(n: int) -> str:
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

    def zloty_form(n: int) -> str:
        if n == 1:
            return "złoty"
        last_digit = n % 10
        last_two = n % 100
        if last_digit == 1 and last_two != 11:
            return "złoty"
        if last_digit in [2, 3, 4] and last_two not in [12, 13, 14]:
            return "złote"
        return "złotych"

    def replace_number(match):
        num = int(match.group(1))
        if num > 9999:
            return match.group(0)
        return number_to_polish(num) + " " + zloty_form(num)

    async def expand_abbreviations(text: str, aggregation_type=None) -> str:
        text = re.sub(r'^otych\b\s*', '', text)
        text = text.replace('złotychotych', 'złotych')
        text = text.replace('złotyotych', 'złoty')
        text = text.replace('złoteotych', 'złote')
        text = re.sub(r'(\d+)\s*złotych\b', replace_number, text)
        text = re.sub(r'(\d+)\s*zł\b', replace_number, text)
        text = re.sub(r'\bul\.', 'ulicy', text)
        text = re.sub(r'\bnr\b', 'numer', text)
        text = re.sub(r'\btel\.', 'telefon', text)
        text = re.sub(r'\bgodz\.', 'godzina', text)
        return text

    if tts_provider == 'cartesia':
        logger.info(f"🎙️ Using Cartesia TTS | voice: 575a5d29")
        tts = CartesiaTTSService(
            api_key=os.getenv("CARTESIA_API_KEY"),
            voice_id="575a5d29-1fdc-4d4e-9afa-5a9a71759864",
            model_id="sonic-hd",
            language="pl",
            sample_rate=8000,
            speed=1.0,
            pitch=0.0,
        )
        tts.add_text_transformer(expand_abbreviations)
        return tts

    if tts_provider == 'openai':
        logger.info(f"🎙️ Using OpenAI TTS | voice: nova")
        tts = OpenAITTSService(
            api_key=os.getenv("OPENAI_API_KEY"),
            model="tts-1",
            voice="nova",
            sample_rate=24000,
        )
        tts.add_text_transformer(expand_abbreviations)
        return tts

    if tts_provider == 'azure':
        logger.info(f"🎙️ Using Azure TTS | voice: pl-PL-AgnieszkaNeural")
        tts = AzureTTSService(
            api_key=os.getenv("AZURE_SPEECH_KEY"),
            region=os.getenv("AZURE_SPEECH_REGION", "westeurope"),
            voice="pl-PL-AgnieszkaNeural",
            sample_rate=8000,
            params=AzureTTSService.InputParams(
                language=Language.PL,
                rate="1.1",
            ),
        )
        tts.add_text_transformer(expand_abbreviations)
        return tts

    if tts_provider == 'google':
        from pipecat.services.google.tts import GoogleTTSService
        import tempfile
        logger.info(f"🎙️ Using Google Chirp3 HD TTS | voice: pl-PL-Chirp3-HD-Kore")
        creds_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
        creds_dict = json.loads(creds_json)
        creds_file = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump(creds_dict, creds_file)
        creds_file.flush()
        tts = GoogleTTSService(
            credentials_path=creds_file.name,
            voice_id="pl-PL-Chirp3-HD-Kore",
            sample_rate=8000,
            params=GoogleTTSService.InputParams(
                language=Language.PL_PL,
                speaking_rate=1.2,
            ),
        )
        tts.add_text_transformer(expand_abbreviations)
        return tts

    else:
        # ElevenLabs (domyślny)
        voice_id = tenant.get('elevenlabs_voice_id') or DEFAULT_ELEVENLABS_VOICE_ID
        logger.info(f"🎙️ Using ElevenLabs TTS (quality mode) | voice: {voice_id}")
        tts = ElevenLabsTTSService(
            api_key=os.getenv("ELEVENLABS_API_KEY"),
            voice_id=voice_id,
            model="eleven_turbo_v2_5",
            output_format="pcm_16000",
            stability=0.6,
            similarity_boost=0.75,
            speed=1.1,
        )
        tts.add_text_transformer(expand_abbreviations)
        return tts

from pipecat.frames.frames import UserStoppedSpeakingFrame, TranscriptionFrame

class FirstResponseFiller(FrameProcessor):
    FILLERS = ["Chwileczkę.", "Już sprawdzam.", "Już patrzę."]
    _filler_index = 0
    
    def __init__(self, stt_ready_time=0, buffer_window=1.5, **kwargs):
        super().__init__(**kwargs)
        self._first_done = False
        self._stt_ready_time = stt_ready_time
        self._buffer_window = buffer_window
        self._buffered_frames = []
        self._pending_stop_frame = None
        self._flushed = False
        self._has_transcript = False

    async def _flush_buffer(self):
        if self._flushed:
            return
        self._flushed = True
        for bf, bd in self._buffered_frames:
            await self.push_frame(bf, bd)
        self._buffered_frames.clear()
        if self._pending_stop_frame:
            await self.push_frame(self._pending_stop_frame[0], self._pending_stop_frame[1])
            self._pending_stop_frame = None

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        now = time.time()

        if isinstance(frame, UserStoppedSpeakingFrame):
            if now < self._stt_ready_time:
                self._pending_stop_frame = (frame, direction)
                return
            await self._flush_buffer()
            if not self._first_done and self._has_transcript:
                self._first_done = True
                filler = FirstResponseFiller.FILLERS[
                    FirstResponseFiller._filler_index % len(FirstResponseFiller.FILLERS)
                ]
                FirstResponseFiller._filler_index += 1
                logger.info(f"🎯 Filler: '{filler}'")
                await self.push_frame(TTSSpeakFrame(text=filler))

        elif isinstance(frame, TranscriptionFrame):
            if now < self._stt_ready_time:
                time_until_ready = self._stt_ready_time - now
                if time_until_ready > self._buffer_window:
                    logger.info(f"🔇 Dropping echo: '{frame.text}'")
                    return
                else:
                    logger.info(f"🔄 Buffering: '{frame.text}'")
                    self._buffered_frames.append((frame, direction))
                    return
            await self._flush_buffer()
            if frame.text and frame.text.strip():
                self._has_transcript = True

        await self.push_frame(frame, direction)
# ==========================================
# WEBSOCKET - Główna logika Pipecat
# ==========================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint dla Twilio Media Streams"""
    await websocket.accept()
    logger.info("🔌 WebSocket connected")

    # ==========================================
    # KROK 1: Odbierz "connected" i "start" events
    # ==========================================

    stream_sid = None
    tenant = None
    call_sid = None
    greeting_played = False
    caller_phone = "nieznany"

    try:
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)
            event = data.get("event")

            if event == "connected":
                logger.info("📡 Twilio stream connected")
                continue

            elif event == "start":
                start_data = data.get("start", {})
                stream_sid = start_data.get("streamSid")
                custom_params = start_data.get("customParameters", {})

                call_sid = custom_params.get("callSid", "unknown")
                tenant_id = custom_params.get("tenantId")
                greeting_played = custom_params.get("greetingPlayed", "false") == "true"
                caller_phone = custom_params.get("callerPhone", "nieznany")
                play_started_at = int(custom_params.get("playStartedAt", 0)) / 1000

                logger.info(f"📋 Stream started: {stream_sid}")
                logger.info(f"📋 Call: {call_sid}, tenant: {tenant_id}")

                if tenant_id:
                    rows = await db.execute("SELECT phone_number, tts_provider FROM tenants WHERE id = ?", [tenant_id])
                    if rows and rows[0].get("phone_number"):
                        tenant = await get_tenant_by_phone(rows[0]["phone_number"])

                        if tenant:
                            raw_tts = dict(rows[0]).get('tts_provider')
                            logger.info(f"🔍 Raw tts_provider from DB: '{raw_tts}'")
                            tenant['tts_provider'] = raw_tts if raw_tts else 'elevenlabs'

                            staff = await db.execute(
                                "SELECT * FROM staff WHERE tenant_id = ? AND is_active = 1",
                                [tenant_id]
                            )

                            staff_list = []
                            for s in staff:
                                staff_dict = dict(s)
                                staff_services = await db.execute(
                                    """SELECT srv.id, srv.name, srv.duration_minutes, srv.price, srv.description
                                    FROM services srv
                                    JOIN staff_services ss ON srv.id = ss.service_id
                                    WHERE ss.staff_id = ?""",
                                    [s["id"]]
                                )
                                staff_dict["services"] = [dict(svc) for svc in staff_services]
                                staff_list.append(staff_dict)

                            tenant["staff"] = staff_list

                            if not tenant.get("services"):
                                all_services = {}
                                for s in staff_list:
                                    for svc in s.get("services", []):
                                        svc_id = svc.get("id")
                                        if svc_id and svc_id not in all_services:
                                            all_services[svc_id] = svc
                                tenant["services"] = list(all_services.values())
                                if tenant["services"]:
                                    logger.info(f"   services: {len(tenant['services'])} (built from staff)")
                                else:
                                    logger.warning(f"   ⚠️ No services found!")
                            else:
                                logger.info(f"   services: {len(tenant['services'])} (from DB)")

                            logger.info(f"✅ Loaded tenant: {tenant.get('name')}")
                            logger.info(f"   tts_provider: {tenant.get('tts_provider')}")
                            logger.info(f"   booking_enabled: {tenant.get('booking_enabled')}")
                            logger.info(f"   info_services: {len(tenant.get('info_services', []))} items")
                            logger.info(f"   working_hours: {len(tenant.get('working_hours', []))} days")
                            logger.info(f"   transfer_enabled: {tenant.get('transfer_enabled')}")
                            logger.info(f"   transfer_number: {tenant.get('transfer_number')}")
                            for st in staff_list:
                                svc_names = [svc['name'] for svc in st.get('services', [])]
                                logger.info(f"   Staff {st['name']}: {svc_names if svc_names else 'wszystkie usługi'}")

                break

    except Exception as e:
        logger.error(f"Error getting start params: {e}")
        await websocket.close()
        return

    if not stream_sid:
        logger.error("❌ No stream_sid!")
        await websocket.close()
        return

    if not tenant:
        logger.error("❌ No tenant data!")
        await websocket.close()
        return

    # ==========================================
    # KROK 2: Tworzymy pipeline
    # ==========================================

    logger.info(f"🔧 Creating pipeline with stream_sid: {stream_sid}")

    from pipecat.audio.vad.vad_analyzer import VADParams

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(
                    confidence=0.6,
                    start_secs=0.2,
                    stop_secs=0.6,
                    min_volume=0.3,
                )
            ),
            serializer=TwilioFrameSerializer(
                stream_sid=stream_sid,
                params=TwilioFrameSerializer.InputParams(auto_hang_up=False)
            ),
        )
    )

    from deepgram import LiveOptions

    tenant_keyterms = build_keyterms(tenant)

    stt = DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
        live_options=LiveOptions(
            model="nova-3",
            language="pl",
            smart_format=True,
            punctuate=True,
            numerals=True,
            interim_results=True,
            utterance_end_ms=1200,
            endpointing=700,
            keyterm=tenant_keyterms,
            no_delay=True
        )
    )

    # 🔥 Rozgrzej Deepgram natychmiast - mamy ~3-4s zanim user skończy słuchać MP3
    tts = create_tts_service(tenant)


    llm = OpenAILLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        model="gpt-4.1-mini",
        params=BaseOpenAILLMService.InputParams(
            temperature=0.3,
            max_completion_tokens=120,
        ),
    )
    logger.info("🧠 Using OpenAI gpt-4.1-mini")

    context = OpenAILLMContext()
    context_aggregator = llm.create_context_aggregator(context)

    # ==========================================
    # ⏱️ TIMING STATE
    # ==========================================

    timing_state = {
        "_stt_end_time": None,
        "_llm_start_time": None,
        "_llm_end_time": None,
        "_tts_start_time": None,
    }

    @stt.event_handler("on_transcript_complete")
    async def on_stt_complete(stt_service, transcript):
        text = ""
        if hasattr(transcript, 'text'):
            text = transcript.text
        elif isinstance(transcript, dict):
            text = transcript.get("text", "")
        if text and text.strip():
            timing_state["_stt_end_time"] = time.time()
            logger.info(f"⏱️ [STT DONE] '{text[:40]}...'")

    @llm.event_handler("on_llm_started")
    async def on_llm_started(llm_service):
        stt_end = timing_state.get("_stt_end_time") or time.time()
        wait_ms = (time.time() - stt_end) * 1000
        timing_state["_llm_start_time"] = time.time()
        logger.info(f"⏱️ [LLM START] Wait from STT: {wait_ms:.0f}ms")

    @llm.event_handler("on_llm_first_token")
    async def on_llm_first_token(llm_service):
        llm_start = timing_state.get("_llm_start_time") or time.time()
        ttfb_ms = (time.time() - llm_start) * 1000
        logger.info(f"⏱️ [LLM TTFB] {ttfb_ms:.0f}ms")

    @llm.event_handler("on_llm_completed")
    async def on_llm_completed(llm_service):
        llm_start = timing_state.get("_llm_start_time") or time.time()
        total_ms = (time.time() - llm_start) * 1000
        timing_state["_llm_end_time"] = time.time()
        logger.info(f"⏱️ [LLM DONE] Total: {total_ms:.0f}ms")

    @tts.event_handler("on_tts_started")
    async def on_tts_started(tts_service):
        llm_end = timing_state.get("_llm_end_time") or time.time()
        wait_ms = (time.time() - llm_end) * 1000
        timing_state["_tts_start_time"] = time.time()
        logger.info(f"⏱️ [TTS START] Wait from LLM: {wait_ms:.0f}ms")

    @tts.event_handler("on_tts_first_audio")
    async def on_tts_first_audio(tts_service):
        tts_start = timing_state.get("_tts_start_time") or time.time()
        stt_end = timing_state.get("_stt_end_time") or time.time()
        tts_ttfb_ms = (time.time() - tts_start) * 1000
        total_ms = (time.time() - stt_end) * 1000
        logger.info(f"⏱️ [TTS TTFB] {tts_ttfb_ms:.0f}ms")
        logger.info(f"⏱️ [TOTAL] User→Bot: {total_ms:.0f}ms ({'🟢' if total_ms < 1500 else '🟡' if total_ms < 2500 else '🔴'})")

    @stt.event_handler("on_transcript")
    async def on_transcript(stt_service, transcript):
        text = transcript.get("text", "") if isinstance(transcript, dict) else str(transcript)
        is_final = transcript.get("is_final", True) if isinstance(transcript, dict) else True
        if text.strip():
            if is_final:
                logger.info(f"🎤 TRANSCRIPT (final): '{text}'")
            else:
                logger.debug(f"🎤 TRANSCRIPT (interim): '{text}'")

    # ==========================================
    # TIMEOUT HANDLING
    # ==========================================

    MAX_CALL_DURATION = 4 * 60
    IDLE_TIMEOUT = 10.0

    call_start_time = datetime.utcnow()
    conversation_ended = False

    async def handle_user_idle(processor: UserIdleProcessor, retry_count: int) -> bool:
        nonlocal conversation_ended

        if conversation_ended:
            logger.info("⏰ Idle triggered but conversation already ended")
            return False

        if flow_manager.state.get("transfer_requested"):
            logger.info("⏰ Idle triggered but transfer in progress - ignoring")
            return False

        try:
            current_node = flow_manager.current_node.get("name", "") if flow_manager.current_node else ""
            if current_node in ["end", "transfer_end"]:
                logger.info(f"⏰ Idle triggered but already in {current_node} node - stopping monitor")
                return False
        except:
            pass

        logger.info(f"⏰ User idle - retry #{retry_count}")

        if retry_count == 1:
            await task.queue_frame(TTSSpeakFrame(text="Halo, czy jest Pan jeszcze przy telefonie?"))
            processor._timeout = 5.0
            return True
        else:
            logger.info("⏰ User idle too long - ending call NOW")
            conversation_ended = True
            await task.queue_frame(TTSSpeakFrame(text="Dziękuję za kontakt, do widzenia!"))

            async def force_hangup():
                await asyncio.sleep(2.0)
                try:
                    await task.queue_frame(EndFrame())
                    logger.info("🔚 EndFrame sent from idle handler")
                except Exception as e:
                    logger.error(f"Error sending EndFrame from idle: {e}")

            asyncio.create_task(force_hangup())
            return False

    user_idle = UserIdleProcessor(
        callback=handle_user_idle,
        timeout=IDLE_TIMEOUT,
    )

    async def check_max_duration():
        nonlocal conversation_ended
        warning_given = False
        silence_warning_given = False

        while True:
            await asyncio.sleep(5)

            if conversation_ended:
                logger.info("⏱️ Duration monitor stopped - conversation ended")
                break

            elapsed = (datetime.utcnow() - call_start_time).total_seconds()

            last_speech = flow_manager.state.get("_stt_end_time")
            if last_speech:
                silence_seconds = time.time() - last_speech

                if silence_seconds > 15 and not silence_warning_given:
                    logger.warning(f"🔇 Extended silence: {silence_seconds:.0f}s - asking if still there")
                    silence_warning_given = True
                    try:
                        await task.queue_frame(TTSSpeakFrame(text="Halo, czy jest Pan jeszcze przy telefonie?"))
                    except Exception as e:
                        logger.error(f"Silence warning error: {e}")

                if silence_seconds > 25:
                    logger.warning(f"🔇 No response for {silence_seconds:.0f}s - ending call")
                    conversation_ended = True
                    try:
                        await task.queue_frame(TTSSpeakFrame(text="Nie słyszę odpowiedzi. Dziękuję za kontakt, do widzenia!"))

                        async def force_end_silence():
                            await asyncio.sleep(2.5)
                            await task.queue_frame(EndFrame())
                            logger.info("🔚 EndFrame sent after extended silence")

                        asyncio.create_task(force_end_silence())
                    except Exception as e:
                        logger.error(f"Silence hangup error: {e}")
                    break

                if silence_seconds < 10:
                    silence_warning_given = False

            if elapsed >= MAX_CALL_DURATION - 30 and not warning_given:
                logger.warning(f"⚠️ Call approaching max duration: {elapsed:.0f}s / {MAX_CALL_DURATION}s")
                warning_given = True
                try:
                    from pipecat.frames.frames import LLMMessagesAppendFrame
                    await task.queue_frame(
                        LLMMessagesAppendFrame(
                            messages=[{
                                "role": "system",
                                "content": "WAŻNE: Zostało 30 sekund rozmowy. Powiedz klientowi: 'Przepraszam, za chwilę będę musiała kończyć rozmowę. Czy mogę jeszcze w czymś szybko pomóc?'"
                            }],
                            run_llm=True
                        )
                    )
                except Exception as e:
                    logger.error(f"Warning message error: {e}")

            if elapsed >= MAX_CALL_DURATION:
                logger.warning(f"🛑 Max call duration reached: {elapsed:.0f}s - FORCING HANGUP")
                conversation_ended = True
                try:
                    await task.queue_frame(TTSSpeakFrame(text="Przepraszam, czas rozmowy się skończył. Dziękuję i do widzenia!"))

                    async def force_end_duration():
                        await asyncio.sleep(3.0)
                        try:
                            await task.queue_frame(EndFrame())
                            logger.info("🔚 EndFrame sent after max duration")
                        except:
                            await task.cancel()

                    asyncio.create_task(force_end_duration())
                except Exception as e:
                    logger.error(f"End call error: {e}")
                    await task.cancel()
                break
    first_filler = FirstResponseFiller(stt_ready_time=0)

    pipeline_components = [
        transport.input(),
        stt,
        first_filler,
        user_idle,
        context_aggregator.user(),
        llm,
        tts,
        transport.output(),
        context_aggregator.assistant(),
    ]

    pipeline = Pipeline(pipeline_components)
    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
            audio_in_sample_rate=8000,
            audio_out_sample_rate=8000,
        )
    )
    # ==========================================
    # PIPECAT FLOWS
    # ==========================================

    flow_manager = FlowManager(
        task=task,
        llm=llm,
        context_aggregator=context_aggregator,
        transport=transport,
        global_functions=[end_conversation_function()],
    )

    flow_manager.state["tenant"] = tenant
    flow_manager.state["call_sid"] = call_sid
    flow_manager.state["stream_sid"] = stream_sid
    flow_manager.state["started_at"] = datetime.utcnow()
    flow_manager.state["greeting_played"] = greeting_played
    flow_manager.state["caller_phone"] = caller_phone

    # ==========================================
    # 🔥 WARM-UP: send_warm_prompt
    # ==========================================

    async def send_warm_prompt():
        try:
            await llm._client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=[{"role": "user", "content": "OK"}],
                max_tokens=1,
            )
            logger.info("🔥 Warm prompt done")
        except Exception as e:
            logger.warning(f"Warm prompt failed: {e}")



    # ==========================================
    # EVENT HANDLERS
    # ==========================================

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("🎤 Client connected - starting flow")

        asyncio.create_task(send_warm_prompt())
        asyncio.create_task(check_max_duration())

        # Daj LLM 300ms head start zanim flow zainicjuje
        await asyncio.sleep(0.3)
        await flow_manager.initialize(create_initial_node(tenant, greeting_played))

        if greeting_played:
            async def greeting_silence_watchdog():
                nonlocal conversation_ended
                await asyncio.sleep(10.0)
                if conversation_ended or flow_manager.state.get("conversation_ended"):
                    return
                try:
                    ctx = flow_manager.get_current_context()
                    has_user = any(m.get("role") == "user" for m in ctx)
                except:
                    has_user = False
                if has_user:
                    logger.info("⏰ Watchdog: user already responded, stopping")
                    return
                logger.info("⏰ No response after greeting - saying Halo")
                await task.queue_frame(TTSSpeakFrame(text="Halo, czy jest Pan jeszcze przy telefonie?"))
                await asyncio.sleep(6.0)
                if conversation_ended or flow_manager.state.get("conversation_ended"):
                    return
                try:
                    ctx2 = flow_manager.get_current_context()
                    has_user2 = any(m.get("role") == "user" for m in ctx2)
                except:
                    has_user2 = False
                if has_user2:
                    logger.info("⏰ Watchdog: user responded after Halo, stopping")
                    return
                logger.info("⏰ Still no response - ending call")
                conversation_ended = True
                await task.queue_frame(TTSSpeakFrame(text="Dziękuję za kontakt, do widzenia!"))
                await asyncio.sleep(2.0)
                await task.queue_frame(EndFrame())
                logger.info("🔚 EndFrame sent from greeting watchdog")

            asyncio.create_task(greeting_silence_watchdog())
            logger.info("⏰ Greeting silence watchdog started (10s)")

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        nonlocal conversation_ended
        conversation_ended = True
        logger.info("📴 Client disconnected")

    # ==========================================
    # RUN PIPELINE
    # ==========================================

    runner = PipelineRunner()

    try:
        logger.info("🚀 Starting pipeline...")
        await runner.run(task)
    except Exception as e:
        logger.error(f"Pipeline error: {e}")
        if tenant and call_sid:
            await log_error(
                tenant_id=tenant.get("id"),
                call_sid=call_sid,
                error_type="pipeline_error",
                error_message=str(e)
            )
    finally:
        conversation_ended = True 
        logger.info("🏁 Pipeline finished")
        await save_call_log(flow_manager)


# ==========================================
# SAVE CALL LOG
# ==========================================

async def save_call_log(flow_manager):
    if flow_manager.state.get("call_logged"):
        return

    try:
        tenant = flow_manager.state.get("tenant", {})
        call_sid = flow_manager.state.get("call_sid")

        if tenant.get("id") and call_sid:
            existing = await db.execute(
                "SELECT id FROM call_logs WHERE call_sid = ?",
                [call_sid]
            )

            if not existing:
                await db.execute(
                    """INSERT INTO call_logs 
                       (id, tenant_id, call_sid, duration_seconds, status, created_at)
                       VALUES (?, ?, ?, 0, 'in_progress', datetime('now'))""",
                    [
                        f"call_{int(datetime.utcnow().timestamp())}",
                        tenant.get("id"),
                        call_sid,
                    ]
                )
                logger.info(f"📊 Call log created: {call_sid}")

            try:
                context = flow_manager.get_current_context()
                saved_contents = set()
                saved_count = 0

                for msg in context:
                    role = msg.get("role", "")
                    content = msg.get("content", "")

                    if role not in ["user", "assistant"]:
                        continue
                    if not content or len(content.strip()) < 2:
                        continue

                    content_key = f"{role}:{content[:100]}"
                    if content_key in saved_contents:
                        continue
                    saved_contents.add(content_key)

                    await save_transcript(tenant.get("id"), call_sid, role, content[:500])
                    saved_count += 1

                logger.info(f"📝 Transcript saved: {saved_count} messages (deduplicated)")
            except Exception as e:
                logger.error(f"Transcript save error: {e}")

            try:
                await db.execute(
                    "DELETE FROM call_transcripts WHERE tenant_id = ? AND created_at < datetime('now', '-30 days')",
                    [tenant.get("id")]
                )
            except:
                pass

            flow_manager.state["call_logged"] = True
    except Exception as e:
        logger.error(f"Save call log error: {e}")


# ==========================================
# TWILIO STATUS CALLBACK
# ==========================================

@app.post("/twilio/status")
async def twilio_status(request: Request):
    form = await request.form()

    call_sid = form.get("CallSid", "")
    call_duration = form.get("CallDuration", "0")
    call_status = form.get("CallStatus", "")
    called = form.get("Called", "")
    caller = form.get("From", "")

    logger.info(f"📊 Twilio status: {call_sid} | {call_status} | {call_duration}s")

    if call_status in ["completed", "busy", "no-answer", "failed", "canceled"]:
        try:
            duration = int(call_duration) if call_duration else 0
            duration_minutes = duration / 60.0

            phone_suffix = called.replace(" ", "").replace("-", "")[-9:]
            rows = await db.execute(
                "SELECT id FROM tenants WHERE phone_number LIKE ?",
                [f"%{phone_suffix}"]
            )

            if rows:
                tenant_id = rows[0]["id"]

                existing = await db.execute(
                    "SELECT id FROM call_logs WHERE call_sid = ?",
                    [call_sid]
                )

                if existing:
                    await db.execute(
                        """UPDATE call_logs 
                           SET duration_seconds = ?, status = ?
                           WHERE call_sid = ?""",
                        [duration, call_status, call_sid]
                    )
                    logger.info(f"📊 Updated call log: {call_sid} → {duration}s")
                else:
                    await db.execute(
                        """INSERT INTO call_logs 
                           (id, tenant_id, call_sid, caller_phone, duration_seconds, status, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
                        [
                            f"call_{int(datetime.utcnow().timestamp())}",
                            tenant_id,
                            call_sid,
                            caller,
                            duration,
                            call_status,
                        ]
                    )
                    logger.info(f"📊 Created call log: {call_sid} → {duration}s")

                if call_status == "completed" and duration > 0:
                    await db.execute(
                        "UPDATE tenants SET minutes_used = minutes_used + ? WHERE id = ?",
                        [duration_minutes, tenant_id]
                    )
                    logger.info(f"📊 Updated minutes: +{duration_minutes:.2f} min for {tenant_id}")

                    tenant_data = await db.execute(
                        "SELECT minutes_used, minutes_limit FROM tenants WHERE id = ?",
                        [tenant_id]
                    )
                    if tenant_data:
                        used = float(tenant_data[0].get("minutes_used", 0))
                        limit = int(tenant_data[0].get("minutes_limit", 100))
                        if used >= limit * 0.99:
                            await db.execute(
                                "UPDATE tenants SET is_blocked = 1 WHERE id = ?",
                                [tenant_id]
                            )
                            logger.warning(f"⚠️ Tenant {tenant_id} BLOCKED - limit reached")
            else:
                logger.warning(f"⚠️ No tenant found for {called}")

        except Exception as e:
            logger.error(f"Twilio status error: {e}")

    return Response(content="OK", media_type="text/plain")


# ==========================================
# TWILIO AFTER STREAM
# ==========================================

@app.post("/twilio/after-stream")
async def twilio_after_stream(request: Request):
    form = await request.form()
    call_sid = request.query_params.get("callSid") or form.get("CallSid", "")

    logger.info(f"📞 After stream callback for {call_sid}")

    try:
        transfer_data = await db.execute(
            "SELECT transfer_number FROM transfer_requests WHERE call_sid = ? AND status = 'pending'",
            [call_sid]
        )

        if transfer_data and transfer_data[0].get("transfer_number"):
            transfer_number = transfer_data[0]["transfer_number"]
            logger.info(f"📞 Executing transfer to {transfer_number}")

            await db.execute(
                "UPDATE transfer_requests SET status = 'completed' WHERE call_sid = ?",
                [call_sid]
            )

            tenant_data = await db.execute(
                """SELECT t.phone_number FROM tenants t 
                   JOIN call_logs cl ON cl.tenant_id = t.id 
                   WHERE cl.call_sid = ?""",
                [call_sid]
            )
            caller_id = tenant_data[0]["phone_number"] if tenant_data else ""

            twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial timeout="20" timeLimit="240" callerId="{caller_id}">
        <Number>{transfer_number}</Number>
    </Dial>
    <Say language="pl-PL" voice="Google.pl-PL-Standard-E">Przepraszamy, nie udało się połączyć. Do widzenia.</Say>
</Response>'''

            logger.info(f"📞 Transfer TwiML generated for {transfer_number}")
            return Response(content=twiml, media_type="application/xml")

    except Exception as e:
        logger.error(f"📞 After stream error: {e}")

    logger.info(f"📞 No transfer for {call_sid} - hanging up")
    twiml = '''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Hangup/>
</Response>'''
    return Response(content=twiml, media_type="application/xml")


# ==========================================
# HEALTH CHECK
# ==========================================

@app.get("/health")
async def health():
    return {"status": "ok", "framework": "pipecat", "version": "1.3", "tts_options": ["elevenlabs", "cartesia"]}


# ==========================================
# TWILIO FALLBACK
# ==========================================

@app.post("/twilio/fallback")
async def twilio_fallback(request: Request):
    logger.error("🚨 FALLBACK TRIGGERED - main bot unavailable!")
    twiml = '''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say language="pl-PL" voice="Google.pl-PL-Standard-E">
        Przepraszamy, asystent głosowy jest chwilowo niedostępny. 
        Prosimy spróbować za kilka minut.
    </Say>
    <Pause length="1"/>
    <Say language="pl-PL" voice="Google.pl-PL-Standard-E">Do widzenia.</Say>
</Response>'''
    return Response(content=twiml, media_type="application/xml")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))