"""Phone number normalization -- the ONE place a phone number is parsed (CP14, C4).

Every write path (contact create/update, CSV import, association, suppression add/import,
the opt-out writers) and every lookup (eligibility, suppression, enqueue) goes through
`normalize_phone`, so the same human input always yields the same stored E.164 string.

Built on `phonenumbers` (libphonenumber). Two things it does by default are NOT acceptable
for a dialer and are rejected here before it is called: it accepts extensions ("x12") and it
converts vanity letters ("1-800-FLOWERS" -> digits). Both would dial a different number than
the operator typed.

Only `InvalidPhoneError` (carrying a reason code) may escape. Callers translate it; nothing
here ever raises a bare library exception, and no message contains the number itself.
"""

import hashlib
import hmac
import re
import unicodedata
from collections.abc import Collection
from dataclasses import dataclass

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat

# Reason codes (the API contract; also the per-row codes of the CSV imports).
EMPTY = "empty"
INVALID_NUMBER = "invalid_number"
TOO_LONG = "too_long"
REGION_NOT_ALLOWED = "region_not_allowed"

_MAX_E164_DIGITS = 15
# Anything longer than this cannot be a phone number; refuse before doing any work on it.
_MAX_RAW_LENGTH = 64
# Digits, a leading '+', and the separators people really type. No letters, '#', ';' or ','
# (those are how extensions, vanity numbers and dial pauses are expressed).
_ALLOWED_CHARS = re.compile(r"^\+?[0-9\s().\-]+$")


class InvalidPhoneError(ValueError):
    """The input is not a dialable phone number. `code` is one of the reason codes above."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class NormalizedPhone:
    e164: str
    region: str  # ISO 3166-1 alpha-2 region the number belongs to


def normalize_phone(
    raw: str | None,
    default_region: str,
    allowed_regions: Collection[str] | None = None,
) -> NormalizedPhone:
    """Parse `raw` into strict E.164, or raise InvalidPhoneError(code).

    `default_region` resolves numbers written without a country code. When `allowed_regions`
    is given the number must belong to one of them (`region_not_allowed` otherwise) -- the
    dial-protection rule. Suppression writers omit it: refusing to record a do-not-call
    number because of its country would only leave it callable.
    """
    if raw is None or not isinstance(raw, str) or not raw.strip():
        raise InvalidPhoneError(EMPTY)
    text = raw.strip()
    if len(text) > _MAX_RAW_LENGTH:
        raise InvalidPhoneError(TOO_LONG)

    # Full-width digits (U+FF10..) fold to ASCII; anything still outside the allowed set
    # (letters, extensions, control characters) is not a plain phone number.
    text = unicodedata.normalize("NFKC", text)
    if not _ALLOWED_CHARS.match(text) or "+" in text[1:]:
        raise InvalidPhoneError(INVALID_NUMBER)
    if sum(ch.isdigit() for ch in text) > _MAX_E164_DIGITS + 2:
        # +2: room for a "00" international prefix or a leading trunk "0" that is stripped.
        raise InvalidPhoneError(TOO_LONG)

    try:
        parsed = phonenumbers.parse(text, default_region)
    except NumberParseException as exc:
        raise InvalidPhoneError(
            TOO_LONG if exc.error_type == NumberParseException.TOO_LONG else INVALID_NUMBER
        ) from None

    e164 = phonenumbers.format_number(parsed, PhoneNumberFormat.E164)
    if len(e164) - 1 > _MAX_E164_DIGITS:
        raise InvalidPhoneError(TOO_LONG)
    if not phonenumbers.is_valid_number(parsed):
        raise InvalidPhoneError(INVALID_NUMBER)

    region = phonenumbers.region_code_for_number(parsed)
    if region is None:  # valid but not attributable to one region (e.g. shared codes)
        raise InvalidPhoneError(INVALID_NUMBER)
    if allowed_regions is not None and region not in {r.upper() for r in allowed_regions}:
        raise InvalidPhoneError(REGION_NOT_ALLOWED)
    return NormalizedPhone(e164=e164, region=region)


def region_of_e164(e164: str) -> str | None:
    """Region of an already-stored number, for the dial-time re-check of legacy rows.
    None when it does not parse as a valid number at all."""
    try:
        parsed = phonenumbers.parse(e164, None)
    except NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.region_code_for_number(parsed)


def is_dialable_region(e164: str, allowed_regions: Collection[str]) -> bool:
    region = region_of_e164(e164)
    return region is not None and region in {r.upper() for r in allowed_regions}


def last4(e164: str) -> str:
    return e164[-4:]


def phone_fingerprint(e164: str, key: str) -> str:
    """Keyed fingerprint for audit correlation: the same number gives the same value, but
    it cannot be reversed by trying all numbers (a plain hash of a 10-digit space can).
    Domain-separated from every other use of the signing key."""
    derived = hmac.new(key.encode(), b"phone-fingerprint:v1", hashlib.sha256).digest()
    return hmac.new(derived, e164.encode(), hashlib.sha256).hexdigest()[:16]


def mask_phone_number(normalized: str) -> str:
    """Checkpoint 07 §13: admin dashboard list/detail views show a masked number by
    default (e.g. +9*******3210) -- only the first two characters and the last 4 stay."""
    if len(normalized) <= 6:
        return "*" * len(normalized)
    country_and_lead = normalized[:2]
    last_four = normalized[-4:]
    masked_middle = "*" * (len(normalized) - len(country_and_lead) - len(last_four))
    return f"{country_and_lead}{masked_middle}{last_four}"
