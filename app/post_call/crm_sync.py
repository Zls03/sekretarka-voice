"""Wysyłka podsumowania rozmowy do zewnętrznego CRM przez webhook n8n (POC: Pipedrive)."""

import os

from loguru import logger

from app.post_call.summary import _parse_summary_fields

# CRM Integration (n8n + dowolny CRM klienta) — patrz CLAUDE.md, sekcja "CRM Integration".
# 2026-09-13: POC ograniczony wyłącznie do numeru demo/sprzedażowego BizVoice
# (+48459050542) przez sztywną listę numerów. 2026-09-18: zastąpione przełącznikiem
# per-tenant z panelu (firms.crm_enabled/crm_provider/crm_domain/crm_api_key,
# patrz bizvoice-panel/src/app/firm/[id]/page.tsx sekcja "Integracja CRM") — każda
# firma sama decyduje i sama podaje swój klucz, zero sztywnych numerów w kodzie.
# _CRM_TEST_PHONE_NUMBERS zostaje jako fallback WYŁĄCZNIE na wypadek starych tenantów
# sprzed migracji których ktoś zapomniał przełączyć w panelu — docelowo martwy kod.
_CRM_TEST_PHONE_NUMBERS = {"+48459050542"}


N8N_CRM_WEBHOOK_URL = os.getenv(
    "N8N_CRM_WEBHOOK_URL", "https://magnus1503.app.n8n.cloud/webhook/call-summary"
)


def _is_crm_test_tenant(tenant: dict) -> bool:
    """Nazwa zostaje z czasów POC (patrz komentarz wyżej) żeby nie zmieniać nazwy w
    wywołaniach w bot_elevenlabs_agent.py/realtime_tools.py — dziś sprawdza realny
    przełącznik per-firma, nie tylko testowy numer."""
    if int(tenant.get("crm_enabled") or 0) == 1 and (tenant.get("crm_api_key") or "").strip():
        return True
    phone = (tenant.get("phone_number") or "").replace(" ", "").replace("-", "")
    return phone in _CRM_TEST_PHONE_NUMBERS


async def estimate_deal_value(summary: str, tenant: dict) -> int | None:
    """Osobne, dodatkowe wywołanie GPT WYŁĄCZNIE do oszacowania wartości transakcji dla
    CRM (pole Deal.value) — woła się tylko dla testowego tenanta CRM i tylko gdy
    rozmowa wygląda na gorący lead (patrz maybe_send_to_crm). Celowo CAŁKOWICIE
    odizolowane od summarize_conversation_lines/generate_conversation_summary (funkcja
    mailowa) — nowa, osobna funkcja, nie modyfikacja tamtego promptu — więc nie ma
    żadnego ryzyka dla raportów mailowych innych firm."""
    try:
        additional_info = (tenant.get("additional_info") or "").strip()
        system_content = (
            "Na podstawie poniższego streszczenia rozmowy telefonicznej i cennika/oferty "
            "firmy oszacuj miesięczną wartość tej transakcji w złotych, jeśli da się to "
            "wywnioskować z rozmowy (np. klient wspomniał konkretny pakiet/usługę z "
            "cennika). Odpowiedz WYŁĄCZNIE samą liczbą całkowitą (bez \"zł\", bez spacji, "
            "bez opisu), albo słowem \"brak\" jeśli nie da się tego ocenić.\n"
            f"Cennik/kontekst firmy: {additional_info}"
        )
        import openai
        client = openai.AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        response = await client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": summary},
            ],
            max_tokens=20,
            temperature=0,
        )
        digits = "".join(ch for ch in response.choices[0].message.content if ch.isdigit())
        return int(digits) if digits else None
    except Exception as e:
        logger.error(f"📋 [CRM] Estymacja wartości deala error: {e}")
        return None


async def maybe_send_to_crm(tenant: dict, caller_phone: str, summary: str) -> None:
    """Wysyła streszczenie rozmowy do n8n → CRM klienta. Nieblokująca — błąd tutaj
    (n8n padł, timeout) nigdy nie może wywrócić resztę maybe_send_call_summary; mail
    idzie niezależnie od tego czy to się uda.

    tenant_phone (NUMER FIRMY, przypisany numer BizVoice — nie mylić z caller_phone,
    czyli numerem DZWONIĄCEGO) jedzie w payloadzie właśnie po to, żeby n8n miał
    stabilny, unikalny klucz do routingu "która firma → który CRM/credential", gdy
    dojdzie kolejny klient z własnym Pipedrive/Bitrix24 (patrz CLAUDE.md).

    2026-09-14 — dodane pola reason/details/outcome (parsowane z summary, patrz
    _parse_summary_fields) i estimated_value (patrz estimate_deal_value, tylko dla
    🔥 GORĄCY LEAD) — dają n8n materiał do ustawienia osobnych pól w Pipedrive (Deal
    custom fields + Deal.value) zamiast tylko jednego bloku tekstu w notatce.

    2026-09-17 — dodane pole `priority` (parsowane z linii "Priorytet: <emoji> <etykieta>"
    w summary, patrz _parse_summary_fields). WAŻNE: priorytet w summary NIE jest na
    początku całego tekstu — to jeden z wypunktowań w środku ("- Priorytet: 🔥 GORĄCY
    LEAD\n- Kto dzwonił: ...", patrz prompt w summarize_conversation_lines()). Test na
    żywym telefonie (2026-09-17) pokazał że zarówno ten hook, jak i n8n Code node,
    błędnie zakładały że summary.startswith(emoji) — to nigdy nie mogło zadziałać na
    prawdziwej rozmowie, tylko na ręcznie spreparowanych testowych payloadach gdzie emoji
    wstawialiśmy na starcie tekstu. Stąd `priority` jako osobne, czyste pole zamiast
    każenia n8n zgadywać format z surowego summary.

    2026-09-17 — dodane pola `name`/`organization` (z nowego punktu "Firma" w prompcie,
    patrz summarize_conversation_lines) — n8n używa ich żeby nazwać kontakt w Pipedrive
    imieniem/firmą klienta zamiast samym numerem telefonu, gdy klient je poda. Jedyna
    zmiana w SAMYM prompcie (nie tylko w parsowaniu) w tym pliku — dodaje jeden,
    opcjonalny punkt do streszczenia używanego też przez mail, ale GPT pomija go gdy
    brak danych, więc raporty innych firm wyglądają tak jak wcześniej."""
    try:
        fields = _parse_summary_fields(summary)
        priority = fields.get("Priorytet") or ""
        estimated_value = None
        if "🔥" in priority:
            estimated_value = await estimate_deal_value(summary, tenant)
        import httpx
        async with httpx.AsyncClient() as client:
            await client.post(
                N8N_CRM_WEBHOOK_URL,
                json={
                    "business_name": tenant.get("name") or "",
                    "tenant_phone": tenant.get("phone_number") or "",
                    "caller_phone": caller_phone or "",
                    # 2026-09-18 — crm_provider/crm_domain/crm_api_key per-firma (panel, patrz
                    # ensureColumns() w bizvoice-panel/api/firms/[id]/route.ts) — n8n routuje po
                    # crm_provider i używa TEGO klucza/domeny zamiast sztywnego credentiala.
                    # Puste dla starego testowego tenanta (fallback po numerze w
                    # _is_crm_test_tenant) — n8n dla niego dalej używa własnego, zapisanego
                    # credentiala Pipedrive dopóki nie zostanie przełączony w panelu.
                    "crm_provider": tenant.get("crm_provider") or "",
                    "crm_domain": tenant.get("crm_domain") or "",
                    "crm_api_key": tenant.get("crm_api_key") or "",
                    "summary": summary,
                    "priority": priority,
                    "name": fields.get("Kto dzwonił") or "",
                    "organization": fields.get("Firma") or "",
                    "reason": fields.get("Powód kontaktu") or "",
                    "details": fields.get("Szczegóły") or "",
                    "outcome": fields.get("Wynik rozmowy") or "",
                    "estimated_value": estimated_value,
                },
                timeout=8.0,
            )
    except Exception as e:
        logger.error(f"📋 [CRM] n8n webhook error: {e}")
