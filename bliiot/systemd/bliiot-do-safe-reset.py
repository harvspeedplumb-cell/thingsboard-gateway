#!/usr/bin/env python3
"""
Boot-time safety net for the BLIIOT BL460AL-CM5002016-X26 IO board's DO (relay/output)
channels.

Why this exists: the X26's DO lines are "sticky" -- a GPIO chardev line keeps outputting
whatever value was last written even after the process holding it exits, crashes, or is
killed. It does NOT float or revert to a safe default on its own (confirmed on real
hardware during the OS migration project). That means the window between the box
powering on and the ThingsBoard Gateway's BliiotGpioConnector actually starting (which
also forces every DO channel safe, but only once it gets that far) is a window where any
DO line last driven "ON" before a reboot/crash stays ON, energising whatever relay or
load is wired to it, with nothing to say so.

This script is intentionally NOT part of the thingsboard_gateway Python package and does
not import it: it needs to keep working even if the gateway's venv, dependencies, or
code are broken, mid-upgrade, or not yet started. It only depends on the `gpiod` module
(the same one the gateway connector uses) and the standard library. Install it as a
systemd oneshot service that runs at boot before the gateway service starts (see
bliiot-do-safe-reset.service in this same directory).

The DO offsets and the sink-type "logic 0 = ON, logic 1 = OFF" polarity below are
intentionally duplicated from thingsboard_gateway/connectors/bliiot_gpio/gpio_map.py
rather than imported from it, for that same independence reason. If the board's wiring
mapping ever changes, update BOTH copies.
"""
import sys
from glob import glob

try:
    import gpiod
    from gpiod.line import Direction, Value
except ImportError:
    print("ERROR: python3-gpiod (libgpiod v2 Python bindings) is not installed. "
          "Install it with: sudo apt install python3-libgpiod  (or: pip install gpiod)",
          file=sys.stderr)
    sys.exit(1)

DO_OFFSETS = {
    "DO1": 24,
    "DO2": 23,
    "DO3": 7,
    "DO4": 3,
}

RP1_CHIP_LABEL = "pinctrl-rp1"

# Safe state: de-energised / contact open. The board's DO bank is sink-type and
# inverted (raw line value 0 = ON/closed, 1 = OFF/open) -- see the module docstring.
SAFE_RAW_VALUE = Value.ACTIVE


def find_rp1_chip(chip_label=RP1_CHIP_LABEL):
    for candidate in sorted(glob("/dev/gpiochip*")):
        try:
            with gpiod.Chip(candidate) as chip:
                info = chip.get_info()
                label = (getattr(info, "label", "") or "")
                name = (getattr(info, "name", "") or "")
        except OSError:
            continue
        if chip_label.lower() in label.lower() or chip_label.lower() in name.lower():
            return candidate
    raise SystemExit(f"No gpiochip with label containing '{chip_label}' found. "
                      f"Run `gpiodetect` to check -- the RP1 chip index is known to move across reflashes.")


def main():
    chip_path = find_rp1_chip()
    line_settings = {offset: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=SAFE_RAW_VALUE)
                      for offset in DO_OFFSETS.values()}
    request = gpiod.request_lines(chip_path, consumer="bliiot-do-safe-reset", config=line_settings)
    try:
        request.set_values({offset: SAFE_RAW_VALUE for offset in DO_OFFSETS.values()})
    finally:
        request.release()
    print(f"bliiot-do-safe-reset: forced {list(DO_OFFSETS)} on {chip_path} to safe/OFF state.")


if __name__ == "__main__":
    main()
