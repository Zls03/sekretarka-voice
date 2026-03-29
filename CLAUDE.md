# CLAUDE.md


This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Voice AI assistant for Polish service businesses (salons, gyms, clinics). Handles inbound phone calls via Twilio, converts speech to text (Deepgram), processes conversation through an LLM (OpenAI/Groq/Cerebras), and responds via TTS (ElevenLabs/Cartesia/Azure/Google/OpenAI). Multi-tenant SaaS with two database sources.

## Working with me
- Communicate in Polish
- Keep responses concise — explain what you changed and why, not every detail
- Before implementing: briefly confirm the plan if change touches >3 files
- When debugging: show the specific error line, not the whole traceback

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally
uvicorn bot:app --host 0.0.0.0 --port 8000

# Production (Heroku)
# Procfile: web: uvicorn bot:app --host 0.0.0.0 --port $PORT
```

Runtime: Python 3.12. No test suite present.

## Architecture

### Call Flow

```
Twilio call → POST /twilio/incoming (returns TwiML)
           → WebSocket /ws (Pipecat pipeline)
                ├─ Deepgram STT
                ├─ LLM (OpenAI/Groq/Cerebras)
                ├─ FlowManager (state machine)
                └─ TTS (ElevenLabs/Cartesia/Azure/Google/OpenAI)
           → POST /twilio/after-stream (cleanup)
```

### Key Files

| File | Purpose |
|------|---------|
| `bot.py` | FastAPI server, Twilio webhook handlers, WebSocket pipeline setup, tenant initialization |
| `flows.py` | Main conversation flow definitions (greeting, check availability, booking initiation, FAQ) |
| `flows_booking_simple.py` | Full booking sub-flow: service/date/time selection, slot validation, DB write |
| `flows_contact.py` | Call transfer and owner contact flow |
| `flows_helpers.py` | Polish date/time parsing, API calls, availability checking logic |
| `helpers.py` | Turso DB client, tenant lookup, AES-GCM encryption for OAuth tokens |
| `polish_mappings.py` | Polish weekday/month names, hour aliases, name-to-gender detection |

### Multi-Tenant Data

Two Turso (serverless SQLite) databases:

- **Admin DB** (`TURSO_DATABASE_URL`): manually-configured businesses. Tables: `tenants`, `services`, `staff`, `bookings`, `working_hours`, `call_logs`
- **SaaS DB** (`SAAS_TURSO_DATABASE_URL`): user-created businesses from web panel. Tenant IDs prefixed with `firm_`. Tables: `firms`, `credits`

`get_tenant_by_phone()` in `helpers.py` checks Admin DB first, then SaaS DB.

### Conversation Flows (Pipecat Flows)

State machine managing multi-turn dialogue. Main states: greeting → {check_availability | start_booking | contact_owner | faq} → end. `flows_booking_simple.py` handles the multi-step booking sub-flow (service → date → time → name → phone → confirm → save).

### TTS Provider Selection

Per-tenant `tts_provider` field selects provider. Default is ElevenLabs. Each provider has its own initialization in `bot.py`.

### Polish Language Handling

`polish_mappings.py` and `flows_helpers.py` handle: relative date parsing ("jutro", "w czwartek"), number-to-word conversion for prices, grammatically-gendered name responses, and STT phoneme correction dictionaries. All are critical for correct Polish-language UX.

### SaaS Credit System

For `firm_` tenants, call cost is deducted from credit balance. Low-balance calls are rejected before the pipeline starts.

## Required Environment Variables

```
DEEPGRAM_API_KEY
OPENAI_API_KEY
ELEVENLABS_API_KEY
TWILIO_ACCOUNT_SID
TWILIO_AUTH_TOKEN
TURSO_DATABASE_URL
TURSO_AUTH_TOKEN
SAAS_TURSO_DATABASE_URL
SAAS_TURSO_AUTH_TOKEN
ENCRYPTION_KEY                          # AES-GCM, for Google OAuth tokens
GOOGLE_APPLICATION_CREDENTIALS_JSON    # Google TTS/Calendar
PANEL_API_URL                           # Dashboard backend (default: http://localhost:3000)
RESEND_API_KEY                          # Email notifications
```

Optional: `GROQ_API_KEY`, `CARTESIA_API_KEY`, `CEREBRAS_API_KEY`, Azure TTS credentials.
