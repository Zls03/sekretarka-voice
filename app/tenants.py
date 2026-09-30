"""Wyszukiwanie firmy (tenanta) po numerze telefonu — najpierw baza admina, potem SaaS.

Firma jest zwracana jako słownik o stałym kształcie, niezależnie od bazy źródłowej.

UWAGA przy dodawaniu kolumny w tabeli `firms` (panel): `_firm_to_tenant` przepisuje
pola JAWNIE, więc nowa kolumna jest niewidoczna dla reszty backendu, dopóki nie zostanie
tu dopisana. Ten błąd powtórzył się już kilka razy (contact_owner_enabled,
custom_report_format, crm_*, transcript_email_enabled) — dopisuj pole od razu.
"""

from loguru import logger

from app.crypto import decrypt_token
from app.db import db, saas_db

DEFAULT_FIRST_MESSAGE = "Dzień dobry, w czym mogę pomóc?"

# Stare dane: nazwa głosu bywała wpisana w kolumnę tts_provider zamiast voice_id.
GOOGLE_VOICES = {
    "pl-PL-Chirp3-HD-Leda",
    "pl-PL-Chirp3-HD-Aoede",
    "pl-PL-Chirp3-HD-Kore",
    "pl-PL-Chirp3-HD-Zephyr",
    "pl-PL-Chirp3-HD-Charon",
    "pl-PL-Chirp3-HD-Fenrir",
    "pl-PL-Chirp3-HD-Orus",
    "pl-PL-Chirp3-HD-Puck",
}
AZURE_VOICES = {"pl-PL-AgnieszkaNeural", "pl-PL-ZofiaNeural", "pl-PL-MarekNeural"}
DEFAULT_VOICES = {"google": "pl-PL-Chirp3-HD-Aoede", "azure": "pl-PL-AgnieszkaNeural"}
DEFAULT_CARTESIA_VOICE = "575a5d29-1fdc-4d4e-9afa-5a9a71759864"


def _int(value, default: int = 0) -> int:
    """Pusta wartość (None, "", 0) -> default."""
    return int(value or default)


def _int_unless_null(value, default: int) -> int:
    """Tylko brak wartości (NULL) -> default; jawne 0 zostaje zerem."""
    return int(value if value is not None else default)


def _text(value) -> str:
    return value or ""


def _parse_working_hours(rows: list[dict], *, open_days_only: bool) -> list[dict]:
    return [
        {
            "day_of_week": int(h["day_of_week"]) if h["day_of_week"] else 0,
            "open_time": h["open_time"],
            "close_time": h["close_time"],
        }
        for h in rows
        if not open_days_only or h.get("open_time")
    ]


def resolve_tts_voice(firm: dict) -> tuple[str, str]:
    """(dostawca TTS, id głosu) zapasowego głosu firmy.

    Jawnie wybrany dostawca zawsze wygrywa. Kolumna voice_id jest współdzielona przez
    zakładki Google/Gemini Live panelu, więc bywa nieaktualna — liczy się tylko przy
    bardzo starych danych bez tts_provider. ElevenLabs ma własną kolumnę głosu.
    """
    db_provider = firm.get("tts_provider")
    provider = db_provider or "google"
    voice_id = _text(firm.get("voice_id"))

    if provider in GOOGLE_VOICES:
        return "google", provider
    if provider in AZURE_VOICES:
        return "azure", provider
    if provider == "cartesia":
        return "cartesia", firm.get("azure_voice_id") or DEFAULT_CARTESIA_VOICE
    if provider == "elevenlabs":
        return "elevenlabs", _text(firm.get("elevenlabs_voice_id"))
    if not db_provider and voice_id in GOOGLE_VOICES:
        return "google", voice_id
    if not db_provider and voice_id in AZURE_VOICES:
        return "azure", voice_id
    return provider, voice_id or DEFAULT_VOICES.get(provider, "")


async def _get_tenant_from_admin(phone_suffix: str) -> dict | None:
    rows = await db.execute("SELECT * FROM tenants WHERE phone_number LIKE ? AND is_active = 1", [f"%{phone_suffix}"])
    if not rows:
        return None
    tenant = rows[0]
    tenant_id = tenant["id"]

    services = await db.execute(
        "SELECT id, name, duration_minutes, price, description FROM services WHERE tenant_id = ? AND is_active = 1",
        [tenant_id],
    )
    hours_rows = await db.execute(
        "SELECT day_of_week, open_time, close_time FROM working_hours WHERE tenant_id = ?", [tenant_id]
    )
    faq_rows = await db.execute(
        "SELECT question, answer FROM tenant_faq WHERE tenant_id = ? ORDER BY sort_order", [tenant_id]
    )
    info_services = await db.execute(
        "SELECT name, price, description FROM info_services WHERE tenant_id = ? ORDER BY sort_order", [tenant_id]
    )
    logger.info(f"✅ [admin] Found tenant: {tenant.get('name')} (id: {tenant_id})")

    return {
        **tenant,
        "source": "admin",
        "business_name": tenant.get("business_name") or tenant.get("name"),
        "services": services,
        "working_hours": _parse_working_hours(hours_rows, open_days_only=False),
        "faq": faq_rows,
        "is_blocked": _int(tenant.get("is_blocked")),
        "minutes_limit": _int(tenant.get("minutes_limit"), 100),
        "minutes_used": float(tenant.get("minutes_used") or 0),
        "first_message": tenant.get("first_message") or DEFAULT_FIRST_MESSAGE,
        "additional_info": _text(tenant.get("additional_info")),
        "industry": _text(tenant.get("industry")),
        "booking_enabled": _int_unless_null(tenant.get("booking_enabled"), 1),
        "transfer_enabled": _int(tenant.get("transfer_enabled")),
        "transfer_number": _text(tenant.get("transfer_number")),
        "notification_email": tenant.get("notification_email") or tenant.get("email") or "",
        "lead_email_enabled": _int(tenant.get("lead_email_enabled")),
        "lead_email": _text(tenant.get("lead_email")),
        "azure_voice_id": tenant.get("azure_voice_id") or "pl-PL-AgnieszkaNeural",
        "info_services": info_services,
        "lead_mode": _int(tenant.get("lead_mode")),
        "lead_triggers": _text(tenant.get("lead_triggers")),
        "lead_collection": _text(tenant.get("lead_collection")),
        "lead_urgency_mode": _int(tenant.get("lead_urgency_mode")),
        "lead_urgency_text": _text(tenant.get("lead_urgency_text")),
        "recording_enabled": _int(tenant.get("recording_enabled")),
    }


async def _load_saas_staff(firm_id: str) -> list[dict]:
    staff_list = []
    for s in await saas_db.execute("SELECT * FROM staff WHERE firm_id = ?", [firm_id]):
        staff_services = await saas_db.execute(
            """SELECT srv.id, srv.name, srv.duration_minutes, srv.price
               FROM services srv
               JOIN staff_services ss ON srv.id = ss.service_id
               WHERE ss.staff_id = ?""",
            [s["id"]],
        )
        staff_list.append({**s, "services": staff_services, "description": s.get("description") or ""})
    return staff_list


async def _resolve_twilio_credentials(firm: dict) -> tuple[str, str]:
    """(Account SID, odszyfrowany Auth Token) firmy, a gdy ich nie ma — jej właściciela."""
    raw_token = _text(firm.get("twilio_auth_token"))
    auth_token = decrypt_token(raw_token) if raw_token else ""
    account_sid = _text(firm.get("twilio_account_sid"))
    if not account_sid:
        user_rows = await saas_db.execute(
            "SELECT twilio_account_sid, twilio_auth_token FROM users WHERE id = ?", [firm["user_id"]]
        )
        if user_rows:
            account_sid = _text(user_rows[0].get("twilio_account_sid"))
            if not auth_token:
                raw_user_token = _text(user_rows[0].get("twilio_auth_token"))
                auth_token = decrypt_token(raw_user_token) if raw_user_token else ""
    return account_sid, auth_token


async def _get_tenant_from_saas(phone_suffix: str) -> dict | None:
    if not saas_db.is_configured:
        logger.debug("SaaS DB not configured — skipping")
        return None

    rows = await saas_db.execute(
        "SELECT * FROM firms WHERE REPLACE(REPLACE(phone_number, ' ', ''), '-', '') LIKE ? "
        "AND is_active = 1 AND is_blocked = 0",
        [f"%{phone_suffix}"],
    )
    if not rows:
        return None
    firm = rows[0]
    firm_id = firm["id"]

    services = await saas_db.execute(
        "SELECT id, name, duration_minutes, price, description, price_text, duration_text "
        "FROM services WHERE firm_id = ?",
        [firm_id],
    )
    hours_rows = await saas_db.execute(
        "SELECT day_of_week, open_time, close_time FROM working_hours WHERE firm_id = ?", [firm_id]
    )
    faq_rows = await saas_db.execute(
        "SELECT question, answer FROM faqs WHERE firm_id = ? ORDER BY created_at", [firm_id]
    )
    staff = await _load_saas_staff(firm_id)
    twilio_sid, twilio_token = await _resolve_twilio_credentials(firm)
    tts_provider, voice_id = resolve_tts_voice(firm)

    logger.info(f"✅ [saas] Found firm: {firm.get('name')} (id: {firm_id})")
    logger.info(f"   tts_provider: {tts_provider} | voice: {voice_id or 'default'}")

    tenant = _firm_to_tenant(firm, twilio_sid, twilio_token, tts_provider, voice_id)
    tenant.update(
        {
            "services": services,
            "working_hours": _parse_working_hours(hours_rows, open_days_only=True),
            "faq": faq_rows,
            "info_services": services,
            "staff": staff,
        }
    )
    return tenant


def _firm_to_tenant(firm: dict, twilio_sid: str, twilio_token: str, tts_provider: str, voice_id: str) -> dict:
    return {
        "id": firm["id"],
        "slug": firm["id"],
        "source": "saas",
        "name": _text(firm.get("name")),
        "business_name": _text(firm.get("name")),
        "industry": _text(firm.get("industry")),
        "address": _text(firm.get("address")),
        "email": _text(firm.get("email")),
        "phone_number": _text(firm.get("phone_number")),
        "user_id": _text(firm.get("user_id")),
        "twilio_account_sid": twilio_sid,
        "twilio_auth_token": twilio_token,
        "assistant_name": firm.get("assistant_name") or "Ania",
        "first_message": firm.get("first_message") or DEFAULT_FIRST_MESSAGE,
        "additional_info": _text(firm.get("additional_info")),
        # Zapasowy głos TTS (komunikaty wypowiadane dosłownie)
        "tts_provider": tts_provider,
        "azure_voice_id": voice_id,
        "elevenlabs_voice_id": voice_id if tts_provider == "elevenlabs" else None,
        # Głos agenta ElevenLabs — niezależny od tts_provider (osobny przełącznik silnika)
        "elevenlabs_agent_voice_id": _text(firm.get("elevenlabs_voice_id")),
        "elevenlabs_tts_stability": firm.get("elevenlabs_tts_stability"),
        "elevenlabs_tts_speed": firm.get("elevenlabs_tts_speed"),
        "elevenlabs_tts_similarity_boost": firm.get("elevenlabs_tts_similarity_boost"),
        "speaking_rate": float(firm.get("speaking_rate") or 1.06),
        "realtime_voice": _text(firm.get("realtime_voice")),
        "gemini_voice": _text(firm.get("gemini_voice")),
        "gemini_native_voice_enabled": _int(firm.get("gemini_native_voice_enabled")),
        "realtime_engine": firm.get("realtime_engine") or "gemini",
        # Status i limity
        "is_active": _int(firm.get("is_active"), 1),
        "is_blocked": _int(firm.get("is_blocked")),
        "minutes_used": float(firm.get("minutes_used") or 0),
        "minutes_limit": _int(firm.get("minutes_limit"), 100),
        # Funkcje rozmowy
        "booking_enabled": _int_unless_null(firm.get("booking_enabled"), 1),
        "transfer_enabled": _int(firm.get("transfer_enabled")),
        "transfer_number": _text(firm.get("transfer_number")),
        "human_first_enabled": _int(firm.get("human_first_enabled")),
        "human_first_timeout_seconds": _int(firm.get("human_first_timeout_seconds"), 15),
        "siperb_sip_username": _text(firm.get("siperb_sip_username")),
        "contact_owner_enabled": _int_unless_null(firm.get("contact_owner_enabled"), 1),
        "contact_owner_closing_line": _text(firm.get("contact_owner_closing_line")),
        # Raporty i powiadomienia
        "custom_report_format": _int(firm.get("custom_report_format")),
        "report_empty_calls": _int(firm.get("report_empty_calls")),
        "transcript_email_enabled": _int(firm.get("transcript_email_enabled")),
        "notification_email": firm.get("notification_email") or firm.get("email") or "",
        "lead_email_enabled": _int(firm.get("lead_email_enabled")),
        "lead_email": _text(firm.get("lead_email")),
        "lead_mode": _int(firm.get("lead_mode")),
        "lead_triggers": _text(firm.get("lead_triggers")),
        "lead_collection": _text(firm.get("lead_collection")),
        "lead_urgency_mode": _int(firm.get("lead_urgency_mode")),
        "lead_urgency_text": _text(firm.get("lead_urgency_text")),
        "recording_enabled": _int(firm.get("recording_enabled")),
        "llm_provider": firm.get("llm_provider") or "groq",
        "llm_model": _text(firm.get("llm_model")),
        # Integracja CRM (klucz API zaszyfrowany w bazie tym samym kluczem co token Twilio)
        "crm_enabled": _int(firm.get("crm_enabled")),
        "crm_provider": _text(firm.get("crm_provider")),
        "crm_domain": _text(firm.get("crm_domain")),
        "crm_api_key": decrypt_token(_text(firm.get("crm_api_key"))),
    }


async def get_tenant_by_phone(phone: str) -> dict | None:
    """Firma obsługująca dany numer; dopasowanie po ostatnich 9 cyfrach (z/bez +48)."""
    phone_clean = phone.replace(" ", "").replace("-", "")
    phone_suffix = phone_clean[-9:] if len(phone_clean) >= 9 else phone_clean

    tenant = await _get_tenant_from_admin(phone_suffix)
    if tenant:
        logger.info(f"📞 Tenant from ADMIN DB: {tenant.get('name')}")
        return tenant

    tenant = await _get_tenant_from_saas(phone_suffix)
    if tenant:
        logger.info(f"📞 Tenant from SAAS DB: {tenant.get('name')}")
        return tenant

    logger.warning(f"❌ No tenant found for suffix: {phone_suffix}")
    return None
