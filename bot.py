import os
import json
import base64
import asyncio
import aiohttp
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

ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

client = openai.OpenAI(api_key=OPENAI_API_KEY)


async def transcribe_audio(audio_data: bytes) -> str:
    """Transkrypcja przez Deepgram"""
    url = "https://api.deepgram.com/v1/listen?model=nova-2&language=pl&encoding=mulaw&sample_rate=8000"
    
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            headers={
                "Authorization": f"Token {DEEPGRAM_API_KEY}",
                "Content-Type": "audio/mulaw"
            },
            data=audio_data
        ) as response:
            if response.status == 200:
                result = await response.json()
                transcript = result.get("results", {}).get("channels", [{}])[0].get("alternatives", [{}])[0].get("transcript", "")
                return transcript
            else:
                logger.error(f"Deepgram error: {response.status}")
                return ""


async def get_ai_response(conversation: list, user_message: str) -> str:
    """Odpowiedź od GPT"""
    conversation.append({"role": "user", "content": user_message})
    
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=conversation,
        max_tokens=150
    )
    
    assistant_message = response.choices[0].message.content
    conversation.append({"role": "assistant", "content": assistant_message})
    
    return assistant_message


async def text_to_speech(text: str) -> bytes:
    """Synteza mowy przez ElevenLabs - zwraca mulaw 8kHz dla Twilio"""
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
                logger.info(f"🔊 ElevenLabs zwrócił {len(audio_data)} bytes audio")
                return audio_data
            else:
                error = await response.text()
                logger.error(f"ElevenLabs error: {response.status} - {error}")
                return b""


async def send_audio_to_twilio(websocket: WebSocket, audio_data: bytes, stream_sid: str):
    """Wyślij audio do Twilio"""
    # Podziel na chunki po 640 bajtów (20ms audio)
    chunk_size = 640
    for i in range(0, len(audio_data), chunk_size):
        chunk = audio_data[i:i + chunk_size]
        payload = base64.b64encode(chunk).decode("utf-8")
        
        message = {
            "event": "media",
            "streamSid": stream_sid,
            "media": {
                "payload": payload
            }
        }
        await websocket.send_text(json.dumps(message))
        await asyncio.sleep(0.02)  # 20ms między chunkami


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/twilio/incoming")
async def twilio_incoming(request: Request):
    host = request.headers.get("host", "localhost")
    
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://{host}/ws/twilio" />
    </Connect>
</Response>"""
    
    logger.info(f"📞 Incoming call")
    return Response(content=twiml, media_type="application/xml")


@app.websocket("/ws/twilio")
async def twilio_websocket(websocket: WebSocket):
    await websocket.accept()
    logger.info("🎤 Twilio WebSocket połączony")
    
    stream_sid = None
    audio_buffer = b""
    conversation = [{"role": "system", "content": SYSTEM_PROMPT}]
    silence_count = 0
    is_speaking = False
    greeting_sent = False
    
    try:
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)
            event = data.get("event")
            
            if event == "connected":
                logger.info("✅ Connected")
                
            elif event == "start":
                stream_sid = data.get("streamSid")
                logger.info(f"📞 Stream: {stream_sid}")
                
                # Wyślij powitanie
                if not greeting_sent and stream_sid:
                    greeting_sent = True
                    logger.info("🎙️ Wysyłam powitanie...")
                    greeting_audio = await text_to_speech("Dzień dobry, w czym mogę pomóc?")
                    if greeting_audio:
                        await send_audio_to_twilio(websocket, greeting_audio, stream_sid)
                        logger.info("✅ Powitanie wysłane")
                
            elif event == "media":
                payload = data.get("media", {}).get("payload", "")
                chunk = base64.b64decode(payload)
                audio_buffer += chunk
                
                # Sprawdź czy to cisza (mulaw 0xFF = cisza)
                silence_threshold = 0.9
                silence_bytes = chunk.count(b'\xff') + chunk.count(b'\x7f')
                is_silence = silence_bytes / len(chunk) > silence_threshold if chunk else True
                
                if is_silence:
                    silence_count += 1
                else:
                    silence_count = 0
                    is_speaking = True
                
                # Po ~1.5 sekundy ciszy (750ms * 2), przetwórz audio
                if is_speaking and silence_count > 75 and len(audio_buffer) > 3200:
                    is_speaking = False
                    logger.info(f"🎵 Przetwarzam audio: {len(audio_buffer)} bytes")
                    
                    # Transkrypcja
                    transcript = await transcribe_audio(audio_buffer)
                    audio_buffer = b""
                    silence_count = 0
                    
                    if transcript and len(transcript.strip()) > 0:
                        logger.info(f"📝 Transkrypcja: {transcript}")
                        
                        # Odpowiedź AI
                        response = await get_ai_response(conversation, transcript)
                        logger.info(f"🤖 Odpowiedź: {response}")
                        
                        # Synteza i wysyłka
                        audio = await text_to_speech(response)
                        if audio and stream_sid:
                            await send_audio_to_twilio(websocket, audio, stream_sid)
                    else:
                        logger.info("⏭️ Pusta transkrypcja, pomijam")
                        audio_buffer = b""
                
            elif event == "stop":
                logger.info("📞 Stop")
                break
                
    except Exception as e:
        logger.error(f"❌ Error: {e}")
    finally:
        logger.info("👋 Closed")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8765))
    uvicorn.run(app, host="0.0.0.0", port=port)