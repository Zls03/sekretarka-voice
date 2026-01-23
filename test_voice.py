import os
from dotenv import load_dotenv

load_dotenv()

print("=== Test kluczy API ===")
print(f"Deepgram: {'OK' if os.getenv('DEEPGRAM_API_KEY') else 'BRAK'}")
print(f"OpenAI: {'OK' if os.getenv('OPENAI_API_KEY') else 'BRAK'}")
print(f"ElevenLabs: {'OK' if os.getenv('ELEVENLABS_API_KEY') else 'BRAK'}")
print(f"Twilio SID: {'OK' if os.getenv('TWILIO_ACCOUNT_SID') else 'BRAK'}")
print(f"Twilio Token: {'OK' if os.getenv('TWILIO_AUTH_TOKEN') else 'BRAK'}")
print("=======================")