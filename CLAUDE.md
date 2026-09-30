# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Voice AI assistant for Polish service businesses (salons, gyms, clinics, warsztaty,
gabinety). Handles inbound phone calls via Twilio/Vonage. Multi-tenant SaaS with two
database sources (patrz "Multi-Tenant Data" niżej).

**Trzy silniki głosowe, wybierane per-tenant polem `realtime_engine`** (panel:
zakładka "Głos agenta"), **JEDNA usługa Railway** (`web-production-fb477.up.railway.app`,
aplikacja `app.main:app`):

| `realtime_engine` | Silnik | Kod |
|---|---|---|
| `gemini` | Gemini Live (audio-to-audio, `gemini-3.1-flash-live-preview`) | `app/engines/gemini_live/` |
| `openai` | OpenAI Realtime (`gpt-realtime-2.1-mini`) — sprawdzony fallback | `app/engines/openai_realtime/` |
| `elevenlabs` | ElevenLabs Conversational AI (agent w ich dashboardzie, my wstrzykujemy prompt per rozmowa) | `app/engines/elevenlabs/` |

Wejście każdej rozmowy to webhooki operatorów w `app/telephony/` (`twilio.py`,
`vonage.py` + `ncco.py`) — tam jest JEDYNE rozgałęzienie po `realtime_engine`.
`bot_gemini_test.py` to już tylko shim (`from app.main import app`) dla starej komendy
startowej `uvicorn bot_gemini_test:app`; nazwy tras z sufiksem `-test` są historyczne,
ale wpisane w konsolach Twilio/Vonage — **nie zmieniać adresów URL**.

**Dla klientów płacących dziś (app.bizvoice.pl) liczą się realnie tylko dwie opcje:
Gemini Live i ElevenLabs** (ERCO Klimatyzacja = gemini, Gabinet Medycyny Pracy + Bizvoice
demo = elevenlabs). OpenAI Realtime zostaje jako sprawdzony fallback.

Stary silnik "cascade" (`bot.py`, `flows*.py`) usunięty 2026-09-29; kod zreorganizowany
do pakietu `app/` 2026-09-30. Historia decyzji: `docs/HISTORIA.md`, pełna historia: git.

## Working with me
- Communicate in Polish
- Keep responses concise — explain what you changed and why, not every detail
- Before implementing: briefly confirm the plan if change touches >3 files
- When debugging: show the specific error line, not the whole traceback
- Push to git only after explicit OK (Railway auto-deploys `main`)

## Commands

```bash
uvicorn app.main:app --port 8000          # lokalnie
pytest                                    # ~90 testów golden, ~20 s, bez sieci
UPDATE_GOLDEN=1 pytest                    # odśwież wzorce po ŚWIADOMEJ zmianie zachowania
ruff check . && ruff format --check .     # lint + format
```

Runtime: Python 3.12, pipecat-ai 1.4.0. Lokalne środowisko jak na Railway: `.venv-railway`
(`requirements-dev.txt` + extras pipecat azure/cartesia/elevenlabs/openai/google/silero —
samo `requirements-gemini-test.txt` NIE wystarcza, `app/tts.py` importuje Azure).

## Architecture

### Call Flow

**Gemini Live / OpenAI Realtime (Pipecat na naszym serwerze):**
```
Twilio/Vonage → app/telephony (dispatch po realtime_engine) → websocket
  → engines/<silnik>/routes.py → session.py: pipeline (transport, lokalny VAD, LLM, monitory)
      ├─ tools: engines/common.py::build_call_tools (contact_owner / end_conversation /
      │         book_appointment / manage_booking / transfer_to_owner — wg CallFeatures)
      ├─ watchdog.py: cisza, limit czasu, (Gemini) ciche zawieszenie sesji
      └─ po rozłączeniu: engines/common.py::finalize_call (transkrypt + raport)
Rozliczenie minut: webhook statusu operatora (/twilio/status, /vonage/events) → billing.py
```

**ElevenLabs (`app/engines/elevenlabs/`):**
```
Twilio: register_call (twilio.py) | Vonage: SIP direct (sip.py, domyślnie) albo most
websocket (vonage_bridge.py, fallback) → agent ElevenLabs z nadpisaniami (conversation.py)
→ webhooks.py: /elevenlabs/personalization, /elevenlabs/tools/*, /elevenlabs/post-call
```

### Key Modules

| Moduł | Rola |
|---|---|
| `app/main.py`, `app/config.py` | Składanie aplikacji; WSZYSTKIE zmienne środowiskowe (klasa `Settings`) |
| `app/telephony/` | Webhooki Twilio/Vonage, NCCO/TwiML (`responses.py`), Vonage REST API, human-first (Siperb) |
| `app/engines/common.py` | Wspólne dla silników Pipecat: `CallFeatures` (bramka funkcji), narzędzia, prompt, transport, VAD, `finalize_call`, progi ciszy |
| `app/prompt/instructions.py` | `build_realtime_instructions()` — WSPÓLNY system prompt dla wszystkich 3 silników |
| `app/tools/` | Narzędzia function-calling + `guards.py` (odrzucanie pustych/meta wywołań) |
| `app/booking/` | Rezerwacje: `book_appointment.py` (potok kroków rozmowy), `manage_booking.py`, dostępność, API panelu, SMS |
| `app/post_call/` | `summary.py` (GPT-4.1-mini), `report.py` (koniec rozmowy Pipecat), `crm_sync.py` (n8n) |
| `app/tenants.py` | `get_tenant_by_phone()` — admin DB, potem SaaS; stały kształt słownika firmy |
| `app/background.py` | `spawn()` — zadania w tle z trzymaną referencją i logowaniem błędów |
| `app/tts.py` | Zapasowy TTS — wyłącznie komunikaty wypowiadane dosłownie w Gemini Live (`speak_directly`) |
| `tests/` | Testy charakteryzujące; wzorce w `tests/golden/` |

**Zasady przy zmianach:** każda zmiana zachowania (tekst mówiony, SQL, NCCO) wychodzi
w `git diff tests/golden` — przeglądaj ją świadomie. Czysty refaktor = wzorce bez zmian.

### Lead capture — architektura (2026-09-03+)

`submit_lead` **został całkowicie usunięty** (frontend+backend) — zbyt duży tool-schema
korelował z zawieszeniami sesji Gemini Live. Zastąpiony dwoma niezależnymi mechanizmami,
oba sterowane osobnymi checkboxami w panelu (`firm/[id]/page.tsx`):

1. **`contact_owner`** (toggle "📨 Zbieranie wiadomości dla właściciela",
   `contact_owner_enabled`, domyślnie ON) — tool wywoływany W TRAKCIE rozmowy gdy
   klient WPROST prosi o kontakt/oddzwonienie. Dopytuje imię+treść, wysyła mail.
   - Gemini Live/OpenAI Realtime: tool dynamicznie DOŁĄCZANY/POMIJANY w `tools[]`
     per rozmowa (`app/engines/common.py::build_call_tools`) — gdy wyłączony,
     structuralnie nie istnieje, model nie ma jak go wywołać.
   - ElevenLabs: tool jest STATYCZNIE przypięty do agenta w ich dashboardzie (nie da
     się go tworzyć/usuwać per-tenant), więc dołączanie/wyłączanie idzie przez
     `conversation_config_override.agent.prompt.tool_ids` (lista ID narzędzi,
     nadpisywana per rozmowa — patrz `CONTACT_OWNER_TOOL_ID` w
     `app/engines/elevenlabs/config.py`). **Wymaga włączonego przełącznika "Tools" w
     `platform_settings.overrides.conversation_config_override.agent.prompt.tool_ids`
     na agencie ElevenLabs** (ustawiane przez `PATCH /v1/convai/agents/{id}`, API
     key potrzebuje scope `ElevenAgents: Write`) — bez tego ElevenLabs po cichu
     ignoruje `tool_ids` z override. Dodatkowy hard block po stronie serwera
     (`elevenlabs_tool_contact_owner`) odmawia wysyłki nawet gdyby model i tak
     spróbował wywołać tool — defense in depth.
2. **Raport z rozmowy** (toggle "📧 Raport z rozmowy na email", `lead_email_enabled`,
   niezależny od powyższego) — **PO KAŻDEJ rozmowie**, niezależnie od jej wyniku,
   `generate_conversation_summary()`/`summarize_conversation_lines()`
   (`app/post_call/summary.py`) woła GPT-4.1-mini z kontekstem firmy (`tenant["additional_info"]`
   wstrzyknięte do promptu ekstrakcji) i zwraca ustrukturyzowane podsumowanie:
   priorytet (🚨 PILNE / 🔥 GORĄCY LEAD / 🟡 STANDARDOWE / —), kto dzwonił, powód,
   szczegóły istotne dla TEJ branży, wynik rozmowy. **Ta sama funkcja, identyczny
   efekt dla Gemini Live, OpenAI Realtime I ElevenLabs** — ElevenLabs konwertuje
   swój `transcript[]` (role `agent`/`user`) do tego samego formatu linii
   `"Klient: .../Asystent: ..."` przed wywołaniem, z fallbackiem na wbudowane
   streszczenie ElevenLabs (`analysis.transcript_summary`) gdyby nasze wywołanie
   zawiodło. To NIE jest narzędzie widoczne dla modelu w trakcie rozmowy — czysto
   serwerowy proces po zakończeniu połączenia, zero wpływu na to co bot mówi na żywo.

**`transfer_to_owner`** (toggle "Przekierowanie na numer", `transfer_enabled`) — żywe
przekierowanie NA NUMER w trakcie rozmowy. **Działa TYLKO na Vonage** w nowych
silnikach (`build_transfer_tool` w `app/tools/transfer.py`, wymaga prawdziwego Vonage call
uuid) — dla Twilio ten checkbox jest martwy w Gemini Live/OpenAI Realtime/ElevenLabs
(Twilio ma swój STARY mechanizm, tylko w cascade: `transfer_requests` + TwiML `<Dial>`
w `/twilio/after-stream`). Fallback gdy właściciel nie odbierze:
`/vonage/transfer-fallback` — wraca do bota zamiast ciszy + wysyła mail "nieodebrany
transfer", więc lead i tak nie ginie.

**Booking** (`book_appointment`/`manage_booking`, `booking_enabled`) — dostępny tylko
gdy DODATKOWO co najmniej jeden pracownik ma podłączony Google Calendar i przypisaną
usługę (identyczny wymóg jak cascade, sprawdzany osobno w każdym silniku żeby
zachowanie się nie rozjeżdżało dla tej samej konfiguracji tenanta).

### CRM Integration (n8n + Pipedrive) — kierunek rozwoju (od 2026-09-13)

**Cel:** każda rozmowa (niezależnie od silnika głosowego) ma trafiać jako
kontakt + notatka do CRM klienta (start: Pipedrive, docelowo dowolny CRM przez
n8n), NIE zastępując istniejącego raportu mailowego (`lead_email_enabled`) —
to dodatkowy, równoległy kanał, sterowany osobnym przełącznikiem per-tenant.

**Punkt zaczepienia (jeden, dla wszystkich aktywnych silników):**
`maybe_send_call_summary()` w `app/post_call/report.py` już dziś jest WSPÓLNYM
hookiem wołanym po KAŻDEJ rozmowie dla Gemini Live, OpenAI Realtime i
ElevenLabs (ElevenLabs konwertuje swój `transcript[]` do tego samego formatu
linii przed wywołaniem — patrz "Lead capture" wyżej). Niezależny od
Twilio vs Vonage — to warstwa telefonii, dispatch po `realtime_engine`
dzieje się wcześniej, zanim dojdzie do tego punktu. Planowany webhook do n8n
dokłada się TUTAJ, obok `send_call_summary_email` — osobna, nieblokująca
funkcja (własny try/except, krótki timeout, nigdy nie może wywrócić
zakończenia rozmowy), gated osobnym polem tenanta (np. `crm_enabled` +
`crm_provider`) analogicznie do `lead_email_enabled`.

(Cascade nie jest już częścią żadnego zakresu — usunięty 2026-09-29, patrz "Project Overview".)

**Przepływ danych:** backend (Railway) → POST webhook → n8n (workflow: znajdź
lub utwórz kontakt po numerze telefonu → dodaj notatkę/aktywność z
podsumowaniem rozmowy, tym samym tekstem co dziś idzie do maila) →
Pipedrive API. n8n jest warstwą routingu — dodanie kolejnego CRM dla innego
klienta to nowa gałąź w workflow n8n, nie zmiana w tym repo.

**Status:** faza nauki/POC (n8n + Pipedrive), zero klientów produkcyjnych na
tym jeszcze. Wdrażać najpierw jako jednokierunkowy zapis (rozmowa → CRM).
Dwukierunkowe wzbogacanie kontekstu rozmowy danymi z CRM (np. rozpoznanie
stałego klienta po numerze przed/w trakcie rozmowy) to świadomie OSOBNY,
późniejszy etap — wchodzi w krytyczną ścieżkę samej rozmowy, wymaga twardego
timeoutu i fallbacku gdy CRM/n8n nie odpowie na czas.

### Multi-Tenant Data

Two Turso (serverless SQLite) databases:

- **Admin DB** (`TURSO_DATABASE_URL`): manually-configured businesses. Tables: `tenants`, `services`, `staff`, `bookings`, `working_hours`, `call_logs`
- **SaaS DB** (`SAAS_TURSO_DATABASE_URL`): user-created businesses from web panel. Tenant IDs prefixed with `firm_`. Tables: `firms`, `credits`

`get_tenant_by_phone()` in `app/tenants.py` checks Admin DB first, then SaaS DB.

### Polish Language Handling

`app/polish/` (odmiana imion, rodzaj, formatowanie godzin/dat) i `app/booking/parsing.py`
(daty względne: "jutro", "w czwartek"). Wszystko krytyczne dla polskiego UX — zmiany
sprawdzaj scenariuszami w `tests/test_booking_flow.py`.

### SaaS Credit System

For `firm_` tenants, call cost is deducted from credit balance. Low-balance calls are rejected before the pipeline starts.

## Environment Variables

Pełna, opisana lista: `app/config.py` (klasa `Settings`) i `.env.example`. Nowa zmienna =
pole w `Settings` + wpis w `.env.example`; nigdzie indziej nie czytamy `os.getenv`.

## "Najpierw dzwoni do właściciela" v2 — przez apkę Siperb/SIP (2026-09-28)

**Historia:** v1 (Vonage Users API + appka WebRTC w `/crm`, zakładka "Telefon") USUNIĘTA
2026-09-28 — nie dzwoniła niezawodnie na zablokowanym telefonie (ograniczenia przeglądarki
w tle). v2 zamiast tego kieruje przez SIP do zewnętrznej, gotowej appki **Siperb**
(natywna integracja z systemem telefonicznym telefonu — CallKit/ConnectionService —
faktycznie dzwoni nawet zablokowany), potwierdzone na żywo 2026-09-27 po naprawie przez
support Siperb (literówka w polu Username + niedopasowana domena From w ich trunk
matching — Vonage wysyła INVITE z domeny `sip.nexmo.com`, więc "Serwer punktu końcowego"
połączenia w Siperb MUSI być ustawiony na `sip.nexmo.com:5060` UDP, nie na własną domenę
trunku Vonage typu `aisekretarka.sip-eu.vonage.com`).

**WAŻNE — brak automatyzacji, konfiguracja ręczna per klient:** Siperb NIE oferuje (na
razie sprawdzone) samoobsługowego, multi-tenant sposobu żeby wielu klientów bezpiecznie
współdzieliło jedno konto — nie ma pewności czy urządzenia zarejestrowane na tym samym
koncie są izolowane per połączenie (Connection) czy dzwonią wszystkie naraz niezależnie od
tego, do której trafiło połączenie przychodzące. Dlatego **każdy klient korzystający z tej
funkcji musi mieć OSOBNE, w pełni odrębne konto Siperb** (nie tylko osobne połączenie na
wspólnym koncie) — inaczej telefon klienta A mógłby usłyszeć połączenie klienta B.

**Sterowane 3 polami tenanta** (panel: `firm/[id]/page.tsx`, zakładka Ustawienia, sekcja
"Najpierw dzwoni do właściciela"): `human_first_enabled`, `human_first_timeout_seconds`,
`siperb_sip_username`. Wszystkie domyślnie 0/15/"" — zero zmiany zachowania dla żadnej
firmy dopóki ktoś świadomie NIE włączy przełącznika I NIE wypełni SIP username (backend
sprawdza oba warunki, patrz niżej). **KRYTYCZNE:** te pola muszą być jawnie przepisane w
`_firm_to_tenant` (`app/tenants.py`) — sam fakt istnienia kolumny w `firms` nie wystarczy
(ten sam błąd co przy `contact_owner_enabled`/`custom_report_format`, powtórzony już
kilka razy w historii tego pliku).

**Mechanizm (`app/telephony/vonage.py`):** `vonage_answer` sprawdza
`human_first_enabled` PRZED zbudowaniem zwykłej NCCO. Jeśli włączone, woła
`app/telephony/human_first.py::build_human_first_ncco`, które buduje NCCO `connect`→SIP→
`sip:<siperb_sip_username>@eu-west-1-sbc-1.siperb.com;transport=udp` z `eventType:
synchronous` + `eventUrl` → `/vonage/human-first-fallback` (identyczny, sprawdzony wzorzec
co `vonage_sip_fallback_elevenlabs` — Vonage odpytuje eventUrl NAWET przy sukcesie, ale
wtedy po prostu ignoruje zwróconą NCCO bo leg już żyje). Gdy `siperb_sip_username` jest
puste (przełącznik włączony, ale klient nie dokończył konfiguracji Siperb) —
`build_human_first_ncco` zwraca `None`, cicho spada na zwykłą ścieżkę AI, klient NIGDY nie
zostaje bez żadnej ścieżki połączenia.

⚠️ **`eu-west-1-sbc-1.siperb.com` jest na sztywno zakodowane** — to domena SBC z JEDYNEGO
przetestowanego konta Siperb (naszego, testowego, "siperb-bizvoice"). Nie potwierdzone czy
każde nowo zakładane konto Siperb dostaje tę samą domenę SBC (może zależeć od regionu
wybranego przy rejestracji) — przy PIERWSZYM prawdziwym kliencie sprawdź w jego apce
Siperb (Connections → jego połączenie → to pole) i popraw `build_human_first_ncco` jeśli
inne (np. przez dodanie kolejnego pola tenanta zamiast stałej).

**Nagrywanie + CRM dla rozmów odebranych osobiście (2026-09-28, DOKOŃCZONE):** gdy
właściciel odbierze przez Siperb, NCCO dokłada `record` (Vonage, `split=conversation`)
równolegle do `connect`/SIP. Po zakończeniu połączenia webhook
`/vonage/human-first-recording` woła `process_human_first_recording()`
(`app/telephony/human_first.py`) — pobiera nagranie (JWT auth), transkrybuje przez Deepgram
prerecorded API (`model=nova-3`, `multichannel=true` — kanał 0 → "Klient", kanał 1 →
"Właściciel", **mapowanie kanałów niepotwierdzone na żywym nagraniu**), podsumowuje tą
samą funkcją `summarize_conversation_lines()` co pozostałe silniki, i zapisuje do
`call_logs`/`call_transcripts` z `answered_by='owner'` — w panelu CRM taka rozmowa
dostaje znaczek "Ty" w tym samym widoku Zgłoszeń, nie osobną zakładkę. Niepotwierdzone
jeszcze na realnym połączeniu (brak testu end-to-end przez przełącznik
`human_first_enabled` z prawdziwym `siperb_sip_username` — dotychczasowy sukces był przez
tymczasowy endpoint testowy, nie przez tę produkcyjną ścieżkę).

Optional: `GROQ_API_KEY`, `CARTESIA_API_KEY`, `CEREBRAS_API_KEY`, Azure TTS credentials.
