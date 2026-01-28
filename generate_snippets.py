"""
Jednorazowy skrypt - generuje MP3 snippety przez ElevenLabs
Uruchom lokalnie: python generate_snippets.py
"""
import os
import requests
import base64
from dotenv import load_dotenv

load_dotenv()

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")

SNIPPETS = {
    "checking_1": "Sprawdzam...",
    "checking_2": "Moment, sprawdzam...",
    "checking_3": "Już patrzę...",
    "saving_1": "Już zapisuję...",
    "saving_2": "Rezerwuję termin...",
    "saving_3": "Sekundkę, zapisuję...",
}

def generate_mp3(name: str, text: str) -> str | None:
    """Generuje MP3 przez ElevenLabs i zwraca base64"""
    print(f"🎙️ Generating: {name} = '{text}'")
    
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
        b64 = base64.b64encode(response.content).decode()
        print(f"   ✅ OK ({len(response.content)} bytes)")
        return b64
    else:
        print(f"   ❌ Error: {response.status_code} - {response.text}")
        return None

def main():
    print("🎵 Generating audio snippets...\n")
    
    results = {}
    for name, text in SNIPPETS.items():
        b64 = generate_mp3(name, text)
        if b64:
            results[name] = b64
    
    # Zapisz do pliku Python
    with open("audio_snippets.py", "w", encoding="utf-8") as f:
        f.write('"""\nAuto-generated audio snippets (MP3 base64)\nNie edytuj ręcznie!\n"""\n\n')
        f.write("AUDIO_SNIPPETS = {\n")
        for name, b64 in results.items():
            f.write(f'    "{name}": "{b64}",\n')
        f.write("}\n")
    
    print(f"\n✅ Zapisano: audio_snippets.py ({len(results)} snippets)")
    print("   Teraz możesz deployować na Railway")

if __name__ == "__main__":
    main()