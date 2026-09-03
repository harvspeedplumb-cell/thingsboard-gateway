#!/usr/bin/env python3
"""
Stand-alone hardware smoke test for the BLIIOT X26 DI/DO channels, meant to be run
directly on the box (not through the gateway) before trusting BliiotGpioConnector.

Run from a checkout of this repo so the `thingsboard_gateway` package is importable,
e.g.:

    cd thingsboard-gateway
    python3 bliiot/test/gpio_smoke_test.py

By default this is READ-ONLY and SAFE: it reports which gpiochip it found, prints the
current logical state of all 8 DI and 4 DO channels, and then forces every DO channel to
its safe (OFF/open) state -- it never energises anything unless you pass --pulse.

    --pulse DO1          energise DO1 for --pulse-seconds (default 2s) then de-energise
                          it again. Only use this with something safe wired to that
                          channel -- see the DO wiring notes in the project's migration
                          log before connecting real 24V loads/relays.
"""
import argparse
import sys
import time

sys.path.insert(0, '.')

from thingsboard_gateway.connectors.bliiot_gpio.gpio_map import (  # noqa: E402
    DEFAULT_DI_OFFSETS,
    DEFAULT_DO_OFFSETS,
    GpioMapError,
    SAFE_DO_STATE,
    do_state_to_raw,
    find_rp1_gpiochip,
    raw_to_di_state,
    raw_to_do_state,
)

import gpiod  # noqa: E402
from gpiod.line import Direction  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--chip', default='auto', help='gpiochip name/path, or "auto" to detect by driver label')
    parser.add_argument('--pulse', metavar='DO_CHANNEL', default=None,
                         help='Energise this DO channel briefly then de-energise it. Omit for a read-only run.')
    parser.add_argument('--pulse-seconds', type=float, default=2.0)
    args = parser.parse_args()

    try:
        chip_path = find_rp1_gpiochip(args.chip)
    except GpioMapError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"Using gpiochip: {chip_path}")

    di_settings = {offset: gpiod.LineSettings(direction=Direction.INPUT) for offset in DEFAULT_DI_OFFSETS.values()}
    di_request = gpiod.request_lines(chip_path, consumer='bliiot-gpio-smoke-test-di', config=di_settings)

    do_settings = {offset: gpiod.LineSettings(direction=Direction.OUTPUT,
                                               output_value=do_state_to_raw(SAFE_DO_STATE))
                   for offset in DEFAULT_DO_OFFSETS.values()}
    do_request = gpiod.request_lines(chip_path, consumer='bliiot-gpio-smoke-test-do', config=do_settings)

    try:
        di_offsets = list(DEFAULT_DI_OFFSETS.values())
        di_values = di_request.get_values(di_offsets)
        print("\nDigital inputs:")
        for name, offset, value in zip(DEFAULT_DI_OFFSETS.keys(), di_offsets, di_values):
            state = raw_to_di_state(value)
            print(f"  {name:4} (BCM{offset:<2}): {'ACTIVE ' if state else 'inactive'}  (raw={value.name})")

        do_offsets = list(DEFAULT_DO_OFFSETS.values())
        do_values = do_request.get_values(do_offsets)
        print("\nDigital outputs (forced to safe/OFF state by this script on startup):")
        for name, offset, value in zip(DEFAULT_DO_OFFSETS.keys(), do_offsets, do_values):
            state = raw_to_do_state(value)
            print(f"  {name:4} (BCM{offset:<2}): {'ON (energised)' if state else 'OFF (safe)':14}  (raw={value.name})")

        if args.pulse:
            if args.pulse not in DEFAULT_DO_OFFSETS:
                print(f"\nERROR: unknown DO channel '{args.pulse}', expected one of {list(DEFAULT_DO_OFFSETS)}",
                      file=sys.stderr)
                return 1
            offset = DEFAULT_DO_OFFSETS[args.pulse]
            print(f"\nPulsing {args.pulse} (BCM{offset}) ON for {args.pulse_seconds}s -- "
                  f"make sure you're happy with whatever is wired there.")
            do_request.set_values({offset: do_state_to_raw(True)})
            time.sleep(args.pulse_seconds)
            do_request.set_values({offset: do_state_to_raw(False)})
            print(f"{args.pulse} de-energised again.")
    finally:
        # Always leave every DO channel safe, pulse test or not.
        do_request.set_values({offset: do_state_to_raw(SAFE_DO_STATE) for offset in DEFAULT_DO_OFFSETS.values()})
        di_request.release()
        do_request.release()
        print("\nAll DO channels left in the safe/OFF state; gpiod requests released.")

    return 0


if __name__ == '__main__':
    sys.exit(main())
