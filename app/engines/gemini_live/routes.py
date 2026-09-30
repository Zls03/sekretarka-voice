"""Websockety rozmów Gemini Live (Twilio Media Streams i Vonage)."""

import asyncio
import json

from fastapi import APIRouter, WebSocket
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import EndFrame, LLMMessagesAppendFrame
from pipecat.pipeline.parallel_pipeline import ParallelPipeline
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.serializers.vonage import VonageFrameSerializer
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport

from app.billing import is_call_allowed
from app.booking.book_appointment import build_book_appointment_tool
from app.booking.manage_booking import build_manage_booking_tool
from app.call_logs import save_call_transcript
from app.crm_contacts import get_crm_contact_name
from app.engines.gemini_live.llm import GEMINI_LIVE_MODEL, build_gemini_live_llm
from app.engines.gemini_live.monitors import GeminiBotMonitor, GeminiUserMonitor, make_gemini_state
from app.engines.gemini_live.watchdog import monitor_gemini_call_health
from app.post_call.report import maybe_send_call_summary
from app.prompt.instructions import append_known_caller_hint, build_realtime_instructions
from app.tenants import get_tenant_by_phone
from app.tools.contact_owner import build_contact_owner_tool
from app.tools.end_conversation import build_end_conversation_tool
from app.tools.transfer import build_transfer_tool
from app.tts import create_tts_service

router = APIRouter()


@router.websocket("/ws-gemini-live-test")
async def websocket_gemini_live_test(websocket: WebSocket):
    await websocket.accept()
    logger.info("🔌 [GEMINI LIVE TEST] WebSocket connected")

    stream_sid = None
    tenant = None
    caller_phone = "nieznany"
    call_sid = None

    try:
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)
            event = data.get("event")
            if event == "connected":
                continue
            if event == "start":
                start_data = data.get("start", {})
                stream_sid = start_data.get("streamSid")
                custom_params = start_data.get("customParameters", {})
                tenant_phone = custom_params.get("phone")
                caller_phone = custom_params.get("callerPhone", "nieznany")
                call_sid = custom_params.get("callSid")
                if tenant_phone:
                    tenant = await get_tenant_by_phone(tenant_phone)
                break
    except Exception as e:
        logger.error(f"[GEMINI LIVE TEST] Błąd startu: {e}")
        await websocket.close()
        return

    if not stream_sid or not tenant:
        logger.error("❌ [GEMINI LIVE TEST] Brak stream_sid lub tenant — zamykam")
        await websocket.close()
        return

    if not await is_call_allowed(tenant):
        logger.warning(f"🚫 [GEMINI LIVE TEST] Tenant {tenant.get('id')} zablokowany — zamykam")
        await websocket.close()
        return

    logger.info(f"✅ [GEMINI LIVE TEST] Tenant: {tenant.get('name')}")

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            # ⚠️ `vad_analyzer=` TU jest martwym parametrem (17.08.2026, zweryfikowane w
            # źródle pipecat) — TransportParams/FastAPIWebsocketParams w ogóle nie ma
            # takiego pola, Pydantic po cichu je ignoruje (extra="ignore" domyślnie).
            # Realny lokalny VAD jest teraz osobnym procesorem w pipeline (vad_processor
            # niżej) — patrz komentarz przy jego tworzeniu.
            serializer=TwilioFrameSerializer(
                stream_sid=stream_sid,
                params=TwilioFrameSerializer.InputParams(auto_hang_up=False),
            ),
        ),
    )

    gemini_state = make_gemini_state()
    task_box = {"task": None}
    context_box = {"context": None}
    # Reużywamy WPROST build_*_tool z realtime_tools.py (patrz docstring
    # build_gemini_live_llm) — call_state=gemini_state, bo ma już pole "ended"
    # którego te handlery potrzebują (patrz make_gemini_state()).
    contact_owner_available = tenant.get("contact_owner_enabled", 1) == 1
    tools = []
    if contact_owner_available:
        tools.append(build_contact_owner_tool(tenant, caller_phone, task_box, gemini_state))
    tools.append(build_end_conversation_tool(task_box, gemini_state))
    # 1:1 z bot.py (cascade): booking_enabled BEZ domyślnej wartości (brak pola =
    # wyłączone, nie włączone) + wymóg że co najmniej jeden pracownik ma połączony
    # Google Calendar i przypisaną usługę — bez tego cascade sam wymusza 0
    # ("booking_enabled forced to 0 — no staff with calendar+services"), więc to samo
    # tenanty musi dawać ten sam wynik tutaj, inaczej zachowanie się rozjeżdża między
    # systemami dla identycznej konfiguracji.
    booking_available = tenant.get("booking_enabled") == 1 and any(
        s.get("google_connected") and len(s.get("services", [])) > 0
        for s in tenant.get("staff", [])
    )
    if booking_available:
        tools.append(build_book_appointment_tool(tenant, caller_phone, gemini_state, context_box))
        tools.append(build_manage_booking_tool(tenant, caller_phone, gemini_state))

    system_prompt = build_realtime_instructions(
        tenant, None, has_booking=booking_available, has_contact_owner=contact_owner_available
    )
    known_name = await get_crm_contact_name(tenant.get("id", ""), caller_phone)
    if known_name:
        system_prompt = append_known_caller_hint(system_prompt, known_name, has_contact_owner=contact_owner_available)
    gemini_voice = (tenant.get("gemini_voice") or "").strip() or None
    llm, user_aggregator, assistant_aggregator, llm_context = build_gemini_live_llm(
        system_prompt, tools=tools, voice=gemini_voice
    )
    context_box["context"] = llm_context
    # Lokalny VAD jako wczesny sygnał "klient mówi" dla GeminiUserMonitor (patrz jego
    # docstring — bug ze złapanym w środku wypowiedzi klienta nudge'em, 17.08.2026).
    # Siedzi zaraz po transport.input(), PRZED user_aggregator/gemini_user_monitor,
    # żeby analizować surowe audio jak najwcześniej. Ten sam SileroVADAnalyzer/VADParams
    # co poprzednio (bezużytecznie) siedział w FastAPIWebsocketParams — teraz faktycznie
    # coś robi, bo VADProcessor to prawdziwy, wspierany mechanizm w tej wersji pipecat
    # (nie parametr transportu).
    vad_processor = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(
            # stop_secs=0.2 — zgodnie z zalecanym domyślnym progiem pipecat, na którym
            # oparte są ich wbudowane szacunki latencji P99 dla Smart Turn (WARNING w
            # logu przy 0.3: "Built-in p99 latency values assume stop_secs=0.2").
            params=VADParams(confidence=0.6, start_secs=0.2, stop_secs=0.2, min_volume=0.4)
        )
    )
    gemini_user_monitor = GeminiUserMonitor(gemini_state)
    gemini_bot_monitor = GeminiBotMonitor(gemini_state)
    # Zawsze natywny głos Gemini (modalities=AUDIO) — hybryda z ElevenLabs jako
    # głównym głosem (2026-09-01) usunięta: mierzalnie wolniejsza (SimpleTextAggregator
    # czeka na CAŁE zdanie z Gemini zanim w ogóle wyśle tekst do TTS, patrz logi z
    # 2026-09-01 — user->bot audio ~4.1s śr. z ElevenLabs vs ~2.4s natywnie), a jakość
    # głosu nie rekompensowała tej różnicy w ocenie na żywo. fallback_tts zostaje w
    # OSOBNEJ gałęzi ParallelPipeline z wymuszonym sample_rate=24000 (ten sam co natywne
    # audio Gemini) wyłącznie pod speak_directly() (idle-nudge, "czy nadal jesteśmy
    # połączeni?") — Gemini nie da się w niezawodny sposób poprosić o wypowiedzenie
    # DOKŁADNEGO tekstu z zewnątrz, gdy jego sesja akurat ucichła.
    fallback_tts = create_tts_service(tenant, sample_rate=24000)
    voice_steps = [ParallelPipeline([llm], [fallback_tts])]

    pipeline = Pipeline([
        transport.input(),
        vad_processor,
        user_aggregator,
        gemini_user_monitor,
        *voice_steps,
        gemini_bot_monitor,
        transport.output(),
        assistant_aggregator,
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
            audio_in_sample_rate=8000,
            audio_out_sample_rate=8000,
        ),
    )
    task_box["task"] = task

    @transport.event_handler("on_client_connected")
    async def on_connect(transport, client):
        logger.info("🎤 [GEMINI LIVE TEST] Klient połączony — wybudzam do przywitania")
        # Poprzednia wersja wołała user_aggregator.push_context_frame() z pustym
        # kontekstem, licząc że GeminiLiveLLMService._handle_context() samo doda seed
        # (patrz źródło: gdy messages puste, dokleja CAŁY system_instruction PONOWNIE
        # jako wiadomość "system" w kontekście, żeby było co wysłać). Efekt na żywym
        # telefonie: pierwsza odpowiedź (powitanie) potrafiła wypaść dopiero ~15-17s
        # po connect, i wyglądało to jakby bot czekał aż klient odezwie się pierwszy
        # ("Halo?"), a nie proaktywnie sam zaczynał. Podejrzenie: model musi wtedy
        # przetworzyć ogromny (~150 linii) system prompt DWA razy — raz jako
        # system_instruction sesji, raz jako doklejony seed — zanim w ogóle wygeneruje
        # pierwszy dźwięk.
        # Teraz zamiast pustego kontekstu wysyłamy jawną, krótką wiadomość startową
        # przez LLMMessagesAppendFrame — GeminiLiveLLMService ma na to bezpośrednią
        # obsługę (_create_single_response / _create_initial_response), a treść do
        # powiedzenia i tak dyktuje system_instruction ("ROZPOCZĘCIE ROZMOWY: ..."),
        # więc ta wiadomość jest tylko "zapłonem" do wywołania inferencji, nie
        # duplikuje całego promptu.
        # EKSPERYMENT 2026-09-10: 1.0s nie było niczym uzasadnione w kodzie/historii —
        # wygląda na nieprzetestowany margines bezpieczeństwa. Skrócone do 0.3s żeby
        # przyspieszyć powitanie; jeśli na żywym telefonie pierwsze słowo powitania
        # zacznie się ucinać/gubić, podnieś z powrotem (sprawdź czy VAD/WS zdążyły się
        # ustabilizować — "Loading Silero VAD model..." w logach tuż po connect).
        await asyncio.sleep(0.3)
        logger.info("🎤 [GEMINI LIVE TEST] Wysyłam LLMMessagesAppendFrame (kick startowy)")
        await task.queue_frames([
            LLMMessagesAppendFrame(
                messages=[{"role": "user", "content": "(początek rozmowy)"}],
                run_llm=True,
            )
        ])
        logger.info("🎤 [GEMINI LIVE TEST] Kick startowy wysłany bez wyjątku")
        asyncio.create_task(monitor_gemini_call_health(task, gemini_state, llm))

    @transport.event_handler("on_client_disconnected")
    async def on_disconnect(transport, client):
        logger.info("📴 [GEMINI LIVE TEST] Klient rozłączony")
        gemini_state["ended"] = True
        await task.queue_frame(EndFrame())

    runner = PipelineRunner()
    logger.info("🚀 [GEMINI LIVE TEST] Start pipeline")
    try:
        await runner.run(task)
    except Exception as e:
        logger.error(f"[GEMINI LIVE TEST] Pipeline error: {e}")
    finally:
        logger.info("🏁 [GEMINI LIVE TEST] Koniec połączenia")
        try:
            # 2026-09-23 — save_call_transcript PRZED maybe_send_call_summary: tworzy wiersz
            # call_logs, który maybe_send_call_summary potem UPDATE'uje (summary/priority dla
            # portalu /crm) — odwrotna kolejność trafiałaby w jeszcze nieistniejący wiersz.
            await save_call_transcript(tenant, call_sid, caller_phone, llm_context)
        except Exception as e:
            logger.error(f"[GEMINI LIVE TEST] Call transcript error: {e}")
        try:
            await maybe_send_call_summary(tenant, caller_phone, llm_context, gemini_state, call_sid=call_sid)
        except Exception as e:
            logger.error(f"[GEMINI LIVE TEST] Call summary error: {e}")


@router.get("/health-gemini-live-test")
async def health_gemini_live():
    return {"status": "ok", "provider": "gemini-live", "model": GEMINI_LIVE_MODEL}


@router.websocket("/ws-gemini-live-test-vonage")
async def websocket_gemini_live_test_vonage(websocket: WebSocket):
    tenant_phone = websocket.query_params.get("phone")
    caller_phone = websocket.query_params.get("callerPhone", "nieznany")
    call_sid = websocket.query_params.get("callSid")
    region_url = websocket.query_params.get("regionUrl") or None
    if not tenant_phone:
        logger.error("❌ [GEMINI LIVE TEST/VONAGE] Brak phone w query params — zamykam")
        await websocket.close()
        return

    await websocket.accept()
    logger.info(f"🔌 [GEMINI LIVE TEST/VONAGE] WebSocket connected, phone={tenant_phone}")

    tenant = await get_tenant_by_phone(tenant_phone)
    if not tenant:
        logger.error("❌ [GEMINI LIVE TEST/VONAGE] Nie znaleziono tenanta — zamykam")
        await websocket.close()
        return

    logger.info(f"✅ [GEMINI LIVE TEST/VONAGE] Tenant: {tenant.get('name')}")

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            # ⚠️ `vad_analyzer=` TU jest martwym parametrem — patrz komentarz przy
            # tej samej sytuacji w websocket_gemini_live_test (trasa Twilio) wyżej.
            serializer=VonageFrameSerializer(
                params=VonageFrameSerializer.InputParams(vonage_sample_rate=16000),
            ),
        ),
    )

    gemini_state = make_gemini_state()
    task_box = {"task": None}
    context_box = {"context": None}
    transfer_available = tenant.get("transfer_enabled", 0) == 1
    contact_owner_available = tenant.get("contact_owner_enabled", 1) == 1
    tools = []
    if contact_owner_available:
        tools.append(build_contact_owner_tool(
            tenant, caller_phone, task_box, gemini_state, has_transfer_tool=transfer_available
        ))
    tools.append(build_end_conversation_tool(task_box, gemini_state))
    # 1:1 z bot.py (cascade): booking_enabled BEZ domyślnej wartości (brak pola =
    # wyłączone, nie włączone) + wymóg że co najmniej jeden pracownik ma połączony
    # Google Calendar i przypisaną usługę — bez tego cascade sam wymusza 0
    # ("booking_enabled forced to 0 — no staff with calendar+services"), więc to samo
    # tenanty musi dawać ten sam wynik tutaj, inaczej zachowanie się rozjeżdża między
    # systemami dla identycznej konfiguracji.
    booking_available = tenant.get("booking_enabled") == 1 and any(
        s.get("google_connected") and len(s.get("services", [])) > 0
        for s in tenant.get("staff", [])
    )
    if booking_available:
        tools.append(build_book_appointment_tool(tenant, caller_phone, gemini_state, context_box, channel="vonage"))
        tools.append(build_manage_booking_tool(tenant, caller_phone, gemini_state))
    if transfer_available:
        # Tylko Vonage (patrz docstring build_transfer_tool w realtime_tools.py) — Twilio
        # ma osobny, już istniejący mechanizm (transfer_requests + /twilio/after-stream),
        # nietknięty tym kodem.
        transfer_host = websocket.headers.get("host", "localhost")
        tools.append(build_transfer_tool(
            tenant, call_sid, gemini_state, region_url,
            caller_phone=caller_phone, host=transfer_host,
        ))

    system_prompt = build_realtime_instructions(
        tenant, None, has_transfer=transfer_available, has_booking=booking_available,
        has_contact_owner=contact_owner_available,
    )
    known_name = await get_crm_contact_name(tenant.get("id", ""), caller_phone)
    if known_name:
        system_prompt = append_known_caller_hint(system_prompt, known_name, has_contact_owner=contact_owner_available)
    gemini_voice = (tenant.get("gemini_voice") or "").strip() or None
    llm, user_aggregator, assistant_aggregator, llm_context = build_gemini_live_llm(
        system_prompt, tools=tools, voice=gemini_voice
    )
    context_box["context"] = llm_context
    # Lokalny VAD jako wczesny sygnał "klient mówi" dla GeminiUserMonitor (patrz jego
    # docstring — bug ze złapanym w środku wypowiedzi klienta nudge'em, 17.08.2026).
    # Siedzi zaraz po transport.input(), PRZED user_aggregator/gemini_user_monitor,
    # żeby analizować surowe audio jak najwcześniej. Ten sam SileroVADAnalyzer/VADParams
    # co poprzednio (bezużytecznie) siedział w FastAPIWebsocketParams — teraz faktycznie
    # coś robi, bo VADProcessor to prawdziwy, wspierany mechanizm w tej wersji pipecat
    # (nie parametr transportu).
    vad_processor = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(
            # stop_secs=0.2 — zgodnie z zalecanym domyślnym progiem pipecat, na którym
            # oparte są ich wbudowane szacunki latencji P99 dla Smart Turn (WARNING w
            # logu przy 0.3: "Built-in p99 latency values assume stop_secs=0.2").
            params=VADParams(confidence=0.6, start_secs=0.2, stop_secs=0.2, min_volume=0.4)
        )
    )
    gemini_user_monitor = GeminiUserMonitor(gemini_state)
    gemini_bot_monitor = GeminiBotMonitor(gemini_state)
    # Zawsze natywny głos Gemini (modalities=AUDIO) — hybryda z ElevenLabs jako
    # głównym głosem (2026-09-01) usunięta: mierzalnie wolniejsza (SimpleTextAggregator
    # czeka na CAŁE zdanie z Gemini zanim w ogóle wyśle tekst do TTS, patrz logi z
    # 2026-09-01 — user->bot audio ~4.1s śr. z ElevenLabs vs ~2.4s natywnie), a jakość
    # głosu nie rekompensowała tej różnicy w ocenie na żywo. fallback_tts zostaje w
    # OSOBNEJ gałęzi ParallelPipeline z wymuszonym sample_rate=24000 (ten sam co natywne
    # audio Gemini) wyłącznie pod speak_directly() (idle-nudge, "czy nadal jesteśmy
    # połączeni?") — Gemini nie da się w niezawodny sposób poprosić o wypowiedzenie
    # DOKŁADNEGO tekstu z zewnątrz, gdy jego sesja akurat ucichła.
    fallback_tts = create_tts_service(tenant, sample_rate=24000)
    voice_steps = [ParallelPipeline([llm], [fallback_tts])]

    pipeline = Pipeline([
        transport.input(),
        vad_processor,
        user_aggregator,
        gemini_user_monitor,
        *voice_steps,
        gemini_bot_monitor,
        transport.output(),
        assistant_aggregator,
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=16000,
        ),
    )
    task_box["task"] = task

    @transport.event_handler("on_client_connected")
    async def on_connect_vonage(transport, client):
        logger.info("🎤 [GEMINI LIVE TEST/VONAGE] Klient połączony — wybudzam do przywitania")
        # Patrz komentarz w on_connect (Twilio) wyżej — zamiast pustego push_context_frame()
        # (który dublował cały system prompt jako seed i dawał ~15-17s do pierwszego dźwięku
        # na żywym telefonie), wysyłamy krótki jawny "zapłon" przez LLMMessagesAppendFrame.
        # EKSPERYMENT 2026-09-10 — patrz komentarz w on_connect (Twilio) wyżej, ten sam eksperyment.
        await asyncio.sleep(0.3)
        logger.info("🎤 [GEMINI LIVE TEST/VONAGE] Wysyłam LLMMessagesAppendFrame (kick startowy)")
        await task.queue_frames([
            LLMMessagesAppendFrame(
                messages=[{"role": "user", "content": "(początek rozmowy)"}],
                run_llm=True,
            )
        ])
        logger.info("🎤 [GEMINI LIVE TEST/VONAGE] Kick startowy wysłany bez wyjątku")
        asyncio.create_task(monitor_gemini_call_health(task, gemini_state, llm))

    @transport.event_handler("on_client_disconnected")
    async def on_disconnect_vonage(transport, client):
        logger.info("📴 [GEMINI LIVE TEST/VONAGE] Klient rozłączony")
        gemini_state["ended"] = True
        await task.queue_frame(EndFrame())

    runner = PipelineRunner()
    logger.info("🚀 [GEMINI LIVE TEST/VONAGE] Start pipeline")
    try:
        await runner.run(task)
    except Exception as e:
        logger.error(f"[GEMINI LIVE TEST/VONAGE] Pipeline error: {e}")
    finally:
        logger.info("🏁 [GEMINI LIVE TEST/VONAGE] Koniec połączenia")
        try:
            await save_call_transcript(tenant, call_sid, caller_phone, llm_context)
        except Exception as e:
            logger.error(f"[GEMINI LIVE TEST/VONAGE] Call transcript error: {e}")
        try:
            await maybe_send_call_summary(tenant, caller_phone, llm_context, gemini_state, call_sid=call_sid)
        except Exception as e:
            logger.error(f"[GEMINI LIVE TEST/VONAGE] Call summary error: {e}")
