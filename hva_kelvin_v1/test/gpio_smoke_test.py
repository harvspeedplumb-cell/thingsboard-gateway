#!/usr/bin/env python3
"""
Stand-alone hardware smoke test for a BLIIOT X-series board's DI/DO channels, meant to be
run directly on the box (not through the gateway) before trusting HVAKelvinV1GpioConnector.

Run from a checkout of this repo so the `thingsboard_gateway` package is importable,
e.g.:

    cd thingsboard-gateway
    python3 hva_kelvin_v1/test/gpio_smoke_test.py

By default this is READ-ONLY and SAFE, and assumes an X26 board (this connector's only
hardware-confirmed board type): it reports which gpiochip it found, prints the current
logical state of every DI/DO channel, and then forces every DO channel to its safe
(OFF/open) state -- it never energises anything unless you pass --pulse.

    --board X23           test a different board type's pin map (see gpio_map.py's
                           BOARD_PIN_MAPS for the full list). Only X26 has been checked
                           against real hardware so far -- treat any other board's result
                           here as the first real-hardware checkpoint for that board, the
                           same way this script itself was for X26.
    --pulse DO1            energise DO1 for --pulse-seconds (default 2s) then de-energise
                            it again. Only use this with something safe wired to that
                            channel -- see the DO wiring notes in the project's migration
                            log before connecting real 24V loads/relays.
"""
import argparse
import sys
import time

sys.path.insert(0, '.')

from thingsboard_gateway.connectors.hva_kelvin_v1_gpio.gpio_map import (  # noqa: E402
    BOARD_PIN_MAPS,
    DEFAULT_BOARD_TYPE,
    GpioMapError,
    SAFE_DO_STATE,
    do_state_to_raw,
    find_rp1_gpiochip,
    get_board_offsets,
    raw_to_di_state,
    raw_to_do_state,
)

import gpiod  # noqa: E402
from gpiod.line import Direction  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--chip', default='auto', help='gpiochip name/path, or "auto" to detect by driver label')
    parser.add_argument('--board', default=DEFAULT_BOARD_TYPE,
                         help=f'X-series board type fitted, selects the DI/DO pin map (default: '
                              f'{DEFAULT_BOARD_TYPE}). One of: {", ".join(sorted(BOARD_PIN_MAPS))}. Only '
                              f'{DEFAULT_BOARD_TYPE} has been run against real hardware -- see gpio_map.py\'s '
                              f'module docstring for the other boards\' derivation and confidence level.')
    parser.add_argument('--pulse', metavar='DO_CHANNEL', default=None,
                         help='Energise this DO channel briefly then de-energise it. Omit for a read-only run.')
    parser.add_argument('--pulse-seconds', type=float, default=2.0)
    args = parser.parse_args()

    try:
        chip_path = find_rp1_gpiochip(args.chip)
        di_offsets_by_name, do_offsets_by_name = get_board_offsets(args.board)
    except GpioMapError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"Using gpiochip: {chip_path}")
    print(f"Board type: {args.board} ({len(di_offsets_by_name)} DI, {len(do_offsets_by_name)} DO)")

    di_settings = {offset: gpiod.LineSettings(direction=Direction.INPUT) for offset in di_offsets_by_name.values()}
    di_request = gpiod.request_lines(chip_path, consumer='hva-kelvin-v1-gpio-smoke-test-di', config=di_settings)

    do_settings = {offset: gpiod.LineSettings(direction=Direction.OUTPUT,
                                               output_value=do_state_to_raw(SAFE_DO_STATE))
                   for offset in do_offsets_by_name.values()}
    do_request = gpiod.request_lines(chip_path, consumer='hva-kelvin-v1-gpio-smoke-test-do', config=do_settings)

    try:
        di_offsets = list(di_offsets_by_name.values())
        di_values = di_request.get_values(di_offsets) if di_offsets else []
        print("\nDigital inputs:")
        for name, offset, value in zip(di_offsets_by_name.keys(), di_offsets, di_values):
            state = raw_to_di_state(value)
            print(f"  {name:4} (BCM{offset:<2}): {'ACTIVE ' if state else 'inactive'}  (raw={value.name})")

        do_offsets = list(do_offsets_by_name.values())
        do_values = do_request.get_values(do_offsets) if do_offsets else []
        print("\nDigital outputs (forced to safe/OFF state by this script on startup):")
        for name, offset, value in zip(do_offsets_by_name.keys(), do_offsets, do_values):
            state = raw_to_do_state(value)
            print(f"  {name:4} (BCM{offset:<2}): {'ON (energised)' if state else 'OFF (safe)':14}  (raw={value.name})")

        if args.pulse:
            if args.pulse not in do_offsets_by_name:
                print(f"\nERROR: unknown DO channel '{args.pulse}', expected one of {list(do_offsets_by_name)}",
                      file=sys.stderr)
                return 1
            offset = do_offsets_by_name[args.pulse]
            print(f"\nPulsing {args.pulse} (BCM{offset}) ON for {args.pulse_seconds}s -- "
                  f"make sure you're happy with whatever is wired there.")
            do_request.set_values({offset: do_state_to_raw(True)})
            time.sleep(args.pulse_seconds)
            do_request.set_values({offset: do_state_to_raw(False)})
            print(f"{args.pulse} de-energised again.")
    finally:
        # Always leave every DO channel safe, pulse test or not.
        do_request.set_values({offset: do_state_to_raw(SAFE_DO_STATE) for offset in do_offsets_by_name.values()})
        di_request.release()
        do_request.release()
        print("\nAll DO channels left in the safe/OFF state; gpiod requests released.")

    return 0


if __name__ == '__main__':
    sys.exit(main())
