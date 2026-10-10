"""The structured-output contract the Dograh QA node must produce -- CP14B.

The QA node's system prompt (docs/CHECKPOINT-14B-NOTES.md §QA setup) instructs Dograh's LLM
to return exactly this JSON. Everything arriving from Dograh -- including text a caller
spoke that the QA LLM echoed into a field -- is UNTRUSTED: this module validates strictly
and never repairs. A violation raises AnalysisLLMValidationError (a retriable invalid-output
failure, bounded by the worker's attempt cap) and never produces a result.

The LLM extracts signals and evidence only. It does NOT supply a score: lead_score stays the
deterministic function in app.services.analysis.scoring (CP06 §14). The booleans below are
consumed by it, and consistency rules stop a contradictory output (e.g. an INTERESTED
classification alongside an explicit rejection or opt-out) from being persisted.
"""

import json
from typing import Any

from app.models.enums import AnalysisIntent, AnalysisNextAction, AnalysisSentiment, InterestStatus
from app.services.analysis.llm.base import AnalysisLLMValidationError
from app.services.analysis.llm.schemas import AnalysisResult, ScoringSignals

CONTRACT_VERSION = "cp14b.v1"

_MAX_SUMMARY = 1_000
_MAX_FEEDBACK = 500
_MAX_ITEM = 200
_MAX_ITEMS = 10
_MAX_LANGUAGE = 16
_MAX_OBJECTIONS_COUNT = 20
_REQUIRED = (
    "schema_version",
    "summary",
    "intent",
    "interest_status",
    "sentiment",
    "next_action",
    "language",
    "signals",
)
_ALLOWED = frozenset(_REQUIRED) | {"feedback", "key_facts", "objections", "customer_needs"}
_BOOL_SIGNALS = (
    "explicit_interest",
    "purchase_intent",
    "requested_callback",
    "requested_pricing",
    "has_timeline",
    "explicit_rejection",
    "opted_out",
    "information_requested",
)


def _fail(reason: str) -> AnalysisLLMValidationError:
    # `reason` is one of our own fixed strings -- never model/caller text.
    return AnalysisLLMValidationError(f"invalid QA output: {reason}")


def _enum(enum_cls: Any, value: Any, field: str) -> Any:
    if not isinstance(value, str):
        raise _fail(f"{field} not a string")
    try:
        return enum_cls(value)
    except ValueError:
        raise _fail(f"{field} has an unknown value") from None


def _text(value: Any, field: str, limit: int, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise _fail(f"{field} missing")
        return None
    if not isinstance(value, str):
        raise _fail(f"{field} not a string")
    cleaned = " ".join(value.split())
    if required and not cleaned:
        raise _fail(f"{field} empty")
    if len(cleaned) > limit:
        raise _fail(f"{field} exceeds {limit} characters")
    return cleaned or None


def _items(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _fail(f"{field} not a list")
    if len(value) > _MAX_ITEMS:
        raise _fail(f"{field} has more than {_MAX_ITEMS} entries")
    out: list[str] = []
    for item in value:
        text = _text(item, field, _MAX_ITEM, required=True)
        assert text is not None
        out.append(text)
    return out


def find_qa_payload(annotations: Any, *, configured_key: str) -> dict[str, Any] | None:
    """Locate the QA node's JSON inside run.annotations. Returns None when absent.

    The key naming is undocumented, so: use the configured key if set; otherwise take the
    single dict entry carrying our contract marker. More than one candidate is ambiguous and
    refused (never guess which one to trust)."""
    if not isinstance(annotations, dict) or not annotations:
        return None
    if configured_key:
        value = annotations.get(configured_key)
        return value if isinstance(value, dict) else None
    candidates = [
        v
        for v in annotations.values()
        if isinstance(v, dict) and v.get("schema_version") == CONTRACT_VERSION
    ]
    if len(candidates) > 1:
        raise _fail("ambiguous: more than one annotation carries the contract marker")
    return candidates[0] if candidates else None


def parse_qa_payload(payload: Any, *, max_bytes: int) -> AnalysisResult:
    if not isinstance(payload, dict):
        raise _fail("not a JSON object")
    try:
        size = len(json.dumps(payload, separators=(",", ":")).encode())
    except (TypeError, ValueError):
        raise _fail("not serialisable") from None
    if size > max_bytes:
        raise _fail("output exceeds the configured size limit")
    missing = [k for k in _REQUIRED if k not in payload]
    if missing:
        raise _fail(f"missing required field(s): {', '.join(sorted(missing))}")
    unknown = set(payload) - _ALLOWED
    if unknown:
        raise _fail("unexpected field(s) present")
    if payload["schema_version"] != CONTRACT_VERSION:
        raise _fail("unsupported schema_version")

    signals_raw = payload["signals"]
    if not isinstance(signals_raw, dict):
        raise _fail("signals not an object")
    if set(signals_raw) - set(_BOOL_SIGNALS) - {"objection_count"}:
        raise _fail("unexpected signal(s) present")
    flags: dict[str, bool] = {}
    for name in _BOOL_SIGNALS:
        raw = signals_raw.get(name, False)
        if not isinstance(raw, bool):
            raise _fail(f"signal {name} not a boolean")
        flags[name] = raw
    count = signals_raw.get("objection_count", 0)
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or not 0 <= count <= _MAX_OBJECTIONS_COUNT
    ):
        raise _fail("objection_count out of range")

    intent = _enum(AnalysisIntent, payload["intent"], "intent")
    interest = _enum(InterestStatus, payload["interest_status"], "interest_status")
    # Consistency (CP14B §9): a positive classification can never coexist with an explicit
    # rejection / opt-out -- reject rather than let a high score override NOT_INTERESTED.
    if (flags["opted_out"] or flags["explicit_rejection"]) and (
        intent in (AnalysisIntent.INTERESTED, AnalysisIntent.CALLBACK_REQUESTED)
        or interest == InterestStatus.INTERESTED
    ):
        raise _fail("positive classification contradicts an explicit rejection or opt-out")

    summary = _text(payload["summary"], "summary", _MAX_SUMMARY, required=True)
    assert summary is not None
    language = _text(payload["language"], "language", _MAX_LANGUAGE, required=True)
    assert language is not None

    return AnalysisResult(
        summary=summary,
        intent=intent,
        interest_status=interest,
        sentiment=_enum(AnalysisSentiment, payload["sentiment"], "sentiment"),
        next_action=_enum(AnalysisNextAction, payload["next_action"], "next_action"),
        feedback=_text(payload.get("feedback"), "feedback", _MAX_FEEDBACK, required=False),
        key_facts=_items(payload.get("key_facts"), "key_facts"),
        objections=_items(payload.get("objections"), "objections"),
        customer_needs=_items(payload.get("customer_needs"), "customer_needs"),
        language=language,
        scoring_signals=ScoringSignals(objection_count=count, **flags),
    )
