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

ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "NacdHGUYR1k3M0FAbAia")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

oai_client = openai.OpenAI(api_key=OPENAI_API_KEY)

# Cache dla audio
audio_cache = {}
# Aktywne rozmowy
conversations = {}


async def transcribe_with_deepgram(audio_data: bytes) -> str:
    """Transkrypcja przez Deepgram Nova-3"""
    url = "https://api.deepgram.com/v1/listen?model=nova-3&language=pl&encoding=mulaw&sample_rate=8000"
    
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
                logger.info(f"📝 Deepgram: {transcript}")
                return transcript
            else:
                error = await response.text()
                logger.error(f"Deepgram error: {response.status} - {error}")
                return ""


async def get_ai_response(call_sid: str, user_message: str) -> str:
    """Odpowiedź od GPT z pamięcią rozmowy"""
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


async def text_to_speech(text: str) -> bytes:
    """Synteza mowy przez ElevenLabs"""
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
                "output_format": "mp3_44100_128"
            }
        ) as response:
            if response.status == 200:
                audio_data = await response.read()
                logger.info(f"🔊 ElevenLabs: {len(audio_data)} bytes")
                return audio_data
            else:
                error = await response.text()
                logger.error(f"ElevenLabs error: {response.status} - {error}")
                return b""


def generate_play_twiml(host: str, audio_id: str, call_sid: str) -> str:
    """Generuj TwiML z Play i Connect do WebSocket"""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>https://{host}/audio/{audio_id}</Play>
    <Connect>
        <Stream url="wss://{host}/ws/deepgram">
            <Parameter name="callSid" value="{call_sid}" />
        </Stream>
    </Connect>
</Response>"""


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/audio/{audio_id}")
async def get_audio(audio_id: str):
    """Serwuj audio dla Twilio"""
    if audio_id in audio_cache:
        audio_data = audio_cache.pop(audio_id)
        return Response(content=audio_data, media_type="audio/mpeg")
    return Response(status_code=404)


@app.post("/twilio/incoming")
async def twilio_incoming(request: Request):
    """Początek rozmowy"""
    host = request.headers.get("host", "localhost")
    form = await request.form()
    call_sid = form.get("CallSid", "unknown")
    
    logger.info(f"📞 Incoming call: {call_sid}")
    
    # Generuj powitanie
    audio = await text_to_speech("Dzień dobry, w czym mogę pomóc?")
    
    if audio:
        audio_id = f"greeting_{call_sid}"
        audio_cache[audio_id] = audio
        twiml = generate_play_twiml(host, audio_id, call_sid)
    else:
        # Fallback
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say language="pl-PL" voice="Google.pl-PL-Wavenet-E">Dzień dobry, w czym mogę pomóc?</Say>
    <Connect>
        <Stream url="wss://{host}/ws/deepgram">
            <Parameter name="callSid" value="{call_sid}" />
        </Stream>
    </Connect>
</Response>"""
    
    return Response(content=twiml, media_type="application/xml")


@app.post("/twilio/continue/{call_sid}")
async def twilio_continue(call_sid: str, request: Request):
    """Kontynuacja rozmowy po odpowiedzi AI"""
    host = request.headers.get("host", "localhost")
    
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://{host}/ws/deepgram">
            <Parameter name="callSid" value="{call_sid}" />
        </Stream>
    </Connect>
</Response>"""
    
    return Response(content=twiml, media_type="application/xml")


@app.websocket("/ws/deepgram")
async def deepgram_websocket(websocket: WebSocket):
    """WebSocket dla Twilio Media Streams + Deepgram STT"""
    await websocket.accept()
    logger.info("🎤 WebSocket połączony")
    
    stream_sid = None
    call_sid = None
    audio_buffer = b""
    silence_frames = 0
    has_speech = False
    
    SILENCE_THRESHOLD = 50  # ~1 sekunda ciszy (50 * 20ms)
    MIN_AUDIO_SIZE = 3200   # Minimum audio do transkrypcji
    
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
                custom_params = start_data.get("customParameters", {})
                call_sid = custom_params.get("callSid", "unknown")
                logger.info(f"📞 Stream start: {stream_sid}, call: {call_sid}")
                
            elif event == "media":
                payload = data.get("media", {}).get("payload", "")
                chunk = base64.b64decode(payload)
                audio_buffer += chunk
                
                # Detekcja ciszy (mulaw)
                silence_bytes = sum(1 for b in chunk if b in (0xff, 0x7f, 0xfe, 0x7e))
                is_silence = silence_bytes / len(chunk) > 0.8 if chunk else True
                
                if is_silence:
                    silence_frames += 1
                else:
                    silence_frames = 0
                    has_speech = True
                
                # Jeśli była mowa i teraz cisza - przetwórz
                if has_speech and silence_frames > SILENCE_THRESHOLD and len(audio_buffer) > MIN_AUDIO_SIZE:
                    logger.info(f"🎵 Przetwarzam {len(audio_buffer)} bytes audio")
                    
                    # Transkrypcja Deepgram
                    transcript = await transcribe_with_deepgram(audio_buffer)
                    
                    # Reset
                    audio_buffer = b""
                    silence_frames = 0
                    has_speech = False
                    
                    if transcript and len(transcript.strip()) > 2:
                        # Odpowiedź AI
                        response = await get_ai_response(call_sid, transcript)
                        
                        # TTS
                        audio = await text_to_speech(response)
                        
                        if audio:
                            audio_id = f"resp_{call_sid}_{id(audio)}"
                            audio_cache[audio_id] = audio
                            
                            # Przekieruj Twilio do odtworzenia audio
                            host = "web-production-f570f.up.railway.app"
                            redirect_url = f"https://{host}/twilio/play/{audio_id}/{call_sid}"
                            
                            # Wyślij redirect przez Twilio REST API
                            await redirect_twilio_call(call_sid, redirect_url)
                
            elif event == "stop":
                logger.info("📞 Stream stop")
                # Cleanup
                if call_sid in conversations:
                    del conversations[call_sid]
                break
                
    except Exception as e:
        logger.error(f"❌ WebSocket error: {e}")
    finally:
        logger.info("👋 WebSocket closed")


async def redirect_twilio_call(call_sid: str, url: str):
    """Przekieruj rozmowę Twilio do nowego URL"""
    twilio_sid = os.getenv("TWILIO_ACCOUNT_SID")
    twilio_token = os.getenv("TWILIO_AUTH_TOKEN")
    
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{twilio_sid}/Calls/{call_sid}.json",
            auth=aiohttp.BasicAuth(twilio_sid, twilio_token),
            data={"Url": url, "Method": "POST"}
        ) as response:
            if response.status == 200:
                logger.info(f"✅ Call redirected: {call_sid}")
            else:
                error = await response.text()
                logger.error(f"❌ Redirect failed: {response.status} - {error}")


@app.post("/twilio/play/{audio_id}/{call_sid}")
async def twilio_play(audio_id: str, call_sid: str, request: Request):
    """Odtwórz audio i kontynuuj nasłuchiwanie"""
    host = request.headers.get("host", "localhost")
    
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>https://{host}/audio/{audio_id}</Play>
    <Connect>
        <Stream url="wss://{host}/ws/deepgram">
            <Parameter name="callSid" value="{call_sid}" />
        </Stream>
    </Connect>
</Response>"""
    
    return Response(content=twiml, media_type="application/xml")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8765))
    uvicorn.run(app, host="0.0.0.0", port=port)