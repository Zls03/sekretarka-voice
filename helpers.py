"""
VOICE AI - HELPERS
==================
Obsługuje dwie bazy Turso:
- Baza ADMINA  (TURSO_DATABASE_URL)      → tabela tenants (ręcznie dodawane firmy)
- Baza SaaS    (SAAS_TURSO_DATABASE_URL) → tabela firms   (firmy z panelu użytkowników)

Funkcja get_tenant_by_phone() sprawdza obie bazy.
Admina ma priorytet — jeśli numer znajdzie się w obu, wygrywa admin.
"""
from dotenv import load_dotenv
load_dotenv()

import os
import httpx
from typing import Optional, Dict, List
from loguru import logger

# ==========================================
# KONFIGURACJA
# ==========================================

TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN   = os.getenv("TURSO_AUTH_TOKEN", "")

SAAS_TURSO_URL   = os.getenv("SAAS_TURSO_DATABASE_URL", "")
SAAS_TURSO_TOKEN = os.getenv("SAAS_TURSO_AUTH_TOKEN", "")

ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")


# ==========================================
# DESZYFROWANIE AUTH TOKEN (AES-GCM)
# ==========================================

def decrypt_token(encrypted: str) -> str:
    if not encrypted or ":" not in encrypted:
        return encrypted

    if not ENCRYPTION_KEY:
        logger.warning("⚠️ ENCRYPTION_KEY not set — cannot decrypt Twilio Auth Token")
        return ""

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        import base64

        key_bytes = ENCRYPTION_KEY[:32].encode("utf-8")
        iv_b64, ct_b64 = encrypted.split(":", 1)
        iv = base64.b64decode(iv_b64)
        ciphertext = base64.b64decode(ct_b64)

        aesgcm = AESGCM(key_bytes)
        plaintext = aesgcm.decrypt(iv, ciphertext, None)
        return plaintext.decode("utf-8")
    except Exception as e:
        logger.error(f"❌ decrypt_token failed: {e}")
        return ""


# ==========================================
# TURSO DATABASE CLIENT
# ==========================================

class TursoDB:
    def __init__(self, url: str, token: str, label: str = "db"):
        self.url   = url.replace("libsql://", "https://") if url else ""
        self.token = token
        self.label = label
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def is_configured(self) -> bool:
        return bool(self.url and self.token)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def execute(self, sql: str, args: List = None) -> List[Dict]:
        if not self.is_configured:
            logger.warning(f"[{self.label}] DB not configured")
            return []

        try:
            client = self._get_client()
            response = await client.post(
                f"{self.url}/v2/pipeline",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
                json={
                    "requests": [
                        {
                            "type": "execute",
                            "stmt": {
                                "sql": sql,
                                "args": [
                                    {"type": "text", "value": str(a) if a is not None else None}
                                    for a in (args or [])
                                ],
                            },
                        },
                        {"type": "close"},
                    ]
                },
            )

            if response.status_code == 200:
                data = response.json()
                results = data.get("results", [])
                if results and results[0].get("type") == "ok":
                    result = results[0].get("response", {}).get("result", {})
                    cols = [c.get("name") for c in result.get("cols", [])]
                    rows = []
                    for row in result.get("rows", []):
                        row_dict = {}
                        for i, col in enumerate(cols):
                            val = row[i]
                            row_dict[col] = val.get("value") if isinstance(val, dict) else val
                        rows.append(row_dict)
                    return rows
            else:
                logger.error(f"[{self.label}] HTTP {response.status_code}: {response.text[:200]}")

        except Exception as e:
            logger.error(f"[{self.label}] DB error: {e}")

        return []


db      = TursoDB(TURSO_DATABASE_URL, TURSO_AUTH_TOKEN, label="admin")
saas_db = TursoDB(SAAS_TURSO_URL, SAAS_TURSO_TOKEN, label="saas")


# ==========================================
# POBIERZ TENANT Z BAZY ADMINA
# ==========================================

async def _get_tenant_from_admin(phone_suffix: str) -> Optional[Dict]:
    rows = await db.execute(
        "SELECT * FROM tenants WHERE phone_number LIKE ? AND is_active = 1",
        [f"%{phone_suffix}"]
    )
    if not rows:
        return None

    tenant = rows[0]
    tenant_id = tenant["id"]

    services = await db.execute(
        "SELECT id, name, duration_minutes, price, description FROM services WHERE tenant_id = ? AND is_active = 1",
        [tenant_id]
    )

    hours_rows = await db.execute(
        "SELECT day_of_week, open_time, close_time FROM working_hours WHERE tenant_id = ?",
        [tenant_id]
    )
    working_hours = [
        {
            "day_of_week": int(h["day_of_week"]) if h["day_of_week"] else 0,
            "open_time":   h["open_time"],
            "close_time":  h["close_time"],
        }
        for h in hours_rows
    ]

    faq_rows = await db.execute(
        "SELECT question, answer FROM tenant_faq WHERE tenant_id = ? ORDER BY sort_order",
        [tenant_id]
    )

    info_services = await db.execute(
        "SELECT name, price, description FROM info_services WHERE tenant_id = ? ORDER BY sort_order",
        [tenant_id]
    )

    logger.info(f"✅ [admin] Found tenant: {tenant.get('name')} (id: {tenant_id})")

    return {
        **tenant,
        "source":             "admin",
        "business_name":      tenant.get("business_name") or tenant.get("name"),
        "services":           services,
        "working_hours":      working_hours,
        "faq":                faq_rows,
        "is_blocked":         int(tenant.get("is_blocked") or 0),
        "minutes_limit":      int(tenant.get("minutes_limit") or 100),
        "minutes_used":       float(tenant.get("minutes_used") or 0),
        "first_message":      tenant.get("first_message") or "Dzień dobry, w czym mogę pomóc?",
        "additional_info":    tenant.get("additional_info") or "",
        "industry":           tenant.get("industry") or "",
        "booking_enabled":    int(tenant.get("booking_enabled") if tenant.get("booking_enabled") is not None else 1),
        "transfer_enabled":   int(tenant.get("transfer_enabled") or 0),
        "transfer_number":    tenant.get("transfer_number") or "",
        "notification_email": tenant.get("notification_email") or tenant.get("email") or "",
        "lead_email_enabled": int(tenant.get("lead_email_enabled") or 0),
        "lead_email":         tenant.get("lead_email") or "",
        "azure_voice_id":     tenant.get("azure_voice_id") or "pl-PL-AgnieszkaNeural",
        "info_services":      info_services,
        "lead_mode":          int(tenant.get("lead_mode") or 0),
        "lead_triggers":      tenant.get("lead_triggers") or "",
        "lead_collection":    tenant.get("lead_collection") or "",
        "lead_urgency_mode":  int(tenant.get("lead_urgency_mode") or 0),
        "lead_urgency_text":  tenant.get("lead_urgency_text") or "",
        "recording_enabled":  int(tenant.get("recording_enabled") or 0),
    }


# ==========================================
# POBIERZ TENANT Z BAZY SaaS
# ==========================================

async def _get_tenant_from_saas(phone_suffix: str) -> Optional[Dict]:
    if not saas_db.is_configured:
        logger.debug("SaaS DB not configured — skipping")
        return None

    rows = await saas_db.execute(
        "SELECT * FROM firms WHERE REPLACE(REPLACE(phone_number, ' ', ''), '-', '') LIKE ? AND is_active = 1 AND is_blocked = 0",
        [f"%{phone_suffix}"]
    )
    if not rows:
        return None

    firm = rows[0]
    firm_id = firm["id"]

    services = await saas_db.execute(
        "SELECT id, name, duration_minutes, price, description FROM services WHERE firm_id = ?",
        [firm_id]
    )

    hours_rows = await saas_db.execute(
        "SELECT day_of_week, open_time, close_time FROM working_hours WHERE firm_id = ?",
        [firm_id]
    )
    working_hours = [
        {
            "day_of_week": int(h["day_of_week"]) if h["day_of_week"] else 0,
            "open_time":   h["open_time"],
            "close_time":  h["close_time"],
        }
        for h in hours_rows
        if h.get("open_time")
    ]

    faq_rows = await saas_db.execute(
        "SELECT question, answer FROM faqs WHERE firm_id = ? ORDER BY created_at",
        [firm_id]
    )

    staff_rows = await saas_db.execute(
        "SELECT * FROM staff WHERE firm_id = ?",
        [firm_id]
    )
    staff_list = []
    for s in staff_rows:
        staff_services = await saas_db.execute(
            """SELECT srv.id, srv.name, srv.duration_minutes, srv.price
               FROM services srv
               JOIN staff_services ss ON srv.id = ss.service_id
               WHERE ss.staff_id = ?""",
            [s["id"]]
        )
        staff_list.append({
            **s,
            "services": staff_services,
            "description": s.get("description") or "",
        })

    # Deszyfruj Auth Token
    raw_token = firm.get("twilio_auth_token") or ""
    decrypted_token = decrypt_token(raw_token) if raw_token else ""

    twilio_sid = firm.get("twilio_account_sid") or ""
    if not twilio_sid:
        user_rows = await saas_db.execute(
            "SELECT twilio_account_sid, twilio_auth_token FROM users WHERE id = ?",
            [firm["user_id"]]
        )
        if user_rows:
            twilio_sid = user_rows[0].get("twilio_account_sid") or ""
            if not decrypted_token:
                raw_user_token = user_rows[0].get("twilio_auth_token") or ""
                decrypted_token = decrypt_token(raw_user_token) if raw_user_token else ""

    # ── Mapowanie TTS provider + voice_id ──
    raw_provider = firm.get("tts_provider") or "google"
    raw_voice_id = firm.get("voice_id") or ""

    # Zabezpieczenie na stare dane gdzie nazwa głosu była wpisana do tts_provider
    google_voices = [
        "pl-PL-Chirp3-HD-Leda", "pl-PL-Chirp3-HD-Aoede",
        "pl-PL-Chirp3-HD-Kore", "pl-PL-Chirp3-HD-Zephyr",
        "pl-PL-Chirp3-HD-Charon", "pl-PL-Chirp3-HD-Fenrir",
        "pl-PL-Chirp3-HD-Orus", "pl-PL-Chirp3-HD-Puck",
    ]
    azure_voices = ["pl-PL-AgnieszkaNeural", "pl-PL-ZofiaNeural", "pl-PL-MarekNeural"]

    if raw_provider in google_voices:
        actual_provider = "google"
        actual_voice_id = raw_provider
    elif raw_provider in azure_voices:
        actual_provider = "azure"
        actual_voice_id = raw_provider
    elif raw_voice_id in google_voices:
        # voice_id wskazuje na głos Google — wymuś google niezależnie od tts_provider
        actual_provider = "google"
        actual_voice_id = raw_voice_id
    elif raw_voice_id in azure_voices:
        actual_provider = "azure"
        actual_voice_id = raw_voice_id
    else:
        actual_provider = raw_provider
        actual_voice_id = raw_voice_id or {
            "google": "pl-PL-Chirp3-HD-Aoede",
            "azure":  "pl-PL-AgnieszkaNeural",
        }.get(actual_provider, "")

    logger.info(f"✅ [saas] Found firm: {firm.get('name')} (id: {firm_id})")
    logger.info(f"   tts_provider: {actual_provider} | voice: {actual_voice_id or 'default'}")

    return {
        "id":               firm_id,
        "slug":             firm_id,
        "source":           "saas",
        "name":             firm.get("name") or "",
        "business_name":    firm.get("name") or "",
        "industry":         firm.get("industry") or "",
        "address":          firm.get("address") or "",
        "email":            firm.get("email") or "",
        "phone_number":     firm.get("phone_number") or "",
        "user_id":          firm.get("user_id") or "", 

        "twilio_account_sid": twilio_sid,
        "twilio_auth_token":  decrypted_token,

        "assistant_name":   firm.get("assistant_name") or "Ania",
        "first_message":    firm.get("first_message") or "Dzień dobry, w czym mogę pomóc?",
        "additional_info":  firm.get("additional_info") or "",

        # TTS — poprawnie rozdzielone
        "tts_provider":        actual_provider,
        "azure_voice_id":      actual_voice_id,
        "elevenlabs_voice_id": actual_voice_id if actual_provider == "elevenlabs" else None,

        "is_active":        int(firm.get("is_active") or 1),
        "is_blocked":       int(firm.get("is_blocked") or 0),
        "minutes_used":     float(firm.get("minutes_used") or 0),
        "minutes_limit":    int(firm.get("minutes_limit") or 100),

        "booking_enabled":    int(firm.get("booking_enabled") if firm.get("booking_enabled") is not None else 1),
        "transfer_enabled":   int(firm.get("transfer_enabled") or 0),
        "transfer_number":    firm.get("transfer_number") or "",

        "notification_email":  firm.get("notification_email") or firm.get("email") or "",
        "lead_email_enabled":  int(firm.get("lead_email_enabled") or 0),
        "lead_email":          firm.get("lead_email") or "",
        "lead_mode":           int(firm.get("lead_mode") or 0),
        "lead_triggers":       firm.get("lead_triggers") or "",
        "lead_collection":     firm.get("lead_collection") or "",
        "lead_urgency_mode":   int(firm.get("lead_urgency_mode") or 0),
        "lead_urgency_text":   firm.get("lead_urgency_text") or "",
        "recording_enabled":   int(firm.get("recording_enabled") or 0),

        "services":      services,
        "working_hours": working_hours,
        "faq":           faq_rows,
        "info_services": services,
        "staff":         staff_list,
    }


# ==========================================
# GŁÓWNA FUNKCJA
# ==========================================

async def get_tenant_by_phone(phone: str) -> Optional[Dict]:
    phone_clean  = phone.replace(" ", "").replace("-", "")
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


# ==========================================
# CRM — profil klienta i zapis wizyty
# ==========================================

PANEL_URL = os.getenv("PANEL_URL", "")
INTERNAL_API_SECRET = os.getenv("INTERNAL_API_SECRET", "")


async def get_client_profile(firm_id: str, phone: str) -> Optional[Dict]:
    """Pobiera profil dzwoniącego klienta z panelu (CRM)."""
    if not PANEL_URL or not INTERNAL_API_SECRET:
        return None
    url = f"{PANEL_URL}/api/internal/client"
    headers = {"x-internal-secret": INTERNAL_API_SECRET}
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            res = await client.get(url, params={"firm_id": firm_id, "phone": phone}, headers=headers)
            if res.status_code == 200:
                return res.json()
    except Exception as e:
        logger.warning(f"CRM lookup failed: {e}")
    return None


async def save_client_visit(firm_id: str, phone: str, name: str, service: str, staff: str, scheduled_at: str, notes: str = ""):
    """Zapisuje/aktualizuje klienta i wizytę w panelu (CRM). Nie blokuje przy błędzie."""
    if not PANEL_URL or not INTERNAL_API_SECRET:
        logger.warning("CRM save_client_visit: PANEL_URL lub INTERNAL_API_SECRET nie ustawione — pomijam")
        return
    url = f"{PANEL_URL}/api/internal/client"
    headers = {
        "x-internal-secret": INTERNAL_API_SECRET,
        "Content-Type": "application/json",
    }
    payload = {
        "firm_id": firm_id,
        "phone": phone,
        "name": name,
        "service": service,
        "staff": staff,
        "scheduled_at": scheduled_at,
    }
    if notes:
        payload["notes"] = notes
    logger.info(f"📋 CRM save: {name} ({phone}) → {service} @ {scheduled_at}")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.post(url, json=payload, headers=headers)
            logger.info(f"📋 CRM response: {res.status_code}")
    except Exception as e:
        logger.warning(f"CRM save failed: {e}")