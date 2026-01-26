# bot.py - Pipecat Voice AI dla salonów
"""
PIPECAT FLOWS MIGRATION v1.2
============================
Naprawiona obsługa TwilioFrameSerializer z auto_hang_up=False
"""

import os
import sys
import json
import asyncio
from datetime import datetime
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

from flows import end_conversation_function, escalate_to_human_function

# FastAPI
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import Response

# Pipecat core
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask, PipelineParams

# Pipecat transports
from pipecat.transports.websocket.fastapi import FastAPIWebsocketTransport, FastAPIWebsocketParams
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.serializers.twilio import TwilioFrameSerializer

# Pipecat services
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.groq import GroqLLMService
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext

# Pipecat Flows
from pipecat_flows import FlowManager

# Idle timeout processor
from pipecat.processors.user_idle_processor import UserIdleProcessor

# Nasze moduły
from helpers import get_tenant_by_phone, db
from flows import create_initial_node

# Konfiguracja logowania
logger.remove()
logger.add(sys.stdout, level="DEBUG", format="{time:HH:mm:ss} | {level} | {message}")

app = FastAPI()

@app.post("/twilio/incoming")
async def twilio_incoming(request: Request):
    """Obsługa połączenia przychodzącego z Twilio"""
    form = await request.form()
    
    called = form.get("Called", form.get("To", ""))
    caller = form.get("From", "")
    call_sid = form.get("CallSid", "")
    
    logger.info(f"📞 Incoming call: {caller} → {called} (CallSid: {call_sid})")
    
    # Pobierz tenant z bazy
    tenant = await get_tenant_by_phone(called)
    
    if not tenant:
        logger.warning(f"❌ No tenant for {called}")
        return Response(
            content='<?xml version="1.0"?><Response><Say language="pl-PL">Przepraszamy, ten numer nie jest aktywny.</Say></Response>',
            media_type="application/xml"
        )
    
    # Sprawdź blokadę (limit minut)
    if tenant.get("is_blocked"):
        logger.warning(f"🚫 Tenant {tenant['id']} BLOCKED")
        return Response(
            content='<?xml version="1.0"?><Response><Say language="pl-PL">Przepraszamy, linia jest chwilowo niedostępna.</Say><Hangup/></Response>',
            media_type="application/xml"
        )
    
    logger.info(f"✅ Tenant: {tenant.get('name')}")
    
    host = request.headers.get("host", "localhost")
    
    # Powitanie - używamy Twilio Say dla instant response
    first_message = tenant.get("first_message") or f"Dzień dobry, tu {tenant.get('name')}. W czym mogę pomóc?"
    
    logger.info(f"🔊 Using Twilio Say for instant greeting")
    
    twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say language="pl-PL" voice="Google.pl-PL-Standard-E">{first_message}</Say>
    <Connect action="https://{host}/twilio/after-stream?callSid={call_sid}">
        <Stream url="wss://{host}/ws">
            <Parameter name="callSid" value="{call_sid}" />
            <Parameter name="tenantId" value="{tenant['id']}" />
            <Parameter name="greetingPlayed" value="true" />
            <Parameter name="callerPhone" value="{caller}" />
        </Stream>
    </Connect>
    <Say language="pl-PL" voice="Google.pl-PL-Standard-E">Do widzenia.</Say>
</Response>'''
    
    return Response(content=twiml, media_type="application/xml")

# ==========================================
# GREETING AUDIO - Serwowanie MP3 dla Twilio
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
# WEBSOCKET - Główna logika Pipecat
# ==========================================
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint dla Twilio Media Streams"""
    await websocket.accept()
    
    logger.info("🔌 WebSocket connected")
    
    # ==========================================
    # KROK 1: Odbierz "connected" i "start" events
    # żeby dostać stream_sid PRZED utworzeniem pipeline
    # ==========================================
    
    stream_sid = None
    tenant = None
    call_sid = None
    greeting_played = False
    
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
                
                logger.info(f"📋 Stream started: {stream_sid}")
                logger.info(f"📋 Call: {call_sid}, tenant: {tenant_id}")
                
                # Pobierz tenant z bazy - używamy get_tenant_by_phone dla pełnych danych
                if tenant_id:
                    # Najpierw pobierz numer telefonu tenant
                    rows = await db.execute("SELECT phone_number FROM tenants WHERE id = ?", [tenant_id])
                    if rows and rows[0].get("phone_number"):
                        # Użyj get_tenant_by_phone - pobiera WSZYSTKIE dane (working_hours, info_services, etc.)
                        tenant = await get_tenant_by_phone(rows[0]["phone_number"])
                        
                        if tenant:
                            # Dodaj staff (get_tenant_by_phone tego nie pobiera)
                            staff = await db.execute(
                                "SELECT * FROM staff WHERE tenant_id = ? AND is_active = 1",
                                [tenant_id]
                            )
                            tenant["staff"] = [dict(s) for s in staff]
                            
                            logger.info(f"✅ Loaded tenant: {tenant.get('name')}")
                            logger.info(f"   booking_enabled: {tenant.get('booking_enabled')}")
                            logger.info(f"   info_services: {len(tenant.get('info_services', []))} items")
                            logger.info(f"   working_hours: {len(tenant.get('working_hours', []))} days")
                
                # Mamy stream_sid - możemy utworzyć pipeline
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
    # KROK 2: Teraz mamy stream_sid - tworzymy pipeline
    # ==========================================
    
    logger.info(f"🔧 Creating pipeline with stream_sid: {stream_sid}")
    
    # Transport - Twilio WebSocket z serializerem
    # WAŻNE: auto_hang_up=False bo nie mamy Twilio credentials
    from pipecat.audio.vad.vad_analyzer import VADParams
    
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(
                    confidence=0.8,      # Wyższy próg (domyślnie 0.7)
                    start_secs=0.3,      # Dłużej czekaj przed uznaniem za mowę
                    stop_secs=1.5,       # ZWIĘKSZONE: dłużej czekaj na koniec wypowiedzi
                    min_volume=0.5,      # Minimalny poziom głośności
                )
            ),
            serializer=TwilioFrameSerializer(
                stream_sid=stream_sid,
                params=TwilioFrameSerializer.InputParams(auto_hang_up=False)
            ),
        )
    )
    
    # STT - Deepgram
    # UWAGA: NIE ustawiamy encoding/sample_rate bo TwilioFrameSerializer
    # już konwertuje mulaw 8kHz → PCM 16kHz dla pipeline
    from deepgram import LiveOptions
    
    stt = DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
        live_options=LiveOptions(
            model="nova-3",
            language="pl",
            smart_format=True,
            punctuate=True,
        )
    )
    
    # TTS - ElevenLabs  
    # Serializer skonwertuje PCM → mulaw dla Twilio
    tts = ElevenLabsTTSService(
        api_key=os.getenv("ELEVENLABS_API_KEY"),
        voice_id=os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"),
        model="eleven_multilingual_v2",
        output_format="pcm_24000",  # Standard PCM, serializer skonwertuje
    )
    
    # Groq - ultra szybki LLM (4-10x szybszy niż OpenAI)
    llm = OpenAILLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        model="gpt-4.1-mini",
    )
    
    # Context
    context = OpenAILLMContext()
    context_aggregator = llm.create_context_aggregator(context)
    
    # ==========================================
    # TIMEOUT HANDLING
    # ==========================================
    
    # Konfiguracja timeoutów
    MAX_CALL_DURATION = 4 * 60  # 4 minuty max
    IDLE_TIMEOUT = 10.0         # 10 sekund ciszy → pytanie
    
    call_start_time = datetime.utcnow()
    call_logged = False
    conversation_ended = False
    
    async def handle_user_idle(processor: UserIdleProcessor, retry_count: int) -> bool:
        """
        Obsługa ciszy od użytkownika.
        retry_count=1 → pierwsze przypomnienie
        retry_count=2 → drugie przypomnienie  
        retry_count>=3 → rozłącz
        
        Zwraca True żeby kontynuować monitoring, False żeby zakończyć.
        """
        # Jeśli rozmowa już zakończona (node "end") - nie rób nic
        try:
            current_node = flow_manager.current_node.get("name", "") if flow_manager.current_node else ""
            if current_node == "end":
                logger.info("⏰ Idle triggered but already in end node - stopping monitor")
                return False
        except:
            pass
        
        logger.info(f"⏰ User idle - retry #{retry_count}")
        
        if retry_count == 1:
            from pipecat.frames.frames import LLMMessagesAppendFrame
            await processor.push_frame(
                LLMMessagesAppendFrame(
                    messages=[{
                        "role": "system",
                        "content": "Użytkownik milczy. Zapytaj uprzejmie: 'Czy jest Pan jeszcze przy telefonie?'"
                    }],
                    run_llm=True
                )
            )
            return True
            
        elif retry_count == 2:
            from pipecat.frames.frames import LLMMessagesAppendFrame
            await processor.push_frame(
                LLMMessagesAppendFrame(
                    messages=[{
                        "role": "system", 
                        "content": "Użytkownik nadal milczy. Powiedz: 'Jeśli potrzebujesz więcej czasu, proszę dać znać.'"
                    }],
                    run_llm=True
                )
            )
            return True
            
        else:
            logger.info("⏰ User idle too long - ending call")
            from pipecat.frames.frames import LLMMessagesAppendFrame, EndFrame
            await processor.push_frame(
                LLMMessagesAppendFrame(
                    messages=[{
                        "role": "system",
                        "content": "Zakończ rozmowę mówiąc: 'Rozumiem, że jest Pan zajęty. Zapraszam do ponownego kontaktu. Do widzenia!'"
                    }],
                    run_llm=True
                )
            )
            await asyncio.sleep(4)
            await processor.push_frame(EndFrame())
            return False
    
    # User Idle Processor - wykrywa ciszę od użytkownika
    user_idle = UserIdleProcessor(
        callback=handle_user_idle,
        timeout=IDLE_TIMEOUT,
    )
    
    # ==========================================
    # MAX CALL DURATION MONITOR
    # ==========================================
    
    async def check_max_duration():
        """Sprawdza czy nie przekroczono max czasu rozmowy"""
        while True:
            await asyncio.sleep(10)  # Sprawdzaj co 10 sekund
            elapsed = (datetime.utcnow() - call_start_time).total_seconds()
            
            if elapsed >= MAX_CALL_DURATION - 30:  # 30 sekund przed końcem
                logger.warning(f"⚠️ Call approaching max duration: {elapsed:.0f}s / {MAX_CALL_DURATION}s")
                
            if elapsed >= MAX_CALL_DURATION:
                logger.warning(f"🛑 Max call duration reached: {elapsed:.0f}s - ending call")
                # Wyślij ostrzeżenie przez TTS i zakończ
                try:
                    from pipecat.frames.frames import TextFrame, EndFrame
                    # Nie mamy łatwego dostępu do pipeline tu, więc po prostu cancelujemy
                    await task.cancel()
                except:
                    pass
                break
    
    # ==========================================
    # PIPELINE
    # ==========================================
    
    pipeline = Pipeline([
        transport.input(),
        stt,
        user_idle, 
        context_aggregator.user(),
        llm,
        tts,
        transport.output(),
        context_aggregator.assistant(),
    ])
    
    # Task - WAŻNE: sample rate 8000 dla Twilio!
    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
            audio_in_sample_rate=8000,   # Twilio wysyła 8kHz
            audio_out_sample_rate=8000,  # Twilio odbiera 8kHz
        )
    )
    
    # Import funkcji end_conversation z flows
    from flows import end_conversation_function
    
    # ==========================================
    # PIPECAT FLOWS - State Machine
    # ==========================================
    from flows import end_conversation_function
    
    
    flow_manager = FlowManager(
        task=task,
        llm=llm,
        context_aggregator=context_aggregator,
        global_functions=[
            end_conversation_function(),
            escalate_to_human_function(tenant),  # NOWE: globalna eskalacja
        ],
    )
    
    # Zapisz dane tenant w state
    flow_manager.state["tenant"] = tenant
    flow_manager.state["call_sid"] = call_sid
    flow_manager.state["stream_sid"] = stream_sid
    flow_manager.state["started_at"] = datetime.utcnow()
    flow_manager.state["greeting_played"] = greeting_played
    flow_manager.state["caller_phone"] = caller_phone
    
    # ==========================================
    # EVENT HANDLERS
    # ==========================================
    
    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("🎤 Client connected - starting flow")
        # Uruchom monitor max czasu rozmowy
        asyncio.create_task(check_max_duration())
        # Przekaż info czy powitanie już odtworzone przez Twilio <Play>
        await flow_manager.initialize(create_initial_node(tenant, greeting_played))
    
    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
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
    finally:
        logger.info("🏁 Pipeline finished")
        # ZAWSZE zapisz log - nawet przy błędzie/crash
        await save_call_log(flow_manager)

async def save_call_log(flow_manager):
    """Tworzy wstępny log rozmowy (czas zostanie zaktualizowany przez Twilio callback)"""
    if flow_manager.state.get("call_logged"):
        return
        
    try:
        tenant = flow_manager.state.get("tenant", {})
        call_sid = flow_manager.state.get("call_sid")
        
        if tenant.get("id") and call_sid:
            # Sprawdź czy już istnieje
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
                logger.info(f"📊 Call log created: {call_sid} (waiting for Twilio callback)")
            
            flow_manager.state["call_logged"] = True
    except Exception as e:
        logger.error(f"Save call log error: {e}")
# ==========================================
# TWILIO STATUS CALLBACK - Dokładny czas rozmowy
# ==========================================
@app.post("/twilio/status")
async def twilio_status(request: Request):
    """Callback po zakończeniu rozmowy - Twilio wysyła rzeczywisty czas"""
    form = await request.form()
    
    call_sid = form.get("CallSid", "")
    call_duration = form.get("CallDuration", "0")  # Sekundy - dokładny czas od Twilio!
    call_status = form.get("CallStatus", "")  # completed, busy, no-answer, failed
    called = form.get("Called", "")  # Numer na który dzwoniono
    caller = form.get("From", "")  # Numer dzwoniącego
    
    logger.info(f"📊 Twilio status: {call_sid} | {call_status} | {call_duration}s")
    
    # Tylko dla zakończonych rozmów
    if call_status in ["completed", "busy", "no-answer", "failed", "canceled"]:
        try:
            duration = int(call_duration) if call_duration else 0
            duration_minutes = duration / 60.0
            
            # Znajdź tenant po numerze telefonu
            phone_suffix = called.replace(" ", "").replace("-", "")[-9:]
            rows = await db.execute(
                "SELECT id FROM tenants WHERE phone_number LIKE ?",
                [f"%{phone_suffix}"]
            )
            
            if rows:
                tenant_id = rows[0]["id"]
                
                # Sprawdź czy już mamy log dla tego call_sid (unikaj duplikatów)
                existing = await db.execute(
                    "SELECT id FROM call_logs WHERE call_sid = ?",
                    [call_sid]
                )
                
                if existing:
                    # Aktualizuj istniejący log z dokładnym czasem
                    await db.execute(
                        """UPDATE call_logs 
                           SET duration_seconds = ?, status = ?
                           WHERE call_sid = ?""",
                        [duration, call_status, call_sid]
                    )
                    logger.info(f"📊 Updated call log: {call_sid} → {duration}s")
                else:
                    # Utwórz nowy log
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
                
                # Aktualizuj zużycie minut (tylko dla completed)
                if call_status == "completed" and duration > 0:
                    await db.execute(
                        "UPDATE tenants SET minutes_used = minutes_used + ? WHERE id = ?",
                        [duration_minutes, tenant_id]
                    )
                    logger.info(f"📊 Updated minutes: +{duration_minutes:.2f} min for {tenant_id}")
                    
                    # Sprawdź limit
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
# TWILIO AFTER STREAM - Obsługa transferu po zakończeniu WebSocket
# ==========================================
@app.post("/twilio/after-stream")
async def twilio_after_stream(request: Request):
    """Po zakończeniu WebSocket - sprawdź czy był request o transfer"""
    form = await request.form()
    call_sid = request.query_params.get("callSid") or form.get("CallSid", "")
    
    logger.info(f"📞 After stream callback for {call_sid}")
    
    try:
        # Sprawdź czy był request o transfer
        transfer_data = await db.execute(
            "SELECT transfer_number FROM transfer_requests WHERE call_sid = ? AND status = 'pending'",
            [call_sid]
        )
        
        if transfer_data and transfer_data[0].get("transfer_number"):
            transfer_number = transfer_data[0]["transfer_number"]
            logger.info(f"📞 Executing transfer to {transfer_number}")
            
            # Oznacz jako wykonany
            await db.execute(
                "UPDATE transfer_requests SET status = 'completed' WHERE call_sid = ?",
                [call_sid]
            )
            
            # Pobierz numer salonu dla caller ID
            tenant_data = await db.execute(
                """SELECT t.phone_number FROM tenants t 
                   JOIN call_logs cl ON cl.tenant_id = t.id 
                   WHERE cl.call_sid = ?""",
                [call_sid]
            )
            caller_id = tenant_data[0]["phone_number"] if tenant_data else ""
            
            # Zwróć TwiML z Dial - przekieruj do właściciela z muzyką na czekanie
            twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>http://twimlets.com/holdmusic?Bucket=com.twilio.music.ambient</Play>
    <Dial timeout="30" callerId="{caller_id}">
        {transfer_number}
    </Dial>
    <Say language="pl-PL" voice="Google.pl-PL-Standard-E">Przepraszamy, nie udało się połączyć. Do widzenia.</Say>
</Response>'''
            
            logger.info(f"📞 Transfer TwiML generated for {transfer_number}")
            return Response(content=twiml, media_type="application/xml")
        
    except Exception as e:
        logger.error(f"📞 After stream error: {e}")
    
    # Brak transferu lub błąd - zakończ normalnie
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
    return {"status": "ok", "framework": "pipecat", "version": "1.2"}

# ==========================================
# TWILIO FALLBACK - gdy bot nie odpowiada
# ==========================================
@app.post("/twilio/fallback")
async def twilio_fallback(request: Request):
    """Fallback gdy główny bot nie odpowiada"""
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