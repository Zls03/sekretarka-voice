"""Zadania w tle typu "odpal i zapomnij" (e-maile, nadzór rozmowy, zapisy poboczne).

Pętla asyncio trzyma tylko słabe referencje do zadań, więc zadanie bez zapisanej
referencji może zostać usunięte przez garbage collector w trakcie działania, a jego
wyjątek przepada bez śladu. `spawn` trzyma referencję do końca i loguje błędy.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from loguru import logger

_running: set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _running.add(task)
    task.add_done_callback(_on_done)
    return task


def _on_done(task: asyncio.Task) -> None:
    _running.discard(task)
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.opt(exception=error).error(f"Zadanie w tle '{task.get_name()}' zakończyło się błędem: {error}")
