"""Shared test helpers for the AI conversation engine -- not a test
file itself (no test_ prefix), just setup shared across test_ai_*.py.
"""

from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CallAttemptState, CampaignStatus, ContactStatus
from app.services.ai.audio.fake_audio import FakeAudioSession
from app.services.ai.conversation.orchestrator import ConversationOrchestrator
from app.services.ai.llm.fake_llm import FakeLLM
from app.services.ai.memory.store import MemoryStore
from app.services.ai.stt.fake_stt import FakeSTT
from app.services.ai.tts.fake_tts import FakeTTS
from app.services.phone import normalize_phone_number


def create_connected_call(db_session, *, phone="555-950-0001", brand="Test Co"):
    campaign = Campaign(name=brand, status=CampaignStatus.ACTIVE)
    db_session.add(campaign)
    db_session.flush()
    contact = Contact(
        campaign_id=campaign.id,
        phone_number=phone,
        normalized_phone_number=normalize_phone_number(phone),
        status=ContactStatus.IN_CONVERSATION,
    )
    db_session.add(contact)
    db_session.flush()
    attempt = CallAttempt(
        contact_id=contact.id,
        attempt_number=1,
        state=CallAttemptState.CONNECTED,
        provider="mock",
        provider_call_id="mock-test-call",
    )
    db_session.add(attempt)
    db_session.flush()
    return campaign, contact, attempt


def build_orchestrator(db_session, call_attempt, contact, *, memory_store=None, agent_config=None):
    stt = FakeSTT()
    llm = FakeLLM()
    tts = FakeTTS()
    audio = FakeAudioSession(session_id="test-session", call_attempt_id=str(call_attempt.id))
    store = memory_store or MemoryStore(db_session, redis_client=None)

    orchestrator = ConversationOrchestrator(
        db_session,
        call_attempt=call_attempt,
        contact=contact,
        stt=stt,
        llm=llm,
        tts=tts,
        audio=audio,
        memory_store=store,
        brand_name="Test Co",
        agent_config=agent_config,
    )
    return orchestrator, stt, llm, tts, audio
