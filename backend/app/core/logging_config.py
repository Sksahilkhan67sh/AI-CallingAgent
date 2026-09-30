"""Structured JSON logging with PII masking -- Checkpoint 09.

Standard library only. Every `extra={...}` field is emitted as a JSON key
(correlation_id, attempt_id, campaign_id, ... ), and any phone-number-like
value -- in the message or in any field -- is masked, so callers cannot
leak a number by accident.
"""

import json
import logging
import re
from typing import Any

from app.core.config import get_settings

_PHONE = re.compile(r"\+?\d[\d\-\s().]{6,}\d")
_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}
_SECRET_KEYS = ("secret", "token", "password", "api_key", "authorization")


def mask_phone(value: str) -> str:
    """'+919876543210' -> '+91******3210' (keeps a short prefix and last 4)."""
    digits = re.sub(r"[^\d+]", "", value)
    if len(digits) <= 6:
        return "*" * len(digits)
    prefix = digits[:3] if digits.startswith("+") else digits[:2]
    return f"{prefix}{'*' * (len(digits) - len(prefix) - 4)}{digits[-4:]}"


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return _PHONE.sub(lambda m: mask_phone(m.group(0)), value)
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_scrub(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "event": _scrub(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            if any(s in key.lower() for s in _SECRET_KEYS):
                entry[key] = "[redacted]"
            else:
                entry[key] = _scrub(value)
        if record.exc_info:
            # Type only: exception messages may embed URLs/identifiers.
            entry["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(entry, default=str)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(get_settings().log_level.upper())
