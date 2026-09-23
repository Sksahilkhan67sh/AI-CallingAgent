"""ConversationOrchestrator -- Checkpoint 04 Step 15.

Owns turn-by-turn flow: finalized utterance -> memory -> policy -> LLM
-> response validation -> TTS -> audio, plus interruption/staleness
protection, silence handling, and bounded conversation limits. It does
NOT talk to raw provider SDKs (only the STT/LLM/TTS/AudioSession
interfaces), does not implement retry-call policy (that's the dialer,
Checkpoint 03), and does not bypass suppression (opt-out still goes
through the one canonical SuppressionRepository).
"""

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.conversation import CallEvent, ConversationMessage, ConversationSession
from app.models.enums import (
    CallAttemptState,
    ContactStatus,
    ConversationRole,
    ConversationSessionStatus,
    Intent,
    MidCallDisconnectReason,
    NextAction,
    SuppressionSource,
)
from app.models.suppression import Suppression
from app.repositories.suppression_repository import SuppressionRepository
from app.services.ai.audio.base import AudioSession
from app.services.ai.conversation.context import build_turn_context
from app.services.ai.llm.base import LLM, LLMProviderError, LLMTimeoutError
from app.services.ai.memory.schema import ObjectionRecord, WorkingMemory
from app.services.ai.memory.store import MemoryStore
from app.services.ai.policy.engine import PolicyEngine
from app.services.ai.schemas import Entities, StructuredOutput, TurnContext
from app.services.ai.stt.base import StreamingSTT, TranscriptEvent
from app.services.ai.tts.base import TTS, TTSProviderError, TTSTimeoutError
from app.services.recovery.factory import get_recovery_scheduler
from app.services.recovery.manager import RecoveryManager

logger = logging.getLogger("ai.conversation")

MAX_LLM_RETRIES = 1  # Step 21 -- bounded, not indefinite
MAX_RESPONSE_REGENERATIONS = 1  # Step 18
MAX_SILENCE_PROMPTS = 1  # Step 26
MAX_CONVERSATION_TURNS = 40  # Step 39/40 -- configuration-driven bound
MAX_CONVERSATION_DURATION_SECONDS = 1800  # 30 minutes -- documented default, Step 40

_FALLBACK_RESPONSE = "I'm sorry, I'm having trouble understanding. I'll follow up another time."


class ConversationSessionAlreadyOwnedError(Exception):
    """Step 35: another orchestrator already owns (has an active
    ConversationSession for) this CallAttempt. Backed by the existing
    unique constraint on conversation_session.call_attempt_id -- a real
    database constraint, not an in-memory lock, so it holds across
    worker processes."""


class ConversationOrchestrator:
    def __init__(
        self,
        db: Session,
        *,
        call_attempt: CallAttempt,
        contact: Contact,
        stt: StreamingSTT,
        llm: LLM,
        tts: TTS,
        audio: AudioSession,
        memory_store: MemoryStore,
        brand_name: str,
        agent_config=None,
        is_reconnect: bool = False,
        previous_attempt_id: str | None = None,
    ) -> None:
        self.db = db
        self.call_attempt = call_attempt
        self.contact = contact
        self.stt = stt
        self.llm = llm
        self.tts = tts
        self.audio = audio
        self.memory_store = memory_store
        self.brand_name = brand_name
        self.agent_config = agent_config
        self.policy = PolicyEngine()

        self.session: ConversationSession | None = None
        self.memory = WorkingMemory(
            attempt_id=str(call_attempt.id), contact_id=str(contact.id)
        )
        self._is_reconnect = is_reconnect
        # Checkpoint 05: a retry creates a NEW CallAttempt, so the memory
        # to restore was checkpointed under the *previous* attempt's ID,
        # not this one. Falls back to the current attempt (CP04's
        # original same-attempt-reconnect case) when not given.
        self._memory_source_attempt_id = previous_attempt_id or str(call_attempt.id)
        self._sequence = 0
        self._generation = 0
        self._consecutive_declines = 0
        self._consecutive_unclear = 0
        self._silence_prompts_sent = 0
        self._turn_count = 0
        self.ended = False
        self.end_reason: str | None = None

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        """Step 5/42: initialize the session once the call is Connected."""
        existing_memory = self.memory_store.load_latest(
            self._memory_source_attempt_id, str(self.contact.id)
        )
        if existing_memory is not None and self._is_reconnect:
            self.memory = existing_memory
            # now checkpointed under the new attempt, not the old one
            self.memory.attempt_id = str(self.call_attempt.id)
            self.memory.disconnect_count += 1
        elif self.agent_config is not None and self.agent_config.required_entity_fields:
            # Step 11/28: script_progress.pending_points seeds from the
            # campaign's required entity fields -- what "the script" for
            # this call actually needs to cover.
            self.memory.script_progress.pending_points = list(
                self.agent_config.required_entity_fields
            )

        self.session = ConversationSession(
            call_attempt_id=self.call_attempt.id, status=ConversationSessionStatus.ACTIVE
        )
        try:
            with self.db.begin_nested():  # SAVEPOINT -- only this insert unwinds on conflict
                self.db.add(self.session)
                self.db.flush()
        except IntegrityError as exc:
            raise ConversationSessionAlreadyOwnedError(
                f"CallAttempt {self.call_attempt.id} already has an active "
                "conversation session"
            ) from exc

        self.audio.start()
        self.audio.on_inbound_audio(self._on_inbound_audio)
        self.stt.start_session(self._on_transcript)

        self._log_event(
            "conversation_reconnected" if self._is_reconnect else "conversation_started"
        )

    def on_barge_in(self) -> None:
        """Step 24: customer started speaking while the AI was speaking.
        Stops outbound audio and bumps the generation counter so any
        in-flight response for the previous turn is discarded rather
        than emitted late (Step 25)."""
        self._generation += 1
        self.audio.interrupt()
        self._log_event("conversation_interrupted")

    def shutdown(self) -> None:
        """Step 37: graceful shutdown -- stop streams, close provider
        connections, without abandoning session state."""
        try:
            self.stt.stop_session()
        finally:
            self.audio.stop()

    # -- turn processing -------------------------------------------------

    def handle_silence(self) -> None:
        """Step 26: bounded silence handling."""
        if self.ended:
            return
        if self._silence_prompts_sent < MAX_SILENCE_PROMPTS:
            self._silence_prompts_sent += 1
            self._speak("Are you still there?", generation=self._generation)
        else:
            self._end_conversation("silence_timeout")

    def handle_final_utterance(self, text: str) -> None:
        if self.ended:
            return

        self._turn_count += 1
        if self._turn_count > MAX_CONVERSATION_TURNS:
            self._end_conversation("max_turns_reached")
            return
        if self._conversation_duration_seconds() > MAX_CONVERSATION_DURATION_SECONDS:
            self._end_conversation("max_duration_reached")
            return

        my_generation = self._generation
        self._silence_prompts_sent = 0  # customer spoke -- reset silence tracking
        self._persist_message(ConversationRole.CONTACT, text)

        output = self._generate_validated_response(text)
        if output is None:
            self._end_conversation("ai_failure")
            return

        if my_generation != self._generation:
            # Step 25: a barge-in happened while we were generating --
            # this turn's output is stale. Discard it entirely: no
            # audio, no memory update, no phase advance.
            logger.info("stale_response_discarded", extra={"attempt_id": str(self.call_attempt.id)})
            return

        self._apply_output(text, output, my_generation)

    # -- internals ---------------------------------------------------

    def _on_transcript(self, event: TranscriptEvent) -> None:
        """Step 7/9: only a finalized utterance becomes a durable
        message / triggers a turn. Partial transcripts are ephemeral."""
        if not event.is_final:
            return
        self.handle_final_utterance(event.text)

    def _on_inbound_audio(self, chunk: bytes) -> None:
        self.stt.send_audio(chunk)

    def _generate_validated_response(self, utterance: str) -> StructuredOutput | None:
        """Step 18/21: bounded retries on LLM failure, bounded
        regeneration on an invalid response, then a safe fallback."""
        context = self._build_context(utterance)

        output = self._call_llm_with_retries(context)
        if output is None:
            return self._fallback_output()

        for _ in range(MAX_RESPONSE_REGENERATIONS):
            if self.policy.validate_response(output):
                return output
            output = self._call_llm_with_retries(context)
            if output is None:
                return self._fallback_output()

        if self.policy.validate_response(output):
            return output
        return self._fallback_output()

    def _call_llm_with_retries(self, context: TurnContext) -> StructuredOutput | None:
        for attempt in range(MAX_LLM_RETRIES + 1):
            try:
                return self.llm.generate_response(context)
            except LLMTimeoutError:
                logger.warning("llm_timeout", extra={"attempt": attempt})
            except LLMProviderError:
                logger.warning("llm_provider_error", extra={"attempt": attempt})
        return None

    def _fallback_output(self) -> StructuredOutput:
        return StructuredOutput(
            intent=Intent.UNCLEAR,
            entities=Entities(),
            next_action=NextAction.END_CALL_POLITE,
            response_text=_FALLBACK_RESPONSE,
        )

    def _build_context(self, utterance: str) -> TurnContext:
        recent = self._recent_message_texts()
        return build_turn_context(
            agent_config=self.agent_config,
            memory=self.memory,
            brand_name=self.brand_name,
            recent_message_texts=recent,
            user_utterance=utterance,
            is_reconnect=self._is_reconnect and self._turn_count == 1,
        )

    def _recent_message_texts(self) -> list[str]:
        if self.session is None:
            return []
        stmt = (
            select(ConversationMessage)
            .where(ConversationMessage.session_id == self.session.id)
            .order_by(ConversationMessage.sequence)
        )
        rows = self.db.execute(stmt).scalars().all()
        return [f"{row.role.value}: {row.content}" for row in rows]

    def _apply_output(self, utterance: str, output: StructuredOutput, generation: int) -> None:
        self._update_memory(utterance, output)

        if output.intent == Intent.NEGATIVE:
            self._consecutive_declines += 1
        else:
            self._consecutive_declines = 0
        if output.intent == Intent.UNCLEAR:
            self._consecutive_unclear += 1
        else:
            self._consecutive_unclear = 0

        has_pending = bool(self.memory.script_progress.pending_points)
        decision = self.policy.decide(
            current_phase=self.memory.script_progress.phase,
            output=output,
            utterance=utterance,
            consecutive_declines=self._consecutive_declines,
            consecutive_unclear=self._consecutive_unclear,
            has_pending_required_fields=has_pending,
        )

        if decision.opt_out_detected:
            self._apply_suppression()

        self.memory.script_progress.phase = decision.next_phase
        self._speak(output.response_text, generation=generation)
        self.memory.last_agent_utterance = output.response_text
        self.memory_store.checkpoint(self.memory)

        if decision.should_terminate:
            self._end_conversation(decision.termination_reason or "wrap_up")

    def _update_memory(self, utterance: str, output: StructuredOutput) -> None:
        entities = output.entities
        for field_name in (
            "interest_level",
            "objection",
            "follow_up_preference",
            "decision_maker_status",
            "free_text_feedback",
        ):
            value = getattr(entities, field_name)
            if value and value != "not_captured":
                self.memory.captured_entities[field_name] = value
                progress = self.memory.script_progress
                if field_name in progress.pending_points:
                    progress.pending_points.remove(field_name)
                    progress.completed_points.append(field_name)

        if entities.objection:
            self.memory.objections_raised.append(
                ObjectionRecord(objection=entities.objection, addressed=False)
            )

        if output.requires_suppression:
            self.memory.requires_suppression = True

        if output.recording_consent.value != "not_applicable":
            self.memory.recording_consent = output.recording_consent

    def _apply_suppression(self) -> None:
        """Opt-out enforcement (Step 17) -- writes through the one
        canonical SuppressionRepository, never a second table."""
        suppressions = SuppressionRepository(self.db)
        if not suppressions.is_suppressed(self.contact.normalized_phone_number):
            suppressions.add(
                Suppression(
                    contact_id=self.contact.id,
                    phone_number=self.contact.normalized_phone_number,
                    reason="opt-out during AI conversation",
                    source=SuppressionSource.AGENT_IN_CALL,
                )
            )
        self.contact.status = ContactStatus.CLOSED

    def _speak(self, text: str, *, generation: int) -> None:
        try:
            audio_bytes = self.tts.synthesize(text)
        except (TTSTimeoutError, TTSProviderError):
            logger.warning("tts_failure", extra={"attempt_id": str(self.call_attempt.id)})
            return

        if generation != self._generation:
            # Interrupted while TTS was generating -- Step 25 again,
            # this time for a response that survived LLM/validation but
            # went stale during synthesis.
            return

        self._persist_message(ConversationRole.AGENT, text)
        self.audio.send_outbound_audio(audio_bytes)

    def _persist_message(self, role: ConversationRole, content: str) -> None:
        if self.session is None:
            return
        self._sequence += 1
        self.db.add(
            ConversationMessage(
                session_id=self.session.id,
                sequence=self._sequence,
                role=role,
                content=content,
            )
        )
        self.db.flush()

    def _log_event(self, event_type: str, payload: dict | None = None) -> None:
        self.db.add(
            CallEvent(call_attempt_id=self.call_attempt.id, event_type=event_type, payload=payload)
        )
        self.db.flush()

    def _conversation_duration_seconds(self) -> float:
        if self.session is None:
            return 0.0
        started = self.session.started_at
        return (datetime.now(UTC) - started).total_seconds()

    def handle_disconnect(self, reason: MidCallDisconnectReason) -> None:
        """Checkpoint 05: the call itself dropped mid-conversation --
        distinct from `_end_conversation`'s graceful, policy-driven
        endings (opt-out, goal met, max turns/duration). Sets the
        CallAttempt to DroppedMidCall with the given reason, checkpoints
        memory one last time, ends the session, and hands off to
        RecoveryManager for the retry/terminal decision -- this method
        never decides that itself (single owner, per CP05 §4)."""
        if self.ended:
            return
        self.ended = True
        self.end_reason = f"disconnected:{reason.value}"

        self.memory.disconnect_count += 1
        self.memory_store.checkpoint(self.memory)
        self._log_event("conversation_disconnected", {"reason": reason.value})

        if self.session is not None:
            self.session.status = ConversationSessionStatus.ENDED
            self.session.ended_at = datetime.now(UTC)

        self.call_attempt.state = CallAttemptState.DROPPED_MID_CALL
        self.call_attempt.disconnect_reason = reason
        self.call_attempt.ended_at = datetime.now(UTC)
        self.call_attempt.recording_consent = self.memory.recording_consent
        self.contact.status = ContactStatus.DISCONNECTED

        self.shutdown()
        self.db.flush()

        campaign = self.db.get(Campaign, self.contact.campaign_id)
        if campaign is not None:
            RecoveryManager(self.db, get_recovery_scheduler()).handle_disconnect(
                self.call_attempt,
                self.contact,
                campaign,
                never_connected=False,
                reason_key=reason.value,
            )

    def _end_conversation(self, reason: str) -> None:
        """Step 43: stop streams, persist final memory/messages/event,
        mark the session, leave CallAttempt in the correct state."""
        if self.ended:
            return
        self.ended = True
        self.end_reason = reason

        self.memory_store.checkpoint(self.memory)
        self._log_event("conversation_ended", {"reason": reason})

        if self.session is not None:
            self.session.status = ConversationSessionStatus.ENDED
            self.session.ended_at = datetime.now(UTC)

        self.call_attempt.state = CallAttemptState.ENDED_NORMALLY
        self.call_attempt.ended_at = datetime.now(UTC)
        self.call_attempt.recording_consent = self.memory.recording_consent

        if reason == "opt_out":
            self.contact.status = ContactStatus.CLOSED
        elif not self.memory.script_progress.pending_points and reason in (
            "end_call_goal_met",
            "wrap_up",
        ):
            self.contact.status = ContactStatus.COMPLETED
        else:
            self.contact.status = ContactStatus.COMPLETED_PARTIAL

        self.shutdown()
        self.db.flush()
