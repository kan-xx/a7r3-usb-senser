"""Authenticated SENSER session wrapper, `/tmp_bt` bounded RAM-staging protocol,
and `ProductInfo` (`pfunc 0x10`) in-process plugin dispatch helper.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import struct
import sys
from typing import Any, Callable
import uuid

from .terminal import PacedTerminal
from .transport import PID_SENSER, PID_SENSER_ALT, SONY_VID

CHUNK_BYTES = 32768
MAX_FILE_BYTES = 196608
MAX_BUNDLE_BYTES = 262144
PFUNC_PRODUCT_INFO = 0x10


def resolve_pmca_root(explicit_root: Path | str | None = None) -> Path | None:
    """Resolve an external Sony-PMCA-RE checkout path from argument or SONY_PMCA_ROOT."""
    candidate = explicit_root or os.environ.get("SONY_PMCA_ROOT")
    if not candidate:
        return None
    root = Path(candidate).expanduser().resolve(strict=True)
    if not (root / "pmca").is_dir():
        raise RuntimeError(f"SONY_PMCA_ROOT does not contain a 'pmca/' package directory: {root}")
    return root


def ensure_pmca_importable(explicit_root: Path | str | None = None) -> None:
    """Ensure `pmca` is importable without vendoring third-party Sony-PMCA-RE code."""
    root = resolve_pmca_root(explicit_root)
    if root is not None and str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        import pmca.usb.sony  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "Unable to import 'pmca'. Clone Sony-PMCA-RE externally and set SONY_PMCA_ROOT=/path/to/Sony-PMCA-RE."
        ) from exc


def pack_plugin_request(command_id: int, payload: bytes = b"", *, subfunc: int = 0) -> bytes:
    """Pack a SENSER ProductInfo (`pfunc 0x10`) plugin command frame.

    Inside `im.elf`, `libsencore.so` extracts the upper byte `NN = (command_id >> 8) & 0xff`,
    calls `dlopen("/usr/lib/libproNN.so", RTLD_LAZY)`, resolves `commandNNNN`, and invokes
    `PluginCommand::DoExecute` on the 4-byte `<HH` header (`subfunc`, `command_id`) followed
    by `payload`. All cached plugin handles are `dlclose`d when the SENSER session closes.
    """
    if not (0 <= subfunc <= 0xFFFF):
        raise ValueError(f"subfunc must be a 16-bit unsigned integer, got {subfunc}")
    if not (0x0100 <= command_id <= 0xFFFF):
        raise ValueError(f"command_id must be in 0x0100..0xFFFF, got {hex(command_id)}")
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError("payload must be bytes")
    return struct.pack("<HH", subfunc, command_id) + bytes(payload)


def send_plugin_command(
    raw_senser: Any,
    command_id: int,
    payload: bytes = b"",
    *,
    subfunc: int = 0,
) -> tuple[int, bytes]:
    """Send a `ProductInfo` (`pfunc 0x10`) command packet and return `(status_code, reply_bytes)`."""
    packet = pack_plugin_request(command_id, payload, subfunc=subfunc)
    status, reply = raw_senser.sendSenserPacket(PFUNC_PRODUCT_INFO, packet)
    return int(status), bytes(reply)


def prepare_ram_bundle(
    cam: Any,
    terminal: Callable[[str], bytes],
    payloads: dict[str, tuple[bytes, str]],
    label: str,
    journal_path: Path | str,
    session: str,
    *,
    stage_prefix: str = "a7r3-upload",
) -> dict[str, str]:
    """Stage SHA-256 verified files into volatile `/tmp_bt` (`tmpfs`) in 32 KiB chunks.

    Safety invariants:
    - `/tmp` on ILCE-7RM3 is persistent VFAT flash (`/dev/nflasha2`) and is NEVER touched.
    - `/tmp_bt` is verified as a real non-symlink directory before creating a private `0700` dir.
    - Every 32 KiB chunk and final noclobber-assembled file is read back and SHA-256 verified.
    - Any mismatch or transport failure records `STOPPED_NO_RETRY` in the fsync'd host journal
      and raises without retrying an indeterminate camera state.
    """
    if not re.fullmatch(r"[a-z0-9-]{1,64}", label):
        raise ValueError("invalid staging label")
    if not re.fullmatch(r"[a-z0-9-]{1,32}", stage_prefix):
        raise ValueError("invalid stage_prefix")
    if not isinstance(payloads, dict) or not 1 <= len(payloads) <= 8:
        raise ValueError("invalid bundle: expected 1..8 entries")

    total = 0
    rows = []
    for name, pair in payloads.items():
        if not re.fullmatch(r"lib[a-zA-Z0-9_]{1,80}\.so", name):
            raise ValueError(f"invalid library name: {name!r}")
        data, digest = pair
        if not isinstance(data, bytes) or not 0 < len(data) <= MAX_FILE_BYTES:
            raise ValueError(f"payload size outside bounded RAM transfer: {name}")
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError(f"payload SHA-256 identity mismatch: {name}")
        total += len(data)
        rows.append({"name": name, "bytes": len(data), "sha256": digest})

    if total > MAX_BUNDLE_BYTES:
        raise ValueError(f"bundle exceeds {MAX_BUNDLE_BYTES}-byte RAM budget ({total} bytes)")

    directory = f"/tmp_bt/{stage_prefix}-{uuid.uuid4().hex}"
    state: dict[str, Any] = {
        "phase": "PREPARED",
        "label": label,
        "session": str(session),
        "directory": directory,
        "files": rows,
        "operations": [],
        "camera_library_loaded": False,
        "persistent_write": False,
        "retry_allowed": False,
        "native_acceptance": False,
    }
    journal = Path(journal_path)

    with journal.open("x", encoding="utf-8") as log:
        def save() -> None:
            log.seek(0)
            json.dump(state, log, indent=2)
            log.write("\n")
            log.truncate()
            log.flush()
            os.fsync(log.fileno())

        def attempt(kind: str, target: Any) -> dict[str, Any]:
            state["phase"] = kind
            row: dict[str, Any] = {"kind": kind, "target": target, "outcome": "ATTEMPTED"}
            state["operations"].append(row)
            save()
            return row

        def run_cmd(cmd_str: str, marker: str) -> None:
            reply = terminal(cmd_str + " && echo " + marker)
            if b"\n" + marker.encode("ascii") + b"\n" not in reply:
                raise RuntimeError("camera command did not acknowledge " + marker)

        save()
        try:
            op = attempt("CREATE_PRIVATE_DIRECTORY", directory)
            run_cmd(
                f"test -d /tmp_bt && test ! -L /tmp_bt && mkdir -m 700 {directory}",
                "RAM_DIRECTORY_CREATED",
            )
            op["outcome"] = "CONFIRMED"
            save()

            result: dict[str, str] = {}
            for index, row in enumerate(rows):
                name = row["name"]
                data = payloads[name][0]
                chunks: list[str] = []
                for number, offset in enumerate(range(0, len(data), CHUNK_BYTES)):
                    part_path = f"{directory}/p{index}_{number}"
                    chunk = data[offset : offset + CHUNK_BYTES]
                    op = attempt("WRITE_CHUNK", part_path)
                    op.update(bytes=len(chunk), sha256=hashlib.sha256(chunk).hexdigest())
                    save()
                    cam.writeFile(part_path, chunk)
                    actual = cam.readFile(part_path)
                    if actual != chunk:
                        raise RuntimeError("chunk readback mismatch: " + part_path)
                    op["outcome"] = "READBACK_VERIFIED"
                    save()
                    chunks.append(part_path)

                assembled = f"{directory}/{name}"
                op = attempt("ASSEMBLE_EXCLUSIVE_FILE", assembled)
                run_cmd(
                    f"(set -C; cat {' '.join(chunks)} > {assembled})",
                    "RAM_ASSEMBLED",
                )
                actual = cam.readFile(assembled)
                if actual != data or hashlib.sha256(actual).hexdigest() != row["sha256"]:
                    raise RuntimeError("assembled readback mismatch: " + assembled)
                run_cmd(f"chmod 400 {assembled}", "RAM_FILE_READONLY")
                op["outcome"] = "WHOLE_FILE_READBACK_VERIFIED"
                save()

                op = attempt("REMOVE_VERIFIED_CHUNKS", list(chunks))
                run_cmd(f"rm {' '.join(chunks)}", "RAM_CHUNKS_REMOVED")
                op["outcome"] = "CONFIRMED"
                save()
                result[name] = assembled

            state["phase"] = "RAM_BUNDLE_VERIFIED"
            state["paths"] = result
            save()
            return result
        except BaseException as exc:
            state["failed_phase"] = state["phase"]
            state["phase"] = "STOPPED_NO_RETRY"
            state["error_type"] = type(exc).__name__
            state["error"] = str(exc)
            save()
            raise


class SenserSession:
    """Context manager for an unprivileged Sony SENSER (`0x054c:0x0336`) USB session."""

    def __init__(
        self,
        *,
        pmca_root: Path | str | None = None,
        bus: int | None = None,
        ports: tuple[int, ...] | None = None,
        enable_terminal: bool = False,
    ) -> None:
        self.pmca_root = pmca_root
        self.bus = bus
        self.ports = ports
        self.enable_terminal = enable_terminal
        self.dev: Any = None
        self.raw: Any = None
        self.auth: Any = None
        self.cam: Any = None
        self.terminal: PacedTerminal | None = None
        self._usb_util: Any = None

    def __enter__(self) -> "SenserSession":
        ensure_pmca_importable(self.pmca_root)
        import usb.backend.libusb1
        import usb.core
        import usb.util
        from pmca.usb.driver.generic import GenericUsbDriver
        from pmca.usb.driver.generic.libusb import UsbBackend
        from pmca.usb.sony import SonySenserAuthDevice, SonySenserCamera, SonySenserDevice

        self._usb_util = usb.util
        try:
            import libusb_package

            backend = usb.backend.libusb1.get_backend(find_library=libusb_package.find_library)
        except ImportError:
            backend = usb.backend.libusb1.get_backend()

        candidates = []
        for candidate in usb.core.find(find_all=True, idVendor=SONY_VID, backend=backend):
            if candidate.idProduct not in (PID_SENSER, PID_SENSER_ALT):
                continue
            if self.bus is not None and candidate.bus != self.bus:
                continue
            if self.ports is not None and tuple(candidate.port_numbers or ()) != self.ports:
                continue
            candidates.append(candidate)

        if len(candidates) != 1:
            for c in candidates:
                usb.util.dispose_resources(c)
            raise RuntimeError(
                f"Expected uniquely transitioned SENSER camera (054c:0336), found {len(candidates)}"
            )

        self.dev = candidates[0]
        driver = GenericUsbDriver(UsbBackend(self.dev))
        self.raw = SonySenserDevice(driver)
        self.auth = SonySenserAuthDevice(driver)
        self.auth.start()
        self.auth.authenticate()
        self.cam = SonySenserCamera(self.raw)
        self.cam.readHasp()
        if self.enable_terminal:
            self.cam.setTerminalEnable(True)
        self.terminal = PacedTerminal(self.raw, self.cam)
        return self

    def send_plugin(
        self,
        command_id: int,
        payload: bytes = b"",
        *,
        subfunc: int = 0,
    ) -> tuple[int, bytes]:
        if self.raw is None:
            raise RuntimeError("SENSER session is not open")
        return send_plugin_command(self.raw, command_id, payload, subfunc=subfunc)

    def stage_ram_bundle(
        self,
        payloads: dict[str, tuple[bytes, str]],
        *,
        label: str,
        journal_path: Path | str,
        session_id: str | None = None,
    ) -> dict[str, str]:
        if self.cam is None or self.terminal is None:
            raise RuntimeError("SENSER session is not open")
        return prepare_ram_bundle(
            self.cam,
            self.terminal,
            payloads,
            label,
            journal_path,
            session_id or uuid.uuid4().hex[:12],
        )

    def close(self) -> None:
        if self.auth is not None:
            try:
                self.auth.stop()
            finally:
                self.auth = None
        if self.dev is not None and self._usb_util is not None:
            try:
                self._usb_util.dispose_resources(self.dev)
            finally:
                self.dev = None

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()
