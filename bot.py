import os
import json
import base64
import asyncio
import aiohttp
from dotenv import load_dotenv
from loguru import logger
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import Response, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

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


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/twilio/incoming")
async def twilio_incoming(request: Request):
    """Użyj Twilio <Say> do testu - to na pewno działa"""
    
    twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say language="pl-PL" voice="Google.pl-PL-Wavenet-E">Dzień dobry, tu sekretarka. W czym mogę pomóc?</Say>
    <Pause length="2"/>
    <Say language="pl-PL" voice="Google.pl-PL-Wavenet-E">Przepraszam, obecnie testujemy system. Proszę zadzwonić później.</Say>
</Response>"""
    
    logger.info(f"📞 Incoming call - using Twilio Say for test")
    return Response(content=twiml, media_type="application/xml")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8765))
    uvicorn.run(app, host="0.0.0.0", port=port)