"""Paced diagnostic shell execution, echo-safe completion marker parsing,
and terminal recovery helpers for Sony SENSER sessions.
"""

from __future__ import annotations

import time
from typing import Any, Callable
import uuid

DEFAULT_PACE_BYTES = 16
DEFAULT_PACE_DELAY_SEC = 0.002


def ordered_terminal_markers(
    result: bytes,
    begin: bytes,
    end: bytes,
) -> tuple[bytes, int, int] | None:
    """Locate executed begin and end markers while rejecting echoed input lines.

    The camera's diagnostic console echoes the input command line (`echo B_<nonce>; ...; echo E_<nonce>`)
    and may wrap the echoed `E_<nonce>` token onto its own line before execution finishes.
    Therefore, `end` is accepted ONLY when it appears on its own line strictly AFTER the
    executed `begin` marker line (`\\n<begin>\\n`).
    """
    normalized = result.replace(b"\r", b"")
    begin_token = b"\n" + begin + b"\n"
    begin_at = normalized.find(begin_token)
    if begin_at < 0:
        return None
    start = begin_at + len(begin) + 2
    end_token = b"\n" + end + b"\n"
    stop = normalized.find(end_token, max(0, start - 1))
    if stop < 0:
        return None
    return normalized, start, stop


def write_paced_command(
    raw_senser: Any,
    wire_command: bytes,
    *,
    chunk_size: int = DEFAULT_PACE_BYTES,
    delay_sec: float = DEFAULT_PACE_DELAY_SEC,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> int:
    """Write a command to the SENSER diagnostic console in paced 16-byte slices.

    Sending bursts larger than 16 bytes can silently drop characters inside the camera's
    console ring buffer even though USB bulk OUT transfers acknowledge success.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    chunks_sent = 0
    for position in range(0, len(wire_command), chunk_size):
        raw_senser.writeTerminal(wire_command[position : position + chunk_size])
        chunks_sent += 1
        if delay_sec > 0:
            sleep_fn(delay_sec)
    return chunks_sent


def recover_terminal(
    cam: Any,
    raw_senser: Any,
    *,
    pause_sec: float = 0.2,
    drain_reads: int = 12,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> list[bytes]:
    """Reset a stalled SENSER diagnostic shell and drain pending output."""
    cam.setTerminalEnable(False)
    sleep_fn(pause_sec)
    cam.setTerminalEnable(True)
    sleep_fn(pause_sec)
    raw_senser.writeTerminal(b"\n")
    drained: list[bytes] = []
    for _ in range(drain_reads):
        chunk = raw_senser.readTerminal()
        drained.append(chunk)
    return drained


class PacedTerminal:
    """High-level wrapper for deterministic, echo-safe SENSER shell command execution."""

    def __init__(
        self,
        raw_senser: Any,
        cam: Any | None = None,
        *,
        pace_bytes: int = DEFAULT_PACE_BYTES,
        pace_delay_sec: float = DEFAULT_PACE_DELAY_SEC,
        clock_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.raw = raw_senser
        self.cam = cam
        self.pace_bytes = pace_bytes
        self.pace_delay_sec = pace_delay_sec
        self._clock = clock_fn
        self._sleep = sleep_fn

    def execute(
        self,
        command: str,
        *,
        timeout: float = 20.0,
        nonce: str | None = None,
        max_attempts: int = 1,
    ) -> bytes:
        """Execute an ASCII shell command and return the normalized output between markers."""
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        encoded_cmd = command.encode("ascii")
        if b"\x00" in encoded_cmd:
            raise ValueError("NUL byte forbidden in terminal command")

        for attempt in range(max_attempts):
            token = nonce or uuid.uuid4().hex[:12]
            begin = f"B_{token}".encode("ascii")
            end = f"E_{token}".encode("ascii")
            wire_command = b"\necho " + begin + b"; " + encoded_cmd + b"; echo " + end + b"\n"

            write_paced_command(
                self.raw,
                wire_command,
                chunk_size=self.pace_bytes,
                delay_sec=self.pace_delay_sec,
                sleep_fn=self._sleep,
            )

            received = b""
            deadline = self._clock() + timeout
            parsed = None
            while self._clock() < deadline:
                chunk = self.raw.readTerminal()
                if chunk:
                    received += chunk
                    parsed = ordered_terminal_markers(received, begin, end)
                    if parsed is not None:
                        break
                else:
                    self._sleep(0.005)

            if parsed is not None:
                normalized, start, stop = parsed
                return b"\n" + normalized[start:stop] + b"\n"

            if attempt + 1 < max_attempts and self.cam is not None:
                recover_terminal(self.cam, self.raw, sleep_fn=self._sleep)
                nonce = None
                continue

            error = TimeoutError(
                f"No ordered completion markers within {timeout}s; received {len(received)} bytes"
            )
            error.received = received  # type: ignore[attr-defined]
            raise error

        raise RuntimeError("Unreachable terminal execution state")

    def __call__(self, command: str, timeout: float = 20.0) -> bytes:
        return self.execute(command, timeout=timeout)
