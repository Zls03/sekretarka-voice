"""Gotowe odpowiedzi dla operatorów: TwiML (Twilio) i NCCO (Vonage)."""

from fastapi.responses import JSONResponse, Response

MSG_NUMBER_INACTIVE = "Numer testowy nieaktywny."
MSG_LINE_UNAVAILABLE = "Przepraszamy, linia jest chwilowo niedostępna."
MSG_CONNECTION_ERROR = "Przepraszamy, wystąpił błąd połączenia."

# Vonage przesyła audio jako surowe PCM 16-bit, 16 kHz.
VONAGE_WEBSOCKET_CONTENT_TYPE = "audio/l16;rate=16000"


def twiml(content: str) -> Response:
    return Response(content=content, media_type="application/xml")


def twiml_say(text: str, *, hangup: bool = False) -> Response:
    return twiml(
        f'<?xml version="1.0"?><Response><Say language="pl-PL">{text}</Say>{"<Hangup/>" if hangup else ""}</Response>'
    )


def twiml_media_stream(host: str, ws_path: str, *, call_sid: str, tenant_phone: str, caller_phone: str) -> Response:
    """Łączy rozmowę z naszym websocketem (Twilio Media Streams) i przekazuje dane połączenia.

    Przekazujemy numer firmy (nie jej id), żeby websocket mógł od razu pobrać pełne dane
    firmy jednym zapytaniem.
    """
    return twiml(f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://{host}/{ws_path}">
            <Parameter name="callSid" value="{call_sid}" />
            <Parameter name="phone" value="{tenant_phone}" />
            <Parameter name="callerPhone" value="{caller_phone}" />
        </Stream>
    </Connect>
</Response>''')


def ncco_talk(text: str) -> list[dict]:
    return [{"action": "talk", "text": text, "language": "pl-PL"}]


def ncco_connect_websocket(uri: str) -> list[dict]:
    return [
        {
            "action": "connect",
            "endpoint": [{"type": "websocket", "uri": uri, "content-type": VONAGE_WEBSOCKET_CONTENT_TYPE}],
        }
    ]


def ncco_response(ncco: list[dict]) -> JSONResponse:
    return JSONResponse(ncco)
