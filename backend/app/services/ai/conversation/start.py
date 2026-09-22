"""The clean internal start_conversation(...) operation -- Checkpoint 04
Step 41-42. Called by the dialer worker once a CallAttempt reaches
Connected (or, on a future reconnect, once a call is re-Connected).
Deliberately NOT exposed as a public API route -- there is no
`POST /conversations/start` endpoint; only internal callers with an
already-connected CallAttempt can reach this.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.redis_client import get_redis
from app.models.agent_config import AgentConfig
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.services.ai.conversation.orchestrator import ConversationOrchestrator
from app.services.ai.factory import (
    get_audio_session,
    get_llm_provider,
    get_stt_provider,
    get_tts_provider,
)
from app.services.ai.memory.store import MemoryStore


def start_conversation(
    db: Session,
    call_attempt: CallAttempt,
    contact: Contact,
    *,
    is_reconnect: bool = False,
    previous_attempt_id: str | None = None,
) -> ConversationOrchestrator:
    campaign = db.get(Campaign, contact.campaign_id)
    agent_config = db.execute(
        select(AgentConfig).where(AgentConfig.campaign_id == contact.campaign_id)
    ).scalar_one_or_none()

    orchestrator = ConversationOrchestrator(
        db,
        call_attempt=call_attempt,
        contact=contact,
        stt=get_stt_provider(),
        llm=get_llm_provider(),
        tts=get_tts_provider(),
        audio=get_audio_session(str(call_attempt.id)),
        memory_store=MemoryStore(db, get_redis()),
        brand_name=campaign.name if campaign else "our company",
        agent_config=agent_config,
        is_reconnect=is_reconnect,
        previous_attempt_id=previous_attempt_id,
    )
    orchestrator.start()
    return orchestrator
