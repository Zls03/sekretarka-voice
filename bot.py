# bot.py - Pipecat Voice AI dla salonów
"""
PIPECAT FLOWS MIGRATION v1.0
============================
Zmigrowany z custom implementation do Pipecat Flows framework.
"""
from pipecat.serializers.twilio import TwilioFrameSerializer
import os
import sys
import json
import asyncio
from datetime import datetime
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

# FastAPI
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import Response

# Pipecat core
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask, PipelineParams

# Pipecat transports - NOWE importy (bez deprecated)
from pipecat.transports.websocket.fastapi import FastAPIWebsocketTransport, FastAPIWebsocketParams
from pipecat.audio.vad.silero import SileroVADAnalyzer

# Pipecat services - NOWE importy (bez deprecated)
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext

# Pipecat Flows
from pipecat_flows import FlowManager

# Nasze moduły
from helpers import get_tenant_by_phone, db
from flows import create_initial_node

# Konfiguracja logowania
logger.remove()
logger.add(sys.stdout, level="DEBUG", format="{time:HH:mm:ss} | {level} | {message}")

app = FastAPI()


# ==========================================
# TWILIO INCOMING - Połączenie przychodzące
# ==========================================
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
    
    # TwiML - połącz z WebSocket
    host = request.headers.get("host", "localhost")
    twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://{host}/ws">
            <Parameter name="callSid" value="{call_sid}" />
            <Parameter name="tenantId" value="{tenant['id']}" />
        </Stream>
    </Connect>
</Response>'''
    
    return Response(content=twiml, media_type="application/xml")


# ==========================================
# WEBSOCKET - Główna logika Pipecat
# ==========================================
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint dla Twilio Media Streams"""
    await websocket.accept()
    
    logger.info("🔌 WebSocket connected")
    
    # Czekaj na wiadomość start z parametrami
    tenant = None
    call_sid = None
    
    try:
        # Odbierz pierwsze wiadomości żeby dostać parametry
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)
            
            if data.get("event") == "start":
                start_data = data.get("start", {})
                custom_params = start_data.get("customParameters", {})
                
                call_sid = custom_params.get("callSid", "unknown")
                tenant_id = custom_params.get("tenantId")
                
                logger.info(f"📋 Call started: {call_sid}, tenant: {tenant_id}")
                
                # Pobierz tenant z bazy
                if tenant_id:
                    rows = await db.execute("SELECT * FROM tenants WHERE id = ?", [tenant_id])
                    if rows:
                        tenant = dict(rows[0])
                        # Dodaj usługi, pracowników
                        services = await db.execute(
                            "SELECT * FROM services WHERE tenant_id = ? AND is_active = 1", 
                            [tenant_id]
                        )
                        staff = await db.execute(
                            "SELECT * FROM staff WHERE tenant_id = ? AND is_active = 1",
                            [tenant_id]
                        )
                        tenant["services"] = [dict(s) for s in services]
                        tenant["staff"] = [dict(s) for s in staff]
                        
                        logger.info(f"✅ Loaded tenant: {tenant.get('name')}")
                break
            
            elif data.get("event") == "connected":
                logger.info("📡 Twilio stream connected")
                continue
    
    except Exception as e:
        logger.error(f"Error getting start params: {e}")
        return
    
    if not tenant:
        logger.error("❌ No tenant data!")
        await websocket.close()
        return
    
    # ==========================================
    # KONFIGURACJA PIPECAT PIPELINE
    # ==========================================
    
    # Transport - Twilio WebSocket
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=SileroVADAnalyzer(),
            serializer=TwilioFrameSerializer(),
        )
    )
    
    # STT - Deepgram
    stt = DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
        language="pl",
        model="nova-3",  # ← Polski model
        encoding="mulaw",  # ← Format Twilio
        sample_rate=8000,
    )
    
    # TTS - ElevenLabs
    tts = ElevenLabsTTSService(
        api_key=os.getenv("ELEVENLABS_API_KEY"),
        voice_id=os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"),
        model="eleven_multilingual_v2",
        output_format="ulaw_8000",
    )
    
    # LLM - OpenAI
    llm = OpenAILLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        model="gpt-4o-mini",
    )
    
    # Context
    context = OpenAILLMContext()
    context_aggregator = llm.create_context_aggregator(context)
    
    # ==========================================
    # PIPELINE
    # ==========================================
    
    pipeline = Pipeline([
        transport.input(),
        stt,
        context_aggregator.user(),
        llm,
        tts,
        transport.output(),
        context_aggregator.assistant(),
    ])
    
    # Task - MUSI BYĆ UTWORZONY PRZED FlowManager!
    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
        )
    )
    
    # ==========================================
    # PIPECAT FLOWS - State Machine
    # FlowManager MUSI być utworzony PO task!
    # ==========================================
    
    flow_manager = FlowManager(
        task=task,  # WAŻNE: task musi być przekazany tutaj!
        llm=llm,
        context_aggregator=context_aggregator,
    )
    
    # Zapisz dane tenant w state (dostępne w całym flow)
    flow_manager.state["tenant"] = tenant
    flow_manager.state["call_sid"] = call_sid
    flow_manager.state["started_at"] = datetime.utcnow()
    
    # ==========================================
    # EVENT HANDLERS
    # ==========================================
    
    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("🎤 Client connected - starting flow")
        # Rozpocznij flow od pierwszego node'a
        await flow_manager.initialize(create_initial_node(tenant))
    
    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("📴 Client disconnected")
        # Zapisz log rozmowy
        await save_call_log(flow_manager)
    
    # ==========================================
    # RUN PIPELINE
    # ==========================================
    
    runner = PipelineRunner()
    
    try:
        await runner.run(task)
    except Exception as e:
        logger.error(f"Pipeline error: {e}")
    finally:
        logger.info("🏁 Pipeline finished")


async def save_call_log(flow_manager):
    """Zapisz log rozmowy do bazy"""
    try:
        tenant = flow_manager.state.get("tenant", {})
        started_at = flow_manager.state.get("started_at")
        ended_at = datetime.utcnow()
        
        if started_at:
            duration = int((ended_at - started_at).total_seconds())
            duration_minutes = duration / 60.0
            
            # Zapisz log
            await db.execute(
                """INSERT INTO call_logs 
                   (id, tenant_id, call_sid, started_at, ended_at, duration_seconds, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'completed', datetime('now'))""",
                [
                    f"call_{int(datetime.utcnow().timestamp())}",
                    tenant.get("id"),
                    flow_manager.state.get("call_sid"),
                    started_at.isoformat(),
                    ended_at.isoformat(),
                    duration,
                ]
            )
            
            # Aktualizuj zużycie minut
            await db.execute(
                "UPDATE tenants SET minutes_used = minutes_used + ? WHERE id = ?",
                [duration_minutes, tenant.get("id")]
            )
            
            logger.info(f"📊 Call logged: {duration}s")
    except Exception as e:
        logger.error(f"Save call log error: {e}")


# ==========================================
# HEALTH CHECK
# ==========================================
@app.get("/health")
async def health():
    return {"status": "ok", "framework": "pipecat"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))