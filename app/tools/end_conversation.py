"""Narzędzie end_conversation — rozłączenie po naturalnym pożegnaniu."""

import asyncio

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.frames.frames import EndFrame
from pipecat.services.llm_service import FunctionCallParams

from app.background import spawn


def build_end_conversation_tool(task_box: dict, call_state: dict) -> FunctionSchema:
    """Global-function odpowiednik end_conversation_function() z usuniętego silnika cascade —
    tam było zawsze dostępne niezależnie od node'a. Bez tego bot nie miał ŻADNEGO
    sposobu żeby rozpoznać koniec rozmowy inaczej niż przez ciszę (10s/20s) — klient
    mówiący "dziękuję, to wszystko" po prostu wisiał w rozmowie aż zadziałał idle timeout.

    call_state: patrz komentarz w build_contact_owner_tool — ten sam fix (call_state["ended"]
    ustawiane od razu), żeby monitor_call_health nie próbował rozłączyć drugi raz."""

    async def handle_end_conversation(params: FunctionCallParams):
        logger.info("👋 [REALTIME TEST] end_conversation — rozłączam po pożegnaniu")
        call_state["ended"] = True
        await params.result_callback({"status": "ok"})

        async def auto_hangup():
            await asyncio.sleep(3.0)  # tyle samo co say_now — tu pożegnanie jest krótkie, z góry znane
            try:
                t = task_box.get("task")
                if t:
                    await t.queue_frame(EndFrame())
                    logger.info("🔚 [REALTIME TEST] EndFrame po end_conversation")
            except Exception as e:
                logger.error(f"[REALTIME TEST] EndFrame po end_conversation error: {e}")

        spawn(auto_hangup())

    return FunctionSchema(
        name="end_conversation",
        description="""Klient KOŃCZY rozmowę — żegna się, dziękuje, mówi że to wszystko. Użyj gdy:
- "dziękuję, to wszystko", "do widzenia", "dzięki, pa", "nic więcej", "to na razie wszystko", "koniec"
⚠️ WAŻNA KOLEJNOŚĆ: wywołaj tę funkcję OD RAZU, NIC nie mówiąc przed nią — żadnego pożegnania,
żadnej zapowiedzi typu "już kończymy". Twoja odpowiedź po wyniku tej funkcji (przyjdzie
automatycznie) to jest to miejsce na krótkie, naturalne pożegnanie (np. "Dziękuję za telefon,
do usłyszenia!", "Miłego dnia!") — RÓŻNE za każdym razem. Jedno pożegnanie, nie dwa.""",
        properties={},
        required=[],
        handler=handle_end_conversation,
    )
