"""Streaming STT abstraction -- Checkpoint 04 Step 7.

Partial transcripts are ephemeral (never written to Postgres); only a
finalized utterance becomes durable. The interface shape follows the
checkpoint's own suggested concepts (start_session, send_audio,
receive_*_transcript, stop_session) since no specific provider is named
anywhere in the specs (see docs/CHECKPOINT-04-NOTES.md).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Protocol


@dataclass
class TranscriptEvent:
    text: str
    is_final: bool


class TranscriptListener(Protocol):
    def __call__(self, event: TranscriptEvent) -> None: ...


class StreamingSTT(ABC):
    @abstractmethod
    def start_session(self, on_transcript: TranscriptListener) -> None: ...

    @abstractmethod
    def send_audio(self, chunk: bytes) -> None: ...

    @abstractmethod
    def stop_session(self) -> None: ...
