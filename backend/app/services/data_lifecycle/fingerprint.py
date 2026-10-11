"""Erasure fingerprint and purge placeholder -- CP15A.

The fingerprint is HMAC-SHA256(pepper, normalized number): keyed, so the stored value cannot
be reversed by trying every phone number, and stable, so `reapply_erasures` can recognise an
erased person in restored data without the number ever being stored.
"""

import hashlib
import hmac
import uuid

from app.core.config import Settings, get_settings

_DEV_PEPPER_LABEL = b"erasure-pepper:dev:v1"
PLACEHOLDER_PREFIX = "purged:"


def _pepper(settings: Settings) -> bytes:
    if settings.erasure_fingerprint_pepper:
        return settings.erasure_fingerprint_pepper.encode()
    # Non-production only (production refuses to boot without a pepper): a value derived from
    # the JWT key, domain-separated, so development still produces stable fingerprints.
    return hmac.new(settings.jwt_signing_key.encode(), _DEV_PEPPER_LABEL, hashlib.sha256).digest()


def erasure_fingerprint(e164: str, settings: Settings | None = None) -> str:
    key = _pepper(settings or get_settings())
    return hmac.new(key, e164.encode(), hashlib.sha256).hexdigest()


def purge_placeholder(contact_id: uuid.UUID) -> str:
    """Unique, non-reversible stand-in for a scrubbed number (derived from the contact id
    only), so the (campaign_id, normalized_phone_number) unique constraint still holds."""
    return f"{PLACEHOLDER_PREFIX}{contact_id.hex}"
