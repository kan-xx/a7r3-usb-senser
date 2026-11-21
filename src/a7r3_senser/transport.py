"""USB VID/PID mode classification, USB 3.1 SuperSpeed 516B packet-rounded auth transport,
and fail-closed MSC-to-SENSER service transition logic.

No camera file, memory, Backup setting, startup flag, or firmware writes are performed.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime
from enum import Enum
import json
import os
from pathlib import Path
import signal
import time
import traceback
from typing import Any, Callable

SONY_VID = 0x054C
PID_MSC = 0x0BFF
PID_PTP = 0x0C33
PID_SENSER = 0x0336
PID_SENSER_ALT = 0x02A9
PID_CHARGER = 0x0994

AUTH_BYTES = 516
SUPPORTED_BULK_MAX_PACKETS = (64, 512, 1024)


class USBMode(str, Enum):
    """Canonical USB enumeration modes observed on Sony ILCE-7RM3."""

    MSC = "msc"
    PTP = "ptp"
    SENSER = "senser"
    CHARGER = "charger"
    UNKNOWN = "unknown"


class AuthFrameLengthError(ValueError):
    """Raised when a bulk IN read does not return the exact 516-byte PMCA auth frame."""


@dataclass(frozen=True)
class USBModeClassification:
    mode: USBMode
    vid: int
    pid: int
    interface_class: int | None = None
    actionable_for_service_entry: bool = False
    is_hazard_trap: bool = False
    summary: str = ""


def classify_usb_mode(
    vid: int,
    pid: int,
    interface_class: int | None = None,
) -> USBModeClassification:
    """Classify a USB device descriptor into its operational or hazard mode."""
    if vid != SONY_VID:
        return USBModeClassification(
            mode=USBMode.UNKNOWN,
            vid=vid,
            pid=pid,
            interface_class=interface_class,
            summary="Non-Sony USB vendor ID",
        )
    if pid == PID_MSC and (interface_class in (None, 8)):
        return USBModeClassification(
            mode=USBMode.MSC,
            vid=vid,
            pid=pid,
            interface_class=interface_class,
            actionable_for_service_entry=True,
            summary="Sony Mass Storage (MSC Bulk-Only); ready for privileged SENSER entry",
        )
    if pid == PID_PTP or interface_class == 6:
        return USBModeClassification(
            mode=USBMode.PTP,
            vid=vid,
            pid=pid,
            interface_class=interface_class,
            summary="Sony PC Remote / MTP / PTP; switch camera USB Connection to Mass Storage first",
        )
    if pid in (PID_SENSER, PID_SENSER_ALT) and (interface_class in (None, 255)):
        return USBModeClassification(
            mode=USBMode.SENSER,
            vid=vid,
            pid=pid,
            interface_class=interface_class,
            summary="Sony SENSER Vendor-Specific (0xFF) service interface; unprivileged libusb claimable",
        )
    if pid == PID_CHARGER:
        return USBModeClassification(
            mode=USBMode.CHARGER,
            vid=vid,
            pid=pid,
            interface_class=interface_class,
            is_hazard_trap=True,
            summary="Sony USB Charger HID trap (054c:0994); fail closed — swap/charge battery or wake camera physically",
        )
    return USBModeClassification(
        mode=USBMode.UNKNOWN,
        vid=vid,
        pid=pid,
        interface_class=interface_class,
        summary=f"Unrecognized Sony USB PID 0x{pid:04x}",
    )


def auth_read_size(max_packet: int) -> int:
    """Round the 516-byte PMCA auth response read buffer up to a whole wMaxPacketSize multiple.

    On USB 3.1 SuperSpeed (wMaxPacketSize=1024), submitting a 516-byte bulk IN read causes
    Darwin/libusb to fail with EIO / babble risk on multi-packet or packet-aligned transfers.
    Rounding to 1024B (SuperSpeed / High-Speed 512B) or 576B (Full-Speed 64B) prevents host
    controller overflow while preserving the exact 516-byte protocol payload check.
    """
    if max_packet not in SUPPORTED_BULK_MAX_PACKETS:
        raise ValueError(f"Unexpected bulk maximum packet size: {max_packet}")
    return ((AUTH_BYTES + max_packet - 1) // max_packet) * max_packet


def read_auth_response(
    reader: Callable[[ int, int, int | None], bytes | bytearray],
    endpoint: int,
    max_packet: int,
    timeout: int | None = None,
    diagnostic: dict[str, Any] | None = None,
) -> bytes:
    """Submit a wMaxPacketSize-aligned bulk IN read and require an exact 516-byte response."""
    submitted = auth_read_size(max_packet)
    if diagnostic is not None:
        diagnostic.update(submitted_bytes=submitted, endpoint_max_packet=max_packet)
    raw = reader(endpoint, submitted, timeout)
    data = bytes(raw)
    if diagnostic is not None:
        diagnostic["returned_bytes"] = len(data)
    if len(data) != AUTH_BYTES:
        raise AuthFrameLengthError(
            f"Expected exact {AUTH_BYTES}-byte PMCA response; received {len(data)}. "
            "No truncation or retry."
        )
    return data


@dataclass(frozen=True)
class ServiceEntryConfig:
    """Target constraints for bounded MSC -> SENSER service mode transition."""

    expected_model: str = "ILCE-7RM3"
    expected_serial: str | None = None
    expected_bus: int | None = None
    expected_ports: tuple[int, ...] | None = None
    timeout_seconds: int = 45
    state_root: Path | None = None
    enable_libusb_debug_log: bool = False

    @classmethod
    def from_env(
        cls,
        *,
        serial: str | None = None,
        model: str = "ILCE-7RM3",
        bus: int | None = None,
        ports: tuple[int, ...] | None = None,
        timeout_seconds: int = 45,
        state_root: Path | None = None,
    ) -> "ServiceEntryConfig":
        resolved_serial = serial if serial is not None else os.environ.get("A7R3_USB_SERIAL")
        if resolved_serial is not None:
            resolved_serial = resolved_serial.strip() or None
        return cls(
            expected_model=model,
            expected_serial=resolved_serial,
            expected_bus=bus,
            expected_ports=ports,
            timeout_seconds=timeout_seconds,
            state_root=state_root,
        )


def _port_matches(
    candidate: Any,
    expected_bus: int | None,
    expected_ports: tuple[int, ...] | None,
) -> bool:
    if expected_bus is not None and getattr(candidate, "bus", None) != expected_bus:
        return False
    if expected_ports is not None and tuple(getattr(candidate, "port_numbers", ()) or ()) != expected_ports:
        return False
    return True


def run_msc_service_entry(
    config: ServiceEntryConfig,
    *,
    enter: bool,
    report_path: Path | None = None,
    usb_core_mod: Any = None,
    usb_util_mod: Any = None,
    backend_getter: Callable[[], Any] | None = None,
    msc_driver_cls: Any = None,
    usb_backend_cls: Any = None,
    senser_auth_cls: Any = None,
    geteuid_fn: Callable[[], int] = os.geteuid,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> tuple[int, dict[str, Any]]:
    """Verify MSC camera identity and optionally transition into temporary SENSER mode.

    Implements fail-closed cleanup rules:
    - Never sends `stop()` or reattaches host MSC drivers if the USB device changed into
      an unverified service candidate (`0x0336`/`0x02a9`), PTP (`0x0c33`), Charger (`0x0994`),
      or disappeared.
    - Returns `(0, report)` on verified success, `(2, report)` if an unverified service
      candidate appeared after partial failure, and `(1, report)` on failure.
    """
    if usb_core_mod is None or usb_util_mod is None:
        import usb.core as _usb_core
        import usb.util as _usb_util

        usb_core_mod = usb_core_mod or _usb_core
        usb_util_mod = usb_util_mod or _usb_util

    if backend_getter is None:
        import usb.backend.libusb1 as _libusb1

        try:
            import libusb_package

            backend_getter = lambda: _libusb1.get_backend(find_library=libusb_package.find_library)
        except ImportError:
            backend_getter = _libusb1.get_backend

    if enter and (msc_driver_cls is None or usb_backend_cls is None or senser_auth_cls is None):
        from .session import ensure_pmca_importable

        ensure_pmca_importable()
        from pmca.usb.driver.generic.libusb import MscBbbDriver, UsbBackend
        from pmca.usb.sony import SonySenserAuthDevice

        usb_backend_cls = usb_backend_cls or UsbBackend
        msc_driver_cls = msc_driver_cls or MscBbbDriver
        senser_auth_cls = senser_auth_cls or SonySenserAuthDevice

    report: dict[str, Any] = {
        "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "uid": geteuid_fn(),
        "camera_persistent_writes": False,
        "camera_memory_writes": False,
        "firmware_flashed": False,
        "native_start_ACK": None,
        "upstream_start_routine_called": False,
        "service_authentication_completed": False,
        "service_USB_reenumeration_verified": False,
        "helper_version": "a7r3-usb-senser-0.1.0",
        "stage": "enumerate",
        "transport_diagnostics": [],
        "mode_observations": [],
        "unverified_service_candidate_after_failure": False,
        "cleanup_policy": "host_resources_only",
    }

    device = None
    capture = None
    auth = None
    debug_log = None
    saved_stderr = None
    log_path: Path | None = None
    bound_bus: int | None = config.expected_bus
    bound_ports: tuple[int, ...] | None = config.expected_ports
    backend = None

    def observe_owner_usb(label: str) -> str:
        observation: dict[str, Any] = {
            "label": label,
            "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "monotonic_start": time.monotonic(),
            "devices": [],
        }
        try:
            for candidate in usb_core_mod.find(find_all=True, idVendor=SONY_VID, backend=backend):
                if _port_matches(candidate, bound_bus, bound_ports):
                    observation["devices"].append(
                        {
                            "pid": hex(candidate.idProduct),
                            "bus": candidate.bus,
                            "ports": list(getattr(candidate, "port_numbers", ()) or ()),
                            "address": getattr(candidate, "address", None),
                        }
                    )
        except Exception as error:
            observation["enumeration_error"] = repr(error)
        observation["monotonic_end"] = time.monotonic()
        observation["classification"] = "unknown"
        if "enumeration_error" not in observation and len(observation["devices"]) == 1:
            row = observation["devices"][0]
            if row["pid"] in ("0x336", "0x2a9"):
                observation["classification"] = "unique_unverified_service_candidate"
            elif row["pid"] == "0xbff" and row["address"] == report.get("initial_USB_address"):
                observation["classification"] = "same_original_msc_address"
            elif row["pid"] == "0xc33":
                observation["classification"] = "normal_ptp"
            elif row["pid"] == "0x994":
                observation["classification"] = "charger_trap"
        report["mode_observations"].append(observation)
        return observation["classification"]

    def _timeout_handler(signum: int, frame: Any) -> None:
        raise TimeoutError(f"Bounded service entry exceeded {config.timeout_seconds} seconds")

    use_alarm = hasattr(signal, "SIGALRM") and config.timeout_seconds > 0
    old_handler = signal.signal(signal.SIGALRM, _timeout_handler) if use_alarm else None
    if use_alarm:
        signal.alarm(config.timeout_seconds)

    try:
        if enter and config.enable_libusb_debug_log and config.state_root is not None:
            os.environ["LIBUSB_DEBUG"] = "4"
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            log_path = config.state_root / "logs" / f"libusb-{stamp}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            debug_log = log_path.open("xb")
            saved_stderr = os.dup(2)
            os.dup2(debug_log.fileno(), 2)
            report["private_libusb_log"] = str(log_path.relative_to(config.state_root))

        backend = backend_getter()
        found = [
            d
            for d in usb_core_mod.find(find_all=True, idVendor=SONY_VID, backend=backend)
            if _port_matches(d, config.expected_bus, config.expected_ports)
        ]
        if len(found) != 1:
            raise AssertionError(f"Expected exactly one matching Sony camera, found {len(found)}")

        report["stage"] = "verify_msc_descriptors"
        device = found[0]
        mode_info = classify_usb_mode(SONY_VID, device.idProduct)
        if mode_info.is_hazard_trap:
            raise AssertionError(
                "Camera enumerated in USB Charger trap (054c:0994); refusing all USB operations"
            )
        if device.idProduct != PID_MSC:
            raise AssertionError(f"Camera is not in Mass Storage mode (observed PID {hex(device.idProduct)})")
        if config.expected_model and device.product != config.expected_model:
            raise AssertionError(
                f"Camera model mismatch: expected {config.expected_model!r}, got {device.product!r}"
            )
        if config.expected_serial and device.serial_number != config.expected_serial:
            raise AssertionError("Camera USB serial mismatch")

        bound_bus = device.bus
        bound_ports = tuple(getattr(device, "port_numbers", ()) or ())
        if not bound_ports:
            raise AssertionError("Physical USB port path unavailable; refusing unbound service transition")

        interface = device.get_active_configuration()[(0, 0)]
        if interface.bInterfaceClass != 8 or interface.bInterfaceProtocol != 0x50:
            raise AssertionError("Expected MSC Bulk-Only (0x08 / 0x50) USB interface")

        report["initial_USB_address"] = device.address
        packet_sizes = {ep.bEndpointAddress: ep.wMaxPacketSize for ep in interface}
        report["endpoint_packet_sizes"] = {hex(k): v for k, v in packet_sizes.items()}
        report["usb"] = {
            "pid": hex(device.idProduct),
            "model": device.product,
            "serial_redacted": bool(device.serial_number),
            "bus": device.bus,
            "ports": list(bound_ports),
        }
        report["identity_verified"] = True

        if not enter:
            return 0, report

        if geteuid_fn() != 0:
            raise PermissionError("Administrator access required by macOS MSC USB interface")

        base_cls = usb_backend_cls or object

        class _CaptureBackend(base_cls):  # type: ignore[misc,valid-type]
            detached = False

            def __init__(self, dev_obj: Any) -> None:
                if usb_backend_cls is not None:
                    super().__init__(dev_obj)
                else:
                    self.dev = dev_obj

            def reset(self) -> None:
                active = self.dev.is_kernel_driver_active(0)
                report["transport_diagnostics"].append({"op": "kernel_driver_state", "active": active})
                if active:
                    self.dev.detach_kernel_driver(0)
                    self.detached = True
                    report["transport_diagnostics"].append({"op": "detach_kernel_driver", "returned": True})

            def read(self, ep: int, length: int, timeout: int | None = None) -> bytes:
                row: dict[str, Any] = {"op": "bulk_read", "endpoint": hex(ep), "requested_bytes": length}
                report["transport_diagnostics"].append(row)
                try:
                    if report["stage"] == "upstream_authenticate" and length == AUTH_BYTES:
                        return read_auth_response(super().read, ep, packet_sizes[ep], timeout, row)
                    data = super().read(ep, length, timeout)
                    row["returned_bytes"] = len(data)
                    return data
                except Exception as exc:
                    row["error"] = repr(exc)
                    row["cause"] = repr(exc.__context__)
                    raise

            def write(self, ep: int, data: bytes) -> Any:
                row: dict[str, Any] = {"op": "bulk_write", "endpoint": hex(ep), "bytes": len(data)}
                report["transport_diagnostics"].append(row)
                try:
                    return super().write(ep, data)
                except Exception as exc:
                    row["error"] = repr(exc)
                    row["cause"] = repr(exc.__context__)
                    raise

            def vendorRequestOut(self, request: int, value: int, index: int, data: bytes = b"") -> Any:
                row: dict[str, Any] = {
                    "op": "vendor_control",
                    "request": request,
                    "value": hex(value),
                    "index": hex(index),
                    "bytes": len(data),
                }
                report["transport_diagnostics"].append(row)
                is_start = (request, value, index) == getattr(
                    senser_auth_cls, "SONY_VendorRequest_StartSenser", (1, 0x37FF, 0xD7AA)
                )
                try:
                    result = super().vendorRequestOut(request, value, index, data)
                    row["returned"] = True
                    if is_start:
                        report["native_start_ACK"] = True
                    return result
                except Exception as exc:
                    row["error"] = repr(exc)
                    row["cause"] = repr(exc.__context__)
                    if is_start:
                        report["native_start_ACK"] = False
                    raise

        report["stage"] = "create_msc_driver"
        capture = _CaptureBackend(device)
        driver = msc_driver_cls(capture)
        report["stage"] = "create_upstream_auth_client"
        auth = senser_auth_cls(driver)

        report["stage"] = "upstream_start"
        report["upstream_start_routine_called"] = True
        observe_owner_usb("before_upstream_start")
        auth.start()
        observe_owner_usb("after_upstream_start")

        report["stage"] = "upstream_authenticate"
        auth.authenticate()
        report["service_authentication_completed"] = True
        report["cleanup_policy"] = "keep_authenticated_transition"
        observe_owner_usb("after_authentication_completed")

        report["stage"] = "verify_service_reenumeration"
        report["service_enumeration_observations"] = []
        for attempt in range(10):
            sleep_fn(0.5)
            seen = []
            try:
                seen = list(usb_core_mod.find(find_all=True, idVendor=SONY_VID, backend=backend))
                matches = [
                    d
                    for d in seen
                    if _port_matches(d, bound_bus, bound_ports)
                    and d.idProduct in (PID_SENSER, PID_SENSER_ALT)
                ]
                report["service_enumeration_observations"].append(
                    {"attempt": attempt, "matching_service_devices": len(matches)}
                )
                if len(matches) == 1:
                    svc = matches[0]
                    assert svc.get_active_configuration()[(0, 0)].bInterfaceClass == 255
                    report["service_USB_reenumeration_verified"] = True
                    report["service_pid"] = hex(svc.idProduct)
                    break
            except Exception as observation_err:
                report["service_enumeration_observations"].append(
                    {"attempt": attempt, "error": repr(observation_err)}
                )
            finally:
                for observed in seen:
                    try:
                        usb_util_mod.dispose_resources(observed)
                    except Exception as cleanup_err:
                        report["transport_diagnostics"].append(
                            {"op": "dispose_observed_device", "error": repr(cleanup_err)}
                        )

        if not report["service_USB_reenumeration_verified"]:
            raise RuntimeError(
                "Authentication completed but independent service USB enumeration not proved"
            )
        report["handoff"] = (
            "Service interface available; unprivileged client must freshly authenticate "
            "and reverify firmware/files before any session operation."
        )
        return 0, report

    except Exception as exc:
        report["error"] = repr(exc)
        report["traceback"] = traceback.format_exc()
        report["exception_chain"] = []
        current: BaseException | None = exc
        seen_ids: set[int] = set()
        while current is not None and id(current) not in seen_ids and len(report["exception_chain"]) < 6:
            seen_ids.add(id(current))
            report["exception_chain"].append(
                {
                    "type": type(current).__name__,
                    "message": str(current),
                    "errno": getattr(current, "errno", None),
                    "backend_error_code": getattr(current, "backend_error_code", None),
                }
            )
            current = current.__cause__ or current.__context__

        if auth is not None and report["upstream_start_routine_called"] and not report["service_authentication_completed"]:
            state = observe_owner_usb("authentication_failed_before_cleanup")
            if state == "unique_unverified_service_candidate":
                report["unverified_service_candidate_after_failure"] = True
                report["cleanup_policy"] = "preserve_unverified_service_candidate"
                report["handoff"] = (
                    "Known service PID observed after failure; retain only as unverified candidate "
                    "for a fresh identity/interface/authentication check. Not authentication success."
                )
                report["failed_auth_stop_skipped_for_candidate"] = True
            elif state == "same_original_msc_address":
                report["cleanup_policy"] = "stop_current_matching_msc"
                try:
                    auth.stop()
                    report["failed_auth_stop_requested"] = True
                except Exception as cleanup_err:
                    report["cleanup_error"] = repr(cleanup_err)
                last_state = "unknown"
                for attempt in range(6):
                    sleep_fn(0.25)
                    last_state = observe_owner_usb(f"after_failed_auth_stop_{attempt}")
                    if last_state == "unique_unverified_service_candidate":
                        report["unverified_service_candidate_after_failure"] = True
                        report["cleanup_policy"] = "preserve_unverified_service_candidate"
                        report["handoff"] = (
                            "Unverified service candidate appeared during cleanup; "
                            "requires fresh independent authentication."
                        )
                        break
                if not report["unverified_service_candidate_after_failure"]:
                    report["cleanup_policy"] = (
                        "restore_only_matching_original_msc"
                        if last_state == "same_original_msc_address"
                        else "host_resources_only_after_mode_change"
                    )
            else:
                report["cleanup_policy"] = "host_resources_only_after_mode_change"
                report["failed_auth_stop_skipped_for_unknown_or_changed_state"] = True

        return (2 if report["unverified_service_candidate_after_failure"] else 1), report

    finally:
        if use_alarm:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
        if capture is not None:
            report["host_driver_detached_by_this_run"] = capture.detached
        if device is not None:
            if (
                capture is not None
                and capture.detached
                and report["cleanup_policy"] == "restore_only_matching_original_msc"
                and observe_owner_usb("before_host_driver_restore") == "same_original_msc_address"
            ):
                try:
                    usb_util_mod.release_interface(device, 0)
                    device.attach_kernel_driver(0)
                    report["host_driver_restored"] = True
                except Exception as restore_err:
                    report["restore_error"] = repr(restore_err)
            if auth is not None and report["upstream_start_routine_called"]:
                observe_owner_usb("before_final_host_disposal")
            try:
                usb_util_mod.dispose_resources(device)
            except Exception as dispose_err:
                report["dispose_error"] = repr(dispose_err)
        if saved_stderr is not None:
            os.dup2(saved_stderr, 2)
            os.close(saved_stderr)
        if debug_log is not None:
            debug_log.close()
        if log_path is not None and report.get("error"):
            try:
                lines = log_path.read_text(errors="replace").splitlines()
                report["libusb_error_summary"] = [
                    line[:500] for line in lines if "error" in line.lower() or "IOKit" in line
                ][-24:]
            except Exception as read_err:
                report["libusb_summary_error"] = repr(read_err)
        if report_path is not None:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
