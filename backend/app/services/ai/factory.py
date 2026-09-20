"""Provider selection for STT/LLM/TTS/audio -- Checkpoint 04 Step 2,
mirroring Checkpoint 03's telephony provider factory. "mock" is the
only supported value for each until real credentials exist (see
docs/CHECKPOINT-04-NOTES.md)."""

import uuid

from app.core.config import get_settings
from app.services.ai.audio.base import AudioSession
from app.services.ai.audio.fake_audio import FakeAudioSession
from app.services.ai.llm.base import LLM
from app.services.ai.llm.fake_llm import FakeLLM
from app.services.ai.stt.base import StreamingSTT
from app.services.ai.stt.fake_stt import FakeSTT
from app.services.ai.tts.base import TTS
from app.services.ai.tts.fake_tts import FakeTTS


def get_stt_provider() -> StreamingSTT:
    settings = get_settings()
    if settings.stt_provider == "mock":
        return FakeSTT()
    raise ValueError(
        f"Unsupported STT_PROVIDER '{settings.stt_provider}' -- only 'mock' is implemented"
    )


def get_llm_provider() -> LLM:
    settings = get_settings()
    if settings.llm_provider == "mock":
        return FakeLLM()
    raise ValueError(
        f"Unsupported LLM_PROVIDER '{settings.llm_provider}' -- only 'mock' is implemented"
    )


def get_tts_provider() -> TTS:
    settings = get_settings()
    if settings.tts_provider == "mock":
        return FakeTTS()
    raise ValueError(
        f"Unsupported TTS_PROVIDER '{settings.tts_provider}' -- only 'mock' is implemented"
    )


def get_audio_session(call_attempt_id: str) -> AudioSession:
    settings = get_settings()
    if settings.audio_gateway_provider == "mock":
        return FakeAudioSession(session_id=str(uuid.uuid4()), call_attempt_id=call_attempt_id)
    raise ValueError(
        f"Unsupported AUDIO_GATEWAY_PROVIDER '{settings.audio_gateway_provider}' -- "
        "only 'mock' is implemented"
    )
