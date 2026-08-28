# usb2notify

`usb2notify` is a small Linux user-session daemon that shows a desktop
notification when a SuperSpeed-capable USB device connects at USB 2.0 speed or
lower.

It compares the device's current speed reported by
the Linux kernel with the SuperSpeed capability advertised in its Binary Device
Object Store (BOS).

```text
current speed <= 480 Mb/s
and the SuperSpeed bit in wSpeedsSupported is set
=> the device has fallen back to USB 2.0
```

## How it works

1. Listen for Linux kobject uevents over `NETLINK_KOBJECT_UEVENT`.
2. Select `ACTION=add`, `SUBSYSTEM=usb`, `DEVTYPE=usb_device` events.
3. Read the device class and current speed from sysfs.
4. Ignore USB hubs and devices already running above 480 Mb/s.
5. Parse `/sys/.../bos_descriptors` and find the SuperSpeed USB Device
   Capability descriptor.
6. Call `notify-send` when the current link is USB 2.0 or lower while the BOS
   advertises SuperSpeed support.

The BOS is cached by the kernel during enumeration, so reading it from sysfs
does not communicate with the device again.

## Requirements

- Linux 6.9 or newer
- Python 3.10 or newer
- `notify-send`, normally provided by libnotify
- systemd user services, only if automatic startup is desired

## Scope and limitations

The implementation deliberately targets the simple, standards-compliant case:

- Only newly added USB devices are inspected.
- Only fallback from USB 3.x to USB 2.0 or lower is reported.
- A 20 or 10 Gb/s device running at 5 Gb/s is not reported.
- USB hubs, whose device class is `09h`, are ignored. A USB 3 hub normally
  exposes separate USB 2 and SuperSpeed logical hubs, so treating its USB 2
  half as a downgraded device would produce false positives.
- Devices with missing, malformed, or incorrect BOS data are not guessed from
  their name or VID/PID.
- No device quirks, capability database, or connection history are maintained.
- There is no alternate BOS retrieval path for Linux 6.8 and older.

Individual sysfs read errors and malformed BOS data are logged without stopping
the event listener.

## Installation

### User-local installation

Install the executable and systemd user unit into the current user's home
directory, then enable the service:

```bash
make install
make enable
```

The installed files are:

```text
~/.local/bin/usb2notify
~/.local/share/systemd/user/usb2notify.service
```

### Arch Linux package

The AUR package installs the executable and user unit system-wide:

```text
/usr/bin/usb2notify
/usr/lib/systemd/user/usb2notify.service
```

Packages do not enable the service automatically. Enable it for the current
user after installation:

```bash
systemctl --user enable --now usb2notify.service
```

Check the service and follow its log with:

```bash
systemctl --user status usb2notify.service
journalctl --user -u usb2notify.service -f
```

Stop and disable the service:

```bash
make disable
```

Remove the installed files:

```bash
make uninstall
```

## Inspecting a device manually

The `--check` mode evaluates one USB device without sending a notification:

```bash
usb2notify --check /sys/bus/usb/devices/1-2
```

Example output:

```text
device: Samsung Portable SSD T7
path: /sys/bus/usb/devices/1-2
class: 00
speed: 480 Mb/s
super-speed-capable: yes
result: downgraded
```

To list the current USB device paths and speeds first:

```bash
for device in /sys/bus/usb/devices/*; do
    test -f "$device/speed" || continue
    printf '%s: %s Mb/s\n' "$device" "$(<"$device/speed")"
done
```

## License

This project is licensed under the [MIT License](LICENSE).
