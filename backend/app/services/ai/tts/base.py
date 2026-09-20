"""TTS abstraction -- Checkpoint 04 Step 22.

synthesize() is the minimum every implementation must support;
stream_synthesize() is used when the provider supports true low-latency
streaming (Step 22's "should support... where the selected provider
supports it") -- the base class provides a default streaming
implementation (yield the one synthesize() result) so a non-streaming
provider still satisfies the interface without every caller needing to
special-case it.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterator


class TTSTimeoutError(Exception):
    pass


class TTSProviderError(Exception):
    pass


class TTS(ABC):
    @abstractmethod
    def synthesize(self, text: str) -> bytes:
        """Raises TTSTimeoutError / TTSProviderError on failure."""

    def stream_synthesize(self, text: str) -> Iterator[bytes]:
        yield self.synthesize(text)
