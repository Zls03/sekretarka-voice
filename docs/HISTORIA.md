# Historia decyzji — opisy modułów sprzed reorganizacji kodu (2026-09-30)

Poniżej dosłowne docstringi modułów z płaskiej struktury plików, zastąpionej pakietem `app/`.
Zawierają kontekst decyzji architektonicznych i przebieg migracji (cascade → silniki realtime).
Aktualny opis architektury: `README.md` i `CLAUDE.md`. Pełna historia zmian: `git log`.


## `bot_gemini_test.py`

bot_gemini_test.py — orkiestrator Fazy 1-2-4 migracji Cascade -> OpenAI Realtime
(patrz CLAUDE.md). FastAPI, webhooki, websockety, transport, monitoring/idle.

```text
Historia: ten plik powstał jako izolowany test latencji audio-to-audio (Gemini Live
vs OpenAI Realtime) na tenantcie testowym (firm_1774140338448_8905c, Vonage). Wyniki
(patrz tabelka w CLAUDE.md) przesądziły o wyborze OpenAI Realtime (gpt-realtime-2.1-mini,
~0.6s user->bot, wszystko 🟢). Od tego commitu plik realizuje Fazy 1-2-4 planu migracji:
prawdziwy system prompt z danych panelu (cennik, godziny, adres, FAQ, ton branży,
tożsamość asystenta) + personalizacja powitania dla powracającego klienta (CRM),
wykrywanie ciszy (dopytanie/rozłączenie) i limit czasu rozmowy, oraz function-calling
(contact_owner, end_conversation).

PODZIAŁ NA PLIKI (zrobiony gdy ten plik przekroczył ~1200 linii, przed Fazą 3, żeby
nie robiło się jeszcze gorzej — Faza 3 dopisze rezerwacje, najbardziej złożoną część):
  - bot_gemini_test.py (TEN plik) — orkiestrator Gemini Live: FastAPI (app), webhooki
    Vonage współdzielone z OpenAI Realtime, dispatch/websockety Gemini Live.
  - gemini_live_pipeline.py — wydzielone 2026-09-30 (po usunięciu cascade, plik znów
    urósł do ~1750 linii): stałe, monitory ciszy/zdrowia sesji, budowa LLM Gemini Live —
    czyste funkcje/klasy bez FastAPI, zero endpointów.
  - bot_openai_realtime.py — orkiestrator OpenAI Realtime (webhooki, websockety,
    monitoring/idle/latencja tej ścieżki), wydzielony z tego pliku 2026-08-24 wyłącznie
    dla czytelności, patrz jego docstring. Montowany tu przez app.include_router().
  - realtime_prompt.py — budowanie system_instruction (tożsamość/styl/biznes/CRM/greeting).
    Odpowiednik roli flows_helpers.py.
  - realtime_tools.py — function-calling tools (contact_owner, end_conversation, wysyłka
    emaila). Odpowiednik roli flows_contact.py / flows_booking_simple.py.

KOLEJNOŚĆ FAZ ŚWIADOMIE ODWRÓCONA względem CLAUDE.md: Faza 4 (kontakt/zgłoszenia) PRZED
Fazą 3 (rezerwacje) — booking jest najbardziej ryzykowną częścią (błąd = podwójna
rezerwacja/zmyślony termin) i wymaga jeszcze podpięcia Google Calendar, więc lepiej
dopracować prostszy tryb informacyjny i function-calling na niższą stawkę (contact_owner)
zanim zabierzemy się za booking.

Gemini Live wcześniej USUNIĘTY (decyzja zapadła na rzecz OpenAI Realtime) — ale
DOŁOŻONY z powrotem na końcu pliku (sekcja "-gemini-live") jako doraźny, ubogi test
porównawczy (2026-08-11), bo Gemini wypuściło gemini-3.1-flash-live-preview i warto
sprawdzić latencję. Świadomie osobne route'y, zero ingerencji w OpenAI Realtime
(bot_openai_realtime.py od 2026-08-24) — patrz docstring tamtego pliku.

NIE dotyka produkcyjnego bot.py. Zero FlowManagera. Treść promptu (realtime_prompt.py)
jest ŚWIADOMIE skopiowana z flows.py/flows_helpers.py zamiast zaimportowana stamtąd
wprost — te moduły ciągną `pipecat_flows`, spięty z pipecat-ai==0.0.104 (stary kontekst
OpenAILLMContext) — import pod pipecat-ai==1.4.0 (wymagany tu do OpenAIRealtimeLLMService)
byłby kruchy. Patrz docstring realtime_prompt.py po pełne wyjaśnienie.

WYMAGANE ZMIENNE ŚRODOWISKOWE (te same co w Railway):
  OPENAI_API_KEY       — klucz OpenAI Realtime
  TWILIO_AUTH_TOKEN    — do walidacji podpisu Twilio (opcjonalnie, można pominąć na testach)
  TEST_TENANT_ID        — wymuszony tenant dla ścieżki Vonage (patrz /vonage/answer)
  RESEND_API_KEY        — do wysyłki emaila w contact_owner (Faza 4) — bez tego funkcja
                          zwróci klientowi uczciwy błąd zamiast fałszywie potwierdzić wysyłkę

PODŁĄCZENIE (Twilio):
  1) W ustawieniach numeru: "A call comes in" -> Webhook (POST)
     https://<twoj-railway-host>/twilio/incoming-gemini-live-test
  2) "Primary handler fails" -> Webhook (POST), ten sam host, /twilio/fallback
     — UWAGA: tej trasy NIE MA w tym pliku (nieużywana), zostaw puste jeśli Twilio wymaga.
  3) "Call status changes" -> Webhook (POST)
     https://<twoj-railway-host>/twilio/status
     — bez tego apply_call_charge() nigdy się nie odpala dla połączeń Twilio (kredyty/minuty
     się nie naliczają, patrz handler /twilio/status niżej, dodany 2026-08-31).

PODŁĄCZENIE (Vonage): patrz sekcja "VONAGE" niżej — bez zmian względem wcześniejszej wersji.

URUCHOMIENIE OBOK ISTNIEJĄCEGO bot.py:
  Ten plik ma WŁASNY obiekt FastAPI (app), własny osobny deploy na Railway
  (requirements-gemini-test.txt, pipecat-ai==1.4.0 — CELOWO inna wersja niż produkcyjny
  bot.py na 0.0.104). Nie instalować obu requirements w tym samym środowisku.
  Start command bez zmian: `uvicorn bot_gemini_test:app` — realtime_prompt.py i
  realtime_tools.py to zwykłe pliki .py w tym samym repo, żadna konfiguracja Railway
  nie musi się zmienić.

CO ZOSTAJE NA PÓŹNIEJ (świadomie NIE tutaj) — STAN NA COMMIT TWORZĄCY TEN PLIK, NIEAKTUALNE:
  - Reszta Fazy 4: zbieranie zgłoszeń (lead collection, wieloturowe), SMS, raport email po rozmowie
  - Żywe przekierowanie rozmowy (transfer) — ani dla Twilio (brak /twilio/after-stream w tym
    pliku) ani dla Vonage (brak mechanizmu w ogóle, wymaga Vonage REST API) — patrz docstring
    realtime_tools.py. Działa TYLKO ścieżka "zostaw wiadomość" (email przez Resend).
  - Faza 3: sprawdz_dostepnosc()/zarezerwuj() jako function-calling tools (ostatnia, bo
    najbardziej ryzykowna — patrz wyżej)
  - Faza 5: credits + call_logs
  ⚠️ POPRAWKA 2026-08-22: powyższe jest już NIEAKTUALNE — booking (book_appointment/
  manage_booking) i transfer (transfer_to_owner, Vonage) SĄ zaimplementowane, patrz has_booking/
  has_transfer w realtime_prompt.py i build_gemini_live_llm/build_contact_owner_tool niżej. Są
  to jednak per-TENANT przełączniki (booking_enabled, transfer_enabled), nie "jeszcze w budowie"
  globalnie — gdy wyłączone na danym tenancie, prompt ma mówić że opcja "nie jest włączona na tej
  linii", NIE że jest w budowie/to wersja testowa (był tu bug: sztywny tekst "jeszcze w budowie"
  mylił brak-per-tenanta z brakiem-funkcji-w-ogóle, złapane na demo-tenancie BizVoice).

FAZA 2 — jak działa wykrywanie ciszy/limitu (patrz monitor_call_health poniżej):
  10s ciszy -> "Przepraszam, czy nadal jesteśmy połączeni?" | 20s ciszy -> pożegnanie + rozłączenie
  | 4 min rozmowy - 30s -> uprzedzenie że kończymy | 4 min -> pożegnanie + rozłączenie.
  Realizowane przez say_now() (response.create z jednorazowym `instructions`), bo
  TTSSpeakFrame/LLMMessagesAppendFrame z cascade NIE działają z tą usługą (patrz
  komentarz przy say_now).
  Rozłączenie po pożegnaniu NIE czeka na realny koniec odtwarzania audio (Realtime
  nie daje eventu "TTS na pewno skończył mówić widziany z zewnątrz w porę do tego") —
  to stały sleep(3.0) po wysłaniu polecenia, potem EndFrame. Cascade (bot.py) robi
  DOKŁADNIE to samo (sleep 2.0-2.5s), więc to nie uproszczenie względem produkcji,
  tylko ten sam, już sprawdzony trik.
```


## `bot_openai_realtime.py`

bot_openai_realtime.py — sekcja OpenAI Realtime, wydzielona z bot_gemini_test.py
(ten plik urósł do ~2077 linii mieszając dwie niezależne implementacje; Gemini Live
jest teraz aktywnie rozwijaną/testowaną ścieżką, a OpenAI Realtime to sprawdzony
fallback — patrz CLAUDE.md, sekcja "PIVOT"). APIRouter, nie własny FastAPI app —
montowany w bot_gemini_test.py przez app.include_router(router).

```text
Historia: ten plik był wcześniej częścią bot_gemini_test.py (linie ~163-1072, sekcja
"OpenAI Realtime"). Wydzielony 2026-08-24 wyłącznie dla czytelności/utrzymania — ZERO
zmian w logice. Zweryfikowane przed podziałem: sekcja OpenAI Realtime i sekcja Gemini
Live (zostająca w bot_gemini_test.py) nie mają między sobą żadnych faktycznych
wywołań — jedyne odwołania w drugą stronę to komentarze/docstringi.

Dwa route'y NIE trafiły tutaj mimo że fizycznie siedziały w tej sekcji: /vonage/events
i /vonage/transfer-fallback. Obsługują połączenia z OBU providerów (billing, logi,
transcript, fallback po nieudanym transferze) — zostają w bot_gemini_test.py, tam gdzie
`app`.

WYMAGANE ZMIENNE ŚRODOWISKOWE (te same co w Railway):
  OPENAI_API_KEY       — klucz OpenAI Realtime
  TWILIO_AUTH_TOKEN    — do walidacji podpisu Twilio (opcjonalnie, można pominąć na testach)
  TEST_TENANT_ID        — wymuszony tenant dla ścieżki Vonage (patrz /vonage/answer)
  RESEND_API_KEY        — do wysyłki emaila w contact_owner — bez tego funkcja
                          zwróci klientowi uczciwy błąd zamiast fałszywie potwierdzić wysyłkę

PODŁĄCZENIE (Twilio):
  1) Wybierz numer testowy w konsoli Twilio (osobny lub tymczasowo przełącz istniejący)
  2) W ustawieniach numeru: "A call comes in" -> Webhook
     POST https://<twoj-railway-host>/twilio/incoming-gemini-test
  3. To wystarczy — nic więcej w konfiguracji Twilio nie trzeba zmieniać.

PODŁĄCZENIE (Vonage): patrz sekcja "VONAGE" niżej.

FAZA 2 — jak działa wykrywanie ciszy/limitu (patrz monitor_call_health poniżej):
  6s ciszy -> "Przepraszam, czy nadal jesteśmy połączeni?" | 14s ciszy -> pożegnanie + rozłączenie
  | 4 min rozmowy - 30s -> uprzedzenie że kończymy | 4 min -> pożegnanie + rozłączenie.
  Realizowane przez say_now() (response.create z jednorazowym `instructions`), bo
  TTSSpeakFrame/LLMMessagesAppendFrame z cascade NIE działają z tą usługą (patrz
  komentarz przy say_now).
  Rozłączenie po pożegnaniu NIE czeka na realny koniec odtwarzania audio (Realtime
  nie daje eventu "TTS na pewno skończył mówić widziany z zewnątrz w porę do tego") —
  to stały sleep(3.0) po wysłaniu polecenia, potem EndFrame. Cascade (bot.py) robi
  DOKŁADNIE to samo (sleep 2.0-2.5s), więc to nie uproszczenie względem produkcji,
  tylko ten sam, już sprawdzony trik.
```

```text
Vonage nie ma pojedynczego pola "webhook" na numerze — numer musi być
przypisany do Vonage "Application" (Voice), a ta aplikacja ma:
  - Answer URL (GET)  -> tu zwracamy NCCO (JSON, nie TwiML)
  - Event URL (POST)  -> status callback (odpowiednik Twilio /twilio/status,
    ale wspólny dla obu providerów — patrz vonage_events w bot_gemini_test.py)

Audio idzie jako surowe PCM 16-bit (nie base64 mu-law jak w Twilio),
dlatego osobny websocket + VonageFrameSerializer zamiast TwilioFrameSerializer.
Tenant przekazujemy przez query param w URI websocketu (Vonage na to pozwala),
więc nie trzeba parsować żadnego eventu "start" jak w Twilio.
```


## `bot_elevenlabs_agent.py`

bot_elevenlabs_agent.py — MVP integracja z ElevenLabs Conversational AI (ElevenAgents).
APIRouter, nie własny FastAPI app — montowany w bot_gemini_test.py przez
app.include_router(router), tak samo jak openai_realtime_router.

```text
Rozmowa NIE leci przez nasz Pipecat pipeline (w odróżnieniu od Gemini Live/OpenAI
Realtime powyżej) — agenta (prompt/głos/LLM) konfigurujesz RĘCZNIE w dashboardzie
ElevenLabs (elevenlabs.io -> Agents). Te trzy endpointy to WYŁĄCZNIE mostek między ich
platformą a naszymi danymi/logiką biznesową, wołany PRZEZ ElevenLabs, nie przez nas:

1. POST /elevenlabs/personalization — "conversation initiation client data" webhook.
   ElevenLabs woła to PRZED startem rozmowy (równolegle z łączeniem Twilio, więc klient
   słyszy sygnał łączenia zamiast ciszy) i dostaje w odpowiedzi override promptu +
   pierwszej wiadomości, zbudowane z panelu tak samo jak dla Gemini Live/OpenAI Realtime
   (reużywamy build_realtime_instructions/build_greeting_message 1:1). Skonfiguruj w
   dashboardzie agenta: Settings -> Advanced -> "Fetch conversation initiation data from
   webhook", URL: https://<railway-host>/elevenlabs/personalization.

2. POST /elevenlabs/tools/contact_owner — webhook tool wywoływany PRZEZ agenta w trakcie
   rozmowy (jak contact_owner w Gemini Live/OpenAI Realtime). Skonfiguruj w dashboardzie
   agenta: Tools -> Add tool -> Webhook, method POST, URL jak wyżej + "/tools/contact_owner",
   body params: customer_name (string), message (string), called_number (string, wartość
   {{system__called_number}} jeśli ta zmienna systemowa istnieje w Twoim workspace —
   sprawdź w edytorze zmiennych po prawej), caller_phone (string, {{system__caller_id}}).

3. POST /elevenlabs/post-call — post-call webhook (Settings -> Webhooks na poziomie CAŁEGO
   workspace, nie per-agent) — nalicza minuty/kredyty tym samym apply_call_charge() co
   Gemini Live/OpenAI Realtime.

4. build_register_call_twiml() — WOŁANE PRZEZ NAS (odwrotny kierunek niż 1-3), z
   twilio_incoming_gemini_live_test() w bot_gemini_test.py, gdy tenant["realtime_engine"]
   == "elevenlabs". Powód istnienia: "Importuj numer -> Z Twilio" w dashboardzie ElevenLabs
   miało (wg ich docs) samo nadpisać webhook Twilio numeru na ich infrastrukturę — na żywo
   sprawdzone 2026-09-02 że NIE nadpisuje (numer nadal wskazywał na nasz Railway po dwóch
   próbach importu), a ręczne wpisanie ich webhooka było niemożliwe bez udokumentowanego
   adresu (ryzyko całkowitego wyłączenia numeru realnego klienta przy błędnym zgadnięciu).
   To "bring your own Twilio" API (conversational_ai.twilio.register_call, patrz
   elevenlabs.io/docs/eleven-agents/phone-numbers/twilio-integration/register-call)
   odwraca kierunek: Twilio zostaje podpięty pod NASZ webhook (nic nie trzeba zmieniać w
   Twilio ani importować numeru do ElevenLabs), a MY przy każdym połączeniu wołamy ich API
   z agent_id + from/to number, dostajemy z powrotem TwiML i przekazujemy je Twilio 1:1.
   Prompt/pierwsza wiadomość budowane 1:1 jak w (1), ale przekazane INLINE w
   conversation_initiation_client_data zamiast osobnego webhooka — mniej round-tripów.

⚠️ AUTORYZACJA — DWIE RÓŻNE, ŻADNA NIEZWERYFIKOWANA NA ŻYWYM WEBHOOKU W MOMENCIE
NAPISANIA (2026-09-02):
- (1) i (2): prosty shared secret w nagłówku (ELEVENLABS_SHARED_SECRET) — ustaw ten sam
  string w Railway i w polu "Custom header" przy konfiguracji webhooka/tool w dashboardzie
  ElevenLabs (nagłówek x-bizvoice-secret). Jeśli ELEVENLABS_SHARED_SECRET puste, check jest
  pomijany (celowo, żeby dało się to podłączyć i przetestować ZANIM ustalisz sekret) —
  DOPISZ go przed jakimkolwiek użyciem produkcyjnym.
- (3) ma osobny mechanizm: HMAC podpis w nagłówku "elevenlabs-signature". Dokumentacja
  ElevenLabs nie podaje dokładnego formatu tego nagłówka/schematu podpisu bez ich SDK
  (którego tu nie ma jako zależności) — na razie TYLKO logujemy nagłówek i całe body przy
  odbiorze, żeby dopisać realną weryfikację na podstawie prawdziwych danych z pierwszego
  live webhooka, zamiast zgadywać format i dawać fałszywe poczucie bezpieczeństwa.

Wszystkie trzy endpointy defensywnie parsują pola z wielu możliwych nazw (np.
called_number/to_number) i szeroko logują surowe body — dokumentacja ElevenLabs nie
pokazuje pełnych przykładowych payloadów dla (1) i (2), więc dokładne nazwy pól
potwierdzimy dopiero na pierwszym prawdziwym połączeniu telefonicznym.
```


## `gemini_live_pipeline.py`

gemini_live_pipeline.py — wydzielone z bot_gemini_test.py (2026-09-30), krok 1
porządkowania po usunięciu cascade (patrz CLAUDE.md "Project Overview").

```text
Czyste funkcje/klasy budujące i monitorujące sesję Gemini Live — zero zależności od
FastAPI/app, zero endpointów. Wydzielone jako pierwszy, najniższego ryzyka krok
podziału bot_gemini_test.py (ten plik ma zero mutowalnego stanu na poziomie modułu i
nic go nie importuje z powrotem z bot_gemini_test.py — bezpieczne do przenoszenia).

Eksportuje: GEMINI_LIVE_MODEL, make_gemini_state, GeminiUserMonitor, GeminiBotMonitor,
speak_directly, monitor_gemini_call_health, build_gemini_live_llm — używane przez
websockety Gemini Live (Twilio i Vonage) w bot_gemini_test.py.
```

```text
Cel: TYLKO zmierzyć latencję/jakość gemini-3.1-flash-live-preview na tym samym
tenancie testowym, do porównania z OpenAI Realtime (patrz bot_openai_realtime.py).
Świadomie ubogie względem tamtej sekcji — bez tools (contact_owner/submit_lead/
end_conversation), bez idle-timeout, bez CRM w tle. To NIE jest kandydat do
rozbudowy 1:1 — jeśli Gemini Live wygra test latencji, wtedy dopiero warto
dociągnąć brakujące funkcje analogicznie do bot_openai_realtime.py.

Osobne route'y (inna ścieżka niż OpenAI Realtime) — NIC z bot_openai_realtime.py nie
jest ruszane. Żeby faktycznie przetestować, trzeba w konsoli Vonage/Twilio ręcznie
przełączyć Answer URL/webhook na endpoint poniżej, i przełączyć z powrotem po
teście.

GeminiLiveLLMService NIE emituje UserStartedSpeakingFrame/UserStoppedSpeakingFrame
(server VAD Gemini nie ma odpowiednika tych zdarzeń w pipecat, patrz docstring
serwisu) — pierwotnie pomiar latencji niżej kotwiczył się więc o TranscriptionFrame
(moment dotarcia transkrypcji). POPRAWKA 2026-08-18: to dawało fałszywie niskie
liczby — na żywej rozmowie TranscriptionFrame przychodził ~2.8s PO realnym końcu
mowy (Gemini batchuje/opóźnia transkrypcję), więc "TOTAL user->bot audio" pokazywał
np. 286ms, podczas gdy realny TTFB logowany przez GeminiLiveLLMService (i policzony
ręcznie z timestampów VAD-stop -> pierwsze audio bota) wynosił 3.1s. Anchor
przełączony na VADUserStoppedSpeakingFrame z lokalnego VADProcessor (patrz pipeline
niżej — analizuje audio lokalnie, niezależnie od Gemini) — to ten sam sygnał co
GeminiUserMonitor już i tak używa do odświeżania idle_since na starcie mowy
(VADUserStartedSpeakingFrame), więc żadnej nowej zależności nie dokłada.
```


## `realtime_tools.py`

realtime_tools.py — function-calling tools dla OpenAI Realtime (Faza 4 planu
migracji, patrz CLAUDE.md). Wydzielone z bot_gemini_test.py.

```text
CONTACT_OWNER — pierwsza funkcja Fazy 4 (kolejność Faz 3/4 odwrócona świadomie —
patrz docstring bot_gemini_test.py: booking jest ryzykowniejszy, więc zostaje na koniec).

TYLKO ścieżka "zostaw wiadomość" — działa dla Twilio I Vonage jednakowo (samo
wysłanie emaila nie zależy od dostawcy telefonii). Żywe przekierowanie rozmowy
(transfer) ŚWIADOMIE pominięte na razie:
  - w cascade transfer dla Twilio idzie przez dwuetapowy trik (zapis do
    transfer_requests + TwiML <Dial> w /twilio/after-stream), którego bot_gemini_test.py
    w ogóle nie ma (brak własnego /twilio/after-stream)
  - dla Vonage nie ma GOTOWEGO mechanizmu wcale — wymagałby osobnego wywołania
    Vonage REST API na żywym połączeniu (patrz docstring bot.py przy sekcji VONAGE)
  To jest dokładnie ta granica, którą plan w CLAUDE.md już wcześniej zaakceptował:
  "acceptable to ship 'leave a message only' for Vonage at first".

END_CONVERSATION — global-function odpowiednik end_conversation_function() z cascade
(flows.py) — bez tego bot nie ma ŻADNEGO sposobu żeby rozpoznać koniec rozmowy inaczej
niż przez ciszę (patrz bot_gemini_test.py::monitor_call_health) — klient mówiący
"dziękuję, to wszystko" po prostu wisiałby w rozmowie aż zadziała idle timeout.

send_message_email() jest SKOPIOWANA z flows.py, nie zaimportowana — ten sam powód co
reszta promptu (patrz docstring realtime_prompt.py): flows.py ciągnie pipecat_flows,
niekompatybilne z pipecat-ai==1.4.0 użytym w tym serwisie.

RAPORT Z ROZMOWY (Faza 5, pierwszy kawałek) — działa PO KAŻDEJ rozmowie, niezależnie
od tego czy klient czegoś konkretnego chciał (to różni się od contact_owner, który
wysyła tylko gdy klient WPROST poprosił o kontakt/wiadomość). Bramkowane DOKŁADNIE tym
samym polem co w cascade (bot.py, sekcja "Lead email po rozmowie"): `lead_email_enabled`
+ `lead_email` (lub `notification_email` jako fallback) — to jest to samo pole co
checkbox "Raport z rozmowy na email" w panelu, więc zero nowej konfiguracji potrzebne.
```


## `realtime_booking.py`

realtime_booking.py — Faza 3 planu migracji (CLAUDE.md): rezerwacje jako function-calling
tool, współdzielony między OpenAI Realtime I Gemini Live (ten sam wzorzec co
realtime_tools.py/realtime_prompt.py — jeden plik, oba providery).

```text
Port z flows_booking_simple.py (cascade, pipecat_flows) — CAŁA logika walidacji kroków
(usługa → pracownik → data → godzina → imię → uwagi → potwierdzenie → zapis) przeniesiona
~1:1. Zmienione tylko I/O: flow_manager.state["booking"] → call_state["booking"] (ten sam
call_state/gemini_state dict co reszta realtime_tools.py), TTSSpeakFrame+node → zwrot przez
FunctionCallParams.result_callback.

ARCHITEKTURA — JEDNO stanowe narzędzie, NIE dwa. CLAUDE.md nazywa fazę 3
"sprawdz_dostepnosc()/zarezerwuj()" (dwa bezstanowe tools) — świadomie NIE tak zrobione:
ten serwis (bot_gemini_test.py) nie ma FlowManager/przełączania node'ów jak cascade,
wszystkie tools są zawsze widoczne naraz. Gdyby dyscyplinę kolejności kroków (usługa
PRZED datą, data PRZED godziną, itd.) zostawić dwóm luźnym tools + samemu promptowi, to
dokładnie to czego cascade świadomie unika (patrz komentarz przy "confirmation" niżej:
"Handler nie zależy od LLM że wpisze zgodę"). Zamiast tego: JEDNO FunctionSchema
(book_appointment) wywoływane co turę, cała dyscyplina w Pythonie.

MODEL NIE IMPROWIZUJE PRZY DATACH/CENACH/GODZINACH. Każdy wynik niesie pole "say_exactly"
— dokładny, z góry obliczony polski tekst. Opis narzędzia (description) wymusza żeby model
powtórzył go SŁOWO W SŁOWO, bez własnych dodatków. To zastępuje _respond() z cascade
(które wypychało TTSSpeakFrame bezpośrednio, z pominięciem generowania przez LLM).
say_now/gemini_say_now (bot_gemini_test.py) NIE nadają się tutaj — to mechanizm do
jednorazowego zagajenia POZA aktywnym kontekstem rozmowy (LLMMessagesAppendFrame z
run_llm=True na nowo budowanym turn), nie do powtarzanego użycia w środku wieloturowej
rozmowy z już podłączonymi context aggregatorami.

PODWÓJNA WALIDACJA SLOTU zostaje (świeży fetch z get_available_slots_from_api tuż przed
zapisem, dokładnie jak _save_booking() w cascade) — PLUS nowa warstwa: POST
/api/panel/{slug}/bookings może teraz zwrócić 409 {"error": "slot_taken"} (unique index
na bookings(staff_id, booking_date, booking_time) dodany w bizvoice-panel w tej samej
sesji) gdy dwie równoległe rozmowy trafią w dokładnie ten sam termin między walidacją a
zapisem — obsłużone identycznie jak nieudana re-walidacja: klientowi proponowany jest
najbliższy inny wolny termin, nie generyczny błąd.

CO ŚWIADOMIE NIE ZOSTAŁO PRZENIESIONE (i dlaczego):
- start_booking_function_simple/handle_start_booking_simple — osobna funkcja startowa
  cascade do pre-wypełniania stanu z pierwszego zdania klienta. W TEJ architekturze
  WSZYSTKIE pola book_appointment są dostępne od razu przy pierwszym wywołaniu (nie ma
  osobnego "wejścia" do trybu rezerwacji) — pre-fill "z pierwszego zdania" to dokładnie
  ten sam kod co pre-fill przy KAŻDYM innym wywołaniu (już obsłużone niżej: sekcje 1-4
  akceptują dowolne pola niezależnie od tego, które to wywołanie z kolei).
- Flaga "_jak_ostatnio" (scripted propozycja "jak ostatnio" dla powracającego klienta,
  inicjowana w handle_start_booking_simple na podstawie client_profile) — tutaj
  client_profile/CRM (last_service/last_staff) już trafia do system promptu (patrz
  realtime_prompt.py::_build_crm_hint) i model MOŻE naturalnie zaproponować "jak
  ostatnio" własnymi słowami, PRZED wywołaniem book_appointment. Gdy klient się zgodzi,
  model wywoła book_appointment z service/staff już wypełnionymi — ten kod obsłuży to
  identycznie jak każde inne pre-wypełnione wywołanie. Nie wymaga osobnego mechanizmu
  (i nie da się bezpiecznie odtworzyć bez sygnału z handle_start_booking_simple, którego
  tu nie ma).
- "soft_interest" (przekazywanie stanu z osobnej funkcji check_availability z cascade) —
  ta funkcja nie istnieje w architekturze Realtime/Gemini Live (poza zakresem tego
  zadania), więc nie ma skąd tego przekazać.
- play_snippet("checking"/"saving") — dźwiękowe wypełniacze cascade podczas wolniejszych
  wywołań API. Brak odpowiednika w tym serwisie (contact_owner też tego nie ma) —
  pominięte, nie jest to poprawnościowe, tylko kosmetyczne.
- fuzzy_match_service/fuzzy_match_staff — używane w cascade WYŁĄCZNIE w pominiętej wyżej
  funkcji startowej. W handle_book_appointment (tej faktycznie portowanej logice) dopasowanie
  usługi/pracownika jest ZAWSZE dokładne (exact match), bo pole "service"/"staff" ma
  "enum" z listą prawdziwych nazw — API function-calling samo wymusza że model może
  zwrócić TYLKO wartość z listy, więc fuzzy matching po naszej stronie jest zbędny.
```


## `realtime_prompt.py`

realtime_prompt.py — budowanie system_instruction dla OpenAI Realtime (Faza 1 planu
migracji, patrz CLAUDE.md). Wydzielone z bot_gemini_test.py, żeby ten plik nie rósł
w nieskończoność (Faza 3 dopisze jeszcze rezerwacje).

```text
Treść promptu jest ŚWIADOMIE skopiowana z flows.py::create_initial_node /
flows_helpers.py::build_business_context, zamiast zaimportowana stamtąd wprost —
flows.py ciągnie `pipecat_flows`, który jest spięty z pipecat-ai==0.0.104 (stary
kontekst OpenAILLMContext). Ten serwis (bot_gemini_test.py) siedzi na pipecat-ai==1.4.0
(wymagany przez OpenAIRealtimeLLMService) — import wprost z flows.py byłby kruchy
i mógłby się wywalić na starcie. flows_helpers.py i polish_mappings.py NIE mają
żadnych zależności od pipecat, więc te importujemy bezpośrednio poniżej — to jedyne
bezpieczne, tożsame źródło prawdy dla treści promptu.
```


## `helpers.py`

```text
VOICE AI - HELPERS
==================
Obsługuje dwie bazy Turso:
- Baza ADMINA  (TURSO_DATABASE_URL)      → tabela tenants (ręcznie dodawane firmy)
- Baza SaaS    (SAAS_TURSO_DATABASE_URL) → tabela firms   (firmy z panelu użytkowników)

Funkcja get_tenant_by_phone() sprawdza obie bazy.
Admina ma priorytet — jeśli numer znajdzie się w obu, wygrywa admin.
```


## `flows_helpers.py`

flows_helpers.py - Funkcje pomocnicze dla Pipecat Flows
WERSJA 1.1 - Dodano eksport get_available_slots_from_api

```text
Zawiera:
- Parsowanie dat i godzin (polskie)
- Formatowanie po polsku
- Integracja z API panelu (kalendarz, rezerwacje)
- Walidacje
```


## `polish_mappings.py`

polish_mappings.py - Mapowania dla polskiego języka (voice AI)
WERSJA 2.0 - Rozszerzona o odmianę imion, wykrywanie płci, naturalne listy

```text
Kompleksowe mapowania dla STT/TTS w języku polskim.
Obsługuje różne formy gramatyczne i błędy transkrypcji.

NOWE W V2:
- 150+ imion z odmianą (dopełniacz)
- Wykrywanie płci po imieniu
- Naturalne listy ("A, B i C")
- Lepsze reguły automatyczne

Używane przez: flows_booking_simple.py, parse_time(), fuzzy_match_staff()
```


## `constants.py`

```text
constants.py
============
Stałe używane w całym projekcie. Zastępuje magic strings
(np. "high", "elevenlabs") na czytelne nazwy klas.
```


## `services/tts_factory.py`

```text
tts_factory.py
==============
Factory: inicjalizacja serwisu TTS na podstawie konfiguracji tenanta.

Obsługiwane providery:
  - elevenlabs (domyślny)
  - cartesia
  - openai
  - azure
  - google  (Gemini 2.5 Flash TTS)
```

## Vonage → ElevenLabs przez SIP direct (`app/telephony/ncco.py`, 2026-09-05…09-18)

Przebieg uruchamiania bezpośredniego połączenia SIP (dawniej komentarz w `build_ai_ncco`).

```text
SIP direct (2026-09-05) — zamiast mostu WebSocket przez nasz serwer, próbujemy
połączyć Vonage BEZPOŚREDNIO z ElevenLabs przez SIP trunk (patrz docstring
ensure_elevenlabs_sip_number w bot_elevenlabs_agent.py po pełne wyjaśnienie).
Usuwa ~500ms/turę narzutu naszego relaya, potwierdzone na żywych połączeniach.
Import numeru jest idempotentny i leniwy — pierwsza rozmowa tej firmy robi
faktyczny import, kolejne dostają 409 (już zaimportowany) = też sukces.
Przy JAKIMKOLWIEK niepowodzeniu (brak klucza, błąd sieci, ElevenLabs down)
spadamy na stary, sprawdzony most WebSocket — klient nigdy nie zostaje bez
ścieżki połączenia.
2026-09-05: Vonage API Support (AI assistant) potwierdził że connect->SIP na
zewnętrzną domenę JEST wspierane "by design" — sip_code=404/cannot_route NIE
znaczy "funkcja niedostępna", tylko że dany request nie mógł dotrzeć do celu.
Kolejne ustalenia z tego samego czatu (po tym jak "from" + brak "+" NIE
naprawiły błędu na żywym teście): domyślny transport dla NCCO connect->SIP to
UDP na porcie 5060, a ElevenLabs SIP endpoint (jak inni SIP-trunk providerzy,
patrz ich dokumentacja Telnyx) wymaga TCP. Dodajemy ";transport=tcp" do URI
(standardowy mechanizm parametrów SIP URI, RFC 3261, wspierany przez Vonage —
potwierdzone w ich dokumentacji SIP Technical Details). Trunk "aisekretarka" z
dashboard.vonage.com/sip-trunking (BYOC/SIP Trunking) NIE ma tu znaczenia
(osobny produkt) — zostawiony założony, ale nieużywany.
2026-09-05: WYŁĄCZONE PONOWNIE — nawet z "from" + bez "+" + transport=tcp
(wszystkie 3 sugestie AI supportu Vonage z rzędu) dalej identyczny
sip_code=404/cannot_route na żywym teście. Eskalowane do prawdziwego
człowieka w Vonage Support (ticket #3122205).
2026-09-08: człowiek z Vonage Support odpisał — zasugerował że używamy
numeru Vonage (LVN) jako identyfikatora w SIP URI i że to może być
przyczyną 404. Znaleziony realny mismatch: import numeru w
ensure_elevenlabs_sip_number szedł Z "+", a URI tutaj budowane było BEZ
"+" (lstrip) — dokumentacja ElevenLabs SIP trunking wprost wymaga
identycznego formatu przy imporcie i przy wywołaniu. Naprawione, ALE na
żywym teście (2026-09-08 11:28) dalej identyczny sip_code=404/cannot_route
— format nie był (jedyną) przyczyną. Potwierdzone niezależnie po stronie
ElevenLabs: zero zapisanych prób w ich logach SIP dla obu testów (ani
tego, ani próby przez natywny produkt "SIP Trunking" Vonage) — więc
INVITE najpewniej nigdy nie opuszczał sieci Vonage.
2026-09-09: kolejna odpowiedź z ticketu #3122205 (człowiek, Aldo) wróciła
do tej samej teorii identyfikatora bez odpowiedzi na pytanie czy INVITE
w ogóle wyszedł. Podał link do dok. NCCO connect->sip — brak tam
wymaganych nagłówków, ALE jest alternatywny sposób zapisu endpointu:
pola "user"+"domain" zamiast "uri" (mutually exclusive wg dokumentacji).
PRZETESTOWANE na żywo (2026-09-09 10:20): Vonage odrzucił to OD RAZU,
na własnej walidacji — reason="invalid sip domain,invalid sip domain
user" (dzwoniący usłyszał "numer zajęty" niemal natychmiast, event
przyszedł przez /vonage/events, NIE przez eventUrl niżej — to była
odmowa żądania jako niepoprawnego, nie porażka próby połączenia).
To POTWIERDZA że "uri" (oryginalny format) jest strukturalnie
poprawny — "user"+"domain" to najwyraźniej pole pod WŁASNE
skonfigurowane trunki Vonage (jak "aisekretarka"), nie pod dowolną
zewnętrzną domenę. Wracamy do "uri". WYŁĄCZONE PONOWNIE — ta sama
zasada co poprzednio, nie włączaj bez nowych ustaleń z ticketu.
Mechanizm eventType=synchronous+eventUrl (/vonage/sip-fallback-elevenlabs)
ZOSTAJE w kodzie na przyszłość — nieszkodliwy gdy SIP_DIRECT_ENABLED=False,
i realnie działa jako siatka bezpieczeństwa dla porażek NA POZIOMIE
połączenia (cannot_route itp.), tylko nie dla odrzuceń walidacji jak ta.
2026-09-10 — Vonage support (Aldo, ticket #3122205) potwierdził że INVITE faktycznie
dociera do ElevenLabs i dostaje 404 Not Found z ICH serwera (nie problem formatu/NCCO
po naszej/Vonage stronie) — przyczyna znaleziona i naprawiona w
ensure_elevenlabs_sip_number (brakujący inbound_trunk_config.allowed_addresses).
WŁĄCZONE DLA WSZYSTKICH numerów Vonage na silniku ElevenLabs (na wyraźną prośbę
użytkownika, po potwierdzeniu na żywo na numerze testowym Bizvoice: poprawny
caller ID, called_number/channel/call_sid w dynamic_variables, X-CALL-ID zgadzający
call_sid z UUID Vonage, transkrypt+raport widoczne w panelu). Fallback na most
WebSocket (ws-elevenlabs-vonage) NIŻEJ zostaje jako siatka bezpieczeństwa przy
jakimkolwiek niepowodzeniu importu/połączenia SIP — klient nigdy nie zostaje bez
ścieżki. NIEPRZETESTOWANE JESZCZE na żywo przez czysty SIP direct: contact_owner
i book_appointment/manage_booking (tylko przez most) — warto obserwować pierwsze
rozmowy firm z tymi włączonymi funkcjami.
```

Dodatkowe ustalenia z tego samego okresu:

```text
2026-09-10 — BUG: tu było "from": sip_number (czyli numer SEKRETARKI,
ten sam co "to") zamiast numeru DZWONIĄCEGO. Vonage wysyła tę wartość
jako Caller-ID/From w SIP INVITE do ElevenLabs, więc ich webhook
personalizacji dostawał caller_id == called_number — mail z raportem
pokazywał numer sekretarki zamiast numeru klienta. Złapane na żywym
pierwszym poprawnie wysłanym raporcie (2026-09-10, "Telefon: 48459050542"
zamiast realnego numeru dzwoniącego).
```

```text
2026-09-10 — bez tego ElevenLabs generuje WŁASNE call_sid (SCL_xxx) dla
SIP-trunkowej nogi połączenia, całkowicie inne niż UUID Vonage pod którym
zapisujemy wpis w call_logs (panel: "Historia rozmów") — transkrypt
(save_elevenlabs_transcript, keyowany po call_sid z /elevenlabs/post-call)
lądował więc pod ID, do którego panel nigdy nie zajrzy, bo szuka po UUID
Vonage. X-CALL-ID to udokumentowany zarezerwowany nagłówek ElevenLabs SIP
trunking (elevenlabs.io/docs -> sip-trunking, sekcja "Standard Metadata
Headers") — NADPISUJE ich system__call_sid naszym UUID, więc oba systemy
zaczynają się zgadzać od pierwszego webhooka (personalizacja) po ostatni
(post-call). Vonage sam dokleja prefiks "X-" do klucza w "headers".
```

```text
2026-09-10 — ten warning strzelał myląco dla KAŻDEGO tenanta na silniku ElevenLabs,
nie tylko numeru testowego — SIP_DIRECT_ENABLED=False (bo to nie jest numer testowy)
też ląduje w tej gałęzi, więc "import nie powiódł się" sugerowało realny błąd tam
gdzie import w ogóle nie był próbowany (most WebSocket to normalna, oczekiwana ścieżka
dla wszystkich poza jednym testowym numerem). Złapane na żywo przy debugowaniu QFX.
```


## Narzędzia agenta ElevenLabs (`app/engines/elevenlabs/config.py`)

```text
tool_id narzędzia "contact_owner" skonfigurowanego w dashboardzie ElevenLabs (agent
"Bizvoice Test" -> Narzędzia -> contact_owner). Do 2026-09-04 to narzędzie było
statycznie przypięte do agenta i ZAWSZE technicznie dostępne dla modelu, niezależnie
od tenant.get("contact_owner_enabled") — jedyną obroną była instrukcja w promptcie
("nie masz tej funkcji, nie oferuj jej") + twardy blok wysyłki po stronie serwera
(patrz elevenlabs_tool_contact_owner niżej), ale model czasem i tak WERBALNIE oferował
zebranie wiadomości (złapane na żywym telefonie, tenant z contact_owner_enabled=0).
Od teraz tool_ids jest jawnie nadpisywany per rozmowa (conversation_config_override.
agent.prompt.tool_ids) — dokładnie ten sam poziom gwarancji co w Gemini Live/OpenAI
Realtime, gdzie narzędzie po prostu nie istnieje w tools[] danej rozmowy. Wymaga
włączonego przełącznika "Tools" w platform_settings.overrides.conversation_config_override.
agent.prompt.tool_ids na agencie (włączone 2026-09-04 przez PATCH /v1/convai/agents —
bez tego ElevenLabs po cichu ignoruje tool_ids z override i zawsze używa domyślnego
zestawu narzędzi agenta, czyli błąd wracałby bez żadnego widocznego sygnału).
2026-09-10 — migracja na nowe konto ElevenLabs (drugi Google, promo Creator 22$/11$,
stare konto konczylo limity). Agent "Bizvoice Test" odtworzony 1:1 (ten sam głos
8EWWaNTDrqObI22Gvo1q, model eleven_flash_v2_5, przełącznik tool_ids override włączony)
i te same 3 narzędzia webhook — wszystkie ID poniżej to ID z NOWEGO konta, stare już
nieaktualne. ELEVENLABS_API_KEY/ELEVENLABS_AGENT_ID (Railway env) podmienione razem z tym
pushem, żeby nie było okna gdzie kod i env wskazują na różne konta.
```

## Pełne komentarze sprzed skrócenia (2026-09-30)

W modułach silników (`app/engines/gemini_live/`, `app/engines/openai_realtime/`) komentarze
opisujące przebieg debugowania na żywych rozmowach (daty, objawy, kolejne próby) zastąpiono
zwięzłym „dlaczego”. Pełne wersje: `git show da31d86:app/engines/gemini_live/monitors.py`
(analogicznie `llm.py`, `watchdog.py` oraz `openai_realtime/*`).
