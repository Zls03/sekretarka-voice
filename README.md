# sekretarka-voice

Backend głosowej sekretarki AI **BizVoice** dla polskich firm usługowych (salony, gabinety,
warsztaty, siłownie). Odbiera połączenia telefoniczne, prowadzi rozmowę po polsku, umawia
i przekłada wizyty, odpowiada na pytania, przekazuje wiadomości właścicielowi i po każdej
rozmowie wysyła podsumowanie do panelu, e-mailem i do CRM.

Jeden backend obsługuje wiele firm (multi-tenant SaaS) — każda ma własny prompt, głos,
godziny, usługi, pracowników i włączone funkcje, konfigurowane w panelu
[bizvoice-panel](../bizvoice-panel).

---

## Architektura

```
  Twilio / Vonage (połączenie przychodzące)
          │
          ▼
  app/telephony/            webhooki operatorów — wybór silnika po tenant.realtime_engine
          │
          ├── "gemini"      app/engines/gemini_live/       Gemini Live, audio-to-audio (Pipecat)
          ├── "openai"      app/engines/openai_realtime/   OpenAI Realtime (Pipecat) — fallback
          └── "elevenlabs"  app/engines/elevenlabs/        agent ElevenLabs Conversational AI
          │                                               (SIP direct, most websocket lub Twilio)
          ▼
  wspólne dla wszystkich silników:
    app/prompt/      system prompt i powitanie z danych firmy
    app/tools/       narzędzia modelu: contact_owner, end_conversation, transfer_to_owner
    app/booking/     rezerwacje: dostępność, walidacja terminów, zapis w panelu, SMS
    app/post_call/   podsumowanie GPT, raport e-mail, synchronizacja CRM (n8n)
    app/billing.py   blokada przed startem rozmowy i rozliczenie minut/kredytów
```

Rozmowy Gemini Live i OpenAI Realtime biegną przez pipeline Pipecat na naszym serwerze
(`app/engines/common.py` — wspólne klocki: transport, VAD, narzędzia, prompt, zakończenie).
ElevenLabs prowadzi rozmowę u siebie; my wstrzykujemy konfigurację per rozmowa i
obsługujemy ich webhooki (personalizacja, narzędzia, post-call).

## Struktura

```
app/
├── main.py                  składanie aplikacji FastAPI (routery)
├── config.py                konfiguracja ze zmiennych środowiskowych (jedyne miejsce, które je czyta)
├── telephony/               twilio.py, vonage.py (webhooki), ncco.py, responses.py (TwiML/NCCO),
│                            vonage_api.py (REST API), human_first.py ("najpierw dzwoni do właściciela")
├── engines/
│   ├── common.py            CallFeatures, narzędzia, prompt, transport, VAD, finalize_call
│   ├── gemini_live/         llm, monitors (stan i ramki), watchdog (cisza/limit/zawieszenie), session, routes
│   ├── openai_realtime/     jw. dla OpenAI Realtime
│   └── elevenlabs/          config, conversation (nadpisania), webhooks, sip, twilio, vonage_bridge
├── tools/                   narzędzia function-calling + heurystyki odrzucające puste wywołania
├── booking/                 book_appointment, manage_booking, availability, parsing, panel_api, sms
├── post_call/               summary, report, crm_sync
├── prompt/                  instructions (system prompt), business_context
├── polish/                  grammar (odmiana imion, rodzaj), formatting (godziny, daty, listy)
├── notifications/           email (Resend), push (web push portalu /crm)
├── db.py · tenants.py · crm_contacts.py · panel_client.py · call_logs.py · billing.py
├── background.py            zadania w tle z logowaniem błędów
└── tts.py                   zapasowy TTS do komunikatów wypowiadanych dosłownie
tests/                       testy charakteryzujące (wzorce w tests/golden/)
docs/HISTORIA.md             historia decyzji i przebieg migracji
bot_gemini_test.py           shim zgodności: `uvicorn bot_gemini_test:app` (stara komenda startowa)
```

## Uruchomienie

```bash
python -m venv .venv && .venv\Scripts\activate    # Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
pip install "pipecat-ai[azure,cartesia,elevenlabs,openai,google,silero]==1.4.0" dateparser pywebpush openai
cp .env.example .env                               # uzupełnij klucze (patrz app/config.py)
uvicorn app.main:app --port 8000
```

Do testów z prawdziwym telefonem potrzebny jest publiczny adres (np. `ngrok http 8000`)
wpisany w konsoli Twilio/Vonage. Adresy webhooków (m.in. `/twilio/incoming-gemini-live-test`,
`/vonage/answer-gemini-live`) mają historyczne nazwy — są skonfigurowane u operatorów,
dlatego się ich nie zmienia.

## Testy i jakość

```bash
pytest                 # ~90 testów, ~20 s, bez sieci i bez prawdziwych kluczy
ruff check . && ruff format --check .
```

Testy są charakteryzujące (golden master): zapisują obecne zachowanie — odpowiedzi
webhooków, treść promptu, schematy narzędzi, skład pipeline'u na każdym websockecie,
scenariusze rozmowy rezerwacyjnej, mapowanie firm z bazy — i pilnują, żeby zmiana kodu
niczego nie przestawiła przypadkiem. Po świadomej zmianie zachowania wzorce odświeża się
przez `UPDATE_GOLDEN=1 pytest` i przegląda różnice w `git diff tests/golden`.

## Dane firm

- **Admin DB** (`TURSO_DATABASE_URL`) — firmy dodane ręcznie (`tenants`, `services`, ...).
- **SaaS DB** (`SAAS_TURSO_DATABASE_URL`) — firmy z panelu (`firms`, `credits`, id z prefiksem `firm_`).

`app/tenants.py::get_tenant_by_phone()` sprawdza najpierw bazę admina, potem SaaS, i zwraca
firmę w jednym, stałym kształcie. Nową kolumnę tabeli `firms` trzeba tam jawnie dopisać.

## Wdrożenie

Railway, jedna usługa. `Procfile`: `uvicorn app.main:app`. Stara komenda
`uvicorn bot_gemini_test:app` nadal działa dzięki shimowi.
