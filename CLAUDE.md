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

## Migration Plan: Cascade → Gemini Live (in progress; pivoted from OpenAI Realtime)

**Status (2026-08-09):** Decision made to migrate production `bot.py` from the
cascade pipeline (Deepgram STT + LLM + TTS + Pipecat Flows) to a realtime
audio-to-audio model. Measured latency comparison on the test tenant
(`firm_1774140338448_8905c`, Vonage) drove this decision:

| Stack | User→Bot latency |
|---|---|
| GPT-4.1-mini cascade (current prod) | ~2.2s avg, mostly 🟡/🔴 |
| Groq Llama-3.3-70b cascade | ~1.9s avg, mixed 🟢/🔴 |
| OpenAI Realtime (`gpt-realtime-2.1-mini`) | ~0.6s avg, all 🟢 |

Initial choice was **`gpt-realtime-2.1-mini`** — see "PIVOT" note below for why
this was superseded.

**PIVOT (2026-08-11): target is now Gemini Live, not OpenAI Realtime.** After
Phase 1/2 of the OpenAI Realtime track (below) were working, Gemini Live
(`gemini-3.1-flash-live-preview`) was added as a second candidate purely for a
side-by-side comparison on the same test tenant/Railway host. Once its
startup bugs were fixed (see `bot_gemini_test.py` git history for the
debugging trail), it won on every axis that mattered:

- **Latency:** native SDK TTFB `0.746s` on a live call — comparable to OpenAI
  Realtime's ~0.6s, not the deciding factor by itself.
- **Polish voice quality:** judged clearly better subjectively (user's own
  verdict, direct side-by-side on the same phone line).
- **Cost:** has a genuine free tier for development (rate-limited, and data
  may be used for model training under free tier — NOT viable for real
  customer calls) plus an affordable paid tier once in production
  (~$0.005/min audio in + $0.018/min audio out, per ai.google.dev/gemini-api/docs/pricing,
  checked 2026-08-11) — no metered-and-verified comparison was done against
  OpenAI Realtime's own pricing, cost was a secondary factor to voice quality.

OpenAI Realtime's `gpt-realtime-2.1-mini` remains a proven fallback (Phases
1-2 below were completed and tested against it) if Gemini Live hits a wall
later — that's a one-file swap, not a rewrite, since both live behind the
same shared `realtime_prompt.py`/`realtime_tools.py` modules (see file split
note under Phase 4).

**Architecture note — vector RAG for long-form content is a DELIBERATELY
SEPARATE later phase, not part of this migration.** Existing short FAQ
(current `faq` table, a handful of Q&A pairs per tenant) STAYS in SQL and
keeps being dumped whole into the prompt, same as `bot.py` does today —
it's small, so retrieval would add failure modes (a miss) without any
upside. A NEW, separate store (Turso vector or similar) is added *alongside*
it in a later phase, specifically for long-form content that doesn't belong
in SQL rows: procedures, policies, branded/industry documentation, multi-page
material a tenant might want the assistant to draw on. Phase 1 of this
migration touches neither — it only wires the existing SQL-backed prompt
(services, hours, address, short FAQ) into the Realtime system_instruction.
Mixing the Realtime migration with introducing RAG at the same time would
make it impossible to attribute latency/quality changes to the right cause.

Target end-state split:
```
SQL (stały kontekst, każdy request)          Turso Vector (RAG, later phase — NOWA rzecz)
├── firma / godziny / usługi / ceny          ├── procedury
├── pracownicy                               ├── długie informacje
├── FAQ (krótkie, zostaje tu na zawsze)       └── dokumentacja branżowa
└── ustawienia
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
4. **Contact-owner, transfer, lead email** — DONE for Gemini Live as of
   2026-08-11 (`bot_gemini_test.py`'s Gemini Live routes, tools defined in
   `realtime_tools.py`, shared with OpenAI Realtime — same `FunctionSchema`
   objects work for both, no per-provider duplication needed):
   `contact_owner` (leave-a-message email), `submit_lead` (conditional on
   `lead_mode`), `end_conversation`. Vonage live transfer
   (`build_transfer_tool`) also added — Vonage REST API (JWT RS256 via
   `VONAGE_APPLICATION_ID`/`VONAGE_PRIVATE_KEY`), reuses the same
   `transfer_enabled`/`transfer_number` tenant fields as the Twilio cascade
   mechanism. **Untested on a live call as of this writing** — first call
   with `transfer_enabled=1` on Vonage is the real test. Twilio transfer is
   untouched, still only the cascade-specific mechanism
   (`transfer_requests` + `/twilio/after-stream` TwiML `<Dial>`) — no Twilio
   equivalent built for Realtime/Gemini Live yet, still "leave a message
   only" there. SMS not ported yet.
5. **Credits + logging** — DONE for Gemini Live (`apply_call_charge`,
   `save_call_transcript`, `maybe_send_call_summary` — same shared
   `realtime_tools.py` functions as OpenAI Realtime, reused as-is).
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
