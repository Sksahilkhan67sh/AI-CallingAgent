"""Test-side phone helpers (CP14).

The pre-CP14 suite wrote fictional US-style numbers ("555-950-0001") straight into the
database, which only worked because the old normalizer accepted anything. The CP14
normalizer is strict (real, valid numbers; IN-only dialing by default), so those fixtures
were rewritten to valid Indian mobile numbers (`989-...`, validated with
`phonenumbers.is_valid_number`). Digits other than the 555 -> 989 prefix are unchanged, so
every uniqueness / ordering / parametrisation property of the old fixtures still holds.

`normalize_phone_number` keeps its old name so test call sites did not need to change; it
normalizes in the default region WITHOUT the dial-region restriction (fixtures sometimes
need a number the API would refuse).
"""

from app.services.phone import InvalidPhoneError, normalize_phone

InvalidPhoneNumberError = InvalidPhoneError


def normalize_phone_number(raw: str, region: str = "IN") -> str:
    return normalize_phone(raw, region).e164
