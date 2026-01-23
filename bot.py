import os
import asyncio
from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.transports.network.fastapi_websocket import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.frames.frames import LLMMessagesFrame

load_dotenv()

# Konfiguracja
SYSTEM_PROMPT = """Jesteś asystentką AI odbierającą telefony dla firmy. 
Mówisz po polsku, jesteś uprzejma i pomocna.
Odpowiadaj krótko i zwięźle.
Na początku rozmowy przywitaj się: "Dzień dobry, w czym mogę pomóc?"
"""

async def create_pipeline(websocket_transport):
    # STT - Deepgram (rozpoznawanie mowy)
    stt = DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
        language="pl"
    )
    
    # LLM - OpenAI (mózg)
    llm = OpenAILLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        model="gpt-4o-mini"
    )
    
    # TTS - ElevenLabs (głos)
    tts = ElevenLabsTTSService(
        api_key=os.getenv("ELEVENLABS_API_KEY"),
        voice_id="21m00Tcm4TlvDq8ikWAM",  # Rachel - zmień na polski głos
        model="eleven_flash_v2_5"
    )
    
    # Kontekst rozmowy
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    context = OpenAILLMContext(messages)
    context_aggregator = llm.create_context_aggregator(context)
    
    # Pipeline
    pipeline = Pipeline([
        websocket_transport.input(),
        stt,
        context_aggregator.user(),
        llm,
        tts,
        websocket_transport.output(),
        context_aggregator.assistant()
    ])
    
    return pipeline, context_aggregator

# FastAPI app
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("WebSocket połączony")
    
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_enabled=True,
            vad_analyzer=SileroVADAnalyzer(),
            transcription_enabled=True
        )
    )
    
    pipeline, context_aggregator = await create_pipeline(transport)
    
    task = PipelineTask(
        pipeline,
        PipelineParams(allow_interruptions=True)
    )
    
    # Rozpocznij rozmowę
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    await task.queue_frames([LLMMessagesFrame(messages)])
    
    runner = PipelineRunner()
    await runner.run(task)

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8765)