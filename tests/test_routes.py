"""Tabela routingu — adresy URL są skonfigurowane w konsolach Twilio/Vonage/ElevenLabs,
więc żadna zmiana w kodzie nie może ich przypadkiem przesunąć ani usunąć."""

from conftest import assert_golden


def _flatten(routes):
    for route in routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from _flatten(original.routes)
            continue
        methods = sorted(getattr(route, "methods", None) or ["WEBSOCKET"])
        yield {"path": route.path, "methods": methods}


def test_route_table_is_stable():
    from bot_gemini_test import app

    table = sorted(_flatten(app.routes), key=lambda r: (r["path"], r["methods"]))
    assert_golden("routes.json", table)
