"""BizVoice — backend głosowej sekretarki AI.

Rozmowy telefoniczne (Twilio/Vonage) obsługiwane przez jeden z trzech silników wybieranych
per firma polem `realtime_engine`: Gemini Live, OpenAI Realtime lub ElevenLabs.
"""

from dotenv import load_dotenv

# Musi zadziałać przed importem jakiegokolwiek modułu czytającego zmienne środowiskowe.
load_dotenv()
