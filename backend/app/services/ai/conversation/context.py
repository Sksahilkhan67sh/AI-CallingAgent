"""Bounded context construction -- Checkpoint 04 Step 14, Prompt-
Specification.md §2.

Never sends the full transcript. Recent messages are capped at
MAX_RECENT_MESSAGES; anything older is represented by a short
placeholder summary line rather than included verbatim (Memory-
Specification.md §10) -- a real summarization pass is not implemented
here (no LLM call budget was spent summarizing in this checkpoint); the
placeholder keeps the context bounded and honest about what's omitted
rather than silently truncating.
"""

from app.models.agent_config import AgentConfig
from app.services.ai.memory.schema import WorkingMemory
from app.services.ai.schemas import TurnContext

MAX_RECENT_MESSAGES = 10


def build_system_prompt(
    agent_config: AgentConfig | None, memory: WorkingMemory, brand_name: str
) -> str:
    """Prompt-Specification.md §2 template, filled from campaign
    configuration + live memory."""
    persona_tone = (agent_config.persona_tone if agent_config else None) or "warm and professional"
    goal = (agent_config.goal if agent_config else None) or "qualify the contact's interest"
    script_points = (agent_config.script_skeleton if agent_config else None) or []
    required_fields = (agent_config.required_entity_fields if agent_config else None) or []
    escalation = (
        agent_config.escalation_contact_method if agent_config else None
    ) or "a callback from our team"

    script_bulleted = "\n".join(f"- {p}" for p in script_points) or "- (none configured)"
    fields_bulleted = "\n".join(f"- {f}" for f in required_fields) or "- (none configured)"

    entities_summary = ", ".join(
        f"{k}={v}" for k, v in memory.captured_entities.items()
    ) or "(none captured yet)"
    progress = memory.script_progress
    progress_summary = (
        f"phase={progress.phase.value}, "
        f"completed={progress.completed_points}, pending={progress.pending_points}"
    )
    objections_summary = (
        ", ".join(
            f"{o.objection} ({'addressed' if o.addressed else 'unaddressed'})"
            for o in memory.objections_raised
        )
        or "(none raised)"
    )

    return f"""You are an automated calling assistant representing {brand_name}.

PERSONA
- Tone: {persona_tone}
- You must identify yourself as an automated assistant if asked whether you are human.
- Do not invent facts you have not been given below. If you don't know something,
  offer to have a human follow up instead of guessing.

GOAL
{goal}

SCRIPT SKELETON (talking points to cover, in a natural order -- not a verbatim script)
{script_bulleted}

REQUIRED FIELDS TO CAPTURE
{fields_bulleted}

CONVERSATION SO FAR
Captured entities: {entities_summary}
Script progress: {progress_summary}
Objections raised: {objections_summary}

GUARDRAILS
- If the contact asks to speak with a human, acknowledge this, offer:
  {escalation}, and move toward a polite close.
- If the contact declines twice, do not persist -- move toward a polite close.
- If the contact asks to be removed from future calls, confirm you will note this
  and end the call politely. Set requires_suppression = true in your output.
- If you cannot understand the contact after a couple of clarification attempts,
  end the call gracefully rather than looping.

OUTPUT FORMAT
Respond only with a single JSON object matching the schema provided separately.
Do not include any text outside the JSON object."""


def build_turn_context(
    *,
    agent_config: AgentConfig | None,
    memory: WorkingMemory,
    brand_name: str,
    recent_message_texts: list[str],
    user_utterance: str,
    is_reconnect: bool = False,
) -> TurnContext:
    system_context = build_system_prompt(agent_config, memory, brand_name)
    bounded_recent = recent_message_texts[-MAX_RECENT_MESSAGES:]
    if len(recent_message_texts) > MAX_RECENT_MESSAGES:
        omitted = len(recent_message_texts) - MAX_RECENT_MESSAGES
        bounded_recent = [f"({omitted} earlier turns summarized)"] + bounded_recent

    return TurnContext(
        system_context=system_context,
        recent_messages=bounded_recent,
        user_utterance=user_utterance,
        is_reconnect=is_reconnect,
        last_agent_utterance=memory.last_agent_utterance,
    )
