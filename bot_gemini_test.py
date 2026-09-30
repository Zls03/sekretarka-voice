"""Zachowany wyłącznie dla kompatybilności wstecznej.

Railway (i ewentualne ręczne komendy startowe) uruchamiają `uvicorn bot_gemini_test:app`.
Właściwa aplikacja mieszka w pakiecie `app/` — patrz `app/main.py`.
"""

from app.main import app

__all__ = ["app"]
