"""Command-line interface for `a7r3-usb-senser`."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .driver import (
    acquire_camera_metadata,
    list_sony_usb_devices,
    probe_msc_transport,
    restore_msc_kernel_driver,
    verify_msc_restoration,
)
from .transport import ServiceEntryConfig, run_msc_service_entry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="a7r3-senser",
        description="Sony ILCE-7RM3 USB mode classifier, SuperSpeed SENSER transport, and macOS driver helper",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List attached Sony USB devices and classify operational/hazard modes")

    p_probe = sub.add_parser("probe", help="Bounded MSC kernel-driver detach/claim/reattach diagnostic")
    p_probe.add_argument("--serial", help="Expected USB serial number (or set A7R3_USB_SERIAL)")

    p_enter = sub.add_parser("enter", help="Verify MSC identity and enter temporary SENSER service mode")
    p_enter.add_argument("--serial", help="Expected USB serial number (or set A7R3_USB_SERIAL)")
    p_enter.add_argument("--model", default="ILCE-7RM3", help="Expected USB product string")
    p_enter.add_argument("--dry-run", action="store_true", help="Verify descriptors only without detaching/entering")
    p_enter.add_argument("--report", type=Path, help="Optional JSON report path")

    p_restore = sub.add_parser("restore", help="Re-attach macOS/host USB MSC kernel driver")
    p_restore.add_argument("--serial", help="Expected USB serial number (or set A7R3_USB_SERIAL)")

    p_verify = sub.add_parser("verify-restore", help="Poll descriptors until normal MSC + kernel driver active")
    p_verify.add_argument("--serial", help="Expected USB serial number (or set A7R3_USB_SERIAL)")
    p_verify.add_argument("--model", default="ILCE-7RM3", help="Expected USB product string")
    p_verify.add_argument("--attempts", type=int, default=12, help="Polling attempts (default: 12)")

    p_acq = sub.add_parser("acquire-info", help="Read-only camera info, settings backup, and /proc metadata")
    p_acq.add_argument("--output", type=Path, required=True, help="New output directory for evidence")
    p_acq.add_argument("--serial", help="Expected USB serial number (or set A7R3_USB_SERIAL)")
    p_acq.add_argument("--model", default="ILCE-7RM3", help="Expected USB product string")
    p_acq.add_argument("--pmca-root", type=Path, help="Path to external Sony-PMCA-RE checkout")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "list":
        print(json.dumps(list_sony_usb_devices(), indent=2, ensure_ascii=False))
        return 0
    if args.command == "probe":
        print(json.dumps(probe_msc_transport(expected_serial=args.serial), indent=2, ensure_ascii=False))
        return 0
    if args.command == "enter":
        cfg = ServiceEntryConfig.from_env(serial=args.serial, model=args.model)
        rc, report = run_msc_service_entry(cfg, enter=not args.dry_run, report_path=args.report)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return rc
    if args.command == "restore":
        print(json.dumps(restore_msc_kernel_driver(expected_serial=args.serial), indent=2, ensure_ascii=False))
        return 0
    if args.command == "verify-restore":
        res = verify_msc_restoration(
            expected_serial=args.serial,
            expected_model=args.model,
            attempts=args.attempts,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res["normal_mass_storage_mode_restored"] else 1
    if args.command == "acquire-info":
        res = acquire_camera_metadata(
            args.output,
            expected_serial=args.serial,
            expected_model=args.model,
            pmca_root=args.pmca_root,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0
    return 64


if __name__ == "__main__":
    sys.exit(main())
