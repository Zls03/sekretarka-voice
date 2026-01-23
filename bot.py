import os
import json
import base64
from dotenv import load_dotenv
from loguru import logger
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware

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
    
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://{host}/ws/twilio" />
    </Connect>
</Response>"""
    
    logger.info(f"📞 Incoming call, connecting to wss://{host}/ws/twilio")
    return Response(content=twiml, media_type="application/xml")

@app.websocket("/ws/twilio")
async def twilio_websocket(websocket: WebSocket):
    """WebSocket dla Twilio Media Streams"""
    await websocket.accept()
    logger.info("🎤 Twilio WebSocket połączony")
    
    stream_sid = None
    
    try:
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)
            
            event = data.get("event")
            
            if event == "connected":
                logger.info("✅ Twilio connected")
                
            elif event == "start":
                stream_sid = data.get("streamSid")
                logger.info(f"📞 Stream started: {stream_sid}")
                
            elif event == "media":
                # Audio przychodzi tutaj (base64 mulaw)
                payload = data.get("media", {}).get("payload", "")
                # Tu będziemy przetwarzać audio przez Deepgram
                
            elif event == "stop":
                logger.info("📞 Stream stopped")
                break
                
    except Exception as e:
        logger.error(f"❌ Błąd: {e}")
    finally:
        logger.info("👋 WebSocket zamknięty")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8765))
    uvicorn.run(app, host="0.0.0.0", port=port)