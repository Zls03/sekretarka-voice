"""Webhooki Vonage: answer (dispatch po silniku), zdarzenia/rozliczenie, fallbacki transferu i SIP."""

import asyncio
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from loguru import logger

from app.billing import apply_call_charge, is_call_allowed
from app.db import db, saas_db
from app.notifications.email import send_missed_transfer_email
from app.telephony.human_first import build_human_first_ncco, process_human_first_recording
from app.telephony.ncco import build_ai_ncco
from app.tenants import get_tenant_by_phone

router = APIRouter()


@router.api_route("/vonage/events", methods=["GET", "POST"])
async def vonage_events(request: Request):
    """Status callback od Vonage — aktualizuje call_logs i nalicza minuty/kredyty.
    1:1 z bot.py::vonage_events (sama logika, port). Samodzielnie ustala tenanta po
    numerze "to" — NIE polega na tym że call_logs już istnieje, bo save_call_transcript()
    (koniec pipeline'u) i ten webhook to dwa niezależne w czasie zdarzenia, ten webhook
    może przyjść pierwszy.

    Vonage wysyła "completed" osobno dla KAŻDEJ nogi połączenia (inbound i outbound,
    ten sam numer "to", różne uuid) — przetwarzamy TYLKO direction=inbound, inaczej
    naliczylibyśmy podwójnie."""
    try:
        if request.method == "POST":
            data = await request.json()
        else:
            data = dict(request.query_params)
    except Exception:
        data = dict(request.query_params)

    status = data.get("status", "")
    call_uuid = data.get("uuid", "")
    duration_str = data.get("duration", "0")
    to_number = data.get("to", "")
    from_number = data.get("from", "") or "nieznany"
    direction = data.get("direction", "")

    logger.info(f"[VONAGE EVENT] {call_uuid} | {status} | {duration_str}s | direction={direction}")

    if status != "completed" or not call_uuid:
        return Response(content="", status_code=200)

    if direction and direction != "inbound":
        logger.info(f"[VONAGE EVENT] Pomijam noga={direction} (liczymy tylko inbound)")
        return Response(content="", status_code=200)

    try:
        duration = int(duration_str) if duration_str else 0

        tenant = await get_tenant_by_phone(to_number) if to_number else None
        if not tenant:
            logger.warning(f"⚠️ [REALTIME TEST/VONAGE] Nie znaleziono tenanta dla {to_number}")
            return Response(content="", status_code=200)

        tenant_id = tenant["id"]
        is_saas_tenant = tenant.get("source") == "saas"
        target_db = saas_db if is_saas_tenant else db

        existing = await target_db.execute("SELECT id FROM call_logs WHERE call_sid = ?", [call_uuid])
        if existing:
            await target_db.execute(
                "UPDATE call_logs SET duration_seconds = ?, status = ? WHERE call_sid = ?",
                [duration, status, call_uuid],
            )
            logger.info(f"📊 [REALTIME TEST/VONAGE] Updated call log: {call_uuid} → {duration}s")
        else:
            # from_number zamiast zaszytego "nieznany" — bug znaleziony na żywym telefonie:
            # ten webhook i save_call_transcript() (koniec pipeline'u websocketu) to dwa
            # niezależne w czasie zdarzenia, ten webhook może przyjść PIERWSZY (potwierdzone
            # w logu: "Created call log" tu wyprzedziło "Transcript saved"). Kto pierwszy
            # stworzy wiersz, tego caller_phone zostaje na stałe — save_call_transcript()
            # widzi że wiersz już istnieje i nie insertuje drugi raz. Wcześniej ten webhook
            # zawsze wpisywał "nieznany" niezależnie od tego czy dane były dostępne — a SĄ,
            # Vonage przekazuje numer dzwoniącego jako "from" w tym samym evencie.
            await target_db.execute(
                """INSERT INTO call_logs
                   (id, tenant_id, call_sid, caller_phone, duration_seconds, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
                [f"call_{int(time.time())}", tenant_id, call_uuid, from_number, duration, status],
            )
            logger.info(f"📊 [REALTIME TEST/VONAGE] Created call log: {call_uuid} → {duration}s")

        await apply_call_charge(tenant_id, is_saas_tenant, call_uuid, status, duration)
    except Exception as e:
        logger.error(f"[REALTIME TEST/VONAGE] vonage_events error: {e}")

    return Response(content="", status_code=200)


@router.api_route("/vonage/transfer-fallback", methods=["GET", "POST"])
async def vonage_transfer_fallback(request: Request):
    """eventUrl akcji "connect" z transfer_vonage_call (realtime_tools.py) — Vonage odpytuje
    TU (eventType=synchronous) gdy próba połączenia z właścicielem kończy się timeout/busy/
    rejected/failed/unanswered. MUSIMY zwrócić nową NCCO, która zastępuje bieżącą — inaczej
    klient zostaje w martwej ciszy aż połączenie samo się urwie (dokładnie to zaobserwowano
    na żywym telefonie przed tą zmianą, z domyślnym 60s timeout i brakiem jakiegokolwiek
    fallbacku). businessName/callerPhone/ownerEmail lecą w query stringu — sami je tam
    wstawiliśmy w build_transfer_tool, bo ten webhook nie ma dostępu do żadnego stanu
    rozmowy (nowe, niezależne wywołanie od Vonage)."""
    try:
        if request.method == "POST":
            data = await request.json()
        else:
            data = dict(request.query_params)
    except Exception:
        data = dict(request.query_params)
    logger.info(f"📞 [TRANSFER FALLBACK] {data}")

    business_name = request.query_params.get("businessName", "Firma")
    caller_phone = request.query_params.get("callerPhone", "")
    owner_email = request.query_params.get("ownerEmail", "")
    if owner_email:
        asyncio.create_task(send_missed_transfer_email(business_name, caller_phone, owner_email))

    ncco = [
        {
            "action": "talk",
            "text": "Niestety nie udało się połączyć. Przekażę wiadomość, żeby ktoś oddzwonił.",
            "language": "pl-PL",
        }
    ]
    return JSONResponse(ncco)


@router.api_route("/vonage/test-siperb", methods=["GET", "POST"])
async def vonage_test_siperb(request: Request):
    """TYMCZASOWY endpoint (2026-09-27, przywrócony 2026-09-26 po odpowiedzi supportu
    Siperb — literówka w polu Username connection "vonage", patrz mail Conrad de Wet)
    — wyłącznie do ręcznego testu Vonage SIP Trunk ("aisekretarka") -> Siperb ("Trunk
    wychodzący", nazwa połączenia "vonage") -> appka Siperb na telefonie. Do usunięcia
    po zakończeniu testu, niezależnie od wyniku.

    2026-09-27 — TYMCZASOWO podpięty jako Answer URL na poziomie CAŁEJ aplikacji Vonage
    ("bizvoice-gemini-test"), która obsługuje DWA numery: Bizvoice (...542, testowy) I
    numer prawdziwej, aktywnej firmy (...552) — złapane na żywo zanim wyrządziło szkodę.
    Dlatego: testowa ścieżka Siperb TYLKO dla numeru Bizvoice, każdy inny numer spada na
    normalną, produkcyjną ścieżkę (identyczną jak vonage_answer_gemini_live), żeby ...552
    działało dokładnie tak jak przed tym testem."""
    to_number = request.query_params.get("to", "")
    if to_number.lstrip("+").lstrip("0") != "48459050542":
        from_number = request.query_params.get("from", "")
        call_uuid = request.query_params.get("uuid", "")
        region_url = request.query_params.get("region_url", "")
        host = request.headers.get("host", "localhost")
        tenant = await get_tenant_by_phone(to_number)
        if not tenant:
            return JSONResponse([{"action": "talk", "text": "Numer testowy nieaktywny.", "language": "pl-PL"}])
        if not await is_call_allowed(tenant):
            return JSONResponse([{"action": "talk", "text": "Przepraszamy, linia jest chwilowo niedostępna.", "language": "pl-PL"}])
        ncco = await build_ai_ncco(tenant, from_number, to_number, call_uuid, host, region_url)
        return JSONResponse(ncco)

    logger.info(f"🧪 [SIPERB TEST] Answer webhook wywołany: {dict(request.query_params)}")
    ncco = [{
        "action": "connect",
        "timeout": 20,
        "eventType": "synchronous",
        "eventUrl": [f"https://{request.headers.get('host', 'localhost')}/vonage/test-siperb-event"],
        "endpoint": [{
            "type": "sip",
            "uri": "sip:siperb-bizvoice@eu-west-1-sbc-1.siperb.com;transport=udp",
        }],
    }]
    return JSONResponse(ncco)


@router.api_route("/vonage/test-siperb-event", methods=["GET", "POST"])
async def vonage_test_siperb_event(request: Request):
    """eventUrl dla powyższego testu — loguje surowy status żeby zobaczyć DOKŁADNIE co
    Vonage/Siperb zwracają (np. sip_code, reason) gdyby połączenie nie doszło do skutku."""
    try:
        body = await request.json()
    except Exception:
        body = (await request.body()).decode("utf-8", errors="replace")
    logger.info(f"🧪 [SIPERB TEST] eventUrl: query={dict(request.query_params)} | body={body}")
    return JSONResponse([])


@router.get("/vonage/answer-gemini-live")
async def vonage_answer_gemini_live(request: Request):
    to_number = request.query_params.get("to", "")
    from_number = request.query_params.get("from", "")
    call_uuid = request.query_params.get("uuid", "")
    # region_url — bug znaleziony na żywym telefonie (400 Bad Request przy transferze,
    # mimo poprawnego JSON body): Vonage przypisuje KAŻDE połączenie do konkretnego
    # regionalnego centrum danych (potwierdzone przez Vonage API Support: "if you
    # receive a 400 or 404 response... your call is likely residing on a different
    # Data Center"). Ten region_url przychodzi TYLKO w tym evencie Answer i trzeba go
    # zapamiętać na całą rozmowę — sztywne api.nexmo.com trafia w złe centrum danych
    # dla połączeń spoza jego regionu.
    region_url = request.query_params.get("region_url", "")
    logger.info(f"📞 [GEMINI LIVE TEST/VONAGE] Answer: {from_number} → {to_number} (region={region_url or 'brak'})")

    tenant = await get_tenant_by_phone(to_number)
    if not tenant:
        ncco = [{"action": "talk", "text": "Numer testowy nieaktywny.", "language": "pl-PL"}]
        return JSONResponse(ncco)

    if not await is_call_allowed(tenant):
        ncco = [{"action": "talk", "text": "Przepraszamy, linia jest chwilowo niedostępna.", "language": "pl-PL"}]
        return JSONResponse(ncco)

    host = request.headers.get("host", "localhost")

    # "Najpierw dzwoni do właściciela" v2 (human_first_enabled, panel: zakładka Ustawienia →
    # "Najpierw dzwoni do właściciela") — przez apkę Siperb (SIP), patrz
    # realtime_tools.py::build_human_first_ncco. Domyślnie WYŁĄCZONE (0) dla każdej firmy,
    # więc zero zmiany zachowania dopóki ktoś świadomie tego nie włączy I nie wypełni
    # siperb_sip_username. Porażka przygotowania (brak/puste SIP username) cicho spada na
    # zwykłą ścieżkę AI niżej — właściciel nigdy nie traci połączenia przez błąd tej funkcji.
    if tenant.get("human_first_enabled"):
        human_first_ncco = await build_human_first_ncco(tenant, from_number, to_number, call_uuid, host, region_url)
        if human_first_ncco:
            logger.info(f"📱 [HUMAN-FIRST/SIPERB] Dzwonię najpierw do apki Siperb właściciela: {tenant.get('id')}")
            return JSONResponse(human_first_ncco)
        logger.warning(f"📱 [HUMAN-FIRST/SIPERB] Brak siperb_sip_username — od razu sekretarka AI: {tenant.get('id')}")

    ncco = await build_ai_ncco(tenant, from_number, to_number, call_uuid, host, region_url)
    return JSONResponse(ncco)


@router.api_route("/vonage/human-first-fallback", methods=["GET", "POST"])
async def vonage_human_first_fallback(request: Request):
    """eventUrl (eventType=synchronous) dla connect->sip w build_human_first_ncco — Vonage
    odpytuje to gdy właściciel nie odbierze apki Siperb (timeout/busy/rejected/failed —
    apka niezalogowana/offline daje ten sam efekt, Siperb po prostu nie znajduje
    zarejestrowanego urządzenia). Zwraca BEZWARUNKOWO świeżą NCCO z build_ai_ncco (ten sam
    sprawdzony wzorzec co vonage_sip_fallback_elevenlabs — Vonage odpytuje ten URL NAWET
    przy sukcesie connect, ale wtedy po prostu ignoruje zwróconą NCCO bo leg już żyje).
    to/from/uuid/regionUrl są przekazane w query stringu z miejsca budowania oryginalnej
    NCCO — ten webhook nie ma dostępu do obiektu tenanta, więc odtwarza go po numerze."""
    to_number = request.query_params.get("to", "")
    from_number = request.query_params.get("from", "")
    call_uuid = request.query_params.get("uuid", "")
    region_url = request.query_params.get("regionUrl", "")
    try:
        body = await request.json()
    except Exception:
        body = (await request.body()).decode("utf-8", errors="replace")
    status = body.get("status") if isinstance(body, dict) else None
    logger.info(f"📱 [HUMAN-FIRST/SIPERB] eventUrl odpytany, status={status!r} | body={body}")

    tenant = await get_tenant_by_phone(to_number)
    if not tenant:
        return JSONResponse([{"action": "talk", "text": "Przepraszamy, wystąpił błąd połączenia.", "language": "pl-PL"}])

    host = request.headers.get("host", "localhost")
    ncco = await build_ai_ncco(tenant, from_number, to_number, call_uuid, host, region_url)
    return JSONResponse(ncco)


@router.api_route("/vonage/human-first-recording", methods=["GET", "POST"])
async def vonage_human_first_recording(request: Request):
    """eventUrl dla action "record" w build_human_first_ncco — Vonage POSTuje tu link do
    nagrania PO zakończeniu połączenia, niezależnie czy właściciel odebrał czy nie (jeśli
    connect->sip nigdy się nie połączył, nagranie jest puste/krótkie —
    process_human_first_recording cicho pomija zapis gdy Deepgram nie zwróci żadnego
    transkryptu). Fire-and-forget: Vonage dostaje szybkie potwierdzenie, faktyczne
    pobranie+transkrypcja+podsumowanie (kilka-kilkanaście sekund) dzieje się w tle."""
    to_number = request.query_params.get("to", "")
    from_number = request.query_params.get("from", "")
    call_uuid = request.query_params.get("uuid", "")
    try:
        body = await request.json()
    except Exception:
        body = {}
    recording_url = body.get("recording_url") if isinstance(body, dict) else None
    logger.info(f"📼 [HUMAN-FIRST/RECORDING] eventUrl odpytany | body={body}")
    if not recording_url:
        return JSONResponse({"status": "ignored"})

    tenant = await get_tenant_by_phone(to_number)
    if not tenant:
        return JSONResponse({"status": "ignored"})

    duration_seconds = 0
    try:
        start = body.get("start_time")
        end = body.get("end_time")
        if start and end:
            from datetime import datetime as _dt
            fmt = "%Y-%m-%dT%H:%M:%SZ"
            duration_seconds = int((_dt.strptime(end, fmt) - _dt.strptime(start, fmt)).total_seconds())
    except Exception:
        pass

    asyncio.create_task(process_human_first_recording(tenant, recording_url, from_number, call_uuid, duration_seconds))
    return JSONResponse({"status": "ok"})


@router.api_route("/vonage/sip-fallback-elevenlabs", methods=["GET", "POST"])
async def vonage_sip_fallback_elevenlabs(request: Request):
    """eventUrl (eventType=synchronous) dla connect->SIP w vonage_answer_gemini_live —
    Vonage odpytuje to gdy próba SIP direct do ElevenLabs zawiedzie (failed/rejected/
    timeout/busy) i oczekuje w odpowiedzi ŚWIEŻEJ NCCO. Bez tego (stan sprzed
    2026-09-09) porażka connect->SIP PO WYSŁANIU NCCO kończyła połączenie bez żadnej
    ścieżki dla dzwoniącego — złapane na żywo dwa razy. wsUri (już zbudowany, gotowy
    URI mostu WebSocket) jest przekazywany w query stringu z miejsca budowania
    oryginalnej NCCO, więc tu nic nie trzeba odtwarzać z tenanta na nowo.

    2026-09-18 — POPRAWKA: wcześniej ten endpoint zakładał bezwarunkowo (samym faktem
    bycia wywołanym), że SIP connect zawiódł, i logował to jako WARNING. Na żywo
    (Voice Inspector + brak "WebSocket connected" w logach most nigdy się realnie NIE
    uruchamiał) potwierdzone, że Vonage odpytuje ten eventUrl RÓWNIEŻ przy udanych
    połączeniach (widziane 2x na rozmowę — raz przy starcie, raz przy końcu), nie tylko
    przy realnej awarii. Zwracamy fresh NCCO z fallbackiem jak dotychczas (siatka
    bezpieczeństwa zostaje — na wypadek gdyby TYM razem to było prawdziwe niepowodzenie,
    Vonage i tak zignoruje tę NCCO jeśli leg już żyje), ale logujemy SUROWĄ treść którą
    Vonage faktycznie przysłał zamiast zgadywać — dopiero to pozwoli kiedyś odróżnić
    realną awarię od nieszkodliwego zdarzenia."""
    ws_uri = request.query_params.get("wsUri", "")
    try:
        body = await request.json()
    except Exception:
        body = (await request.body()).decode("utf-8", errors="replace")
    # 2026-09-18 — status'y realnie zaobserwowane na żywo (2 pełne, udane rozmowy,
    # potwierdzone Voice Inspectorem) dla NORMALNEGO przebiegu leg'a SIP: started →
    # ringing → answered → completed. Tylko coś SPOZA tej listy (np. failed/rejected/
    # busy/timeout/cannot_route — nazwy nie potwierdzone na żywo, bo jeszcze nie
    # złapaliśmy prawdziwej awarii PO naprawie allowed_addresses) oznacza że warto na
    # to realnie zerknąć — stąd WARNING tylko dla nieznanego statusu, reszta to INFO.
    _BENIGN_SIP_STATUSES = {"started", "ringing", "answered", "completed"}
    status = body.get("status") if isinstance(body, dict) else None
    log_fn = logger.info if status in _BENIGN_SIP_STATUSES else logger.warning
    log_fn(
        f"{'ℹ️' if status in _BENIGN_SIP_STATUSES else '⚠️'} [ELEVENLABS/VONAGE SIP] "
        f"eventUrl odpytany, status={status!r} (spoza {_BENIGN_SIP_STATUSES} = warto sprawdzić) "
        f"| query={dict(request.query_params)} | body={body}"
    )
    if not ws_uri:
        return JSONResponse([{"action": "talk", "text": "Przepraszamy, wystąpił błąd połączenia.", "language": "pl-PL"}])
    ncco = [{
        "action": "connect",
        "endpoint": [{"type": "websocket", "uri": ws_uri, "content-type": "audio/l16;rate=16000"}],
    }]
    return JSONResponse(ncco)
