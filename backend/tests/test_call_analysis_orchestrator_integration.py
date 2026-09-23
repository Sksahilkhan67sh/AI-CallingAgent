"""Verifies enqueue_call_analysis is actually reached from the real
ConversationOrchestrator._end_conversation() call site -- not just
that the admission function works in isolation
(tests/test_call_analysis_admission.py already covers that).
"""

from app.models.call_analysis import CallAnalysis
from app.services.analysis.factory import get_analysis_queue
from tests.ai_helpers import build_orchestrator, create_connected_call


def test_normal_call_completion_admits_analysis(db_session, redis_client):
    _, contact, attempt = create_connected_call(db_session)
    orchestrator, *_ = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    orchestrator.handle_final_utterance("no thanks")  # ends the call, not via opt-out

    analysis = (
        db_session.query(CallAnalysis).filter(CallAnalysis.call_attempt_id == attempt.id).one()
    )
    assert analysis.status.value == "pending"
    queue = get_analysis_queue()
    assert redis_client.xlen(queue.stream_key) == 1


def test_opt_out_call_completion_does_not_admit_analysis(db_session, redis_client):
    _, contact, attempt = create_connected_call(db_session)
    orchestrator, *_ = build_orchestrator(db_session, attempt, contact)
    orchestrator.start()

    orchestrator.handle_final_utterance("please stop calling me")

    assert orchestrator.ended is True
    assert (
        db_session.query(CallAnalysis).filter(CallAnalysis.call_attempt_id == attempt.id).count()
        == 0
    )
