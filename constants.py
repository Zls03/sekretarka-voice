"""
constants.py
============
Stałe używane w całym projekcie. Zastępuje magic strings
(np. "high", "elevenlabs") na czytelne nazwy klas.
"""


class Urgency:
    """Priorytety zgłoszeń serwisowych (lead/contact flow)."""
    HIGH = "high"
    NORMAL = "normal"


class TTSProvider:
    """Identyfikatory dostawców syntezy mowy (TTS)."""
    ELEVENLABS = "elevenlabs"
    CARTESIA = "cartesia"
    OPENAI = "openai"
    AZURE = "azure"
    GOOGLE = "google"


class BookingField:
    """Klucze stanu w trakcie rezerwacji (flows_booking_simple.py)."""
    SERVICE = "service"
    STAFF = "staff"
    DATE = "date"
    TIME = "time"
    NAME = "name"
    PHONE = "phone"
