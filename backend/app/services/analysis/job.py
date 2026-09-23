"""Analysis job payload -- Checkpoint 06 §7. Deliberately thin, mirroring
app/services/queue/job.py's DialJob and app/services/recovery/job.py's
RecoveryJob: just enough to let the worker resolve durable state from
PostgreSQL, which remains the source of truth. No transcript, no
conversation history, no LLM prompt in the Redis payload.
"""

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime


@dataclass
class AnalysisJob:
    job_id: str
    analysis_id: str
    call_attempt_id: str
    contact_id: str
    campaign_id: str
    conversation_session_id: str | None
    enqueued_at: str
    trace_id: str

    @classmethod
    def new(
        cls,
        *,
        analysis_id: uuid.UUID,
        call_attempt_id: uuid.UUID,
        contact_id: uuid.UUID,
        campaign_id: uuid.UUID,
        conversation_session_id: uuid.UUID | None,
    ) -> "AnalysisJob":
        return cls(
            job_id=str(uuid.uuid4()),
            analysis_id=str(analysis_id),
            call_attempt_id=str(call_attempt_id),
            contact_id=str(contact_id),
            campaign_id=str(campaign_id),
            conversation_session_id=(
                str(conversation_session_id) if conversation_session_id else None
            ),
            enqueued_at=datetime.now(UTC).isoformat(),
            trace_id=str(uuid.uuid4()),
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> "AnalysisJob":
        return cls(**json.loads(data))
