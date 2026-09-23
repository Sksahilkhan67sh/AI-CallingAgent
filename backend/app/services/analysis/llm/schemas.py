"""Analysis LLM structured-output contract -- Checkpoint 06 §12.

Mirrors app/services/ai/schemas.py's StructuredOutput: this is the one
shape every analysis-LLM provider adapter must produce, regardless of
the provider's own API shape. The LLM extracts signals and evidence;
it does NOT compute lead_score directly (§14) -- that is a deterministic
function of `ScoringSignals`, applied by app/services/analysis/scoring.py.
"""

from dataclasses import dataclass, field

from app.models.enums import AnalysisIntent, AnalysisNextAction, AnalysisSentiment, InterestStatus


@dataclass
class ScoringSignals:
    """§14 -- structured signals the deterministic scorer consumes.
    Booleans/flags only; the LLM never proposes a numeric score itself."""

    explicit_interest: bool = False
    purchase_intent: bool = False
    requested_callback: bool = False
    requested_pricing: bool = False
    has_timeline: bool = False
    objection_count: int = 0
    explicit_rejection: bool = False
    opted_out: bool = False
    information_requested: bool = False


@dataclass
class AnalysisResult:
    summary: str
    intent: AnalysisIntent
    interest_status: InterestStatus
    sentiment: AnalysisSentiment
    next_action: AnalysisNextAction
    feedback: str | None = None
    key_facts: list[str] = field(default_factory=list)
    objections: list[str] = field(default_factory=list)
    customer_needs: list[str] = field(default_factory=list)
    language: str = "en"
    scoring_signals: ScoringSignals = field(default_factory=ScoringSignals)
    input_tokens: int | None = None
    output_tokens: int | None = None
