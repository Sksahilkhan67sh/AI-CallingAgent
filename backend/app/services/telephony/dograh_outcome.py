"""CP13 (C6): the ONE place that decides what a Dograh signal means.

Two inputs reach this backend from Dograh and both are classified here, so no other
module may branch on a provider status string, HTTP status or disposition:

  * the synchronous trigger failure (`classify_trigger_error`), and
  * the asynchronous completion webhook (`classify_call_outcome`, `is_opt_out`).

Classification only REPORTS. Whether to retry is RecoveryManager's decision, driven by the
`reason_key` returned here; nothing in this module schedules anything.

Vocabulary provenance -- SOURCE-VERIFIED, NOT LIVE-VERIFIED. `call_status` values come from
Dograh's open-source code, not from a live instance:
  * telephony statuses: dograh-hq/dograh `api/enums.py::TelephonyCallStatus`
    (initiated, ringing, in-progress, answered, completed, failed, busy, no-answer,
    canceled, error);
  * call-ended reasons: dograh-hq/pipecat `src/pipecat/utils/enums.py::EndTaskReason`.
The mapping from a Dograh value to OUR state/reason is a documented policy decision (see
docs/CHECKPOINT-13-NOTES.md §6); where Dograh's meaning is not certain the value is mapped to
the conservative side (never a redial of a call that may have happened).
"""

import enum
import re
from dataclasses import dataclass
from typing import Final

from app.models.enums import (
    CallAttemptState,
    MidCallDisconnectReason,
    NeverConnectedFailureReason,
)
from app.services.telephony.dograh_client import DograhApiError, DograhErrorCategory

# RecoveryManager reason keys introduced by CP13. They are plain strings (RecoveryManager's
# rule lookup is keyed on strings), so no database enum changes. A key that is absent from a
# campaign's retry rules is, by RecoveryManager's existing contract, NOT retryable.
REASON_PROVIDER_ERROR: Final = "provider_error"
REASON_RATE_LIMITED: Final = "provider_rate_limited"
REASON_CONFIGURATION: Final = "provider_configuration_error"
REASON_UNRECOGNIZED: Final = "unrecognized_status"


def _token(value: str | None) -> str:
    """Case/whitespace and `-`/space/`_` variants collapse to one token (`no-answer`,
    `No Answer`, `no_answer` are the same value)."""
    return re.sub(r"[\s\-_]+", "_", (value or "").strip().lower()).strip("_")


@dataclass(frozen=True)
class CallOutcome:
    state: CallAttemptState
    never_connected_reason: NeverConnectedFailureReason | None = None
    mid_call_reason: MidCallDisconnectReason | None = None
    # The key RecoveryManager looks up in the campaign's retry rules.
    reason_key: str | None = None
    recognized: bool = True
    # The status says the run has not finished (ringing, in-progress...). No decision may be
    # taken from it: the real call may still be live.
    non_final: bool = False
    # "status" | "disposition" | "unrecognized" | "conflict" -- audit metadata only.
    basis: str = "status"


_NEVER = CallAttemptState.FAILED_TO_CONNECT
_N = NeverConnectedFailureReason
_M = MidCallDisconnectReason


def _never(reason: _N, key: str | None = None) -> CallOutcome:
    return CallOutcome(_NEVER, never_connected_reason=reason, reason_key=key or reason.value)


def _dropped(reason: _M) -> CallOutcome:
    return CallOutcome(
        CallAttemptState.DROPPED_MID_CALL, mid_call_reason=reason, reason_key=reason.value
    )


_ENDED = CallOutcome(CallAttemptState.ENDED_NORMALLY)
_NON_FINAL = CallOutcome(_NEVER, non_final=True, recognized=True)

# token -> outcome. Keys are `_token()` output.
_VOCABULARY: Final[dict[str, CallOutcome]] = {
    # A conversation took place and finished (by either side, or a Dograh limit).
    "completed": _ENDED,
    "user_hangup": _ENDED,
    "end_call": _ENDED,  # the agent ended the call: the normal success path
    "call_transferred": _ENDED,
    "transfer_call": _ENDED,
    "call_duration_exceeded": _ENDED,
    "user_idle_max_duration_exceeded": _ENDED,  # connected; a redial would be a duplicate call
    # The call never reached a person.
    "busy": _never(_N.BUSY),
    "no_answer": _never(_N.NO_ANSWER),
    "voicemail_detected": _never(_N.NO_ANSWER),  # reached a machine, not a person
    "failed": _never(_N.PROVIDER_ERROR),
    "canceled": _never(_N.PROVIDER_ERROR),
    "error": _never(_N.PROVIDER_ERROR),  # also what Dograh stamps on a run it rejected at start
    # The call was live and then broke.
    "unexpected_error": _dropped(_M.TECHNICAL_ISSUE),
    "pipeline_error": _dropped(_M.TECHNICAL_ISSUE),
    "system_cancelled": _dropped(_M.TECHNICAL_ISSUE),
    # Not terminal: a completion webhook should not carry these.
    "initiated": _NON_FINAL,
    "ringing": _NON_FINAL,
    "in_progress": _NON_FINAL,
    "answered": _NON_FINAL,
}

_UNRECOGNIZED = CallOutcome(
    _NEVER,
    never_connected_reason=_N.PROVIDER_ERROR,
    # Not in the retry rules => RecoveryManager terminalizes. Unknown must never become a
    # redial: the call may well have happened.
    reason_key=REASON_UNRECOGNIZED,
    recognized=False,
    basis="unrecognized",
)


def classify_call_outcome(call_status: str | None, call_disposition: str | None) -> CallOutcome:
    """`call_status` ("observed reason the call ended") is the lifecycle authority.
    `call_disposition` falls back to the termination reason, so it is only consulted when it
    is itself a known status value:
      * it may supply the outcome when the status is unknown/empty, but only a FAILURE (acting
        on a failure signal is the safe direction; a success disposition never rescues an
        unknown status);
      * if both are known and disagree on "ended normally?", that is a conflict, handled as
        unrecognized (terminal, audited) rather than trusted either way.
    Business dispositions (`qualified`, `do_not_call`...) are not in the vocabulary, are
    ignored here, and are read by `is_opt_out` instead."""
    by_status = _VOCABULARY.get(_token(call_status))
    by_disposition = _VOCABULARY.get(_token(call_disposition))

    if by_status is None:
        if (
            by_disposition is not None
            and not by_disposition.non_final
            and by_disposition.state != CallAttemptState.ENDED_NORMALLY
        ):
            return _with_basis(by_disposition, "disposition")
        return _UNRECOGNIZED

    if by_disposition is None or by_disposition.non_final or by_status.non_final:
        return by_status
    if (by_status.state == CallAttemptState.ENDED_NORMALLY) != (
        by_disposition.state == CallAttemptState.ENDED_NORMALLY
    ):
        return _with_basis(_UNRECOGNIZED, "conflict")
    return by_status


def _with_basis(outcome: CallOutcome, basis: str) -> CallOutcome:
    return CallOutcome(
        outcome.state,
        never_connected_reason=outcome.never_connected_reason,
        mid_call_reason=outcome.mid_call_reason,
        reason_key=outcome.reason_key,
        recognized=outcome.recognized,
        non_final=outcome.non_final,
        basis=basis,
    )


def is_opt_out(
    call_disposition: str | None,
    mapped_call_disposition: str | None,
    configured_codes: list[str],
) -> bool:
    """Deterministic, exact-match opt-out detection. Only codes the operator configured count
    (Dograh publishes no fixed opt-out code); no substring or free-text matching, and nothing
    an LLM said. Both the raw and the organization-mapped disposition are checked because
    either may be where the workflow's `do_not_call` ends up."""
    codes = {_token(code) for code in configured_codes if code.strip()}
    if not codes:
        return False
    return any(
        value and _token(value) in codes for value in (call_disposition, mapped_call_disposition)
    )


class TriggerPolicy(str, enum.Enum):
    AMBIGUOUS = "ambiguous"  # the trigger may have created a call: reconcile, never retry blind
    RATE_LIMITED = "rate_limited"  # definite no-call; retry later, honoring Retry-After
    PERMANENT = "permanent"  # configuration/auth/request fault: retrying cannot help
    TRANSIENT = "transient"  # definite no-call, provider-side and plausibly recoverable


@dataclass(frozen=True)
class TriggerFailure:
    policy: TriggerPolicy
    reason_key: str
    retry_after_seconds: int | None = None


_PERMANENT_CATEGORIES = frozenset(
    {
        DograhErrorCategory.AUTHENTICATION_ERROR,  # 401 / 403
        DograhErrorCategory.PROVIDER_REJECTED,  # 404: trigger/workflow not found or inactive
        DograhErrorCategory.VALIDATION_ERROR,  # 400 / 402 / 409 / 422 ... other 4xx
    }
)


def classify_trigger_error(exc: DograhApiError) -> TriggerFailure:
    """Dograh's trigger route (source-verified, api/routes/public_agent.py) answers 401 bad
    key, 403 wrong org, 404 trigger/workflow missing or inactive, 400 telephony unconfigured
    or call initiation failed, 402 quota exhausted, 409 workflow without an owner, 429
    concurrent-call limit. None of those is something a redial of the same contact fixes
    except the 429, which is capacity."""
    if exc.is_ambiguous:
        return TriggerFailure(TriggerPolicy.AMBIGUOUS, REASON_PROVIDER_ERROR)
    if exc.category == DograhErrorCategory.RATE_LIMITED:
        return TriggerFailure(
            TriggerPolicy.RATE_LIMITED, REASON_RATE_LIMITED, exc.retry_after_seconds
        )
    if exc.category in _PERMANENT_CATEGORIES:
        return TriggerFailure(TriggerPolicy.PERMANENT, REASON_CONFIGURATION)
    return TriggerFailure(TriggerPolicy.TRANSIENT, REASON_PROVIDER_ERROR)
