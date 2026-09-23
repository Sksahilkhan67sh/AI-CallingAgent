"""Deterministic lead scoring -- Checkpoint 06 §14.

LLM extracts signals (ScoringSignals) -> this function -> a bounded
0-100 score. The LLM never bypasses this; a score is never accepted
directly from the model. Documented scoring rules, applied in a fixed
order so the result is reproducible for the same signals:

  +35  explicit_interest
  +25  purchase_intent
  +20  requested_callback
  +10  requested_pricing
  +10  has_timeline
  +10  information_requested
   -8  per objection raised (capped at -24)
  -40  explicit_rejection
  -100 opted_out (floors the score at 0 regardless of other signals --
       an opted-out contact is not a lead to prioritize)

The result is an application-generated lead-priority signal, not a
guaranteed prediction of conversion.
"""

from app.services.analysis.llm.schemas import ScoringSignals

_MAX_OBJECTION_PENALTY = 24
_OBJECTION_PENALTY_PER_ITEM = 8


def compute_lead_score(signals: ScoringSignals) -> int:
    if signals.opted_out:
        return 0

    score = 0
    if signals.explicit_interest:
        score += 35
    if signals.purchase_intent:
        score += 25
    if signals.requested_callback:
        score += 20
    if signals.requested_pricing:
        score += 10
    if signals.has_timeline:
        score += 10
    if signals.information_requested:
        score += 10

    objection_penalty = min(
        signals.objection_count * _OBJECTION_PENALTY_PER_ITEM, _MAX_OBJECTION_PENALTY
    )
    score -= objection_penalty

    if signals.explicit_rejection:
        score -= 40

    return max(0, min(100, score))
