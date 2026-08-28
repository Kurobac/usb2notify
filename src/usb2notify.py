#!/usr/bin/env python3
"""Notify when a SuperSpeed-capable USB device connects at USB 2 speed."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import socket
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence


LOGGER = logging.getLogger("usb2notify")

# Linux UAPI: include/uapi/linux/netlink.h
NETLINK_KOBJECT_UEVENT = 15
KOBJECT_UEVENT_MULTICAST_GROUP = 1
UEVENT_BUFFER_SIZE = 64 * 1024

USB_HUB_CLASS = 0x09
USB2_MAX_SPEED_MBPS = Decimal("480")

BOS_DESCRIPTOR_TYPE = 0x0F
DEVICE_CAPABILITY_DESCRIPTOR_TYPE = 0x10
SUPERSPEED_USB_DEVICE_CAPABILITY = 0x03
SUPERSPEED_SUPPORTED_BIT = 1 << 3

SUPPORTED_NOTIFICATION_LANGUAGES = frozenset(("en", "zh"))


class BOSFormatError(ValueError):
    """The cached BOS does not contain a structurally valid descriptor set."""


class DeviceInspectionError(RuntimeError):
    """A USB device cannot be inspected through its sysfs directory."""


class Outcome(Enum):
    DOWNGRADED = "downgraded"
    OPERATING_ABOVE_USB2 = "operating-above-usb2"
    NOT_SUPERSPEED_CAPABLE = "not-superspeed-capable"
    IGNORED_HUB = "ignored-hub"


@dataclass(frozen=True)
class Inspection:
    sysfs_path: Path
    name: str
    device_class: int
    speed_mbps: Decimal | None
    superspeed_capable: bool | None
    outcome: Outcome

    @property
    def downgraded(self) -> bool:
        return self.outcome is Outcome.DOWNGRADED


def parse_uevent(message: bytes) -> dict[str, str]:
    """Parse a NUL-delimited kobject uevent datagram."""

    event: dict[str, str] = {}
    for field in message.split(b"\0"):
        key, separator, value = field.partition(b"=")
        if not separator:
            continue
        event[key.decode("ascii", errors="replace")] = value.decode(
            "utf-8", errors="replace"
        )
    return event


def is_usb_device_add(event: Mapping[str, str]) -> bool:
    return (
        event.get("ACTION") == "add"
        and event.get("SUBSYSTEM") == "usb"
        and event.get("DEVTYPE") == "usb_device"
    )


def sysfs_path_from_event(event: Mapping[str, str]) -> Path:
    """Convert a kernel DEVPATH into its corresponding path below /sys."""

    devpath = event.get("DEVPATH")
    if not devpath:
        raise DeviceInspectionError("USB add event has no DEVPATH")

    pure_path = PurePosixPath(devpath)
    if not devpath.startswith("/devices/") or ".." in pure_path.parts:
        raise DeviceInspectionError(f"invalid kernel DEVPATH: {devpath!r}")

    return Path("/sys").joinpath(*pure_path.parts[1:])


def parse_bos_superspeed_capability(bos: bytes) -> bool:
    """Return whether a structurally valid BOS advertises SuperSpeed support."""

    if len(bos) < 5:
        raise BOSFormatError(f"BOS is too short: {len(bos)} bytes")

    bos_length = bos[0]
    if bos_length != 5:
        raise BOSFormatError(f"invalid BOS bLength: {bos_length}, expected 5")
    if bos[1] != BOS_DESCRIPTOR_TYPE:
        raise BOSFormatError(
            f"invalid BOS bDescriptorType: 0x{bos[1]:02x}, expected 0x0f"
        )

    total_length = int.from_bytes(bos[2:4], byteorder="little")
    expected_capability_count = bos[4]
    if total_length != len(bos):
        raise BOSFormatError(
            f"BOS wTotalLength is {total_length}, but sysfs returned {len(bos)} bytes"
        )

    offset = bos_length
    capability_count = 0
    superspeed_capable = False

    while offset < total_length:
        remaining = total_length - offset
        if remaining < 3:
            raise BOSFormatError(
                f"truncated device capability header at offset {offset}"
            )

        capability_length = bos[offset]
        if capability_length < 3:
            raise BOSFormatError(
                f"invalid capability bLength {capability_length} at offset {offset}"
            )

        capability_end = offset + capability_length
        if capability_end > total_length:
            raise BOSFormatError(
                f"capability at offset {offset} extends past BOS wTotalLength"
            )

        descriptor_type = bos[offset + 1]
        if descriptor_type != DEVICE_CAPABILITY_DESCRIPTOR_TYPE:
            raise BOSFormatError(
                "invalid descriptor type "
                f"0x{descriptor_type:02x} at BOS capability offset {offset}"
            )

        capability_type = bos[offset + 2]
        if capability_type == SUPERSPEED_USB_DEVICE_CAPABILITY:
            if capability_length != 10:
                raise BOSFormatError(
                    "invalid SuperSpeed capability length "
                    f"{capability_length}, expected 10"
                )
            supported_speeds = int.from_bytes(
                bos[offset + 4 : offset + 6], byteorder="little"
            )
            if supported_speeds & SUPERSPEED_SUPPORTED_BIT:
                superspeed_capable = True

        capability_count += 1
        offset = capability_end

    if capability_count != expected_capability_count:
        raise BOSFormatError(
            "BOS declares "
            f"{expected_capability_count} capabilities, parsed {capability_count}"
        )

    return superspeed_capable


def _read_required_text(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as error:
        raise DeviceInspectionError(f"cannot read {path}: {error.strerror}") from error
    if not value:
        raise DeviceInspectionError(f"empty sysfs attribute: {path}")
    return value


def _read_optional_text(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8", errors="replace").strip()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise DeviceInspectionError(f"cannot read {path}: {error.strerror}") from error
    return value or None


def _parse_hex_attribute(path: Path) -> int:
    value = _read_required_text(path)
    try:
        return int(value, 16)
    except ValueError as error:
        raise DeviceInspectionError(
            f"invalid hexadecimal value in {path}: {value!r}"
        ) from error


def _parse_speed(path: Path) -> Decimal:
    value = _read_required_text(path)
    if value == "unknown":
        raise DeviceInspectionError(f"kernel reports unknown USB speed in {path}")
    try:
        speed = Decimal(value)
    except InvalidOperation as error:
        raise DeviceInspectionError(
            f"invalid USB speed value in {path}: {value!r}"
        ) from error
    if not speed.is_finite() or speed <= 0:
        raise DeviceInspectionError(
            f"invalid USB speed value in {path}: {value!r}"
        )
    return speed


def sanitize_device_text(value: str, max_length: int = 120) -> str:
    """Remove control characters and bound untrusted USB string descriptors."""

    without_controls = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in value
    )
    normalized = " ".join(without_controls.split())
    return normalized[:max_length]


def _device_name(sysfs_path: Path) -> str:
    product = _read_optional_text(sysfs_path / "product")
    manufacturer = _read_optional_text(sysfs_path / "manufacturer")
    vendor_id = _read_optional_text(sysfs_path / "idVendor") or "????"
    product_id = _read_optional_text(sysfs_path / "idProduct") or "????"
    hardware_id = f"{vendor_id}:{product_id}"

    if product:
        sanitized_product = sanitize_device_text(product)
        if sanitized_product:
            return sanitized_product
    if manufacturer:
        sanitized_manufacturer = sanitize_device_text(manufacturer)
        if sanitized_manufacturer:
            return f"{sanitized_manufacturer} ({hardware_id})"
    return hardware_id


def inspect_device(sysfs_path: Path) -> Inspection:
    """Inspect one USB device sysfs directory without performing USB I/O."""

    device_class = _parse_hex_attribute(sysfs_path / "bDeviceClass")
    name = _device_name(sysfs_path)

    if device_class == USB_HUB_CLASS:
        return Inspection(
            sysfs_path=sysfs_path,
            name=name,
            device_class=device_class,
            speed_mbps=None,
            superspeed_capable=None,
            outcome=Outcome.IGNORED_HUB,
        )

    speed = _parse_speed(sysfs_path / "speed")
    if speed > USB2_MAX_SPEED_MBPS:
        return Inspection(
            sysfs_path=sysfs_path,
            name=name,
            device_class=device_class,
            speed_mbps=speed,
            superspeed_capable=None,
            outcome=Outcome.OPERATING_ABOVE_USB2,
        )

    bos_path = sysfs_path / "bos_descriptors"
    try:
        bos = bos_path.read_bytes()
    except FileNotFoundError:
        if not sysfs_path.exists():
            raise DeviceInspectionError(
                f"USB device disappeared during inspection: {sysfs_path}"
            )
        superspeed_capable = False
    except OSError as error:
        raise DeviceInspectionError(
            f"cannot read {bos_path}: {error.strerror}"
        ) from error
    else:
        superspeed_capable = parse_bos_superspeed_capability(bos)

    return Inspection(
        sysfs_path=sysfs_path,
        name=name,
        device_class=device_class,
        speed_mbps=speed,
        superspeed_capable=superspeed_capable,
        outcome=(
            Outcome.DOWNGRADED
            if superspeed_capable
            else Outcome.NOT_SUPERSPEED_CAPABLE
        ),
    )


def format_speed(speed_mbps: Decimal) -> str:
    value = format(speed_mbps, "f")
    if "." in value:
        value = value.rstrip("0").rstrip(".")
    return value


def format_inspection(inspection: Inspection) -> str:
    speed = (
        f"{format_speed(inspection.speed_mbps)} Mb/s"
        if inspection.speed_mbps is not None
        else "not checked"
    )
    superspeed = {
        True: "yes",
        False: "no",
        None: "not checked",
    }[inspection.superspeed_capable]
    return "\n".join(
        (
            f"device: {inspection.name}",
            f"path: {inspection.sysfs_path}",
            f"class: {inspection.device_class:02x}",
            f"speed: {speed}",
            f"super-speed-capable: {superspeed}",
            f"result: {inspection.outcome.value}",
        )
    )


def select_notification_language(
    environment: Mapping[str, str] | None = None,
) -> str:
    """Select a minimal notification language from the POSIX locale."""

    if environment is None:
        environment = os.environ

    locale_name = ""
    for variable in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = environment.get(variable, "").strip()
        if value:
            locale_name = value
            break

    normalized = locale_name.split(".", 1)[0].split("@", 1)[0]
    normalized = normalized.replace("-", "_").lower()
    if normalized == "zh" or normalized.startswith("zh_"):
        return "zh"
    return "en"


def build_notification_text(
    inspection: Inspection, language: str
) -> tuple[str, str]:
    if not inspection.downgraded or inspection.speed_mbps is None:
        raise ValueError("notification requested for a device that is not downgraded")
    if language not in SUPPORTED_NOTIFICATION_LANGUAGES:
        raise ValueError(f"unsupported notification language: {language!r}")

    speed = format_speed(inspection.speed_mbps)
    if language == "zh":
        return (
            "USB 设备以较低速度连接",
            f"{inspection.name} 当前仅以 {speed} Mb/s 连接；"
            "该设备支持 SuperSpeed（至少 5 Gb/s）。",
        )
    return (
        "USB device connected at a lower speed",
        f"{inspection.name} is connected at only {speed} Mb/s; "
        "this device supports SuperSpeed (at least 5 Gb/s).",
    )


def send_notification(
    inspection: Inspection, notify_send: str, language: str | None = None
) -> None:
    if not inspection.downgraded or inspection.speed_mbps is None:
        raise ValueError("notification requested for a device that is not downgraded")

    resolved_language = (
        select_notification_language() if language is None else language
    )
    summary, body = build_notification_text(inspection, resolved_language)
    subprocess.run(
        (
            notify_send,
            "--app-name=usb2notify",
            "--urgency=normal",
            "--expire-time=10000",
            "--icon=dialog-warning",
            "--",
            summary,
            body,
        ),
        check=True,
        timeout=5,
    )


def monitor(notify_send: str) -> int:
    with socket.socket(
        socket.AF_NETLINK, socket.SOCK_DGRAM, NETLINK_KOBJECT_UEVENT
    ) as uevent_socket:
        uevent_socket.bind((0, KOBJECT_UEVENT_MULTICAST_GROUP))
        LOGGER.info("listening for USB device add events")

        while True:
            message = uevent_socket.recv(UEVENT_BUFFER_SIZE)
            event = parse_uevent(message)
            if not is_usb_device_add(event):
                continue

            try:
                sysfs_path = sysfs_path_from_event(event)
                inspection = inspect_device(sysfs_path)
            except (BOSFormatError, DeviceInspectionError) as error:
                LOGGER.warning("cannot inspect USB device: %s", error)
                continue

            if not inspection.downgraded:
                LOGGER.debug(
                    "ignored %s: %s", inspection.name, inspection.outcome.value
                )
                continue

            try:
                send_notification(inspection, notify_send)
            except subprocess.TimeoutExpired:
                LOGGER.error("notify-send timed out for %s", inspection.name)
            except subprocess.CalledProcessError as error:
                LOGGER.error(
                    "notify-send failed for %s with exit status %d",
                    inspection.name,
                    error.returncode,
                )
            else:
                assert inspection.speed_mbps is not None
                LOGGER.info(
                    "notified for %s at %s Mb/s",
                    inspection.name,
                    format_speed(inspection.speed_mbps),
                )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Notify when a SuperSpeed-capable USB device connects at USB 2 speed."
        )
    )
    parser.add_argument(
        "--check",
        metavar="SYSFS_PATH",
        type=Path,
        help="inspect one USB device sysfs directory without sending a notification",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="include ignored devices in the log",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_argument_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if arguments.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if arguments.check is not None:
        try:
            inspection = inspect_device(arguments.check)
        except (BOSFormatError, DeviceInspectionError) as error:
            LOGGER.error("%s", error)
            return 2
        print(format_inspection(inspection))
        return 0

    notify_send = shutil.which("notify-send")
    if notify_send is None:
        LOGGER.error("notify-send was not found in PATH")
        return 1

    try:
        return monitor(notify_send)
    except KeyboardInterrupt:
        return 0
    except OSError as error:
        LOGGER.error("USB event monitor failed: %s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
