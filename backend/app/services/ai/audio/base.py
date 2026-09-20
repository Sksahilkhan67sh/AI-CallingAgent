"""Provider-independent audio session abstraction -- Checkpoint 04
Step 4. The conversation engine operates only on this normalized
surface; no LiveKit/SIP/provider-specific type crosses into
orchestration code.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable


class AudioSession(ABC):
    session_id: str
    call_attempt_id: str

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def send_outbound_audio(self, chunk: bytes) -> None:
        """Send AI-generated audio (from TTS) to the customer."""

    @abstractmethod
    def interrupt(self) -> None:
        """Stop sending outbound audio immediately -- Step 24 barge-in.
        Discards anything queued but not yet actually emitted."""

    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    def on_inbound_audio(self, callback: Callable[[bytes], None]) -> None:
        """Register the callback that receives customer audio frames
        (forwarded to STT.send_audio by the orchestrator)."""
