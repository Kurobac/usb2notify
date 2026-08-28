from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import usb2notify  # noqa: E402


def make_bos(*capabilities: bytes, declared_count: int | None = None) -> bytes:
    total_length = 5 + sum(len(capability) for capability in capabilities)
    count = len(capabilities) if declared_count is None else declared_count
    header = bytes(
        (
            5,
            usb2notify.BOS_DESCRIPTOR_TYPE,
            total_length & 0xFF,
            total_length >> 8,
            count,
        )
    )
    return header + b"".join(capabilities)


def superspeed_capability(supported_speeds: int = 0x000E) -> bytes:
    return bytes(
        (
            10,
            usb2notify.DEVICE_CAPABILITY_DESCRIPTOR_TYPE,
            usb2notify.SUPERSPEED_USB_DEVICE_CAPABILITY,
            0,
            supported_speeds & 0xFF,
            supported_speeds >> 8,
            3,
            10,
            0xFF,
            0x07,
        )
    )


def usb2_extension_capability() -> bytes:
    return bytes(
        (
            7,
            usb2notify.DEVICE_CAPABILITY_DESCRIPTOR_TYPE,
            0x02,
            0x02,
            0,
            0,
            0,
        )
    )


class BOSParserTests(unittest.TestCase):
    def test_detects_superspeed_capability(self) -> None:
        bos = make_bos(superspeed_capability())
        self.assertTrue(usb2notify.parse_bos_superspeed_capability(bos))

    def test_superspeed_descriptor_without_gen1_bit_is_not_capable(self) -> None:
        bos = make_bos(superspeed_capability(supported_speeds=0x0006))
        self.assertFalse(usb2notify.parse_bos_superspeed_capability(bos))

    def test_finds_superspeed_after_another_capability(self) -> None:
        bos = make_bos(usb2_extension_capability(), superspeed_capability())
        self.assertTrue(usb2notify.parse_bos_superspeed_capability(bos))

    def test_bos_without_superspeed_capability_is_not_capable(self) -> None:
        bos = make_bos(usb2_extension_capability())
        self.assertFalse(usb2notify.parse_bos_superspeed_capability(bos))

    def test_rejects_short_bos(self) -> None:
        with self.assertRaisesRegex(usb2notify.BOSFormatError, "too short"):
            usb2notify.parse_bos_superspeed_capability(b"\x05\x0f")

    def test_rejects_wrong_bos_descriptor_type(self) -> None:
        bos = bytes((5, 0x10, 5, 0, 0))
        with self.assertRaisesRegex(usb2notify.BOSFormatError, "bDescriptorType"):
            usb2notify.parse_bos_superspeed_capability(bos)

    def test_rejects_total_length_mismatch(self) -> None:
        bos = bytearray(make_bos(usb2_extension_capability()))
        bos[2:4] = (99).to_bytes(2, byteorder="little")
        with self.assertRaisesRegex(usb2notify.BOSFormatError, "wTotalLength"):
            usb2notify.parse_bos_superspeed_capability(bytes(bos))

    def test_rejects_zero_length_capability(self) -> None:
        bos = bytes((5, 0x0F, 8, 0, 1, 0, 0x10, 0x02))
        with self.assertRaisesRegex(usb2notify.BOSFormatError, "bLength 0"):
            usb2notify.parse_bos_superspeed_capability(bos)

    def test_rejects_truncated_capability(self) -> None:
        bos = bytes((5, 0x0F, 9, 0, 1, 7, 0x10, 0x02, 0x02))
        with self.assertRaisesRegex(usb2notify.BOSFormatError, "extends past"):
            usb2notify.parse_bos_superspeed_capability(bos)

    def test_rejects_capability_count_mismatch(self) -> None:
        bos = make_bos(usb2_extension_capability(), declared_count=2)
        with self.assertRaisesRegex(usb2notify.BOSFormatError, "declares 2"):
            usb2notify.parse_bos_superspeed_capability(bos)

    def test_rejects_invalid_superspeed_capability_length(self) -> None:
        malformed = superspeed_capability()[:-1]
        malformed = bytes((len(malformed),)) + malformed[1:]
        bos = make_bos(malformed)
        with self.assertRaisesRegex(
            usb2notify.BOSFormatError, "SuperSpeed capability length"
        ):
            usb2notify.parse_bos_superspeed_capability(bos)


class UeventTests(unittest.TestCase):
    def test_parses_and_selects_usb_device_add(self) -> None:
        message = (
            b"add@/devices/pci0000:00/usb1/1-2\0"
            b"ACTION=add\0"
            b"DEVPATH=/devices/pci0000:00/usb1/1-2\0"
            b"SUBSYSTEM=usb\0"
            b"DEVTYPE=usb_device\0"
        )
        event = usb2notify.parse_uevent(message)
        self.assertTrue(usb2notify.is_usb_device_add(event))
        self.assertEqual(
            usb2notify.sysfs_path_from_event(event),
            Path("/sys/devices/pci0000:00/usb1/1-2"),
        )

    def test_ignores_usb_interface_add(self) -> None:
        event = {
            "ACTION": "add",
            "SUBSYSTEM": "usb",
            "DEVTYPE": "usb_interface",
        }
        self.assertFalse(usb2notify.is_usb_device_add(event))

    def test_rejects_missing_devpath(self) -> None:
        with self.assertRaisesRegex(usb2notify.DeviceInspectionError, "no DEVPATH"):
            usb2notify.sysfs_path_from_event({})

    def test_rejects_devpath_outside_devices(self) -> None:
        with self.assertRaisesRegex(
            usb2notify.DeviceInspectionError, "invalid kernel DEVPATH"
        ):
            usb2notify.sysfs_path_from_event({"DEVPATH": "/class/../etc"})


class SysfsFixture:
    def __init__(
        self,
        root: Path,
        *,
        device_class: str = "00",
        speed: str = "480",
        bos: bytes | None = None,
        product: str | None = "Test USB Device",
        manufacturer: str | None = "Test Vendor",
    ) -> None:
        self.path = root / "1-2"
        self.path.mkdir()
        (self.path / "bDeviceClass").write_text(
            f"{device_class}\n", encoding="ascii"
        )
        (self.path / "speed").write_text(f"{speed}\n", encoding="ascii")
        (self.path / "idVendor").write_text("1234\n", encoding="ascii")
        (self.path / "idProduct").write_text("5678\n", encoding="ascii")
        if bos is not None:
            (self.path / "bos_descriptors").write_bytes(bos)
        if product is not None:
            (self.path / "product").write_text(product, encoding="utf-8")
        if manufacturer is not None:
            (self.path / "manufacturer").write_text(
                manufacturer, encoding="utf-8"
            )


class DeviceInspectionTests(unittest.TestCase):
    def inspect_fixture(self, **kwargs: object) -> usb2notify.Inspection:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = SysfsFixture(Path(temporary_directory), **kwargs)
            return usb2notify.inspect_device(fixture.path)

    def test_usb3_device_at_high_speed_is_downgraded(self) -> None:
        inspection = self.inspect_fixture(
            speed="480", bos=make_bos(superspeed_capability())
        )
        self.assertEqual(inspection.outcome, usb2notify.Outcome.DOWNGRADED)
        self.assertTrue(inspection.downgraded)

    def test_usb3_device_at_full_speed_is_downgraded(self) -> None:
        inspection = self.inspect_fixture(
            speed="12", bos=make_bos(superspeed_capability())
        )
        self.assertEqual(inspection.outcome, usb2notify.Outcome.DOWNGRADED)

    def test_device_at_superspeed_is_not_downgraded(self) -> None:
        inspection = self.inspect_fixture(speed="5000")
        self.assertEqual(
            inspection.outcome, usb2notify.Outcome.OPERATING_ABOVE_USB2
        )
        self.assertIsNone(inspection.superspeed_capable)

    def test_device_at_superspeed_plus_is_not_downgraded(self) -> None:
        inspection = self.inspect_fixture(speed="10000")
        self.assertEqual(
            inspection.outcome, usb2notify.Outcome.OPERATING_ABOVE_USB2
        )

    def test_usb2_device_without_bos_is_not_downgraded(self) -> None:
        inspection = self.inspect_fixture(speed="480")
        self.assertEqual(
            inspection.outcome, usb2notify.Outcome.NOT_SUPERSPEED_CAPABLE
        )
        self.assertFalse(inspection.superspeed_capable)

    def test_bos_without_superspeed_is_not_downgraded(self) -> None:
        inspection = self.inspect_fixture(
            speed="480", bos=make_bos(usb2_extension_capability())
        )
        self.assertEqual(
            inspection.outcome, usb2notify.Outcome.NOT_SUPERSPEED_CAPABLE
        )

    def test_hub_is_ignored_before_speed_and_bos_checks(self) -> None:
        inspection = self.inspect_fixture(
            device_class="09", speed="unknown", bos=b"malformed"
        )
        self.assertEqual(inspection.outcome, usb2notify.Outcome.IGNORED_HUB)
        self.assertIsNone(inspection.speed_mbps)

    def test_malformed_bos_is_reported(self) -> None:
        with self.assertRaises(usb2notify.BOSFormatError):
            self.inspect_fixture(speed="480", bos=b"malformed")

    def test_unknown_speed_is_reported(self) -> None:
        with self.assertRaisesRegex(
            usb2notify.DeviceInspectionError, "unknown USB speed"
        ):
            self.inspect_fixture(speed="unknown")

    def test_missing_device_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing_path = Path(temporary_directory) / "disconnected"
            with self.assertRaisesRegex(
                usb2notify.DeviceInspectionError, "cannot read"
            ):
                usb2notify.inspect_device(missing_path)

    def test_device_disappearing_before_bos_read_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = SysfsFixture(Path(temporary_directory), speed="480")
            with mock.patch.object(Path, "exists", return_value=False):
                with self.assertRaisesRegex(
                    usb2notify.DeviceInspectionError, "disappeared"
                ):
                    usb2notify.inspect_device(fixture.path)

    def test_product_name_is_sanitized(self) -> None:
        inspection = self.inspect_fixture(
            speed="5000", product="Unsafe\nName\x00\u202e"
        )
        self.assertEqual(inspection.name, "Unsafe Name")

    def test_uses_manufacturer_and_hardware_id_without_product(self) -> None:
        inspection = self.inspect_fixture(
            speed="5000", product=None, manufacturer="Test Vendor"
        )
        self.assertEqual(inspection.name, "Test Vendor (1234:5678)")


class NotificationTests(unittest.TestCase):
    def downgraded_inspection(self) -> usb2notify.Inspection:
        return usb2notify.Inspection(
            sysfs_path=Path("/sys/devices/test"),
            name="Test Device",
            device_class=0,
            speed_mbps=usb2notify.Decimal("480"),
            superspeed_capable=True,
            outcome=usb2notify.Outcome.DOWNGRADED,
        )

    def test_lc_all_selects_chinese(self) -> None:
        language = usb2notify.select_notification_language(
            {
                "LC_ALL": "zh_CN.UTF-8",
                "LC_MESSAGES": "en_US.UTF-8",
                "LANG": "en_US.UTF-8",
            }
        )
        self.assertEqual(language, "zh")

    def test_lc_messages_is_used_when_lc_all_is_empty(self) -> None:
        language = usb2notify.select_notification_language(
            {
                "LC_ALL": "",
                "LC_MESSAGES": "zh-TW.UTF-8",
                "LANG": "en_US.UTF-8",
            }
        )
        self.assertEqual(language, "zh")

    def test_lang_is_used_when_more_specific_variables_are_unset(self) -> None:
        language = usb2notify.select_notification_language({"LANG": "zh_Hans"})
        self.assertEqual(language, "zh")

    def test_non_chinese_and_posix_locales_select_english(self) -> None:
        for locale_name in ("en_US.UTF-8", "de_DE.UTF-8", "C", "POSIX", ""):
            with self.subTest(locale_name=locale_name):
                language = usb2notify.select_notification_language(
                    {"LANG": locale_name}
                )
                self.assertEqual(language, "en")

    def test_builds_chinese_notification_without_advice_sentence(self) -> None:
        summary, body = usb2notify.build_notification_text(
            self.downgraded_inspection(), "zh"
        )
        self.assertEqual(summary, "USB 设备以较低速度连接")
        self.assertEqual(
            body,
            "Test Device 当前仅以 480 Mb/s 连接；"
            "该设备支持 SuperSpeed（至少 5 Gb/s）。",
        )

    def test_builds_english_notification(self) -> None:
        summary, body = usb2notify.build_notification_text(
            self.downgraded_inspection(), "en"
        )
        self.assertEqual(summary, "USB device connected at a lower speed")
        self.assertEqual(
            body,
            "Test Device is connected at only 480 Mb/s; "
            "this device supports SuperSpeed (at least 5 Gb/s).",
        )

    def test_rejects_an_unsupported_language(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported notification language"):
            usb2notify.build_notification_text(
                self.downgraded_inspection(), "ja"
            )

    @mock.patch("usb2notify.subprocess.run")
    def test_notification_is_passed_as_arguments_without_a_shell(
        self, run: mock.Mock
    ) -> None:
        inspection = self.downgraded_inspection()

        usb2notify.send_notification(
            inspection, "/usr/bin/notify-send", language="en"
        )

        command = run.call_args.args[0]
        self.assertIsInstance(command, tuple)
        self.assertEqual(command[0], "/usr/bin/notify-send")
        self.assertIn("--", command)
        self.assertIn("USB device connected at a lower speed", command)
        self.assertIn(
            "Test Device is connected at only 480 Mb/s; "
            "this device supports SuperSpeed (at least 5 Gb/s).",
            command,
        )
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertTrue(run.call_args.kwargs["check"])
        self.assertEqual(run.call_args.kwargs["timeout"], 5)


if __name__ == "__main__":
    unittest.main()
