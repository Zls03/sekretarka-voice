import os
import asyncio
import json
from dotenv import load_dotenv
from loguru import logger
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.transports.network.websocket_server import WebSocketServerTransport, WebSocketServerParams
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.frames.frames import LLMMessagesFrame

load_dotenv()

SYSTEM_PROMPT = """Jesteś asystentką AI odbierającą telefony dla firmy. 
Mówisz po polsku, jesteś uprzejma i pomocna.
Odpowiadaj krótko i zwięźle.
Na początku rozmowy przywitaj się: "Dzień dobry, w czym mogę pomóc?"
"""

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/twilio/incoming")
async def twilio_incoming(request: Request):
    """Twilio dzwoni tutaj gdy ktoś zadzwoni na numer"""
    host = request.headers.get("host", "localhost")
    
    # TwiML - każ Twilio połączyć się przez WebSocket
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://{host}/ws/twilio" />
    </Connect>
</Response>"""
    
    return Response(content=twiml, media_type="application/xml")

@app.websocket("/ws/twilio")
async def twilio_websocket(websocket: WebSocket):
    """WebSocket dla Twilio Media Streams"""
    await websocket.accept()
    logger.info("🎤 Twilio WebSocket połączony")
    
    stream_sid = None
    
    try:
        # STT - Deepgram
        stt = DeepgramSTTService(
            api_key=os.getenv("DEEPGRAM_API_KEY"),
            language="pl"
        )
        
        # LLM - OpenAI
        llm = OpenAILLMService(
            api_key=os.getenv("OPENAI_API_KEY"),
            model="gpt-4o-mini"
        )
        
        # TTS - ElevenLabs
        tts = ElevenLabsTTSService(
            api_key=os.getenv("ELEVENLABS_API_KEY"),
            voice_id="21m00Tcm4TlvDq8ikWAM",
            model="eleven_flash_v2_5"
        )
        
        # Kontekst
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        context = OpenAILLMContext(messages)
        context_aggregator = llm.create_context_aggregator(context)
        
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)
            
            event = data.get("event")
            
            if event == "start":
                stream_sid = data.get("streamSid")
                logger.info(f"📞 Rozmowa rozpoczęta: {stream_sid}")
                
            elif event == "media":
                # Tu będzie przetwarzanie audio
                pass
                
            elif event == "stop":
                logger.info("📞 Rozmowa zakończona")
                break
                
    except Exception as e:
        logger.error(f"❌ Błąd: {e}")
    finally:
        logger.info("👋 WebSocket zamknięty")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8765))
    uvicorn.run(app, host="0.0.0.0", port=port)