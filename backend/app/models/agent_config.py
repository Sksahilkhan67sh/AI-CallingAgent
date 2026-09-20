"""AgentConfig -- Database Design §2.3.

One row per campaign; the agent's own persona/script configuration
(AI Agent Specification §10), separate from retry policy. Added in
Checkpoint 04, the first checkpoint that actually needs it -- see
docs/CHECKPOINT-04-NOTES.md, including the one column (`goal`) added
beyond §2.3's literal column list.
"""

import uuid

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AgentConfig(Base):
    __tablename__ = "agent_config"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campaign.id"), nullable=False, unique=True
    )
    persona_tone: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Not in Database-Design.md §2.3 -- added because AI-Agent-Specification.md
    # §2 and Prompt-Specification.md §2 both require it. See notes doc.
    goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    script_skeleton: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    required_entity_fields: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    escalation_contact_method: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_template_version: Mapped[str] = mapped_column(String, nullable=False, default="v1")
