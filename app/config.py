"""Konfiguracja aplikacji ze zmiennych środowiskowych — jedyne miejsce, które je czyta.

Wartości są odczytywane raz, przy starcie procesu (lokalnie także z pliku .env).
Brak klucza danej usługi wyłącza tylko tę funkcję — reszta aplikacji działa dalej.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str | None = None) -> str | None:
    return os.getenv(name, default)


@dataclass(frozen=True)
class Settings:
    # Bazy Turso: admina (firmy dodawane ręcznie) i SaaS (firmy z panelu)
    turso_database_url: str
    turso_auth_token: str
    saas_turso_database_url: str
    saas_turso_auth_token: str
    encryption_key: str  # AES-GCM, ten sam co w panelu (sekrety firm w bazie)

    # Panel (Next.js)
    panel_url: str  # wewnętrzne API profilu klienta
    internal_api_secret: str
    panel_api_url: str  # API rezerwacji
    admin_panel_api_url: str
    panel_slug: str

    # Modele AI
    openai_api_key: str | None
    openai_realtime_model: str
    openai_realtime_voice: str
    google_api_key: str | None  # Gemini Live (Developer API, nie Vertex)
    elevenlabs_api_key: str
    elevenlabs_agent_id: str  # agent domyślny, gdy firma nie ma własnego
    elevenlabs_shared_secret: str  # nagłówek x-bizvoice-secret webhooków ElevenLabs
    deepgram_api_key: str | None
    deepgram_base_url: str

    # Operatorzy
    twilio_account_sid: str | None
    twilio_auth_token: str | None
    vonage_api_key: str | None  # SMS
    vonage_api_secret: str | None
    vonage_application_id: str | None  # Voice API (JWT): transfer, nagrania
    vonage_private_key: str | None

    # Powiadomienia i integracje
    resend_api_key: str | None
    vapid_private_key: str | None  # web push portalu /crm
    n8n_crm_webhook_url: str

    # Zapasowy TTS (komunikaty wypowiadane dosłownie)
    cartesia_api_key: str | None
    azure_speech_key: str | None
    azure_speech_region: str
    google_application_credentials_json: str | None

    # Testy na żywo: wymuszona firma dla starej trasy /vonage/answer
    test_tenant_id: str

    @classmethod
    def from_env(cls) -> Settings:
        panel_api_url = _env("PANEL_API_URL", "http://localhost:3000")
        return cls(
            turso_database_url=_env("TURSO_DATABASE_URL", ""),
            turso_auth_token=_env("TURSO_AUTH_TOKEN", ""),
            saas_turso_database_url=_env("SAAS_TURSO_DATABASE_URL", ""),
            saas_turso_auth_token=_env("SAAS_TURSO_AUTH_TOKEN", ""),
            encryption_key=_env("ENCRYPTION_KEY", ""),
            panel_url=_env("PANEL_URL", ""),
            internal_api_secret=_env("INTERNAL_API_SECRET", ""),
            panel_api_url=panel_api_url,
            admin_panel_api_url=_env("ADMIN_PANEL_API_URL", panel_api_url),
            panel_slug=_env("PANEL_SLUG", ""),
            openai_api_key=_env("OPENAI_API_KEY"),
            openai_realtime_model=_env("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1-mini"),
            openai_realtime_voice=_env("OPENAI_REALTIME_VOICE", "cedar"),
            google_api_key=_env("GOOGLE_API_KEY"),
            elevenlabs_api_key=_env("ELEVENLABS_API_KEY", ""),
            elevenlabs_agent_id=_env("ELEVENLABS_AGENT_ID", ""),
            elevenlabs_shared_secret=_env("ELEVENLABS_SHARED_SECRET", ""),
            deepgram_api_key=_env("DEEPGRAM_API_KEY"),
            deepgram_base_url=(_env("DEEPGRAM_BASE_URL", "") or "").strip() or "api.deepgram.com",
            twilio_account_sid=_env("TWILIO_ACCOUNT_SID"),
            twilio_auth_token=_env("TWILIO_AUTH_TOKEN"),
            vonage_api_key=_env("VONAGE_API_KEY"),
            vonage_api_secret=_env("VONAGE_API_SECRET"),
            vonage_application_id=_env("VONAGE_APPLICATION_ID"),
            vonage_private_key=_env("VONAGE_PRIVATE_KEY"),
            resend_api_key=_env("RESEND_API_KEY"),
            vapid_private_key=_env("VAPID_PRIVATE_KEY"),
            n8n_crm_webhook_url=_env("N8N_CRM_WEBHOOK_URL", "https://magnus1503.app.n8n.cloud/webhook/call-summary"),
            cartesia_api_key=_env("CARTESIA_API_KEY"),
            azure_speech_key=_env("AZURE_SPEECH_KEY"),
            azure_speech_region=_env("AZURE_SPEECH_REGION", "westeurope"),
            google_application_credentials_json=_env("GOOGLE_APPLICATION_CREDENTIALS_JSON"),
            test_tenant_id=_env("TEST_TENANT_ID", ""),
        )


settings = Settings.from_env()
