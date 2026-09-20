"""Deterministic fake STT -- no real speech engine exists to test
against (see docs/CHECKPOINT-04-NOTES.md). Driven directly via
`simulate_utterance` rather than needing audio fixtures, the same
approach Checkpoint 03's MockTelephonyProvider used for outcomes."""

from app.services.ai.stt.base import StreamingSTT, TranscriptEvent, TranscriptListener


class FakeSTT(StreamingSTT):
    def __init__(self) -> None:
        self._listener: TranscriptListener | None = None
        self._active = False
        self.audio_chunks_received = 0

    def start_session(self, on_transcript: TranscriptListener) -> None:
        self._listener = on_transcript
        self._active = True

    def send_audio(self, chunk: bytes) -> None:
        if not self._active:
            raise RuntimeError("send_audio called on an STT session that is not active")
        self.audio_chunks_received += 1

    def stop_session(self) -> None:
        self._active = False
        self._listener = None

    def simulate_utterance(self, text: str, *, partials: list[str] | None = None) -> None:
        """Test helper: emits any given partial transcripts (never
        written to durable storage by the orchestrator) followed by one
        final transcript."""
        if self._listener is None:
            raise RuntimeError("simulate_utterance called before start_session")
        for partial in partials or []:
            self._listener(TranscriptEvent(text=partial, is_final=False))
        self._listener(TranscriptEvent(text=text, is_final=True))
