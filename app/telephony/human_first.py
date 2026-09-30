""" "Najpierw dzwoni do właściciela": NCCO do apki Siperb (SIP) oraz nagranie i transkrypcja
rozmów odebranych osobiście."""

import time
import uuid
from urllib.parse import quote

from loguru import logger

from app.call_logs import _ensure_call_logs_columns
from app.config import settings
from app.db import saas_db
from app.notifications.push import _send_push_notifications
from app.post_call.summary import _parse_summary_fields, summarize_conversation_lines
from app.telephony.vonage_api import _download_vonage_recording


async def build_human_first_ncco(
    tenant: dict,
    from_number: str,
    to_number: str,
    call_uuid: str,
    host: str,
    region_url: str,
) -> list | None:
    """NCCO dla firm z human_first_enabled=1 — dzwoni NAJPIERW do apki Siperb właściciela
    (SIP, konto założone RĘCZNIE dla TEJ FIRMY — patrz panel: zakładka Ustawienia, sekcja
    "Najpierw dzwoni do właściciela") zamiast od razu do sekretarki AI. v2 tego mechanizmu:
    poprzednia wersja (Vonage Users API + appka WebRTC w /crm) USUNIĘTA 2026-09-28 — nie
    dzwoniła niezawodnie na zablokowanym telefonie (ograniczenia przeglądarki w tle). SIP
    przez natywną appkę Siperb (CallKit/ConnectionService) faktycznie dzwoni — potwierdzone
    na żywo 2026-09-27 (patrz historia sesji: sip_code 404 cannot_route naprawiony przez
    support Siperb — literówka w polu Username + niedopasowana domena From).

    Zwraca None gdy tenant nie ma wypełnionego siperb_sip_username (nawet jeśli
    human_first_enabled=1 — właściciel włączył przełącznik, ale nie dokończył jeszcze
    zakładania/wpisywania połączenia Siperb) — wołający spada wtedy z powrotem na zwykłą
    ścieżkę AI, klient nigdy nie zostaje bez żadnej ścieżki połączenia.

    ⚠️ "eu-west-1-sbc-1.siperb.com" jest na sztywno — to domena SBC z JEDYNEGO konta
    Siperb jakie na razie przetestowaliśmy (nasze, "siperb-bizvoice"). Nie potwierdzone czy
    KAŻDE nowo zakładane konto Siperb dostaje tę samą domenę SBC, czy to zależy od regionu
    wybranego przy zakładaniu konta — przy PIERWSZYM prawdziwym kliencie sprawdź w jego
    apce Siperb (Connections → jego połączenie → to pole) i popraw jeśli inne."""
    sip_username = (tenant.get("siperb_sip_username") or "").strip()
    if not sip_username:
        return None
    timeout = int(tenant.get("human_first_timeout_seconds") or 15)
    fallback_url = (
        f"https://{host}/vonage/human-first-fallback"
        f"?to={quote(to_number, safe='')}&from={quote(from_number, safe='')}"
        f"&uuid={quote(call_uuid, safe='')}&regionUrl={quote(region_url or '', safe='')}"
    )
    # Nagrywanie od momentu odebrania — MUSI być NCCO action PRZED connect, żeby objęło
    # połączoną rozmowę (patrz record_human_first_recording niżej). split="conversation"
    # daje 2-kanałowy plik: kanał 0 = ta noga (dzwoniący klient), kanał 1 = noga połączona
    # (właściciel przez Siperb) — kolejność NIEPOTWIERDZONA na żywo, zweryfikować przy
    # pierwszym prawdziwym nagraniu (patrz komentarz w process_human_first_recording).
    # Darmowe (Vonage call recording = $0.00/min, sprawdzone wcześniej w tej sesji).
    recording_event_url = (
        f"https://{host}/vonage/human-first-recording"
        f"?to={quote(to_number, safe='')}&from={quote(from_number, safe='')}"
        f"&uuid={quote(call_uuid, safe='')}"
    )
    return [
        {
            "action": "record",
            "eventUrl": [recording_event_url],
            "format": "wav",
            "split": "conversation",
        },
        {
            "action": "connect",
            "from": from_number.lstrip("+") if from_number else to_number.lstrip("+"),
            "timeout": timeout,
            "eventType": "synchronous",
            "eventUrl": [fallback_url],
            "endpoint": [
                {
                    "type": "sip",
                    "uri": f"sip:{sip_username}@eu-west-1-sbc-1.siperb.com;transport=udp",
                }
            ],
        },
    ]


async def _transcribe_recording_deepgram(audio_bytes: bytes) -> list[str]:
    """Transkrypcja nagrania rozmowy odebranej osobiście przez właściciela (Deepgram,
    tryb prerecorded — INNY endpoint niż live streaming w bot.py/cascade). multichannel
    rozdziela oba kanały (patrz split="conversation" w build_human_first_ncco) na osobne
    transkrypty, utterances=true grupuje słowa w zdania z czasem startu — łączymy oba
    kanały w jedną chronologiczną listę "Klient: .../Właściciel: ..." pod
    summarize_conversation_lines(), dokładnie ten sam format co dla rozmów z AI."""
    api_key = settings.deepgram_api_key
    if not api_key:
        logger.warning("📼 [HUMAN-FIRST/RECORDING] Brak DEEPGRAM_API_KEY — transkrypcja pominięta")
        return []
    try:
        import httpx

        # nova-3 + language=pl — TEN SAM model co bot.py/cascade (DeepgramSTTService,
        # live_options) używa dla polskiego na żywo od dawna, sprawdzony na produkcji.
        # DEEPGRAM_BASE_URL (opcjonalny, "api.eu.deepgram.com") — ten sam env var co
        # cascade, żeby oba tory trzymały się tego samego regionu gdy ktoś go ustawi.
        base_url = settings.deepgram_base_url
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"https://{base_url}/v1/listen",
                params={
                    "model": "nova-3",
                    "language": "pl",
                    "multichannel": "true",
                    "utterances": "true",
                    "punctuate": "true",
                    "smart_format": "true",
                    "numerals": "true",
                },
                headers={"Authorization": f"Token {api_key}", "Content-Type": "audio/wav"},
                content=audio_bytes,
                timeout=60.0,
            )
        if response.status_code != 200:
            logger.error(f"📼 [HUMAN-FIRST/RECORDING] Deepgram error: {response.status_code} {response.text[:200]}")
            return []
        data = response.json()
        utterances = data.get("results", {}).get("utterances", [])
        utterances_sorted = sorted(utterances, key=lambda u: u.get("start", 0))
        lines = []
        for u in utterances_sorted:
            text = (u.get("transcript") or "").strip()
            if not text:
                continue
            # ⚠️ Kanał 0 = "Klient", kanał 1 = "Właściciel" — założenie z docstringu
            # build_human_first_ncco, NIEPOTWIERDZONE na żywo. Jeśli pierwsze prawdziwe
            # nagranie pokaże odwrotną kolejność, zamień etykiety tutaj.
            speaker_label = "Klient" if u.get("channel", 0) == 0 else "Właściciel"
            lines.append(f"{speaker_label}: {text}")
        return lines
    except Exception as e:
        logger.error(f"📼 [HUMAN-FIRST/RECORDING] Deepgram — wyjątek: {e}")
        return []


async def _save_human_first_call_log(
    tenant: dict,
    call_uuid: str,
    caller_phone: str,
    duration_seconds: int,
    summary: str,
    lines: list[str],
) -> None:
    """Zapisuje rozmowę odebraną osobiście przez właściciela do call_logs/call_transcripts —
    TA SAMA tabela i kształt co rozmowy z AI (żeby portal /crm nie potrzebował żadnej
    specjalnej obsługi), plus answered_by='owner' żeby CRM mógł to oznaczyć osobnym
    znaczkiem zamiast mylić z rozmową bota. Świadomie BEZ apply_call_charge/naliczania
    minut — to nie jest zużycie AI, właściciel rozmawiał sam, nic nie obciąża limitu."""
    tenant_id = tenant.get("id", "")
    if not tenant_id.startswith("firm_"):
        return  # portal /crm dotyczy tylko firm_ (SaaS) — admin DB nie ma tej zakładki
    try:
        await _ensure_call_logs_columns()
        priority = _parse_summary_fields(summary).get("Priorytet") or ""
        call_id = f"call_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        await saas_db.execute(
            """INSERT INTO call_logs
               (id, tenant_id, call_sid, caller_phone, duration_seconds, status, summary, priority, answered_by, created_at)
               VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, 'owner', datetime('now'))""",
            [call_id, tenant_id, call_uuid, caller_phone, duration_seconds, summary, priority],
        )
        for line in lines:
            role, _, content = line.partition(": ")
            await saas_db.execute(
                "INSERT INTO call_transcripts (id, tenant_id, call_sid, role, content, created_at) "
                "VALUES (?, ?, ?, ?, ?, datetime('now'))",
                [f"tr_{uuid.uuid4().hex[:12]}", tenant_id, call_uuid, role or "Rozmowa", content[:500]],
            )
        logger.info(f"📼 [HUMAN-FIRST/RECORDING] Zapisano rozmowę odebraną osobiście: {tenant_id}")
        caller_display = (
            caller_phone
            if caller_phone and caller_phone.lower() not in ("nieznany", "unknown", "")
            else "numer zastrzeżony"
        )
        await _send_push_notifications(
            tenant,
            title="📞 Odebrałeś osobiście",
            body=f"{caller_display}: {summary}",
        )
    except Exception as e:
        logger.error(f"📼 [HUMAN-FIRST/RECORDING] Zapis call_logs — wyjątek: {e}")


async def process_human_first_recording(
    tenant: dict,
    recording_url: str,
    caller_phone: str,
    call_uuid: str,
    duration_seconds: int,
) -> None:
    """Orkiestruje całość: pobranie nagrania -> Deepgram -> podsumowanie (ten sam GPT-4.1-mini
    co dla AI) -> zapis do CRM. Wołane jako osobny task (fire-and-forget) z webhooka
    /vonage/human-first-recording w bot_gemini_test.py — Vonage dostaje szybkie potwierdzenie,
    a przetwarzanie (kilka-kilkanaście sekund: pobranie + Deepgram + GPT) dzieje się w tle."""
    try:
        audio_bytes = await _download_vonage_recording(recording_url)
        if not audio_bytes:
            return
        lines = await _transcribe_recording_deepgram(audio_bytes)
        if not lines:
            logger.warning(f"📼 [HUMAN-FIRST/RECORDING] Pusty transkrypt dla {tenant.get('id')} — pomijam zapis")
            return
        summary = await summarize_conversation_lines(lines, tenant)
        await _save_human_first_call_log(tenant, call_uuid, caller_phone, duration_seconds, summary, lines)
    except Exception as e:
        logger.error(f"📼 [HUMAN-FIRST/RECORDING] process_human_first_recording — wyjątek: {e}")
