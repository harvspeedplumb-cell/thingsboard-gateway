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
Hardware knowledge for the BLIIOT ARMxy BL460 X-series IO boards.

Everything in this module was derived empirically against real hardware during the
Raspberry Pi OS migration project (see the project's migration log) and is kept
separate from the connector/converter classes so it can be reused by out-of-gateway
tooling too (e.g. the standalone smoke-test script and the systemd boot-time safe-reset
script under hva_kelvin_v1/, neither of which should have to depend on the gateway package).

Key confirmed facts (do not change these without re-verifying on real hardware):

* The X-series DI/DO lines are plain BCM2712 (RP1) GPIO -- there is no I2C/SPI expander
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
  independent systemd boot-time script in hva_kelvin_v1/systemd/.
* DI is ALSO inverted, the same way DO is -- confirmed 2026-09-03 on real hardware
  (Kelvin26001, an X26 board): with nothing wired to any of the 8 DI terminals
  (confirmed with the user), every channel read raw ACTIVE. The datasheet defines
  wet-contact DI logic as "Logic 0 = 0-3V DC, Logic 1 = 10-30V DC" -- i.e. idle/no-signal
  (0V, well inside the 0-3V "Logic 0" band) is the manual's "Logic 0", not "Logic 1".
  Raw ACTIVE at idle therefore does NOT match the manual's logic directly, exactly like
  DO -- almost certainly the same opto-isolator circuit topology on this board (LED
  unlit at idle leaves the phototransistor open and a pull-up holds the RP1 input high;
  a real 10-30V wet-contact signal turns the LED on, pulling the input low). Default
  active_low for DI is therefore True, same as DO, applied by the same
  do_state_to_raw()-style helpers below. **Fully confirmed 2026-09-03** via a direct
  dry-contact test on the box (X26 connector pin 1 = DI6 shorted to pin 11 = GND, the
  manual's documented dry-contact "closed" test): DI6 alone flipped from raw ACTIVE to
  raw INACTIVE while the other 7 channels stayed raw ACTIVE, matching this inversion
  exactly (see the migration log). The manual states the "Logic 0 = closed / Logic 1 =
  open" convention generically for "the X board", not X26-specifically, so the same
  active_low=True default is applied to every board type below -- but this has only
  been physically verified on X26. Treat it as a reasonable starting assumption on any
  other board type, not a hardware-confirmed fact, until it's actually smoke-tested
  there the same way.

Multi-board support ("boardType"): the BLIIOT BL460 carrier accepts a family of X-series
IO daughterboards (see the datasheet's "X Series I/O Board Model List"), each exposing a
different subset of DI/DO/serial channels on a 6, 10, or 20-pin connector. CAN-equipped
variants (X11/X12/X21/X22/X24/X25/X27/X29) are explicitly listed by the manual as "Not
support[ed]" on the BL460 series, so they're out of scope entirely -- there's no CAN GPIO
to reserve or plan around on this carrier board. BOARD_PIN_MAPS below covers the 9
supported boards that carry DI/DO channels this connector can address (X10 and X20 are
RS485/RS232-only -- no DI/DO at all, handled by the gateway's Modbus connector instead;
X16 exposes 4 raw GPIO lines with no DI/DO polarity/logic convention, not modelled here).

**Derivation method for boards other than X26 (X13/X14/X15/X16/X23/X28): manual-derived,
NOT independently hardware-confirmed.** The manual's appendix ("9. 40-Pin Pin Multiplexing
Description", user manual p.51) gives two connector-size-wide physical-pin-to-BCM tables
-- one for the 6-pin connector shared by X13/X14/X15/X16, one for the 20-pin connector
shared by X23/X26/X28 -- independent of which board is fitted. Cross-referencing each
board's own port-name table (manual section 2.2.1) against the matching connector-size
table gives every other board's DI/DO-to-BCM mapping without needing to guess. This
method was validated by reproducing X26's already hardware-confirmed 12-channel mapping
exactly, position for position, from the 20-pin table alone -- strong evidence the method
is sound, but it is still a paper derivation for every board except X26. The same
smoke-test-first, physical-confirmation-before-trusting-it discipline that caught the DI
polarity bug on X26 applies to any of these other boards' pin maps the first time one is
actually wired up.
"""

from glob import glob
from time import monotonic

try:
    import gpiod
    from gpiod.line import Direction, Value
except ImportError:
    # The connector itself already handles installing gpiod via TBUtility.install_package
    # before importing this module; this bare import is kept here too so this module can
    # also be used stand-alone (e.g. from hva_kelvin_v1/systemd/hva-kelvin-v1-do-safe-reset.py) without
    # pulling in the gateway package at all.
    gpiod = None
    Direction = None
    Value = None

# --- Per-board-type pin mappings (BCM/RP1 line offsets, fixed by board wiring) -----
#
# Each entry is {"DI": {name: offset, ...}, "DO": {name: offset, ...}}. Board types with
# no DI/DO channels for this connector (X10, X20: RS485/RS232-only; X16: raw GPIO, not
# DI/DO) are intentionally omitted -- get_board_offsets() raises a clear error for them
# rather than silently returning an empty/useless connector.
#
# X26: the only board physically confirmed on real hardware (Kelvin26001, 2026-09-03 DI
# polarity test). X13/X14/X15/X23/X28: derived from the manual's appendix per-connector-
# size BCM tables -- see the module docstring above for the method and its confidence
# level.

BOARD_PIN_MAPS = {
    # 20-pin connector (X23/X26/X28 all share the same physical-pin-to-BCM wiring; see
    # the module docstring's appendix cross-reference).
    "X26": {  # 2x RS485/RS232, 8x DI, 4x DO -- HARDWARE CONFIRMED
        "DI": {"DI1": 12, "DI2": 4, "DI3": 16, "DI4": 27, "DI5": 25, "DI6": 22, "DI7": 13, "DI8": 5},
        "DO": {"DO1": 24, "DO2": 23, "DO3": 7, "DO4": 3},
    },
    "X23": {  # 4x RS485/RS232, 4x DI, 4x DO -- manual-derived, not hardware-confirmed
        "DI": {"DI1": 16, "DI2": 27, "DI3": 25, "DI4": 22},
        "DO": {"DO1": 24, "DO2": 23, "DO3": 7, "DO4": 3},
    },
    "X28": {  # 2x RS485/RS232, 12x DI, no DO -- manual-derived, not hardware-confirmed
        "DI": {"DI1": 12, "DI2": 4, "DI3": 16, "DI4": 27, "DI5": 25, "DI6": 22, "DI7": 13, "DI8": 5,
               "DI9": 24, "DI10": 23, "DI11": 7, "DI12": 3},
        "DO": {},
    },
    # 6-pin connector (X13/X14/X15/X16 share the same physical-pin-to-BCM wiring; X16's
    # own port table names its 4 ports directly by BCM number, which is what confirms
    # this table -- see the module docstring).
    "X13": {  # 2x DI, 2x DO (labelled DO3/DO4, matching the larger boards' numbering)
        "DI": {"DI1": 24, "DI2": 23},
        "DO": {"DO3": 7, "DO4": 3},
    },
    "X14": {  # 4x DI, no DO -- manual-derived, not hardware-confirmed
        "DI": {"DI1": 24, "DI2": 23, "DI3": 7, "DI4": 3},
        "DO": {},
    },
    "X15": {  # no DI, 4x DO -- manual-derived, not hardware-confirmed
        "DI": {},
        "DO": {"DO1": 24, "DO2": 23, "DO3": 7, "DO4": 3},
    },
}

# Board types this connector deliberately does not support, with a reason to surface in
# get_board_offsets()'s error rather than a bare KeyError.
UNSUPPORTED_BOARD_TYPES = {
    "X10": "X10 is RS485/RS232-only (2 serial ports, no DI/DO) -- use the gateway's "
           "built-in Modbus connector for it, not this GPIO connector.",
    "X20": "X20 is RS485/RS232-only (4 serial ports, no DI/DO) -- use the gateway's "
           "built-in Modbus connector for it, not this GPIO connector.",
    "X16": "X16 exposes 4 raw GPIO lines with no DI/DO polarity/logic convention -- not "
           "modelled by this connector's DI/DO abstraction.",
}

# Default board type when a config predates (or simply omits) "boardType" -- X26 is the
# only board that has ever shipped in a config on this project's real hardware
# (Kelvin26001), so this keeps every existing deployment behaving identically.
DEFAULT_BOARD_TYPE = "X26"

# Backward-compatible aliases: this connector's only hardware-confirmed board, X26 --
# kept as top-level names since existing code (the standalone smoke-test script, and any
# config predating the "boardType" field) imports these directly.
DEFAULT_DI_OFFSETS = BOARD_PIN_MAPS["X26"]["DI"]
DEFAULT_DO_OFFSETS = BOARD_PIN_MAPS["X26"]["DO"]


def get_board_offsets(board_type=None):
    """
    Look up the (DI offsets, DO offsets) pair for a given "boardType" config value.

    :param board_type: a key from BOARD_PIN_MAPS (case-insensitive), or falsy/None to mean
                        DEFAULT_BOARD_TYPE ("X26") -- so an absent "boardType" field in
                        config behaves exactly as it did before this option existed.
    :return: (di_offsets, do_offsets) -- two {name: offset} dicts, each possibly empty
             (e.g. X15 has no DI, X28 has no DO).
    :raises GpioMapError: for a board this connector deliberately doesn't support (with
                           the specific reason from UNSUPPORTED_BOARD_TYPES), or for any
                           other string that isn't a recognised board type at all.
    """
    board_type = str(board_type).strip().upper() if board_type else DEFAULT_BOARD_TYPE

    if board_type in BOARD_PIN_MAPS:
        board = BOARD_PIN_MAPS[board_type]
        return dict(board["DI"]), dict(board["DO"])

    if board_type in UNSUPPORTED_BOARD_TYPES:
        raise GpioMapError(f"boardType \"{board_type}\" is not supported by this connector: "
                            f"{UNSUPPORTED_BOARD_TYPES[board_type]}")

    valid = ", ".join(sorted(BOARD_PIN_MAPS))
    raise GpioMapError(f"Unknown boardType \"{board_type}\" -- valid types are: {valid} "
                        f"(also recognised but unsupported: {', '.join(sorted(UNSUPPORTED_BOARD_TYPES))})")

# BCM lines that must never be requested by this connector, whatever a config file says.
# GPIO2 = run LED, GPIO6/ID_SD = hardware watchdog, GPIO14/15 = debug console UART,
# GPIO17 = network status LED. Applies to every board type -- these are CM5 carrier-board
# reservations, not specific to any one IO daughterboard.
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
        "Run `gpiodetect` on the box and check thingsboard_gateway/config/hva_kelvin_v1_gpio.json's "
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


def force_do_lines_safe(chip_path, do_offsets, consumer="hva-kelvin-v1-do-safe-reset", active_low_by_offset=None):
    """
    Standalone helper: open the given DO offsets on chip_path as outputs, immediately
    drive every one of them to the safe (OFF/open) state, and release the request.

    This is intentionally self-contained (no gateway imports) so it can be called both
    by the connector's own startup path and by the independent systemd boot-time script
    in hva_kelvin_v1/systemd/hva-kelvin-v1-do-safe-reset.py, which must keep working even if the
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
