"""
VOICE AI - HELPERS (cleaned)
============================
Zawiera tylko używane funkcje:
- TursoDB - baza danych
- get_tenant_by_phone - pobieranie tenant
"""
from dotenv import load_dotenv
load_dotenv()

import os
import httpx
from datetime import datetime
from typing import Optional, Dict, List
from loguru import logger

# ==========================================
# KONFIGURACJA
# ==========================================
TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")


# ==========================================
# TURSO DATABASE
# ==========================================
class TursoDB:
    def __init__(self):
        self.url = TURSO_DATABASE_URL.replace("libsql://", "https://")
        self.token = TURSO_AUTH_TOKEN
        
    async def execute(self, sql: str, args: List = None) -> List[Dict]:
        if not self.url or not self.token:
            logger.warning("DB not configured")
            return []
            
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.url}/v2/pipeline",
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "requests": [
                            {
                                "type": "execute",
                                "stmt": {
                                    "sql": sql,
                                    "args": [{"type": "text", "value": str(a) if a is not None else None} for a in (args or [])]
                                }
                            },
                            {"type": "close"}
                        ]
                    },
                    timeout=10.0
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
        except Exception as e:
            logger.error(f"DB error: {e}")
        return []

db = TursoDB()


# ==========================================
# TENANT
# ==========================================
async def get_tenant_by_phone(phone: str) -> Optional[Dict]:
    """Pobierz tenant po numerze telefonu"""
    phone_clean = phone.replace(" ", "").replace("-", "")
    phone_suffix = phone_clean[-9:] if len(phone_clean) >= 9 else phone_clean
    
    rows = await db.execute(
        "SELECT * FROM tenants WHERE phone_number LIKE ? AND is_active = 1",
        [f"%{phone_suffix}"]
    )
    
    if not rows:
        return None
    
    tenant = rows[0]
    tenant_id = tenant["id"]
    
    # Usługi
    services = await db.execute(
        "SELECT id, name, duration_minutes, price, description FROM services WHERE tenant_id = ? AND is_active = 1",
        [tenant_id]
    )
    
    # Godziny pracy - jako lista (dla build_business_context)
    hours_rows = await db.execute(
        "SELECT day_of_week, open_time, close_time FROM working_hours WHERE tenant_id = ?",
        [tenant_id]
    )
    working_hours = []
    for h in hours_rows:
        working_hours.append({
            "day_of_week": int(h["day_of_week"]) if h["day_of_week"] else 0,
            "open_time": h["open_time"],
            "close_time": h["close_time"]
        })
    
    # FAQ
    faq_rows = await db.execute(
        "SELECT question, answer FROM tenant_faq WHERE tenant_id = ? ORDER BY sort_order",
        [tenant_id]
    )
    
    # Usługi informacyjne (dla trybu bez rezerwacji)
    info_services = await db.execute(
        "SELECT name, price, description FROM info_services WHERE tenant_id = ? ORDER BY sort_order",
        [tenant_id]
    )
    
    return {
        **tenant,
        "business_name": tenant.get("business_name") or tenant.get("name"),
        "services": services,
        "working_hours": working_hours,
        "faq": faq_rows,
        "is_blocked": int(tenant.get("is_blocked") or 0),
        "minutes_limit": int(tenant.get("minutes_limit") or 100),
        "minutes_used": float(tenant.get("minutes_used") or 0),
        "first_message": tenant.get("first_message") or "Dzień dobry, w czym mogę pomóc?",
        "additional_info": tenant.get("additional_info") or "",
        "industry": tenant.get("industry") or "",
        "booking_enabled": int(tenant.get("booking_enabled") if tenant.get("booking_enabled") is not None else 1),
        "transfer_enabled": int(tenant.get("transfer_enabled") or 0),
        "transfer_number": tenant.get("transfer_number") or "",
        "notification_email": tenant.get("notification_email") or tenant.get("email") or "",
        "info_services": info_services,
    }