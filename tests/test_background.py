"""app.background.spawn — zadania w tle nie giną i nie połykają błędów po cichu."""

import asyncio

from loguru import logger

from app import background


def test_spawn_keeps_reference_until_done_and_logs_errors():
    messages = []
    sink = logger.add(messages.append, level="ERROR")

    async def fails():
        await asyncio.sleep(0)
        raise ValueError("boom")

    async def scenario():
        task = background.spawn(fails(), name="fails")
        assert task in background._running
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        assert task not in background._running

    try:
        asyncio.run(scenario())
    finally:
        logger.remove(sink)
    assert any("fails" in str(m) and "boom" in str(m) for m in messages)
