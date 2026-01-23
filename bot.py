import os
import json
import base64
import asyncio
import aiohttp
import audioop
from dotenv import load_dotenv
from loguru import logger
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
import openai

load_dotenv()

SYSTEM_PROMPT = """Jesteś asystentką AI odbierającą telefony dla firmy. 
Mówisz po polsku, jesteś uprzejma i pomocna.
Odpowiadaj krótko i zwięźle - max 2-3 zdania.
"""

ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "NacdHGUYR1k3M0FAbAia")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

oai_client = openai.OpenAI(api_key=OPENAI_API_KEY)

# Conversations memory
conversations = {}


async def get_ai_response(call_sid: str, user_message: str) -> str:
    """Odpowiedź od GPT"""
    if call_sid not in conversations:
        conversations[call_sid] = [{"role": "system", "content": SYSTEM_PROMPT}]
    
    conversations[call_sid].append({"role": "user", "content": user_message})
    
    response = oai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=conversations[call_sid],
        max_tokens=150
    )
    
    assistant_message = response.choices[0].message.content
    conversations[call_sid].append({"role": "assistant", "content": assistant_message})
    
    logger.info(f"🤖 GPT: {assistant_message}")
    return assistant_message


async def text_to_speech_mulaw(text: str) -> bytes:
    """ElevenLabs TTS -> mulaw 8kHz dla Twilio"""
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "text": text,
                "model_id": "eleven_flash_v2_5",
                "output_format": "ulaw_8000"
            }
        ) as response:
            if response.status == 200:
                audio_data = await response.read()
                logger.info(f"🔊 ElevenLabs: {len(audio_data)} bytes mulaw")
                return audio_data
            else:
                error = await response.text()
                logger.error(f"ElevenLabs error: {response.status} - {error}")
                return b""


async def send_audio_to_twilio(websocket: WebSocket, audio_data: bytes, stream_sid: str):
    """Wyślij audio mulaw do Twilio przez WebSocket"""
    chunk_size = 160  # 20ms of audio at 8kHz
    
    for i in range(0, len(audio_data), chunk_size):
        chunk = audio_data[i:i + chunk_size]
        if len(chunk) < chunk_size:
            chunk = chunk + b'\xff' * (chunk_size - len(chunk))  # Pad with silence
        
        payload = base64.b64encode(chunk).decode('utf-8')
        
        message = {
            "event": "media",
            "streamSid": stream_sid,
            "media": {
                "payload": payload
            }
        }
        
        await websocket.send_text(json.dumps(message))
        await asyncio.sleep(0.018)  # ~20ms between chunks for real-time playback


async def send_clear_to_twilio(websocket: WebSocket, stream_sid: str):
    """Wyczyść bufor audio Twilio (przerwanie)"""
    message = {
        "event": "clear",
        "streamSid": stream_sid
    }
    await websocket.send_text(json.dumps(message))


class DeepgramTranscriber:
    """Real-time Deepgram transcription via WebSocket"""
    
    def __init__(self, on_transcript):
        self.on_transcript = on_transcript
        self.ws = None
        self.is_connected = False
        
    async def connect(self):
        """Połącz z Deepgram WebSocket"""
        url = "wss://api.deepgram.com/v1/listen?model=nova-3-general&language=pl&encoding=mulaw&sample_rate=8000&punctuate=true&interim_results=false&endpointing=300"
        
        session = aiohttp.ClientSession()
        self.ws = await session.ws_connect(
            url,
            headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"}
        )
        self.is_connected = True
        logger.info("🎤 Deepgram connected")
        
        # Start listening for transcripts
        asyncio.create_task(self._listen())
        
    async def _listen(self):
        """Nasłuchuj transkrypcji od Deepgram"""
        try:
            async for msg in self.ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    
                    if data.get("type") == "Results":
                        transcript = data.get("channel", {}).get("alternatives", [{}])[0].get("transcript", "")
                        is_final = data.get("is_final", False)
                        
                        if transcript and is_final:
                            logger.info(f"📝 Deepgram: {transcript}")
                            await self.on_transcript(transcript)
                            
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error(f"Deepgram WS error: {msg.data}")
                    break
        except Exception as e:
            logger.error(f"Deepgram listen error: {e}")
        finally:
            self.is_connected = False
            
    async def send_audio(self, audio_data: bytes):
        """Wyślij audio do Deepgram"""
        if self.ws and self.is_connected:
            await self.ws.send_bytes(audio_data)
            
    async def close(self):
        """Zamknij połączenie"""
        if self.ws:
            await self.ws.close()
            self.is_connected = False


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/twilio/incoming")
async def twilio_incoming(request: Request):
    """Początek rozmowy - bidirectional stream"""
    host = request.headers.get("host", "localhost")
    form = await request.form()
    call_sid = form.get("CallSid", "unknown")
    
    logger.info(f"📞 Incoming call: {call_sid}")
    
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://{host}/ws/voice" />
    </Connect>
</Response>"""
    
    return Response(content=twiml, media_type="application/xml")


@app.websocket("/ws/voice")
async def voice_websocket(websocket: WebSocket):
    """Bidirectional WebSocket: Twilio <-> Deepgram <-> GPT <-> ElevenLabs"""
    await websocket.accept()
    logger.info("🎤 Twilio WebSocket connected")
    
    stream_sid = None
    call_sid = None
    is_speaking = False  # Czy AI właśnie mówi
    
    # Transcript handler
    async def on_transcript(transcript: str):
        nonlocal is_speaking
        
        if not transcript or len(transcript.strip()) < 2:
            return
            
        if is_speaking:
            # Przerwij AI jeśli user mówi
            await send_clear_to_twilio(websocket, stream_sid)
            is_speaking = False
            logger.info("🛑 User interrupted")
        
        # Odpowiedź AI
        response = await get_ai_response(call_sid, transcript)
        
        # TTS
        audio = await text_to_speech_mulaw(response)
        
        if audio and stream_sid:
            is_speaking = True
            await send_audio_to_twilio(websocket, audio, stream_sid)
            is_speaking = False
    
    # Deepgram transcriber
    deepgram = DeepgramTranscriber(on_transcript)
    
    try:
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)
            event = data.get("event")
            
            if event == "connected":
                logger.info("✅ Stream connected")
                
            elif event == "start":
                stream_sid = data.get("streamSid")
                start_data = data.get("start", {})
                call_sid = start_data.get("callSid", "unknown")
                logger.info(f"📞 Stream start: {stream_sid}, call: {call_sid}")
                
                # Connect to Deepgram
                await deepgram.connect()
                
                # Send greeting
                greeting_audio = await text_to_speech_mulaw("Dzień dobry, w czym mogę pomóc?")
                if greeting_audio:
                    is_speaking = True
                    await send_audio_to_twilio(websocket, greeting_audio, stream_sid)
                    is_speaking = False
                    logger.info("✅ Greeting sent")
                
            elif event == "media":
                # Forward audio to Deepgram
                payload = data.get("media", {}).get("payload", "")
                audio_chunk = base64.b64decode(payload)
                await deepgram.send_audio(audio_chunk)
                
            elif event == "stop":
                logger.info("📞 Stream stop")
                break
                
    except Exception as e:
        logger.error(f"❌ WebSocket error: {e}")
    finally:
        await deepgram.close()
        if call_sid in conversations:
            del conversations[call_sid]
        logger.info("👋 WebSocket closed")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8765))
    uvicorn.run(app, host="0.0.0.0", port=port)