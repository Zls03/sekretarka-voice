"""SMS z potwierdzeniem wizyty (Twilio lub Vonage) i licznik wysłanych SMS."""

import os

import httpx
from loguru import logger

from app.db import db


async def send_booking_sms(
    tenant: dict, customer_phone: str, service_name: str,
    staff_name: str, date_str: str, time_str: str, booking_code: str
) -> bool:
    """Wysyła SMS z potwierdzeniem wizyty przez Twilio"""
    import os

    twilio_sid = os.getenv("TWILIO_ACCOUNT_SID")
    twilio_token = os.getenv("TWILIO_AUTH_TOKEN")

    # Użyj numeru firmy z bazy (tenant), nie globalnego!
    twilio_number = tenant.get("phone_number")

    if not twilio_number:
        logger.warning(f"⚠️ No phone_number for tenant {tenant.get('name')}")
        return False

    # Upewnij się że numer ma format +48...
    if not twilio_number.startswith("+"):
        twilio_number = f"+48{twilio_number.replace(' ', '').replace('-', '')[-9:]}"

    if not all([twilio_sid, twilio_token, twilio_number]):
        logger.warning("⚠️ Twilio SMS not configured")
        return False

    if not customer_phone or len(customer_phone) < 9:
        logger.warning(f"⚠️ Invalid phone for SMS: {customer_phone}")
        return False

    # Formatuj numer
    phone = customer_phone.replace(" ", "").replace("-", "")
    if not phone.startswith("+"):
        phone = f"+48{phone[-9:]}"

    business_name = tenant.get("name", "Salon")

    # Krótki SMS (max 160 znaków)
    sms_text = f"{business_name}: {service_name} {date_str} g.{time_str}, {staff_name}. Kod:{booking_code}. Do zobaczenia!"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{twilio_sid}/Messages.json",
                auth=(twilio_sid, twilio_token),
                data={
                    "From": twilio_number,
                    "To": phone,
                    "Body": sms_text,
                }
            )

            if response.status_code in [200, 201]:
                logger.info(f"📱 SMS sent to {phone}: {sms_text[:50]}...")
                return True
            else:
                logger.error(f"📱 SMS error: {response.status_code} - {response.text}")
                return False

    except Exception as e:
        logger.error(f"📱 SMS error: {e}")
        return False


async def send_booking_sms_vonage(
    tenant: dict, customer_phone: str, service_name: str,
    staff_name: str, date_str: str, time_str: str, booking_code: str
) -> bool:
    """Wysyła SMS z potwierdzeniem wizyty przez Vonage SMS API — odpowiednik send_booking_sms()
    dla tenantów obsługiwanych przez Vonage (numer firmy NIE jest numerem Twilio, więc
    send_booking_sms() zwróci błąd Twilio 21659 "not a Twilio phone number"). Wymaga
    VONAGE_API_KEY/VONAGE_API_SECRET (klasyczny klucz/sekret, INNE poświadczenie niż
    VONAGE_APPLICATION_ID/VONAGE_PRIVATE_KEY używane do transferu połączeń w realtime_tools.py —
    to osobny produkt Vonage, trzeba go włączyć/skonfigurować osobno w panelu Vonage)."""
    vonage_key = os.getenv("VONAGE_API_KEY")
    vonage_secret = os.getenv("VONAGE_API_SECRET")

    if not vonage_key or not vonage_secret:
        logger.warning("⚠️ VONAGE_API_KEY/VONAGE_API_SECRET not configured — SMS pominięty")
        return False

    sender = tenant.get("phone_number")
    if not sender:
        logger.warning(f"⚠️ No phone_number for tenant {tenant.get('name')}")
        return False
    sender = sender.replace(" ", "").replace("-", "").lstrip("+")

    if not customer_phone or len(customer_phone) < 9:
        logger.warning(f"⚠️ Invalid phone for SMS: {customer_phone}")
        return False
    phone = customer_phone.replace(" ", "").replace("-", "")
    if not phone.startswith("+"):
        phone = f"+48{phone[-9:]}"
    phone = phone.lstrip("+")

    business_name = tenant.get("name", "Salon")
    sms_text = f"{business_name}: {service_name} {date_str} g.{time_str}, {staff_name}. Kod:{booking_code}. Do zobaczenia!"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                "https://rest.nexmo.com/sms/json",
                data={
                    "api_key": vonage_key,
                    "api_secret": vonage_secret,
                    "from": sender,
                    "to": phone,
                    "text": sms_text,
                },
            )
            if response.status_code == 200:
                data = response.json()
                status = (data.get("messages") or [{}])[0].get("status")
                if status == "0":
                    logger.info(f"📱 [VONAGE] SMS sent to {phone}: {sms_text[:50]}...")
                    return True
                error_text = (data.get("messages") or [{}])[0].get("error-text")
                logger.error(f"📱 [VONAGE] SMS error: status={status} {error_text}")
                return False
            logger.error(f"📱 [VONAGE] SMS error: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logger.error(f"📱 [VONAGE] SMS error: {e}")
        return False


async def increment_sms_count(tenant_id: str):
    """Zwiększa licznik SMS dla tenant"""
    try:
        await db.execute(
            "UPDATE tenants SET sms_count = COALESCE(sms_count, 0) + 1 WHERE id = ?",
            [tenant_id]
        )
        logger.info(f"📱 SMS count incremented for {tenant_id}")
    except Exception as e:
        logger.error(f"📱 SMS count error: {e}")
