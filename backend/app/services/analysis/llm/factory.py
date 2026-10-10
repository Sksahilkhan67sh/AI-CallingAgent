"""Provider selection for the analysis LLM -- Checkpoint 06 §19, extended by CP14B.

"mock" is the CP06 deterministic fake. "dograh_qa" reads the Dograh QA node's structured
result through Dograh's documented GET run endpoint (Dograh is the approved platform; no
other LLM provider is integrated -- adding one needs explicit owner approval).
"""

from app.core.config import get_settings
from app.services.analysis.llm.base import AnalysisLLM
from app.services.analysis.llm.fake_llm import FakeAnalysisLLM


def get_analysis_llm_provider() -> AnalysisLLM:
    settings = get_settings()
    if settings.analysis_llm_provider == "mock":
        return FakeAnalysisLLM()
    if settings.analysis_llm_provider == "dograh_qa":
        from app.services.analysis.llm.dograh_qa import DograhQAAnalysisLLM
        from app.services.telephony.factory import get_dograh_client

        return DograhQAAnalysisLLM(get_dograh_client())
    raise ValueError(
        f"Unsupported ANALYSIS_LLM_PROVIDER '{settings.analysis_llm_provider}' -- "
        "supported: 'mock', 'dograh_qa'"
    )
