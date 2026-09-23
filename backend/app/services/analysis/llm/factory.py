"""Provider selection for the analysis LLM -- Checkpoint 06 §19,
mirroring app/services/ai/factory.py. "mock" is the only supported
value until real credentials exist.
"""

from app.core.config import get_settings
from app.services.analysis.llm.base import AnalysisLLM
from app.services.analysis.llm.fake_llm import FakeAnalysisLLM


def get_analysis_llm_provider() -> AnalysisLLM:
    settings = get_settings()
    if settings.analysis_llm_provider == "mock":
        return FakeAnalysisLLM()
    raise ValueError(
        f"Unsupported ANALYSIS_LLM_PROVIDER '{settings.analysis_llm_provider}' -- "
        "only 'mock' is implemented"
    )
