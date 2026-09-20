"""Deterministic fake audio gateway. No real telephony audio transport
exists to integrate against yet (Checkpoint 03 only implemented call
*initiation*, not a live media stream) -- see docs/CHECKPOINT-04-NOTES.md."""

from collections.abc import Callable

from app.services.ai.audio.base import AudioSession


class FakeAudioSession(AudioSession):
    def __init__(self, session_id: str, call_attempt_id: str) -> None:
        self.session_id = session_id
        self.call_attempt_id = call_attempt_id
        self._connected = False
        self._inbound_callback: Callable[[bytes], None] | None = None
        self.outbound_audio_log: list[bytes] = []
        self.interrupt_count = 0

    def start(self) -> None:
        self._connected = True

    def stop(self) -> None:
        self._connected = False

    def send_outbound_audio(self, chunk: bytes) -> None:
        if not self._connected:
            raise RuntimeError("send_outbound_audio called on a stopped audio session")
        self.outbound_audio_log.append(chunk)

    def interrupt(self) -> None:
        self.interrupt_count += 1

    def is_connected(self) -> bool:
        return self._connected

    def on_inbound_audio(self, callback: Callable[[bytes], None]) -> None:
        self._inbound_callback = callback

    def simulate_inbound_audio(self, chunk: bytes) -> None:
        if self._inbound_callback is None:
            raise RuntimeError("no inbound audio callback registered")
        self._inbound_callback(chunk)
