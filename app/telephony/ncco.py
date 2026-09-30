"""NCCO dla Vonage: wybór silnika głosowego (realtime_engine) i sposobu połączenia."""

from urllib.parse import quote

from loguru import logger

from app.engines.elevenlabs.config import ELEVENLABS_SIP_DOMAIN
from app.engines.elevenlabs.config import _resolve_agent_id as resolve_elevenlabs_agent_id
from app.engines.elevenlabs.sip import ensure_elevenlabs_sip_number


async def build_ai_ncco(tenant: dict, from_number: str, to_number: str, call_uuid: str, host: str, region_url: str) -> list:
    """Wyodrębnione z vonage_answer_gemini_live (2026-09-25) żeby dało się wołać ten sam
    dispatch po realtime_engine z osobnej funkcji zwracającej listę NCCO zamiast JSONResponse
    (historycznie też z /vonage/human-first-fallback — mechanizm "najpierw dzwoni do
    właściciela" usunięty 2026-09-28, nie działał niezawodnie na zablokowanym telefonie;
    ewentualny powrót do tego pomysłu przez appkę SIP typu Siperb, nie Vonage Users API)."""
    # realtime_engine ('gemini'/'openai'/'elevenlabs', panel: zakładka "Głos agenta")
    # decyduje który pipeline odbiera ten numer — SAM numer telefonu obsługuje wszystkie
    # trzy silniki, tu jest jedyne miejsce rozgałęzienia. /ws-gemini-test-vonage to
    # websocket z bot_openai_realtime.py, /ws-elevenlabs-vonage z bot_elevenlabs_agent.py
    # (oba montowane w tym samym Railway deployu) — żaden z nich nie czyta regionUrl
    # (nie robią transferu Vonage, patrz ich handlery).
    if tenant.get("realtime_engine") == "openai":
        ws_uri = (
            f"wss://{host}/ws-gemini-test-vonage?phone={tenant['phone_number']}"
            f"&callerPhone={from_number}&callSid={call_uuid}"
        )
    elif tenant.get("realtime_engine") == "elevenlabs":
        # SIP direct (2026-09-05) — zamiast mostu WebSocket przez nasz serwer, próbujemy
        # połączyć Vonage BEZPOŚREDNIO z ElevenLabs przez SIP trunk (patrz docstring
        # ensure_elevenlabs_sip_number w bot_elevenlabs_agent.py po pełne wyjaśnienie).
        # Usuwa ~500ms/turę narzutu naszego relaya, potwierdzone na żywych połączeniach.
        # Import numeru jest idempotentny i leniwy — pierwsza rozmowa tej firmy robi
        # faktyczny import, kolejne dostają 409 (już zaimportowany) = też sukces.
        # Przy JAKIMKOLWIEK niepowodzeniu (brak klucza, błąd sieci, ElevenLabs down)
        # spadamy na stary, sprawdzony most WebSocket — klient nigdy nie zostaje bez
        # ścieżki połączenia.
        # 2026-09-05: Vonage API Support (AI assistant) potwierdził że connect->SIP na
        # zewnętrzną domenę JEST wspierane "by design" — sip_code=404/cannot_route NIE
        # znaczy "funkcja niedostępna", tylko że dany request nie mógł dotrzeć do celu.
        # Kolejne ustalenia z tego samego czatu (po tym jak "from" + brak "+" NIE
        # naprawiły błędu na żywym teście): domyślny transport dla NCCO connect->SIP to
        # UDP na porcie 5060, a ElevenLabs SIP endpoint (jak inni SIP-trunk providerzy,
        # patrz ich dokumentacja Telnyx) wymaga TCP. Dodajemy ";transport=tcp" do URI
        # (standardowy mechanizm parametrów SIP URI, RFC 3261, wspierany przez Vonage —
        # potwierdzone w ich dokumentacji SIP Technical Details). Trunk "aisekretarka" z
        # dashboard.vonage.com/sip-trunking (BYOC/SIP Trunking) NIE ma tu znaczenia
        # (osobny produkt) — zostawiony założony, ale nieużywany.
        # 2026-09-05: WYŁĄCZONE PONOWNIE — nawet z "from" + bez "+" + transport=tcp
        # (wszystkie 3 sugestie AI supportu Vonage z rzędu) dalej identyczny
        # sip_code=404/cannot_route na żywym teście. Eskalowane do prawdziwego
        # człowieka w Vonage Support (ticket #3122205).
        # 2026-09-08: człowiek z Vonage Support odpisał — zasugerował że używamy
        # numeru Vonage (LVN) jako identyfikatora w SIP URI i że to może być
        # przyczyną 404. Znaleziony realny mismatch: import numeru w
        # ensure_elevenlabs_sip_number szedł Z "+", a URI tutaj budowane było BEZ
        # "+" (lstrip) — dokumentacja ElevenLabs SIP trunking wprost wymaga
        # identycznego formatu przy imporcie i przy wywołaniu. Naprawione, ALE na
        # żywym teście (2026-09-08 11:28) dalej identyczny sip_code=404/cannot_route
        # — format nie był (jedyną) przyczyną. Potwierdzone niezależnie po stronie
        # ElevenLabs: zero zapisanych prób w ich logach SIP dla obu testów (ani
        # tego, ani próby przez natywny produkt "SIP Trunking" Vonage) — więc
        # INVITE najpewniej nigdy nie opuszczał sieci Vonage.
        # 2026-09-09: kolejna odpowiedź z ticketu #3122205 (człowiek, Aldo) wróciła
        # do tej samej teorii identyfikatora bez odpowiedzi na pytanie czy INVITE
        # w ogóle wyszedł. Podał link do dok. NCCO connect->sip — brak tam
        # wymaganych nagłówków, ALE jest alternatywny sposób zapisu endpointu:
        # pola "user"+"domain" zamiast "uri" (mutually exclusive wg dokumentacji).
        # PRZETESTOWANE na żywo (2026-09-09 10:20): Vonage odrzucił to OD RAZU,
        # na własnej walidacji — reason="invalid sip domain,invalid sip domain
        # user" (dzwoniący usłyszał "numer zajęty" niemal natychmiast, event
        # przyszedł przez /vonage/events, NIE przez eventUrl niżej — to była
        # odmowa żądania jako niepoprawnego, nie porażka próby połączenia).
        # To POTWIERDZA że "uri" (oryginalny format) jest strukturalnie
        # poprawny — "user"+"domain" to najwyraźniej pole pod WŁASNE
        # skonfigurowane trunki Vonage (jak "aisekretarka"), nie pod dowolną
        # zewnętrzną domenę. Wracamy do "uri". WYŁĄCZONE PONOWNIE — ta sama
        # zasada co poprzednio, nie włączaj bez nowych ustaleń z ticketu.
        # Mechanizm eventType=synchronous+eventUrl (/vonage/sip-fallback-elevenlabs)
        # ZOSTAJE w kodzie na przyszłość — nieszkodliwy gdy SIP_DIRECT_ENABLED=False,
        # i realnie działa jako siatka bezpieczeństwa dla porażek NA POZIOMIE
        # połączenia (cannot_route itp.), tylko nie dla odrzuceń walidacji jak ta.
        # 2026-09-10 — Vonage support (Aldo, ticket #3122205) potwierdził że INVITE faktycznie
        # dociera do ElevenLabs i dostaje 404 Not Found z ICH serwera (nie problem formatu/NCCO
        # po naszej/Vonage stronie) — przyczyna znaleziona i naprawiona w
        # ensure_elevenlabs_sip_number (brakujący inbound_trunk_config.allowed_addresses).
        # WŁĄCZONE DLA WSZYSTKICH numerów Vonage na silniku ElevenLabs (na wyraźną prośbę
        # użytkownika, po potwierdzeniu na żywo na numerze testowym Bizvoice: poprawny
        # caller ID, called_number/channel/call_sid w dynamic_variables, X-CALL-ID zgadzający
        # call_sid z UUID Vonage, transkrypt+raport widoczne w panelu). Fallback na most
        # WebSocket (ws-elevenlabs-vonage) NIŻEJ zostaje jako siatka bezpieczeństwa przy
        # jakimkolwiek niepowodzeniu importu/połączenia SIP — klient nigdy nie zostaje bez
        # ścieżki. NIEPRZETESTOWANE JESZCZE na żywo przez czysty SIP direct: contact_owner
        # i book_appointment/manage_booking (tylko przez most) — warto obserwować pierwsze
        # rozmowy firm z tymi włączonymi funkcjami.
        SIP_DIRECT_ENABLED = True
        agent_id = resolve_elevenlabs_agent_id(tenant)
        sip_ready = SIP_DIRECT_ENABLED and await ensure_elevenlabs_sip_number(tenant["phone_number"], agent_id)
        if sip_ready:
            sip_number = to_number if to_number.startswith("+") else f"+{to_number}"
            fallback_ws_uri = (
                f"wss://{host}/ws-elevenlabs-vonage?phone={tenant['phone_number']}"
                f"&callerPhone={from_number}&callSid={call_uuid}"
            )
            event_url = (
                f"https://{host}/vonage/sip-fallback-elevenlabs?wsUri={quote(fallback_ws_uri, safe='')}"
            )
            ncco = [{
                "action": "connect",
                # 2026-09-10 — BUG: tu było "from": sip_number (czyli numer SEKRETARKI,
                # ten sam co "to") zamiast numeru DZWONIĄCEGO. Vonage wysyła tę wartość
                # jako Caller-ID/From w SIP INVITE do ElevenLabs, więc ich webhook
                # personalizacji dostawał caller_id == called_number — mail z raportem
                # pokazywał numer sekretarki zamiast numeru klienta. Złapane na żywym
                # pierwszym poprawnie wysłanym raporcie (2026-09-10, "Telefon: 48459050542"
                # zamiast realnego numeru dzwoniącego).
                "from": from_number.lstrip("+") if from_number else sip_number.lstrip("+"),
                "eventType": "synchronous",
                "eventUrl": [event_url],
                "endpoint": [{
                    "type": "sip",
                    "uri": f"sip:{sip_number}@{ELEVENLABS_SIP_DOMAIN};transport=tcp",
                    # 2026-09-10 — bez tego ElevenLabs generuje WŁASNE call_sid (SCL_xxx) dla
                    # SIP-trunkowej nogi połączenia, całkowicie inne niż UUID Vonage pod którym
                    # zapisujemy wpis w call_logs (panel: "Historia rozmów") — transkrypt
                    # (save_elevenlabs_transcript, keyowany po call_sid z /elevenlabs/post-call)
                    # lądował więc pod ID, do którego panel nigdy nie zajrzy, bo szuka po UUID
                    # Vonage. X-CALL-ID to udokumentowany zarezerwowany nagłówek ElevenLabs SIP
                    # trunking (elevenlabs.io/docs -> sip-trunking, sekcja "Standard Metadata
                    # Headers") — NADPISUJE ich system__call_sid naszym UUID, więc oba systemy
                    # zaczynają się zgadzać od pierwszego webhooka (personalizacja) po ostatni
                    # (post-call). Vonage sam dokleja prefiks "X-" do klucza w "headers".
                    "headers": {"CALL-ID": call_uuid},
                }],
            }]
            logger.info(f"📞 [ELEVENLABS/VONAGE SIP] Bezpośrednie połączenie (uri, z fallbackiem): {sip_number}")
            return ncco
        # 2026-09-10 — ten warning strzelał myląco dla KAŻDEGO tenanta na silniku ElevenLabs,
        # nie tylko numeru testowego — SIP_DIRECT_ENABLED=False (bo to nie jest numer testowy)
        # też ląduje w tej gałęzi, więc "import nie powiódł się" sugerowało realny błąd tam
        # gdzie import w ogóle nie był próbowany (most WebSocket to normalna, oczekiwana ścieżka
        # dla wszystkich poza jednym testowym numerem). Złapane na żywo przy debugowaniu QFX.
        if SIP_DIRECT_ENABLED:
            logger.warning("⚠️ [ELEVENLABS/VONAGE SIP] Import numeru nie powiódł się — fallback na most WebSocket")
        ws_uri = (
            f"wss://{host}/ws-elevenlabs-vonage?phone={tenant['phone_number']}"
            f"&callerPhone={from_number}&callSid={call_uuid}"
        )
    else:
        ws_uri = (
            f"wss://{host}/ws-gemini-live-test-vonage?phone={tenant['phone_number']}"
            f"&callerPhone={from_number}&callSid={call_uuid}&regionUrl={quote(region_url, safe='')}"
        )

    return [
        {
            "action": "connect",
            "endpoint": [
                {
                    "type": "websocket",
                    "uri": ws_uri,
                    "content-type": "audio/l16;rate=16000",
                }
            ],
        }
    ]
