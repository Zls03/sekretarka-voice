"""Punkt wejścia aplikacji FastAPI — składa routery wszystkich kanałów i silników.

Uruchomienie: `uvicorn app.main:app` (lokalnie także `python -m app.main`).
"""

import os
import sys

from fastapi import FastAPI
from loguru import logger

from app.config import settings
from app.engines.elevenlabs import vonage_bridge as elevenlabs_vonage_bridge
from app.engines.elevenlabs import webhooks as elevenlabs_webhooks
from app.engines.gemini_live import routes as gemini_live_routes
from app.engines.openai_realtime import routes as openai_realtime_routes
from app.telephony import twilio, vonage

logger.remove()
logger.add(sys.stdout, level="DEBUG", format="{time:HH:mm:ss} | {level} | {message}")


def create_app() -> FastAPI:
    # Backend obsługuje wyłącznie webhooki operatorów — publiczna dokumentacja API
    # niepotrzebnie ujawniałaby listę adresów. Lokalnie: ENABLE_API_DOCS=1.
    docs = settings.enable_api_docs
    application = FastAPI(
        title="BizVoice Voice",
        openapi_url="/openapi.json" if docs else None,
        docs_url="/docs" if docs else None,
        redoc_url=None,
    )
    for module in (
        openai_realtime_routes,
        elevenlabs_webhooks,
        vonage,
        twilio,
        gemini_live_routes,
        elevenlabs_vonage_bridge,
    ):
        application.include_router(module.router)
    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8001)))
