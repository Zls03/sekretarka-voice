"""Konfiguracja ElevenLabs Conversational AI: klucze, agent, identyfikatory narzędzi."""

import os

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")


# Agent domyślny/fallback ze zmiennej środowiskowej — używany TYLKO gdy tenant nie ma
# własnego elevenlabs_agent_id (panel: zakładka "Głos agenta" -> ElevenLabs). Per-tenant
# pole dopisane 2026-09-03 razem z mostem Vonage (patrz run_elevenlabs_vonage_bot niżej) —
# wcześniej to było na sztywno jeden wspólny agent dla WSZYSTKICH tenantów, co dawało
# każdej firmie ten sam prompt-bazę/głos-bazę ElevenLabs (nasze override'y treści promptu
# i tak nadpisują treść per-rozmowa, ale ustawienia samego agenta typu domyślny model LLM,
# tembr itp. były wspólne).
ELEVENLABS_AGENT_ID = os.getenv("ELEVENLABS_AGENT_ID", "")


# tool_id narzędzia "contact_owner" skonfigurowanego w dashboardzie ElevenLabs (agent
# "Bizvoice Test" -> Narzędzia -> contact_owner). Do 2026-09-04 to narzędzie było
# statycznie przypięte do agenta i ZAWSZE technicznie dostępne dla modelu, niezależnie
# od tenant.get("contact_owner_enabled") — jedyną obroną była instrukcja w promptcie
# ("nie masz tej funkcji, nie oferuj jej") + twardy blok wysyłki po stronie serwera
# (patrz elevenlabs_tool_contact_owner niżej), ale model czasem i tak WERBALNIE oferował
# zebranie wiadomości (złapane na żywym telefonie, tenant z contact_owner_enabled=0).
# Od teraz tool_ids jest jawnie nadpisywany per rozmowa (conversation_config_override.
# agent.prompt.tool_ids) — dokładnie ten sam poziom gwarancji co w Gemini Live/OpenAI
# Realtime, gdzie narzędzie po prostu nie istnieje w tools[] danej rozmowy. Wymaga
# włączonego przełącznika "Tools" w platform_settings.overrides.conversation_config_override.
# agent.prompt.tool_ids na agencie (włączone 2026-09-04 przez PATCH /v1/convai/agents —
# bez tego ElevenLabs po cichu ignoruje tool_ids z override i zawsze używa domyślnego
# zestawu narzędzi agenta, czyli błąd wracałby bez żadnego widocznego sygnału).
# 2026-09-10 — migracja na nowe konto ElevenLabs (drugi Google, promo Creator 22$/11$,
# stare konto konczylo limity). Agent "Bizvoice Test" odtworzony 1:1 (ten sam głos
# 8EWWaNTDrqObI22Gvo1q, model eleven_flash_v2_5, przełącznik tool_ids override włączony)
# i te same 3 narzędzia webhook — wszystkie ID poniżej to ID z NOWEGO konta, stare już
# nieaktualne. ELEVENLABS_API_KEY/ELEVENLABS_AGENT_ID (Railway env) podmienione razem z tym
# pushem, żeby nie było okna gdzie kod i env wskazują na różne konta.
CONTACT_OWNER_TOOL_ID = "tool_7401m1epk46deb1tfab5se2bmgy6"


# tool_id narzędzi rezerwacji (2026-09-08, port realtime_booking.py pod ElevenLabs) —
# ten sam mechanizm/gwarancja co CONTACT_OWNER_TOOL_ID wyżej.
BOOK_APPOINTMENT_TOOL_ID = "tool_4601m1z6ej9verx8qg719pfghxkx"


MANAGE_BOOKING_TOOL_ID = "tool_0801m1z6x57se7396k77bqrhxfs7"


ELEVENLABS_SIP_DOMAIN = "sip.rtc.elevenlabs.io:5060"


def _resolve_agent_id(tenant: dict) -> str:
    """Per-tenant elevenlabs_agent_id (panel: zakładka ElevenLabs), z fallbackiem na
    stałą środowiskową ELEVENLABS_AGENT_ID dla tenantów które go jeszcze nie ustawiły."""
    return (tenant.get("elevenlabs_agent_id") or "").strip() or ELEVENLABS_AGENT_ID


ELEVENLABS_SHARED_SECRET = os.getenv("ELEVENLABS_SHARED_SECRET", "")
