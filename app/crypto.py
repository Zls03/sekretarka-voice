"""Odszyfrowywanie sekretów zapisanych przez panel (AES-GCM, klucz ENCRYPTION_KEY)."""


from loguru import logger

from app.config import settings

ENCRYPTION_KEY = settings.encryption_key


def decrypt_token(encrypted: str) -> str:
    if not encrypted or ":" not in encrypted:
        return encrypted

    if not ENCRYPTION_KEY:
        logger.warning("⚠️ ENCRYPTION_KEY not set — cannot decrypt Twilio Auth Token")
        return ""

    try:
        import base64

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

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
