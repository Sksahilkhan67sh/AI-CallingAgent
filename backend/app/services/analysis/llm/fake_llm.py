"""Deterministic fake analysis LLM -- no real model is named in the specs
and no credentials exist in this environment, mirroring
app/services/ai/llm/fake_llm.py's FakeLLM. Keyword-driven off the
prepared transcript so tests can construct predictable analyses without
depending on an internet API. Real classification accuracy is a
real-provider concern; this fake exists to test the CP06 pipeline
(admission, worker, scoring, idempotency, retries) deterministically.
"""

from app.models.enums import AnalysisIntent, AnalysisNextAction, AnalysisSentiment, InterestStatus
from app.services.analysis.llm.base import (
    AnalysisLLM,
    AnalysisLLMProviderError,
    AnalysisLLMTimeoutError,
    AnalysisLLMValidationError,
)
from app.services.analysis.llm.schemas import AnalysisResult, ScoringSignals

_OPT_OUT_PHRASES = ("stop calling", "don't call", "do not call", "remove me", "no more calls")
_DECLINE_PHRASES = ("not interested", "no thanks", "no thank you")
_AFFIRMATIVE_PHRASES = ("yes", "sure", "sounds good", "interested", "okay")
_CALLBACK_PHRASES = ("call me back", "callback", "call back later")
_PRICING_PHRASES = ("how much", "pricing", "price", "cost")


class FakeAnalysisLLM(AnalysisLLM):
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.force_timeout = False
        self.force_error = False
        self.force_malformed = False

    def analyze(self, transcript_lines: list[str], *, brand_name: str) -> AnalysisResult:
        self.calls.append(transcript_lines)

        if self.force_timeout:
            raise AnalysisLLMTimeoutError("fake analysis LLM configured to time out")
        if self.force_error:
            raise AnalysisLLMProviderError("fake analysis LLM configured to error")
        if self.force_malformed:
            raise AnalysisLLMValidationError(
                "fake analysis LLM configured to return malformed output"
            )

        contact_text = " ".join(
            line.split(": ", 1)[1] for line in transcript_lines if line.startswith("contact: ")
        ).lower()

        if not contact_text.strip():
            return AnalysisResult(
                summary="Call connected but no customer dialogue was captured.",
                intent=AnalysisIntent.UNCLEAR,
                interest_status=InterestStatus.UNKNOWN,
                sentiment=AnalysisSentiment.UNKNOWN,
                next_action=AnalysisNextAction.MANUAL_REVIEW,
                feedback=None,
                language="en",
                scoring_signals=ScoringSignals(),
            )

        signals = ScoringSignals(
            explicit_interest=any(p in contact_text for p in _AFFIRMATIVE_PHRASES),
            requested_callback=any(p in contact_text for p in _CALLBACK_PHRASES),
            requested_pricing=any(p in contact_text for p in _PRICING_PHRASES),
            explicit_rejection=any(p in contact_text for p in _DECLINE_PHRASES),
            opted_out=any(p in contact_text for p in _OPT_OUT_PHRASES),
            objection_count=sum(1 for line in transcript_lines if "but" in line.lower()),
        )

        if signals.opted_out:
            return AnalysisResult(
                summary="Customer asked not to be contacted again; call ended on opt-out.",
                intent=AnalysisIntent.NOT_INTERESTED,
                interest_status=InterestStatus.NOT_INTERESTED,
                sentiment=AnalysisSentiment.NEGATIVE,
                next_action=AnalysisNextAction.NO_ACTION,
                feedback="Requested removal from the calling list.",
                scoring_signals=signals,
            )

        if signals.explicit_rejection:
            return AnalysisResult(
                summary="Customer declined; not interested at this time.",
                intent=AnalysisIntent.NOT_INTERESTED,
                interest_status=InterestStatus.NOT_INTERESTED,
                sentiment=AnalysisSentiment.NEGATIVE,
                next_action=AnalysisNextAction.NO_ACTION,
                feedback="Declined the offer.",
                scoring_signals=signals,
            )

        if signals.requested_callback:
            return AnalysisResult(
                summary="Customer asked to be called back at a later time.",
                intent=AnalysisIntent.CALLBACK_REQUESTED,
                interest_status=InterestStatus.MAYBE,
                sentiment=AnalysisSentiment.NEUTRAL,
                next_action=AnalysisNextAction.CALLBACK,
                feedback=None,
                scoring_signals=signals,
            )

        if signals.requested_pricing:
            return AnalysisResult(
                summary="Customer asked about pricing/cost details.",
                intent=AnalysisIntent.INFORMATION_REQUESTED,
                interest_status=InterestStatus.MAYBE,
                sentiment=AnalysisSentiment.NEUTRAL,
                next_action=AnalysisNextAction.SEND_INFORMATION,
                feedback="Requested pricing information.",
                scoring_signals=signals,
            )

        if signals.explicit_interest:
            return AnalysisResult(
                summary="Customer expressed interest and engaged with the script.",
                intent=AnalysisIntent.INTERESTED,
                interest_status=InterestStatus.INTERESTED,
                sentiment=AnalysisSentiment.POSITIVE,
                next_action=AnalysisNextAction.SALES_CONTACT,
                feedback=None,
                scoring_signals=signals,
            )

        return AnalysisResult(
            summary="Customer's interest could not be clearly determined from the call.",
            intent=AnalysisIntent.UNCLEAR,
            interest_status=InterestStatus.UNKNOWN,
            sentiment=AnalysisSentiment.NEUTRAL,
            next_action=AnalysisNextAction.MANUAL_REVIEW,
            feedback=None,
            scoring_signals=signals,
        )
