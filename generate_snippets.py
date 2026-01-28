"""
Jednorazowy skrypt - generuje MP3 przerywniki przez ElevenLabs
Uruchom: python generate_snippets.py
"""
import os
import requests
import base64
from dotenv import load_dotenv

load_dotenv()

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")

SNIPPETS = {
    "checking": "Sprawdzam dostępność...",
    "saving": "Już zapisuję...",
    "moment": "Chwileczkę...",
}

def generate_mp3(text: str, filename: str):
    """Generuje MP3 przez ElevenLabs"""
    response = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
        headers={
            "xi-api-key": ELEVENLABS_API_KEY,
            "Content-Type": "application/json"
        },
        json={
            "text": text,
            "model_id": "eleven_turbo_v2_5",
            "voice_settings": {
                "stability": 0.6,
                "similarity_boost": 0.75
            }
        }
    )
    
    if response.status_code == 200:
        # Zapisz MP3
        with open(filename, "wb") as f:
            f.write(response.content)
        
        # Zapisz też base64 (do użycia w kodzie)
        b64 = base64.b64encode(response.content).decode()
        with open(filename.replace(".mp3", ".b64"), "w") as f:
            f.write(b64)
        
        print(f"✅ Generated: {filename} ({len(response.content)} bytes)")
    else:
        print(f"❌ Error: {response.status_code} - {response.text}")

if __name__ == "__main__":
    os.makedirs("snippets", exist_ok=True)
    
    for name, text in SNIPPETS.items():
        generate_mp3(text, f"snippets/{name}.mp3")
    
    print("\n🎉 Done! MP3 files in ./snippets/")