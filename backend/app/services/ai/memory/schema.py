"""Working memory -- Memory-Specification.md §2, with one deliberate
divergence: `turns` is not stored as a field on this object (see
docs/CHECKPOINT-04-NOTES.md) -- it's read from ConversationMessage on
demand by whoever needs recent turns (the context builder). Everything
else matches the spec's schema exactly.

Compact, structured, deterministic, serializable -- Checkpoint 04
Step 11. This is NOT the transcript (Step 12).
"""

from dataclasses import dataclass, field

from app.models.enums import ConversationPhase, RecordingConsent

SCHEMA_VERSION = "v1"


@dataclass
class ScriptProgress:
    phase: ConversationPhase = ConversationPhase.OPENING
    completed_points: list[str] = field(default_factory=list)
    pending_points: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "phase": self.phase.value,
            "completed_points": self.completed_points,
            "pending_points": self.pending_points,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ScriptProgress":
        return cls(
            phase=ConversationPhase(data.get("phase", ConversationPhase.OPENING.value)),
            completed_points=list(data.get("completed_points", [])),
            pending_points=list(data.get("pending_points", [])),
        )


@dataclass
class ObjectionRecord:
    objection: str
    addressed: bool = False

    def to_dict(self) -> dict:
        return {"objection": self.objection, "addressed": self.addressed}

    @classmethod
    def from_dict(cls, data: dict) -> "ObjectionRecord":
        return cls(objection=data["objection"], addressed=data.get("addressed", False))


@dataclass
class WorkingMemory:
    attempt_id: str
    contact_id: str
    captured_entities: dict = field(default_factory=dict)
    script_progress: ScriptProgress = field(default_factory=ScriptProgress)
    objections_raised: list[ObjectionRecord] = field(default_factory=list)
    last_agent_utterance: str | None = None
    disconnect_count: int = 0
    requires_suppression: bool = False
    recording_consent: RecordingConsent = RecordingConsent.NOT_APPLICABLE
    schema_version: str = SCHEMA_VERSION

    def to_snapshot_dict(self) -> dict:
        """The subset that actually gets persisted to
        working_memory_snapshot -- everything except attempt_id/contact_id
        (which live on the row's own FK / are derivable) and turns
        (deliberately not duplicated -- see module docstring)."""
        return {
            "captured_entities": self.captured_entities,
            "script_progress": self.script_progress.to_dict(),
            "objections_raised": [o.to_dict() for o in self.objections_raised],
            "last_agent_utterance": self.last_agent_utterance,
            "disconnect_count": self.disconnect_count,
            "requires_suppression": self.requires_suppression,
            "recording_consent": self.recording_consent,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_snapshot_row(cls, attempt_id: str, contact_id: str, row) -> "WorkingMemory":
        """Reload from a WorkingMemorySnapshot ORM row. Handles a
        snapshot written under a previous schema_version by falling back
        to defaults for any field that row doesn't have -- a reconnect
        must never fail solely because the snapshot predates a schema
        change (Memory-Specification.md §8)."""
        return cls(
            attempt_id=attempt_id,
            contact_id=contact_id,
            captured_entities=dict(row.captured_entities or {}),
            script_progress=ScriptProgress.from_dict(row.script_progress or {}),
            objections_raised=[
                ObjectionRecord.from_dict(o) for o in (row.objections_raised or [])
            ],
            last_agent_utterance=row.last_agent_utterance,
            disconnect_count=row.disconnect_count,
            requires_suppression=row.requires_suppression,
            recording_consent=row.recording_consent,
            schema_version=row.schema_version,
        )
