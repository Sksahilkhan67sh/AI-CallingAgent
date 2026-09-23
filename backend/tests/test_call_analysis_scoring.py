"""Checkpoint 06 §32.E -- deterministic lead scoring."""

from app.services.analysis.llm.schemas import ScoringSignals
from app.services.analysis.scoring import compute_lead_score


def test_interested_signal_increases_score():
    score = compute_lead_score(ScoringSignals(explicit_interest=True))
    assert score == 35


def test_no_signals_scores_zero():
    assert compute_lead_score(ScoringSignals()) == 0


def test_callback_and_pricing_signals_stack():
    score = compute_lead_score(ScoringSignals(requested_callback=True, requested_pricing=True))
    assert score == 30


def test_objections_reduce_score_but_are_capped():
    score = compute_lead_score(ScoringSignals(explicit_interest=True, objection_count=10))
    # 35 - min(10*8, 24) = 35 - 24
    assert score == 11


def test_explicit_rejection_reduces_score():
    score = compute_lead_score(ScoringSignals(explicit_interest=True, explicit_rejection=True))
    # 35 - 40 = -5, clamped to 0
    assert score == 0


def test_score_never_goes_below_zero():
    score = compute_lead_score(ScoringSignals(explicit_rejection=True, objection_count=5))
    assert score == 0


def test_score_never_exceeds_100():
    score = compute_lead_score(
        ScoringSignals(
            explicit_interest=True,
            purchase_intent=True,
            requested_callback=True,
            requested_pricing=True,
            has_timeline=True,
            information_requested=True,
        )
    )
    assert score == 100


def test_opted_out_floors_score_at_zero_regardless_of_other_signals():
    score = compute_lead_score(
        ScoringSignals(explicit_interest=True, purchase_intent=True, opted_out=True)
    )
    assert score == 0


def test_scoring_is_deterministic_for_identical_signals():
    signals = ScoringSignals(explicit_interest=True, requested_callback=True)
    assert compute_lead_score(signals) == compute_lead_score(signals)
