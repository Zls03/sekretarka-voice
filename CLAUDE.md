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

## Migration Plan: Cascade → OpenAI Realtime (in progress)

**Status (2026-08-09):** Decision made to migrate production `bot.py` from the
cascade pipeline (Deepgram STT + LLM + TTS + Pipecat Flows) to OpenAI Realtime
(audio-to-audio, single model). Measured latency comparison on the test tenant
(`firm_1774140338448_8905c`, Vonage) drove this decision:

| Stack | User→Bot latency |
|---|---|
| GPT-4.1-mini cascade (current prod) | ~2.2s avg, mostly 🟡/🔴 |
| Groq Llama-3.3-70b cascade | ~1.9s avg, mixed 🟢/🔴 |
| OpenAI Realtime (`gpt-realtime-2.1-mini`) | ~0.6s avg, all 🟢 |

Model choice: start with **`gpt-realtime-2.1-mini`** (already tested, works,
cheaper). Fallback to `gpt-realtime-2` if quality/tool-calling reliability
becomes an issue — this is a one-line env var change, not an architecture
decision.

**Architecture note — RAG for FAQ/documents is a DELIBERATELY SEPARATE later
phase, not part of this migration.** FAQ/documents are intended to be moved to
vector RAG (Turso vector or similar) in a later phase. Phase 1 keeps the
existing FAQ behavior (whole FAQ dumped into the prompt via SQL, same as
`bot.py` does today) so the Realtime migration is isolated and easy to test —
mixing two large architectural changes at once makes it impossible to
attribute latency/quality changes to the right cause. RAG should be built when
a real tenant's FAQ/documentation volume actually justifies it (large FAQ,
multi-page procedures, franchise docs) — not preemptively for every tenant;
for a handful of FAQ items, SQL-in-prompt stays simpler and more reliable than
retrieval (nothing to "miss" when everything already fits in context).

Target end-state split:
```
SQL (stały kontekst, każdy request)          Turso Vector (RAG, later phase)
├── firma / godziny / usługi / ceny          ├── FAQ (jeśli duże)
├── pracownicy                               ├── procedury
└── ustawienia                               ├── długie informacje
                                              └── dokumentacja branżowa
        ↓                                            ↓
   zawsze w system_instruction              tylko relevant fragmenty, wstrzykiwane
                                             po retrieval przed odpowiedzią
```

**Phases (each ends with a phone test on the Vonage test tenant before moving on):**

1. **Real system prompt from panel data** — port prompt-building (cennik,
   godziny, FAQ, adres, ton branży, tożsamość asystenta) from
   `flows_helpers.py` into the Realtime service's `system_instruction`, plus
   returning-caller greeting personalization (CRM lookup, `first_message` —
   already fixed in `flows.py`'s dedup logic, reuse it). Test: facts spoken on
   the call match what's configured in the panel.
2. **Idle timeout + max call duration** — port `UserIdleProcessor` +
   `check_max_duration()` pattern (silence → "czy słyszysz?" → hangup; max
   call length). Test: go silent mid-call.
3. **Booking as guarded function-calling** — `sprawdz_dostepnosc()` /
   `zarezerwuj()` as tools, with server-side validation BEFORE and AFTER
   (mirrors existing `flows_booking_simple.py` slot-validation discipline) so
   the model can't invent availability. Test: deliberately try to get it to
   book an already-taken slot; it must refuse/correct.
4. **Contact-owner, SMS, lead email** — port `contact_owner`/transfer,
   `send_booking_sms`, lead email. Vonage transfer still needs its own
   mechanism (Vonage REST API call to redirect a live call) since the
   existing transfer flow is Twilio-specific (`transfer_requests` table +
   `/twilio/after-stream` TwiML `<Dial>`) — acceptable to ship "leave a
   message only" for Vonage at first.
5. **Credits + logging** — port `apply_call_charge` (already
   transport-agnostic, reused as-is) and `call_logs`/transcript saving.
6. **Cutover** — replace `bot.py`'s pipeline; Twilio + Vonage both supported
   from day one (pattern already proven in the current cascade system).

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
