"""
constants.py
============
Stałe używane w całym projekcie. Zastępuje magic strings
(np. "high", "elevenlabs") na czytelne nazwy klas.
"""


class TTSProvider:
    """Identyfikatory dostawców syntezy mowy (TTS)."""
    ELEVENLABS = "elevenlabs"
    CARTESIA = "cartesia"
    OPENAI = "openai"
    AZURE = "azure"
    GOOGLE = "google"


