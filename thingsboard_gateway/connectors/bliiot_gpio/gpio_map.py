#     Copyright 2026. ThingsBoard
#
#     Licensed under the Apache License, Version 2.0 (the "License");
#     you may not use this file except in compliance with the License.
#     You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#     Unless required by applicable law or agreed to in writing, software
#     distributed under the License is distributed on an "AS IS" BASIS,
#     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#     See the License for the specific language governing permissions and
#     limitations under the License.
"""
Hardware knowledge for the BLIIOT BL460AL-CM5002016-X26 IO board.

Everything in this module was derived empirically against real hardware during the
Raspberry Pi OS migration project (see the project's migration log) and is kept
separate from the connector/converter classes so it can be reused by out-of-gateway
tooling too (e.g. the standalone smoke-test script and the systemd boot-time safe-reset
script under bliiot/, neither of which should have to depend on the gateway package).

Key confirmed facts (do not change these without re-verifying on real hardware):

* The X26 DI/DO lines are plain BCM2712 (RP1) GPIO -- there is no I2C/SPI expander
  chip in the path. They are addressed through the standard Linux gpiochip character
  device (libgpiod v2 on both Bookworm and Trixie Raspberry Pi OS builds).
* The RP1 gpiochip's *index* is NOT stable across OS images/reflashes -- it enumerated
  as gpiochip15 on a Bookworm build and gpiochip0 on a Trixie build of the same board.
  Never hardcode an index; identify the chip by its driver label ("pinctrl-rp1")
  instead, which is what find_rp1_gpiochip() below does.
* The DO bank is SINK-type and its logic is inverted from the intuitive assumption:
  per the manufacturer manual, "Logic 0 corresponds to a closed state [energized/ON],
  and logic 1 corresponds to an open state [de-energized/OFF]." This module hides that
  inversion behind do_state_to_raw()/raw_to_do_state() so the rest of the connector
  can talk in plain "on"/"off" terms.
* DO outputs are "sticky": a GPIO chardev line keeps outputting whatever value was
  last written even after the requesting process exits -- it does not float or revert
  to a safe default on its own. The connector MUST explicitly drive every DO line to
  its safe (OFF/open) state on startup, which is why SAFE_DO_STATE and
  force_do_lines_safe() exist and are used both by the connector and by the
  independent systemd boot-time script in bliiot/systemd/.
* DI is ALSO inverted, the same way DO is -- confirmed 2026-09-03 on real hardware
  (Kelvin26001): with nothing wired to any of the 8 DI terminals (confirmed with the
  user), every channel read raw ACTIVE. The datasheet defines wet-contact DI logic as
  "Logic 0 = 0-3V DC, Logic 1 = 10-30V DC" -- i.e. idle/no-signal (0V, well inside the
  0-3V "Logic 0" band) is the manual's "Logic 0", not "Logic 1". Raw ACTIVE at idle
  therefore does NOT match the manual's logic directly, exactly like DO -- almost
  certainly the same opto-isolator circuit topology on this board (LED unlit at idle
  leaves the phototransistor open and a pull-up holds the RP1 input high; a real
  10-30V wet-contact signal turns the LED on, pulling the input low). Default
  active_low for DI is therefore True, same as DO, applied by the same
  do_state_to_raw()-style helpers below. **Fully confirmed 2026-09-03** via a direct
  dry-contact test on the box (X26 connector pin 1 = DI6 shorted to pin 11 = GND, the
  manual's documented dry-contact "closed" test): DI6 alone flipped from raw ACTIVE to
  raw INACTIVE while the other 7 channels stayed raw ACTIVE, matching this inversion
  exactly (see the migration log).
"""

from glob import glob
from time import monotonic

try:
    import gpiod
    from gpiod.line import Direction, Value
except ImportError:
    # The connector itself already handles installing gpiod via TBUtility.install_package
    # before importing this module; this bare import is kept here too so this module can
    # also be used stand-alone (e.g. from bliiot/systemd/bliiot-do-safe-reset.py) without
    # pulling in the gateway package at all.
    gpiod = None
    Direction = None
    Value = None

# --- X26 board pin mapping (BCM/RP1 line offsets, fixed by board wiring, confirmed
# working on both the Bookworm and Trixie builds of Raspberry Pi OS) ---------------

DEFAULT_DI_OFFSETS = {
    "DI1": 12,
    "DI2": 4,
    "DI3": 16,
    "DI4": 27,
    "DI5": 25,
    "DI6": 22,
    "DI7": 13,
    "DI8": 5,
}

DEFAULT_DO_OFFSETS = {
    "DO1": 24,
    "DO2": 23,
    "DO3": 7,
    "DO4": 3,
}

# BCM lines that must never be requested by this connector, whatever a config file says.
# GPIO2 = run LED, GPIO6/ID_SD = hardware watchdog, GPIO14/15 = debug console UART,
# GPIO17 = network status LED.
RESERVED_BCM_OFFSETS = {2, 6, 14, 15, 17}

# The RP1 GPIO controller's kernel driver label. Used to find the right /dev/gpiochipN
# regardless of which index it happens to enumerate as on a given boot/image.
RP1_CHIP_LABEL = "pinctrl-rp1"

# Safe/inactive logical state for every DO channel: de-energized / contact open.
SAFE_DO_STATE = False


class GpioMapError(Exception):
    """Raised for anything wrong with the board's GPIO configuration or chip discovery."""


def find_rp1_gpiochip(preferred=None, chip_label=RP1_CHIP_LABEL):
    """
    Resolve the RP1 GPIO chip's device path.

    :param preferred: explicit override from config ("auto" or falsy means "detect it");
                       anything else is treated as either a bare chip name (e.g.
                       "gpiochip0") or a full /dev path and is used as-is after existence
                       checking, so a known-good box can skip the scan entirely.
    :param chip_label: driver label to match against when scanning (default: the RP1
                        pinctrl driver's label, "pinctrl-rp1").
    :return: a "/dev/gpiochipN" path.
    :raises GpioMapError: if no matching chip is found.
    """
    if preferred and str(preferred).lower() != "auto":
        path = preferred if str(preferred).startswith("/dev/") else f"/dev/{preferred}"
        if gpiod is not None and not gpiod.is_gpiochip_device(path):
            raise GpioMapError(f"Configured gpiochip '{preferred}' ({path}) is not a valid gpiochip device")
        return path

    if gpiod is None:
        raise GpioMapError("gpiod is not importable; cannot auto-detect the RP1 gpiochip")

    candidates = sorted(glob("/dev/gpiochip*"))
    if not candidates:
        raise GpioMapError("No /dev/gpiochip* device nodes found on this system")

    for candidate in candidates:
        try:
            with gpiod.Chip(candidate) as chip:
                info = chip.get_info()
                label = getattr(info, "label", "") or ""
                name = getattr(info, "name", "") or ""
        except OSError:
            continue
        if chip_label.lower() in label.lower() or chip_label.lower() in name.lower():
            return candidate

    raise GpioMapError(
        f"No gpiochip with label containing '{chip_label}' found among: {candidates}. "
        "Run `gpiodetect` on the box and check thingsboard_gateway/config/bliiot_gpio.json's "
        "\"gpio\".\"chip\" setting -- the RP1 chip index is known to move across reflashes."
    )


def validate_offsets(di_offsets, do_offsets):
    """Raise GpioMapError if any configured channel collides with a reserved line or
    another configured channel."""
    all_offsets = {}
    for name, offset in {**di_offsets, **do_offsets}.items():
        if offset in RESERVED_BCM_OFFSETS:
            raise GpioMapError(f"Channel {name} is configured on BCM{offset}, which is reserved "
                                f"(run LED / watchdog / UART console / network LED)")
        if offset in all_offsets:
            raise GpioMapError(f"Channel {name} and {all_offsets[offset]} both configured on BCM{offset}")
        all_offsets[offset] = name


def do_state_to_raw(on: bool, active_low: bool = True):
    """Map a logical DO state (True = energized/closed/ON) to the raw line Value to
    write, honouring the board's confirmed sink-type inverted wiring by default."""
    if active_low:
        return Value.INACTIVE if on else Value.ACTIVE
    return Value.ACTIVE if on else Value.INACTIVE


def raw_to_do_state(value, active_low: bool = True) -> bool:
    """Inverse of do_state_to_raw()."""
    if active_low:
        return value == Value.INACTIVE
    return value == Value.ACTIVE


def raw_to_di_state(value, active_low: bool = True) -> bool:
    """Map a raw DI line Value to a logical state. Confirmed inverted on this board's wet
    contacts, same as DO -- see this module's docstring -- so active_low defaults to True.
    Still configurable per channel in case a dry-contact wiring or a different board
    variant ever differs."""
    if active_low:
        return value == Value.INACTIVE
    return value == Value.ACTIVE


def force_do_lines_safe(chip_path, do_offsets, consumer="bliiot-do-safe-reset", active_low_by_offset=None):
    """
    Standalone helper: open the given DO offsets on chip_path as outputs, immediately
    drive every one of them to the safe (OFF/open) state, and release the request.

    This is intentionally self-contained (no gateway imports) so it can be called both
    by the connector's own startup path and by the independent systemd boot-time script
    in bliiot/systemd/bliiot-do-safe-reset.py, which must keep working even if the
    gateway itself is broken or not yet running.

    :param active_low_by_offset: optional {offset: bool} map for per-channel polarity;
                                  defaults to active_low=True (the board's confirmed
                                  default) for every offset not listed.
    :return: dict of {name: offset} that were reset.
    """
    if gpiod is None:
        raise GpioMapError("gpiod is not importable")
    active_low_by_offset = active_low_by_offset or {}
    line_settings = {}
    for name, offset in do_offsets.items():
        active_low = active_low_by_offset.get(offset, True)
        line_settings[offset] = gpiod.LineSettings(
            direction=Direction.OUTPUT,
            output_value=do_state_to_raw(SAFE_DO_STATE, active_low=active_low),
        )
    request = gpiod.request_lines(chip_path, consumer=consumer, config=line_settings)
    try:
        # request_lines() already applied the safe output_value above; re-assert it
        # explicitly so this function's guarantee doesn't silently depend on that.
        request.set_values({offset: do_state_to_raw(SAFE_DO_STATE, active_low=active_low_by_offset.get(offset, True))
                             for offset in do_offsets.values()})
    finally:
        request.release()
    return dict(do_offsets)


def now_ms() -> int:
    return int(monotonic() * 1000)
