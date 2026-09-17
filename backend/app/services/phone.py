"""
Phone number normalization.

Contact deduplication must not rely on raw user-entered phone strings
(Database Design §2.4, Checkpoint 01 spec). This produces a stable E.164-
ish representation: `+` followed by digits only, assuming US/Canada (+1)
for bare 10-digit numbers since no per-contact country is captured yet.
A dedicated phone-parsing library can replace this once international
numbers are in scope -- kept deliberately minimal for this checkpoint.
"""

import re


class InvalidPhoneNumberError(ValueError):
    pass


def normalize_phone_number(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")

    if raw.strip().startswith("+"):
        normalized = f"+{digits}"
    elif len(digits) == 10:
        normalized = f"+1{digits}"
    elif len(digits) == 11 and digits.startswith("1"):
        normalized = f"+{digits}"
    else:
        normalized = f"+{digits}" if digits else ""

    if len(normalized) < 8:
        raise InvalidPhoneNumberError(f"'{raw}' is not a valid phone number")

    return normalized
