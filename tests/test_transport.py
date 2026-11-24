"""Offline unit tests for USB mode classification, USB3 516B->1024B/576B packet rounding,
and fail-closed MSC-to-SENSER cleanup transitions.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from a7r3_senser.transport import (
    AUTH_BYTES,
    PID_CHARGER,
    PID_MSC,
    PID_PTP,
    PID_SENSER,
    PID_SENSER_ALT,
    SONY_VID,
    AuthFrameLengthError,
    ServiceEntryConfig,
    USBMode,
    auth_read_size,
    classify_usb_mode,
    read_auth_response,
    run_msc_service_entry,
)


class _MockInterface(list):
    bInterfaceClass = 8
    bInterfaceProtocol = 0x50


class _MockServiceInterface(list):
    bInterfaceClass = 255
    bInterfaceProtocol = 0


class _MockMscDevice:
    def __init__(self, pid: int = PID_MSC, serial: str = "SYNTHETIC_SN_01") -> None:
        self.idVendor = SONY_VID
        self.idProduct = pid
        self.product = "ILCE-7RM3"
        self.serial_number = serial
        self.bus = 1
        self.port_numbers = (2,)
        self.address = 7
        self.active = True
        self.attaches = 0

    def is_kernel_driver_active(self, _iface: int) -> bool:
        return self.active

    def detach_kernel_driver(self, _iface: int) -> None:
        self.active = False

    def attach_kernel_driver(self, _iface: int) -> None:
        self.active = True
        self.attaches += 1

    def get_active_configuration(self) -> dict[tuple[int, int], _MockInterface]:
        return {
            (0, 0): _MockInterface(
                [
                    SimpleNamespace(bEndpointAddress=0x81, wMaxPacketSize=1024),
                    SimpleNamespace(bEndpointAddress=0x02, wMaxPacketSize=1024),
                ]
            )
        }


class TransportGeometryAndModeTests(unittest.TestCase):
    def test_usb_mode_classifier(self) -> None:
        msc = classify_usb_mode(SONY_VID, PID_MSC, 8)
        self.assertEqual(msc.mode, USBMode.MSC)
        self.assertTrue(msc.actionable_for_service_entry)
        self.assertFalse(msc.is_hazard_trap)

        ptp = classify_usb_mode(SONY_VID, PID_PTP, 6)
        self.assertEqual(ptp.mode, USBMode.PTP)
        self.assertFalse(ptp.actionable_for_service_entry)

        senser = classify_usb_mode(SONY_VID, PID_SENSER, 255)
        self.assertEqual(senser.mode, USBMode.SENSER)
        senser_alt = classify_usb_mode(SONY_VID, PID_SENSER_ALT, 255)
        self.assertEqual(senser_alt.mode, USBMode.SENSER)

        charger = classify_usb_mode(SONY_VID, PID_CHARGER, 3)
        self.assertEqual(charger.mode, USBMode.CHARGER)
        self.assertTrue(charger.is_hazard_trap)
        self.assertFalse(charger.actionable_for_service_entry)

        other = classify_usb_mode(0x1234, PID_MSC, 8)
        self.assertEqual(other.mode, USBMode.UNKNOWN)

    def test_usb3_superspeed_read_rounds_516_to_1024(self) -> None:
        calls: list[tuple[int, int, int | None]] = []
        payload = bytes(range(256)) * 2 + b"\xaa\xbb\xcc\xdd"
        self.assertEqual(len(payload), AUTH_BYTES)
        diagnostic: dict[str, int] = {}

        def reader(ep: int, size: int, timeout: int | None) -> bytes:
            calls.append((ep, size, timeout))
            if size % 1024 != 0:
                raise OSError(5, "Fixture rejects non-packet-aligned SuperSpeed read")
            return payload

        out = read_auth_response(reader, 0x81, 1024, 1000, diagnostic)
        self.assertEqual(out, payload)
        self.assertEqual(calls, [(0x81, 1024, 1000)])
        self.assertEqual(diagnostic["submitted_bytes"], 1024)
        self.assertEqual(diagnostic["returned_bytes"], 516)

    def test_high_speed_and_full_speed_packet_rounding(self) -> None:
        self.assertEqual(auth_read_size(1024), 1024)
        self.assertEqual(auth_read_size(512), 1024)
        self.assertEqual(auth_read_size(64), 576)
        self.assertEqual(read_auth_response(lambda ep, n, t: bytes(516), 0x81, 512), bytes(516))
        self.assertEqual(read_auth_response(lambda ep, n, t: bytes(516), 0x81, 64), bytes(516))

    def test_no_truncation_or_zero_padding_accepted(self) -> None:
        for bad_len in (0, 64, 512, 515, 517, 576, 1024):
            with self.subTest(bad_len=bad_len), self.assertRaises(AuthFrameLengthError):
                read_auth_response(lambda ep, n, t: bytes(bad_len), 0x81, 1024)

    def test_io_error_propagated_without_retry(self) -> None:
        calls: list[tuple[int, int, int | None]] = []

        def failing_reader(ep: int, n: int, t: int | None) -> bytes:
            calls.append((ep, n, t))
            raise OSError(5, "Input/Output Error")

        with self.assertRaises(OSError):
            read_auth_response(failing_reader, 0x81, 1024)
        self.assertEqual(len(calls), 1)

    def test_unknown_packet_geometry_rejected_before_io(self) -> None:
        calls: list[object] = []
        with self.assertRaises(ValueError):
            read_auth_response(lambda *a: calls.append(a) or bytes(516), 0x81, 123)  # type: ignore[return-value]
        self.assertEqual(calls, [])


class ServiceEntryCleanupTests(unittest.TestCase):
    def _run_case(
        self,
        *,
        service_before_stop: bool = False,
        initial_pid: int = PID_MSC,
        failed_state: str = "msc",
        keep_msc_after_stop: bool = False,
        succeed: bool = False,
    ) -> tuple[int, dict, dict]:
        state = {"phase": "initial", "stops": 0, "auth_calls": 0}
        device = _MockMscDevice(pid=initial_pid, serial="SYNTHETIC_SN_01")
        service = SimpleNamespace(
            idVendor=SONY_VID,
            idProduct=PID_SENSER,
            bus=1,
            port_numbers=(2,),
            address=8,
            get_active_configuration=lambda: {(0, 0): _MockServiceInterface()},
        )
        normal_ptp = SimpleNamespace(
            idVendor=SONY_VID, idProduct=PID_PTP, bus=1, port_numbers=(2,), address=9
        )

        def finder(**_kwargs: object) -> list[object]:
            if state["phase"] == "authenticated":
                return [service]
            if state["phase"] == "failed":
                if service_before_stop:
                    return [service]
                if failed_state == "ptp":
                    return [normal_ptp]
                if failed_state == "absent":
                    return []
                if failed_state == "error":
                    raise OSError("Simulated enumeration failure")
            if state["phase"] == "stopped":
                return [device] if keep_msc_after_stop else [normal_ptp]
            return [device]

        class _BaseUsbBackend:
            def __init__(self, dev_obj: object) -> None:
                self.dev = dev_obj

            def read(self, _ep: int, length: int, _timeout: int | None = None) -> bytes:
                self.last_read_length = length
                return bytes(AUTH_BYTES)

            def write(self, _ep: int, data: bytes) -> int:
                return len(data)

            def vendorRequestOut(self, *_args: object) -> int:
                return 0

        class _MockAuth:
            SONY_VendorRequest_StartSenser = (1, 0x37FF, 0xD7AA)

            def __init__(self, driver: SimpleNamespace) -> None:
                self.backend = driver.backend
                self.backend.reset()

            def start(self) -> None:
                state["phase"] = "started"
                self.backend.vendorRequestOut(1, 0x37FF, 0xD7AA, b"")

            def authenticate(self) -> None:
                state["auth_calls"] += 1
                if succeed:
                    data = self.backend.read(0x81, AUTH_BYTES, 1000)
                    assert len(data) == AUTH_BYTES
                    state["phase"] = "authenticated"
                    return
                state["phase"] = "failed"
                raise OSError(5, "Simulated USB auth read failure")

            def stop(self) -> None:
                state["stops"] += 1
                state["phase"] = "stopped"

        cfg = ServiceEntryConfig(
            expected_model="ILCE-7RM3",
            expected_serial="SYNTHETIC_SN_01",
            expected_bus=1,
            expected_ports=(2,),
            timeout_seconds=0,
        )
        with tempfile.TemporaryDirectory() as td:
            report_path = Path(td) / "report.json"
            rc, report = run_msc_service_entry(
                cfg,
                enter=True,
                report_path=report_path,
                usb_core_mod=SimpleNamespace(find=finder),
                usb_util_mod=SimpleNamespace(
                    dispose_resources=lambda _d: None,
                    release_interface=lambda _d, _i: None,
                ),
                backend_getter=lambda: object(),
                usb_backend_cls=_BaseUsbBackend,
                msc_driver_cls=lambda b: SimpleNamespace(backend=b),
                senser_auth_cls=_MockAuth,
                geteuid_fn=lambda: 0,
                sleep_fn=lambda _s: None,
            )
            saved = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["service_authentication_completed"], report["service_authentication_completed"])
        state["attaches"] = device.attaches
        return rc, report, state

    def test_successful_usb3_entry_verifies_reenumeration(self) -> None:
        rc, d, state = self._run_case(succeed=True)
        self.assertEqual(rc, 0)
        self.assertTrue(d["service_authentication_completed"])
        self.assertTrue(d["service_USB_reenumeration_verified"])
        self.assertEqual(d["service_pid"], "0x336")
        bulk_reads = [x for x in d["transport_diagnostics"] if x["op"] == "bulk_read"]
        self.assertEqual(len(bulk_reads), 1)
        self.assertEqual(bulk_reads[0]["requested_bytes"], 516)
        self.assertEqual(bulk_reads[0]["submitted_bytes"], 1024)
        self.assertEqual(bulk_reads[0]["returned_bytes"], 516)
        self.assertEqual(state["attaches"], 0)

    def test_new_service_candidate_is_not_stopped_or_claimed_authenticated(self) -> None:
        rc, d, state = self._run_case(service_before_stop=True)
        self.assertEqual(rc, 2)
        self.assertEqual(state["stops"], 0)
        self.assertTrue(d["unverified_service_candidate_after_failure"])
        self.assertTrue(d["failed_auth_stop_skipped_for_candidate"])
        self.assertEqual(state["attaches"], 0)

    def test_no_service_preserves_existing_stop_with_before_after_evidence(self) -> None:
        rc, d, state = self._run_case()
        self.assertEqual(rc, 1)
        self.assertEqual(state["stops"], 1)
        before = next(x for x in d["mode_observations"] if x["label"] == "authentication_failed_before_cleanup")
        after = next(x for x in d["mode_observations"] if x["label"] == "after_failed_auth_stop_0")
        self.assertEqual(before["devices"][0]["pid"], "0xbff")
        self.assertEqual(after["devices"][0]["pid"], "0xc33")
        self.assertEqual(state["attaches"], 0)

    def test_ptp_and_charger_modes_rejected_before_authentication(self) -> None:
        for pid in (PID_PTP, PID_CHARGER):
            with self.subTest(pid=hex(pid)):
                rc, d, state = self._run_case(initial_pid=pid)
                self.assertEqual(rc, 1)
                self.assertEqual(state["auth_calls"], 0)
                self.assertEqual(state["stops"], 0)
                self.assertFalse(d["upstream_start_routine_called"])

    def test_changed_ptp_or_missing_enumeration_receives_no_stale_cleanup(self) -> None:
        for status in ("ptp", "absent", "error"):
            with self.subTest(status=status):
                rc, _d, state = self._run_case(failed_state=status)
                self.assertEqual(rc, 1)
                self.assertEqual(state["stops"], 0)
                self.assertEqual(state["attaches"], 0)

    def test_driver_restored_only_when_same_msc_remains_after_stop(self) -> None:
        rc, d, state = self._run_case(keep_msc_after_stop=True)
        self.assertEqual(rc, 1)
        self.assertEqual(state["stops"], 1)
        self.assertEqual(state["attaches"], 1)
        self.assertTrue(d["host_driver_restored"])

    def test_model_override_and_allow_any_model(self) -> None:
        import os
        from unittest import mock

        device = _MockMscDevice(pid=PID_MSC, serial="SYNTHETIC_SN_02")
        device.product = "ILCE-7M3"
        usb_core_mod = SimpleNamespace(find=lambda **_k: [device])
        usb_util_mod = SimpleNamespace(
            dispose_resources=lambda _d: None,
            release_interface=lambda _d, _i: None,
        )

        # Default config rejects non-ILCE-7RM3 product string
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("A7R3_USB_MODEL", None)
            default_cfg = ServiceEntryConfig.from_env(serial="SYNTHETIC_SN_02", bus=1, ports=(2,), timeout_seconds=0)
        rc, report = run_msc_service_entry(
            default_cfg,
            enter=False,
            usb_core_mod=usb_core_mod,
            usb_util_mod=usb_util_mod,
            backend_getter=lambda: object(),
        )
        self.assertEqual(rc, 1)
        self.assertIn("Camera model mismatch", report["error"])

        # A7R3_USB_MODEL environment variable matches alternate model
        with mock.patch.dict(os.environ, {"A7R3_USB_MODEL": "ILCE-7M3"}):
            env_cfg = ServiceEntryConfig.from_env(serial="SYNTHETIC_SN_02", bus=1, ports=(2,), timeout_seconds=0)
        rc2, report2 = run_msc_service_entry(
            env_cfg,
            enter=False,
            usb_core_mod=usb_core_mod,
            usb_util_mod=usb_util_mod,
            backend_getter=lambda: object(),
        )
        self.assertEqual(rc2, 0)
        self.assertTrue(report2["identity_verified"])

        # allow_any_model skips USB product string lock while preserving MSC interface check
        any_cfg = ServiceEntryConfig.from_env(
            serial="SYNTHETIC_SN_02",
            allow_any_model=True,
            bus=1,
            ports=(2,),
            timeout_seconds=0,
        )
        self.assertEqual(any_cfg.expected_model, "")
        rc3, report3 = run_msc_service_entry(
            any_cfg,
            enter=False,
            usb_core_mod=usb_core_mod,
            usb_util_mod=usb_util_mod,
            backend_getter=lambda: object(),
        )
        self.assertEqual(rc3, 0)
        self.assertEqual(report3["usb"]["model"], "ILCE-7M3")


if __name__ == "__main__":
    unittest.main()
