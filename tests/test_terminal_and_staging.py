"""Offline unit tests for terminal echo-vs-execution marker ordering,
16B/2ms command pacing, ProductInfo 0x10 plugin framing, and `/tmp_bt` RAM staging.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest

from a7r3_senser.session import (
    CHUNK_BYTES,
    MAX_BUNDLE_BYTES,
    MAX_FILE_BYTES,
    PFUNC_PRODUCT_INFO,
    pack_plugin_request,
    prepare_ram_bundle,
    send_plugin_command,
)
from a7r3_senser.terminal import (
    PacedTerminal,
    ordered_terminal_markers,
    recover_terminal,
    write_paced_command,
)


class TerminalProtocolTests(unittest.TestCase):
    def test_echoed_end_marker_before_executed_begin_is_ignored(self) -> None:
        begin = b"B_112233445566"
        end = b"E_112233445566"
        # Console line-wrap puts echoed E_... on its own line BEFORE execution output!
        echo_only = (
            b"# echo B_112233445566; cat /proc/mounts; echo \r\n"
            b"E_112233445566\r\n"
            b"B_112233445566\r\n"
            b"none /tmp_bt tmpfs rw 0 0\r\n"
        )
        self.assertIsNone(ordered_terminal_markers(echo_only, begin, end))

        completed = echo_only + b"E_112233445566\r\n# "
        parsed = ordered_terminal_markers(completed, begin, end)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        normalized, start, stop = parsed
        self.assertEqual(normalized[start:stop], b"none /tmp_bt tmpfs rw 0 0")

    def test_write_paced_command_slices_at_16_bytes(self) -> None:
        written: list[bytes] = []
        sleeps: list[float] = []

        class _Raw:
            def writeTerminal(self, data: bytes) -> None:
                written.append(data)

        payload = b"0123456789abcdefABCDEF0123456789!"
        count = write_paced_command(_Raw(), payload, chunk_size=16, delay_sec=0.002, sleep_fn=sleeps.append)
        self.assertEqual(count, 3)
        self.assertEqual([len(x) for x in written], [16, 16, 1])
        self.assertEqual(b"".join(written), payload)
        self.assertEqual(sleeps, [0.002, 0.002, 0.002])

    def test_paced_terminal_recovers_after_stalled_first_attempt(self) -> None:
        toggles: list[bool] = []
        writes: list[bytes] = []
        attempt_counter = {"n": 0}

        class _Cam:
            def setTerminalEnable(self, flag: bool) -> None:
                toggles.append(flag)

        class _Raw:
            def __init__(self) -> None:
                self.current_wire = b""
                self.responded = False

            def writeTerminal(self, data: bytes) -> None:
                writes.append(data)
                self.current_wire += data

            def readTerminal(self) -> bytes:
                if b"; echo E_" not in self.current_wire or not self.current_wire.endswith(b"\n"):
                    return b""
                if attempt_counter["n"] == 0:
                    # Simulate stalled console on attempt 1
                    return b""
                if not self.responded:
                    self.responded = True
                    # Parse nonce out of wire command
                    parts = self.current_wire.split(b"B_", 1)[1]
                    nonce = parts[:12]
                    return b"\r\nB_" + nonce + b"\r\nOK_LINE\r\nE_" + nonce + b"\r\n"
                return b""

        clock = {"t": 0.0}

        def fake_clock() -> float:
            clock["t"] += 0.25
            return clock["t"]

        def fake_sleep(dt: float) -> None:
            if dt >= 0.2:
                attempt_counter["n"] = 1
                raw.current_wire = b""

        cam = _Cam()
        raw = _Raw()
        term = PacedTerminal(raw, cam, clock_fn=fake_clock, sleep_fn=fake_sleep)
        out = term.execute("echo OK_LINE", timeout=1.0, max_attempts=2)
        self.assertEqual(out, b"\nOK_LINE\n")
        self.assertEqual(toggles, [False, True])

    def test_recover_terminal_helper(self) -> None:
        events: list[object] = []

        class _Cam:
            def setTerminalEnable(self, en: bool) -> None:
                events.append(("enable", en))

        class _Raw:
            def writeTerminal(self, d: bytes) -> None:
                events.append(("write", d))

            def readTerminal(self) -> bytes:
                return b"."

        drained = recover_terminal(_Cam(), _Raw(), drain_reads=4, sleep_fn=lambda _s: None)
        self.assertEqual(len(drained), 4)
        self.assertEqual(events[:3], [("enable", False), ("enable", True), ("write", b"\n")])

    def test_product_info_plugin_packet_helper(self) -> None:
        challenge = b"CHALLENGE_16BYTE"
        pkt = pack_plugin_request(0xFA00, challenge)
        self.assertEqual(pkt[:4], struct.pack("<HH", 0, 0xFA00))
        self.assertEqual(pkt[4:], challenge)

        sent: list[tuple[int, bytes]] = []

        class _Raw:
            def sendSenserPacket(self, pfunc: int, data: bytes) -> tuple[int, bytes]:
                sent.append((pfunc, data))
                return 1, b"A7RCAN1\x00" + challenge

        status, reply = send_plugin_command(_Raw(), 0xFA00, challenge)
        self.assertEqual(status, 1)
        self.assertEqual(reply, b"A7RCAN1\x00" + challenge)
        self.assertEqual(sent, [(PFUNC_PRODUCT_INFO, pkt)])

        with self.assertRaises(ValueError):
            pack_plugin_request(0x00FF, b"")


class BoundedRamStageTests(unittest.TestCase):
    def _make_camera_and_terminal(self, corrupt_readback_chunk: int | None = None):
        fs: dict[str, bytes] = {}
        commands: list[str] = []
        write_count = {"n": 0}

        class _Cam:
            def writeFile(self, path: str, data: bytes) -> None:
                self_idx = write_count["n"]
                write_count["n"] += 1
                if corrupt_readback_chunk is not None and self_idx == corrupt_readback_chunk:
                    fs[path] = data[:-1] + bytes([data[-1] ^ 0xFF])
                else:
                    fs[path] = bytes(data)

            def readFile(self, path: str) -> bytes:
                return fs[path]

        def fake_terminal(cmd: str) -> bytes:
            commands.append(cmd)
            if " > " in cmd and "(set -C; cat " in cmd:
                inner = cmd.split("(set -C; cat ", 1)[1].split(")", 1)[0]
                srcs_str, dst = inner.split(" > ")
                assembled = b"".join(fs[p] for p in srcs_str.split())
                assert dst not in fs
                fs[dst] = assembled
            elif cmd.startswith("rm "):
                targets = cmd.split(" && ", 1)[0][3:].split()
                for t in targets:
                    fs.pop(t, None)
            marker = cmd.rsplit("echo ", 1)[1]
            return b"\n" + marker.encode("ascii") + b"\n"

        return _Cam(), fake_terminal, fs, commands

    def test_multi_chunk_tmp_bt_stage_verifies_and_cleans_chunks(self) -> None:
        blob = bytes((i * 17 + 3) & 0xFF for i in range(CHUNK_BYTES * 2 + 1234))
        digest = hashlib.sha256(blob).hexdigest()
        cam, term, fs, commands = self._make_camera_and_terminal()

        with tempfile.TemporaryDirectory() as td:
            journal = Path(td) / "stage.json"
            paths = prepare_ram_bundle(
                cam,
                term,
                {"libtest_alpha.so": (blob, digest)},
                "unit-stage",
                journal,
                "sess-001",
            )
            state = json.loads(journal.read_text(encoding="utf-8"))

        staged_path = paths["libtest_alpha.so"]
        self.assertTrue(staged_path.startswith("/tmp_bt/a7r3-upload-"))
        self.assertFalse(staged_path.startswith("/tmp/"))
        self.assertEqual(fs[staged_path], blob)
        # All p0_* chunks removed, only final library remains
        self.assertEqual(list(fs.keys()), [staged_path])
        self.assertEqual(state["phase"], "RAM_BUNDLE_VERIFIED")
        self.assertFalse(state["persistent_write"])
        self.assertTrue(any("test -d /tmp_bt && test ! -L /tmp_bt" in c for c in commands))

    def test_validation_rejects_bad_inputs_before_creating_journal(self) -> None:
        cam, term, _fs, _cmds = self._make_camera_and_terminal()
        good = b"abcd"
        good_sha = hashlib.sha256(good).hexdigest()

        with tempfile.TemporaryDirectory() as td:
            j = Path(td) / "never-created.json"
            with self.assertRaises(ValueError):
                prepare_ram_bundle(cam, term, {"badname.bin": (good, good_sha)}, "ok", j, "s1")
            with self.assertRaises(ValueError):
                prepare_ram_bundle(cam, term, {"libok.so": (good, "0" * 64)}, "ok", j, "s1")
            with self.assertRaises(ValueError):
                big = bytes(MAX_FILE_BYTES + 1)
                prepare_ram_bundle(
                    cam, term, {"libbig.so": (big, hashlib.sha256(big).hexdigest())}, "ok", j, "s1"
                )
            with self.assertRaises(ValueError):
                b1 = bytes(MAX_FILE_BYTES)
                b2 = bytes(MAX_BUNDLE_BYTES - MAX_FILE_BYTES + 1)
                prepare_ram_bundle(
                    cam,
                    term,
                    {
                        "libpart1.so": (b1, hashlib.sha256(b1).hexdigest()),
                        "libpart2.so": (b2, hashlib.sha256(b2).hexdigest()),
                    },
                    "ok",
                    j,
                    "s1",
                )
            self.assertFalse(j.exists())

    def test_chunk_readback_mismatch_stops_without_retry_and_records_journal(self) -> None:
        blob = b"X" * 4096
        digest = hashlib.sha256(blob).hexdigest()
        cam, term, _fs, _cmds = self._make_camera_and_terminal(corrupt_readback_chunk=0)

        with tempfile.TemporaryDirectory() as td:
            journal = Path(td) / "failed-stage.json"
            with self.assertRaises(RuntimeError):
                prepare_ram_bundle(
                    cam, term, {"libcorrupt.so": (blob, digest)}, "fail-test", journal, "sess-002"
                )
            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "STOPPED_NO_RETRY")
            self.assertEqual(state["failed_phase"], "WRITE_CHUNK")
            self.assertFalse(state["retry_allowed"])


if __name__ == "__main__":
    unittest.main()
