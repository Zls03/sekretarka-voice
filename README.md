# sekretarka-voice

Głosowy asystent AI dla polskich firm usługowych (salony, siłownie, warsztaty, przychodnie). Odbiera połączenia telefoniczne, prowadzi naturalną rozmowę po polsku i obsługuje: umawianie wizyt, odpowiedzi na pytania FAQ, przekazywanie zgłoszeń serwisowych oraz przekierowania do właściciela.

System działa w architekturze multi-tenant SaaS — jeden backend obsługuje wiele firm jednocześnie, każda z własną konfiguracją głosu, przepływu rozmowy i integracji.

---

## Jak to działa

```
Klient dzwoni na numer Twilio
        │
        ▼
POST /twilio/incoming          ← Twilio webhook, zwraca TwiML z adresem WebSocket
        │
        ▼
WebSocket /ws/{call_sid}       ← Pipecat pipeline (real-time audio stream)
   ├─ Deepgram STT             ← rozpoznawanie mowy (polski)
   ├─ OpenAI / Groq LLM        ← rozumienie intencji, generowanie odpowiedzi
   ├─ FlowManager              ← maszyna stanów (Pipecat Flows)
   └─ TTS (ElevenLabs/Google/Azure/Cartesia)  ← synteza mowy
        │
        ▼
POST /twilio/after-stream      ← zapis logu rozmowy, naliczanie kredytów
```

---

## Funkcje

- **Rezerwacje** — wybór usługi, pracownika, daty i godziny; walidacja slotów w czasie rzeczywistym; zapis do bazy danych
- **FAQ** — odpowiedzi na pytania o ceny, godziny, lokalizację, płatności
- **Zgłoszenia serwisowe** — zbieranie opisu problemu, wysyłka e-mail do właściciela z priorytetem (pilne / normalne)
- **Przekierowanie** — transfer rozmowy do właściciela lub zostawienie wiadomości
- **Multi-tenant** — każda firma ma swój głos TTS, prompt systemowy, godziny pracy, listę usług i pracowników
- **Polskie NLP** — parsowanie dat względnych ("jutro", "w przyszły piątek"), odmiana nazw przez przypadki, korekcja STT

---

## Stos technologiczny

| Warstwa | Technologia |
|---------|-------------|
| Framework konwersacyjny | [Pipecat](https://github.com/pipecat-ai/pipecat) 0.0.104 |
| API serwera | FastAPI + uvicorn |
| Telefonia | Twilio (WebSocket audio, TwiML) |
| STT | Deepgram (model nova-3, język PL) |
| LLM | OpenAI GPT-4o-mini / Groq / Cerebras |
| TTS | ElevenLabs (domyślny) · Google Chirp3-HD · Azure Neural · Cartesia |
| Baza danych | Turso (serverless SQLite) — dwie instancje: Admin + SaaS |
| Hosting | Railway (backend) |

---

## Struktura plików

```
sekretarka-voice/
├── bot.py                    # FastAPI server, WebSocket pipeline, Twilio webhooks
├── flows.py                  # Główny przepływ rozmowy (greeting → booking/FAQ/contact)
├── flows_booking_simple.py   # Sub-flow rezerwacji (wieloetapowy: usługa→termin→potwierdzenie)
├── flows_contact.py          # Flow kontaktu z właścicielem i zgłoszeń serwisowych
├── flows_helpers.py          # Parsowanie polskich dat/godzin, integracje z API
├── helpers.py                # Klient Turso DB, lookup tenanta, szyfrowanie AES-GCM
├── polish_mappings.py        # Słowniki językowe (nazwy dni, miesięcy, odmiana imion)
├── constants.py              # Stałe: Urgency, TTSProvider, BookingField
├── services/
│   └── tts_factory.py        # Fabryka serwisów TTS (wybór providera per tenant)
└── schema.sql                # Schemat bazy danych
```

---

## Uruchomienie lokalne

```bash
# 1. Klonuj i wejdź do katalogu
git clone <repo-url>
cd sekretarka-voice

# 2. Środowisko wirtualne
python -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate

# 3. Zależności
pip install -r requirements.txt

# 4. Zmienne środowiskowe
cp .env.example .env           # uzupełnij kluczami API

# 5. Uruchom serwer
uvicorn bot:app --host 0.0.0.0 --port 8000
```

Do lokalnego testowania połączeń Twilio potrzebujesz tunelu (np. `ngrok http 8000`) i ustawienia webhooka w konsoli Twilio.

---

## Zmienne środowiskowe

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
ENCRYPTION_KEY                         # AES-GCM (tokeny Google OAuth)
GOOGLE_APPLICATION_CREDENTIALS_JSON   # konto usługowe Google TTS/Calendar (JSON)
PANEL_API_URL                          # URL panelu SaaS (domyślnie: http://localhost:3000)
RESEND_API_KEY                         # e-mail notyfikacje
```

Opcjonalne: `GROQ_API_KEY`, `CARTESIA_API_KEY`, `CEREBRAS_API_KEY`, `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION`.

---

## Multi-tenant

Dane firm pobierane są z dwóch źródeł:

- **Admin DB** (`TURSO_DATABASE_URL`) — firmy skonfigurowane ręcznie, tabele: `tenants`, `services`, `staff`, `bookings`, `working_hours`, `call_logs`
- **SaaS DB** (`SAAS_TURSO_DATABASE_URL`) — firmy założone przez panel webowy (prefix `firm_`), tabele: `firms`, `credits`

`get_tenant_by_phone()` w `helpers.py` sprawdza Admin DB, a jeśli nie znajdzie — SaaS DB.

---

## Połączone projekty

- **[bizvoice-panel](../bizvoice-panel)** — panel SaaS (Next.js) do zarządzania firmami, usługami, pracownikami i kredytami
