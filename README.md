# sekretarka-voice

Głosowy asystent AI dla polskich firm usługowych (salony, siłownie, warsztaty, przychodnie). Odbiera połączenia telefoniczne, prowadzi naturalną rozmowę po polsku i obsługuje: umawianie wizyt, odpowiedzi na pytania FAQ, przekazywanie zgłoszeń serwisowych oraz przekierowania do właściciela.

System działa w architekturze multi-tenant SaaS — jeden backend obsługuje wiele firm jednocześnie, każda z własną konfiguracją głosu, przepływu rozmowy i integracji. Ten plik to skrót — pełny, aktualny opis architektury jest w [`CLAUDE.md`](./CLAUDE.md), traktuj go jako źródło prawdy gdy coś tu wygląda nieaktualnie.

> **2026-09-29:** stary silnik "cascade" (Deepgram STT + LLM + Pipecat Flows, plik
> `bot.py` + towarzyszące `flows*.py`) został usunięty z repo — obsługiwał tylko firmy
> sprzed migracji na Gemini Live/OpenAI Realtime/ElevenLabs, a jego usługa Railowa
> (`web-production-f570f`) już wcześniej przestała istnieć. Cała logika, która była
> warta zachowania (rezerwacje, integracja z Deepgram), została już wcześniej 1:1
> przeniesiona do aktywnego kodu niżej. Historia w git, jeśli kiedyś potrzebna.

---

## Jak to działa

**Trzy silniki głosowe, wybierane per-firma polem `realtime_engine`** — dziś aktywne
dla realnych, płacących klientów: **Gemini Live** i **ElevenLabs** (OpenAI Realtime jako
sprawdzony fallback, nie promowany aktywnie).

```
                    Twilio/Vonage — połączenie przychodzące
                                │
                                ▼
                bot_gemini_test.py (orkiestrator, mimo nazwy)
                dispatch po tenant.realtime_engine
                    ├─ "gemini"      → Gemini Live (audio-to-audio, w tym pliku)
                    ├─ "openai"      → bot_openai_realtime.py (router)
                    └─ "elevenlabs"  → bot_elevenlabs_agent.py (router — agent
                                        skonfigurowany w dashboardzie ElevenLabs)
                                │
                                ▼
                realtime_tools.py: podsumowanie GPT-4.1-mini, zapis do CRM
                (call_logs/call_transcripts), naliczanie kredytów — wspólne
                dla wszystkich 3 silników
```

Jedna usługa Railway (`web-production-fb477`, start command `bot_gemini_test:app`
ustawiony bezpośrednio w Railway dashboardzie).

---

## Funkcje

- **Rezerwacje** — wybór usługi, pracownika, daty i godziny do Google Calendar; walidacja slotów w czasie rzeczywistym
- **FAQ** — odpowiedzi na pytania o ceny, godziny, lokalizację, płatności
- **Zgłoszenia / kontakt z właścicielem** — zbieranie wiadomości w trakcie rozmowy (`contact_owner`), przekierowanie na żywo (Vonage)
- **Podsumowanie + CRM** — po każdej rozmowie: priorytet, kto dzwonił, powód, wynik — trafia do panelu CRM i (opcjonalnie) mailem
- **Multi-tenant** — każda firma ma swój głos, prompt systemowy, godziny pracy, listę usług i pracowników
- **Polskie NLP** — parsowanie dat względnych ("jutro", "w przyszły piątek"), odmiana nazw przez przypadki, wykrywanie płci rozmówcy

---

## Stos technologiczny

| Warstwa | Technologia |
|---------|-------------|
| Silniki głosowe | Gemini Live (`gemini-3.1-flash-live-preview`) · ElevenLabs Conversational AI · OpenAI Realtime (`gpt-realtime-2.1-mini`, fallback) |
| Framework konwersacyjny | [Pipecat](https://github.com/pipecat-ai/pipecat) |
| API serwera | FastAPI + uvicorn |
| Telefonia | Twilio i Vonage (WebSocket audio / NCCO) |
| Baza danych | Turso (serverless SQLite) — dwie instancje: Admin + SaaS |
| Hosting | Railway |

---

## Struktura plików

```
sekretarka-voice/
├── bot_gemini_test.py         # Orkiestrator — Gemini Live, dispatch, webhooki
│                               Twilio+Vonage, montuje routery niżej
├── bot_openai_realtime.py     # Router OpenAI Realtime, montowany w bot_gemini_test.py
├── bot_elevenlabs_agent.py    # Most do ElevenLabs Conversational AI (agent w ich dashboardzie)
├── realtime_prompt.py         # Wspólny system prompt dla wszystkich 3 silników
├── realtime_tools.py          # Wspólne tools (contact_owner, transfer, itd.) + podsumowanie
│                               rozmowy (GPT-4.1-mini) + zapis do CRM
├── realtime_booking.py        # Rezerwacje (Google Calendar)
│
├── flows_helpers.py           # Parsowanie polskich dat/godzin, budowa kontekstu firmy
├── polish_mappings.py         # Słowniki językowe (dni, miesiące, odmiana imion, płeć)
├── helpers.py                 # Klient Turso DB, lookup tenanta (obie bazy), AES-GCM
├── constants.py                # TTSProvider enum — jedyne co jeszcze stąd realnie importuje
│                               services/tts_factory.py
├── services/
│   └── tts_factory.py         # Fabryka serwisów TTS
└── schema.sql                 # Historyczny snapshot schematu Admin DB — NIE aktualizowany
                                na bieżąco, realne kolumny dodawane przez lazy ALTER TABLE
                                w helpers.py/realtime_tools.py mogą się różnić
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
uvicorn bot_gemini_test:app --host 0.0.0.0 --port 8000
```

Do lokalnego testowania połączeń Twilio/Vonage potrzebujesz tunelu (np. `ngrok http 8000`) i ustawienia webhooka w konsoli Twilio/Vonage.

---

## Zmienne środowiskowe

```
DEEPGRAM_API_KEY                        # transkrypcja nagrań "najpierw dzwoni do właściciela"
OPENAI_API_KEY                          # GPT-4.1-mini — podsumowania rozmów
ELEVENLABS_API_KEY                      # agent ElevenLabs (scope ElevenAgents: Write)
TWILIO_ACCOUNT_SID
TWILIO_AUTH_TOKEN
TURSO_DATABASE_URL
TURSO_AUTH_TOKEN
SAAS_TURSO_DATABASE_URL
SAAS_TURSO_AUTH_TOKEN
ENCRYPTION_KEY                          # AES-GCM (tokeny Google OAuth)
GOOGLE_API_KEY                          # Gemini Live (Developer API, nie Vertex)
GOOGLE_APPLICATION_CREDENTIALS_JSON     # Google Calendar
VONAGE_APPLICATION_ID / VONAGE_PRIVATE_KEY   # JWT RS256 — transfer_to_owner, Siperb
ELEVENLABS_AGENT_ID                     # fallback gdy tenant nie ma własnego
ELEVENLABS_SHARED_SECRET                # opcjonalna weryfikacja webhooków ElevenLabs
VAPID_PRIVATE_KEY                       # web push do /crm po rozmowach z realną treścią
PANEL_API_URL                           # URL panelu SaaS (domyślnie: http://localhost:3000)
RESEND_API_KEY                          # e-mail notyfikacje
TEST_TENANT_ID                          # wymuszony tenant na ścieżce Vonage testowej
```

Opcjonalne: `GROQ_API_KEY`, `CARTESIA_API_KEY`, `CEREBRAS_API_KEY` — pełna lista z komentarzem do każdej w [`CLAUDE.md`](./CLAUDE.md#required-environment-variables).

---

## Multi-tenant

Dane firm pobierane są z dwóch źródeł:

- **Admin DB** (`TURSO_DATABASE_URL`) — firmy skonfigurowane ręcznie, tabele: `tenants`, `services`, `staff`, `bookings`, `working_hours`, `call_logs`
- **SaaS DB** (`SAAS_TURSO_DATABASE_URL`) — firmy założone przez panel webowy (prefix `firm_`), tabele: `firms`, `credits`

`get_tenant_by_phone()` w `helpers.py` sprawdza Admin DB, a jeśli nie znajdzie — SaaS DB.

---

## Połączone projekty

- **[bizvoice-panel](../bizvoice-panel)** — panel SaaS (Next.js) do zarządzania firmami, usługami, pracownikami i kredytami
