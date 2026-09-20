"""
Domain enums.

Canonical values and taxonomies come from docs/specs/Architecture/
Call-State-Machine.md (contact/call-attempt states, the two independent
failure-reason taxonomies) and docs/specs/Backend/Database-Design.md
(recording consent, suppression source). Two distinct reason taxonomies
exist by design and must never be mixed -- see Call State Machine §5.
"""

import enum


class ContactStatus(str, enum.Enum):
    """Call State Machine §2.1."""

    PENDING = "Pending"
    DIALING = "Dialing"
    IN_CONVERSATION = "InConversation"
    DISCONNECTED = "Disconnected"
    RETRY_SCHEDULED = "RetryScheduled"
    RECONNECTING = "Reconnecting"
    COMPLETED = "Completed"
    COMPLETED_PARTIAL = "CompletedPartial"
    CLOSED = "Closed"


class CampaignStatus(str, enum.Enum):
    """Database Design §2.1."""

    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"


class CallAttemptState(str, enum.Enum):
    """Call State Machine §3.1."""

    INITIATED = "Initiated"
    CONNECTED = "Connected"
    FAILED_TO_CONNECT = "FailedToConnect"
    DROPPED_MID_CALL = "DroppedMidCall"
    ENDED_NORMALLY = "EndedNormally"


class NeverConnectedFailureReason(str, enum.Enum):
    """Call State Machine §5.1 -- Dialing -> call_failed only."""

    NO_ANSWER = "no_answer"
    BUSY = "busy"
    INVALID_NUMBER = "invalid_number"
    REJECTED = "rejected"
    NETWORK_ERROR = "network_error"
    PROVIDER_ERROR = "provider_error"


class MidCallDisconnectReason(str, enum.Enum):
    """Call State Machine §5.2 -- InConversation -> call_disconnected only.

    Distinct from NeverConnectedFailureReason: `technical_issue` is valid
    here only and must never appear in never-connected retry logic.
    """

    TECHNICAL_ISSUE = "technical_issue"
    NETWORK_PROBLEM = "network_problem"
    PROVIDER_ERROR = "provider_error"
    AI_ERROR = "ai_error"
    UNKNOWN = "unknown"
    CUSTOMER_HANGUP = "customer_hangup"


class RecordingConsent(str, enum.Enum):
    """Recording-Consent.md §4, §6. Never defaults to GRANTED."""

    GRANTED = "granted"
    DENIED = "denied"
    UNCLEAR = "unclear"
    NOT_APPLICABLE = "not_applicable"


class SuppressionSource(str, enum.Enum):
    """Database Design §2.12."""

    AGENT_IN_CALL = "agent_in_call"
    MANUAL_API = "manual_api"


class ConversationRole(str, enum.Enum):
    """Speaker role for a single conversation turn."""

    AGENT = "agent"
    CONTACT = "contact"
    SYSTEM = "system"


class ConversationSessionStatus(str, enum.Enum):
    ACTIVE = "active"
    ENDED = "ended"


class ConversationPhase(str, enum.Enum):
    """Conversation-Flow.md §1. Distinct from CallAttemptState/ContactStatus
    (call-connectivity state) -- this is conversation-content state
    (Checkpoint 04 Step 30)."""

    OPENING = "Opening"
    DISCOVERY = "Discovery"
    OBJECTION_HANDLING = "ObjectionHandling"
    CLOSING = "Closing"
    WRAP_UP = "WrapUp"


class Intent(str, enum.Enum):
    """Prompt-Specification.md §4 structured-output schema."""

    AFFIRMATIVE = "affirmative"
    NEGATIVE = "negative"
    QUESTION = "question"
    OBJECTION = "objection"
    REQUEST_CALLBACK = "request_callback"
    REQUEST_HUMAN = "request_human"
    OFF_TOPIC = "off_topic"
    END_CALL = "end_call"
    UNCLEAR = "unclear"


class NextAction(str, enum.Enum):
    """Prompt-Specification.md §4 structured-output schema."""

    CONTINUE_SCRIPT = "continue_script"
    ANSWER_QUESTION = "answer_question"
    HANDLE_OBJECTION = "handle_objection"
    CONFIRM_AND_CLOSE = "confirm_and_close"
    ESCALATE_TO_HUMAN_OFFER = "escalate_to_human_offer"
    END_CALL_POLITE = "end_call_polite"
    END_CALL_GOAL_MET = "end_call_goal_met"
