"""Deterministic fake TTS. No real provider is named in the specs and
none is configured in this environment (see docs/CHECKPOINT-04-NOTES.md)."""

from app.services.ai.tts.base import TTS, TTSProviderError, TTSTimeoutError


class FakeTTS(TTS):
    def __init__(self) -> None:
        self.synthesized_texts: list[str] = []
        self.force_timeout = False
        self.force_error = False

    def synthesize(self, text: str) -> bytes:
        if self.force_timeout:
            raise TTSTimeoutError("fake TTS configured to time out")
        if self.force_error:
            raise TTSProviderError("fake TTS configured to error")
        self.synthesized_texts.append(text)
        return text.encode("utf-8")  # stand-in "audio" -- deterministic and inspectable
