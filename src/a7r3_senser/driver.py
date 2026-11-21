"""macOS / libusb USB kernel driver inspection, detach/restore helpers,
and read-only camera identification / metadata snapshot acquisition.
"""

from __future__ import annotations

import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import time
from typing import Any

from .session import ensure_pmca_importable
from .transport import (
    PID_MSC,
    PID_SENSER,
    PID_SENSER_ALT,
    SONY_VID,
    classify_usb_mode,
    read_auth_response,
)


def _get_libusb_backend() -> Any:
    import usb.backend.libusb1

    try:
        import libusb_package

        return usb.backend.libusb1.get_backend(find_library=libusb_package.find_library)
    except ImportError:
        return usb.backend.libusb1.get_backend()


def list_sony_usb_devices() -> list[dict[str, Any]]:
    """Enumerate attached Sony (`0x054c`) USB devices without claiming any interface."""
    import usb.core
    import usb.util

    backend = _get_libusb_backend()
    if backend is None:
        raise RuntimeError("No libusb1 backend available")

    rows: list[dict[str, Any]] = []
    for dev in usb.core.find(find_all=True, idVendor=SONY_VID, backend=backend):
        iface_class: int | None = None
        try:
            cfg = dev.get_active_configuration()
            if cfg is not None:
                iface_class = cfg[(0, 0)].bInterfaceClass
        except Exception:
            iface_class = None

        classification = classify_usb_mode(dev.idVendor, dev.idProduct, iface_class)
        item: dict[str, Any] = {
            "vendor_id": hex(dev.idVendor),
            "product_id": hex(dev.idProduct),
            "mode": classification.mode.value,
            "is_hazard_trap": classification.is_hazard_trap,
            "summary": classification.summary,
            "bus": dev.bus,
            "ports": list(dev.port_numbers or ()),
            "address": dev.address,
        }
        try:
            item["product"] = dev.product
            item["serial"] = dev.serial_number
            item["kernel_driver_active"] = dev.is_kernel_driver_active(0)
        except Exception as exc:
            item["descriptor_note"] = repr(exc)
        finally:
            usb.util.dispose_resources(dev)
        rows.append(item)
    return rows


def probe_msc_transport(*, expected_serial: str | None = None) -> dict[str, Any]:
    """Bounded host transport probe: detach MSC kernel driver, claim interface 0, and restore.

    Sends zero camera-specific commands and writes nothing to camera storage.
    """
    import usb.core
    import usb.util

    serial = expected_serial or os.environ.get("A7R3_USB_SERIAL")
    backend = _get_libusb_backend()
    devices = [
        d
        for d in usb.core.find(find_all=True, idVendor=SONY_VID, idProduct=PID_MSC, backend=backend)
        if (not serial or d.serial_number == serial)
    ]
    if len(devices) != 1:
        for d in devices:
            usb.util.dispose_resources(d)
        raise RuntimeError(f"Expected 1 matching Sony MSC device, found {len(devices)}")

    dev = devices[0]
    detached = False
    claimed = False
    result: dict[str, Any] = {
        "pid": hex(dev.idProduct),
        "bus": dev.bus,
        "ports": list(dev.port_numbers or ()),
        "detached_kernel_driver": False,
        "claimed_interface": False,
        "reattached_kernel_driver": False,
    }
    try:
        result["config_before"] = dev.get_active_configuration().bConfigurationValue
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
            detached = True
            result["detached_kernel_driver"] = True
        result["config_after"] = dev.get_active_configuration().bConfigurationValue
        usb.util.claim_interface(dev, 0)
        claimed = True
        result["claimed_interface"] = True
    finally:
        if claimed:
            usb.util.release_interface(dev, 0)
        if detached:
            try:
                dev.attach_kernel_driver(0)
                result["reattached_kernel_driver"] = True
            except Exception as exc:
                result["reattach_error"] = repr(exc)
        usb.util.dispose_resources(dev)
    return result


def restore_msc_kernel_driver(*, expected_serial: str | None = None) -> list[dict[str, Any]]:
    """Request macOS/host USB MSC kernel driver reattachment after an interrupted capture."""
    import usb.core
    import usb.util

    serial = expected_serial or os.environ.get("A7R3_USB_SERIAL")
    backend = _get_libusb_backend()
    results: list[dict[str, Any]] = []
    for device in usb.core.find(find_all=True, idVendor=SONY_VID, backend=backend):
        row: dict[str, Any] = {"pid": hex(device.idProduct), "bus": device.bus}
        try:
            if device.idProduct != PID_MSC:
                row["action"] = "skipped_non_msc"
                results.append(row)
                continue
            if serial and device.serial_number != serial:
                row["action"] = "skipped_serial_mismatch"
                results.append(row)
                continue
            row["kernel_driver_active_before"] = device.is_kernel_driver_active(0)
            device.attach_kernel_driver(0)
            row["action"] = "reattach_requested"
            row["kernel_driver_active_after"] = device.is_kernel_driver_active(0)
        except Exception as exc:
            row["error"] = repr(exc)
        finally:
            usb.util.dispose_resources(device)
        results.append(row)
    return results


def verify_msc_restoration(
    *,
    expected_serial: str | None = None,
    expected_model: str = "ILCE-7RM3",
    attempts: int = 12,
    interval_sec: float = 0.5,
) -> dict[str, Any]:
    """Poll read-only USB descriptors until normal MSC mode and host driver are active."""
    serial = expected_serial or os.environ.get("A7R3_USB_SERIAL")
    observed: list[dict[str, Any]] = []
    matches: list[dict[str, Any]] = []
    for _ in range(max(1, attempts)):
        observed = list_sony_usb_devices()
        matches = [
            x
            for x in observed
            if x.get("product_id") == hex(PID_MSC)
            and (not expected_model or x.get("product") == expected_model)
            and (not serial or x.get("serial") == serial)
            and x.get("kernel_driver_active") is True
        ]
        if len(matches) == 1:
            break
        time.sleep(interval_sec)

    return {
        "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "normal_mass_storage_mode_restored": len(matches) == 1,
        "devices": observed,
    }


def acquire_camera_metadata(
    output_dir: Path,
    *,
    expected_serial: str | None = None,
    expected_model: str = "ILCE-7RM3",
    pmca_root: Path | str | None = None,
) -> dict[str, Any]:
    """Read-only camera identification, settings backup verification, and `/proc` metadata snapshot."""
    ensure_pmca_importable(pmca_root)
    import usb.core
    import usb.util
    from pmca.backup import BackupFile
    from pmca.usb.driver.generic import GenericUsbDriver
    from pmca.usb.driver.generic.libusb import MscBbbDriver, UsbBackend
    from pmca.usb.sony import (
        SonyExtCmdCamera,
        SonyMscExtCmdDevice,
        SonySenserAuthDevice,
        SonySenserCamera,
        SonySenserDevice,
    )

    serial = expected_serial or os.environ.get("A7R3_USB_SERIAL")
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=False)

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report: dict[str, Any] = {
        "started_utc": stamp,
        "complete_firmware_backup": False,
        "restorability_verified": False,
        "files": [],
        "errors": [],
        "scope": "identification, settings backup, and storage metadata reads only",
    }

    backend = _get_libusb_backend()
    if backend is None:
        raise RuntimeError("No libusb1 backend available")

    class CapturedBackend(UsbBackend):
        def __init__(self, dev_obj: Any, packet_sizes: dict[int, int] | None = None) -> None:
            super().__init__(dev_obj)
            self.detached = False
            self.packet_sizes = packet_sizes or {}
            self.auth_phase = False

        def reset(self) -> None:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
                self.detached = True

        def read(self, ep: int, length: int, timeout: int | None = None) -> bytes:
            if self.auth_phase and length == 516 and ep in self.packet_sizes:
                return read_auth_response(super().read, ep, self.packet_sizes[ep], timeout)
            return super().read(ep, length, timeout)

        def restore(self) -> None:
            usb.util.release_interface(self.dev, 0)
            if self.detached:
                self.dev.attach_kernel_driver(0)
                self.detached = False
            usb.util.dispose_resources(self.dev)

    def publish(name: str, data: bytes, source: str) -> None:
        path = output_dir / name
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        digest = hashlib.sha256(data).hexdigest()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
        report["files"].append(
            {"path": name, "source": source, "bytes": len(data), "sha256": digest}
        )

    devices = list(usb.core.find(find_all=True, idVendor=SONY_VID, backend=backend))
    if len(devices) != 1:
        for d in devices:
            usb.util.dispose_resources(d)
        raise RuntimeError(f"Expected exactly one Sony USB device, found {len(devices)}")

    device = devices[0]
    if (expected_model and device.product != expected_model) or (
        serial and device.serial_number != serial
    ):
        usb.util.dispose_resources(device)
        raise RuntimeError("Target camera model or serial mismatch")

    interface = device.get_active_configuration()[(0, 0)]
    if interface.bInterfaceClass != 8:
        usb.util.dispose_resources(device)
        raise RuntimeError("Select Mass Storage mode on the camera first")

    packet_sizes = {ep.bEndpointAddress: ep.wMaxPacketSize for ep in interface}
    original_port = (device.bus, tuple(device.port_numbers or ()))
    if not original_port[1]:
        usb.util.dispose_resources(device)
        raise RuntimeError("Physical USB port path unavailable")

    capture = CapturedBackend(device, packet_sizes)
    service_capture = None
    auth = None
    service_auth = None
    try:
        initial_driver = MscBbbDriver(capture)
        mass_storage = SonyMscExtCmdDevice(initial_driver)
        inquiry = mass_storage.getDeviceInfo()._asdict()
        info = SonyExtCmdCamera(mass_storage).getCameraInfo()._asdict()
        plist = info.pop("plist")
        report["inquiry"] = inquiry
        report["camera_info"] = info
        publish("camera-info.plist", plist, "SonyExtCmdCamera.getCameraInfo")

        auth = SonySenserAuthDevice(initial_driver)
        auth.start()
        capture.auth_phase = True
        auth.authenticate()
        capture.auth_phase = False
        report["initial_service_authentication"] = True

        service_device = None
        for _ in range(20):
            time.sleep(0.5)
            candidates = [
                d
                for d in usb.core.find(find_all=True, idVendor=SONY_VID, backend=backend)
                if d.idProduct in (PID_SENSER, PID_SENSER_ALT)
                and (d.bus, tuple(d.port_numbers or ())) == original_port
            ]
            if len(candidates) == 1:
                service_device = candidates[0]
                break
        if service_device is None:
            raise RuntimeError("Camera did not enumerate in SENSER service mode")

        service_capture = CapturedBackend(service_device)
        svc_driver = GenericUsbDriver(service_capture)
        service = SonySenserDevice(svc_driver)
        service_auth = SonySenserAuthDevice(svc_driver)
        service_auth.start()
        service_auth.authenticate()
        camera = SonySenserCamera(service)
        report["service_authentication"] = True

        backups = []
        for idx in (1, 2):
            data = camera.getBackupPresetData(True)
            publish(
                f"settings-backup-read{idx}.bin",
                data,
                "SonySenserCamera.getBackupPresetData(fromMemory=True)",
            )
            backups.append(data)
            parsed = BackupFile(io.BytesIO(data))
            report[f"settings_backup_read{idx}_validation"] = {
                "checksum_valid": True,
                "declared_bytes": parsed.size,
                "received_bytes": len(data),
                "region": parsed.getRegion(),
                "revision": parsed.revision,
                "exact_length_matches": len(data) == parsed.size,
            }
        report["settings_backup_repeat_reads_match"] = backups[0] == backups[1]

        for remote, local_name in [
            ("/setting/updater/dat4", "firmware-version.bin"),
            ("/proc/mtd", "proc-mtd.txt"),
            ("/proc/partitions", "proc-partitions.txt"),
            ("/proc/mounts", "proc-mounts.txt"),
            ("/proc/version", "proc-version.txt"),
            ("/proc/cmdline", "proc-cmdline.txt"),
        ]:
            try:
                publish(local_name, camera.readFile(remote), remote)
            except Exception as exc:
                report["errors"].append({"read": remote, "error": repr(exc)})
    finally:
        if auth is not None:
            try:
                (service_auth or auth).stop()
            except Exception as exc:
                report["service_exit_error"] = repr(exc)
        for cap in (service_capture, capture):
            if cap is not None:
                try:
                    cap.restore()
                except Exception as exc:
                    report["errors"].append({"cleanup": "host_driver_restore", "error": repr(exc)})
                    usb.util.dispose_resources(cap.dev)
        (output_dir / "manifest.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n"
        )
    return report
