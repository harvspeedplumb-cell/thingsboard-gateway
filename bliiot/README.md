# BLIIOT X26 GPIO connector

A ThingsBoard IoT Gateway connector for the BLIIOT BL460AL-CM5002016-X26 IO board (8x DI,
4x DO), running under stock Raspberry Pi OS on the CM5 carrier board -- no vendor driver,
no I2C/SPI expander chip. It talks to the RP1 GPIO controller directly through
`libgpiod` v2, using the pin mapping and hardware behaviour confirmed on real hardware
during this project's OS migration (see the project's migration log for the full
history: chip index instability across reflashes, DI/DO polarity inversion, the "sticky
output" finding, etc.).

It also reports which WAN path (Ethernet vs the onboard 4G modem) currently holds the
default route, as a device attribute, since the box runs with Ethernet-primary /
cellular-backup failover and that should be visible from the ThingsBoard dashboard.

## Layout

```
thingsboard_gateway/connectors/bliiot_gpio/   the connector itself (installed with the gateway)
  gpio_map.py                                 board pin mapping, polarity, chip auto-detect
  bliiot_gpio_connector.py                    Connector implementation
  bliiot_gpio_uplink_converter.py             device data -> ConvertedData
  bliiot_gpio_downlink_converter.py           RPC/attribute request -> raw gpiod write
thingsboard_gateway/config/bliiot_gpio.json   example connector config (default pin map)
bliiot/                                       this folder -- deployment helpers, not part
                                               of the installed Python package
  systemd/bliiot-do-safe-reset.py             standalone boot-time DO safety net
  systemd/bliiot-do-safe-reset.service        systemd unit for the above
  test/gpio_smoke_test.py                     stand-alone hardware smoke test (run first)
```

## Why a custom connector instead of the "custom"/extensions mechanism

The gateway docs describe two ways to add a connector: a lightweight "custom" connector
dropped into `extensions/<type>/` at runtime, or a proper connector shipped inside
`thingsboard_gateway/connectors/`. This is built the second way, alongside Modbus/BLE/etc,
since it's going into your own fork rather than being layered on top of a stock install:
it's registered in `DEFAULT_CONNECTORS` (`thingsboard_gateway/gateway/constants.py`) and
listed in `setup.py`'s `packages=[...]`, so it installs and loads exactly like any other
built-in connector -- you only need `"type": "bliiot_gpio"` (no `"class"` field needed) in
`tb_gateway.json`.

## Hardware mapping

| Channel | BCM offset | | Channel | BCM offset |
|---|---|---|---|---|
| DI1 | 12 | | DO1 | 24 |
| DI2 | 4  | | DO2 | 23 |
| DI3 | 16 | | DO3 | 7  |
| DI4 | 27 | | DO4 | 3  |
| DI5 | 25 | | | |
| DI6 | 22 | | | |
| DI7 | 13 | | | |
| DI8 | 5  | | | |

These are the defaults baked into `gpio_map.py` and `bliiot_gpio.json`; override any of
them per-channel in the config file if a board revision or Y-board ever differs. BCM2,
6, 14, 15 and 17 (run LED, hardware watchdog, debug UART, network LED) are hard-refused
by `validate_offsets()` even if a config file tries to use them.

**DI/DO polarity is handled for you.** The X26's DO bank is sink-type and its raw GPIO
logic is inverted (`raw 0 = energised/closed`, `raw 1 = de-energised/open`). The 8 DI
channels are inverted the same way (`raw ACTIVE = idle/open`, `raw INACTIVE = closed
contact`) -- confirmed on real hardware (Kelvin26001, 2026-09-03) by shorting the X26
connector's pin 1 (DI6) to pin 11 (GND, the manual's documented dry-contact test): DI6
alone flipped from raw ACTIVE to raw INACTIVE while the other 7 channels stayed raw
ACTIVE. Every RPC method, shared attribute, and telemetry key this connector exposes
talks in plain logical `true = ON`/`closed` / `false = OFF`/`open` terms -- the
inversion is applied internally in `gpio_map.do_state_to_raw()`/`raw_to_do_state()`/
`raw_to_di_state()` (both default to `active_low=True`, overridable per channel in
`bliiot_gpio.json` if a dry-contact wiring or board revision ever differs). You never
need to think about it unless you're editing that module.

**DO outputs are "sticky"** -- a line keeps outputting its last value even after the
process holding it exits or is killed. This connector forces every DO channel to its
safe (OFF/open) state as soon as it requests the lines at startup, and again on a clean
`close()`/shutdown. That does **not** cover the gap between a hard crash and systemd
restarting the gateway service, nor the gap before the gateway has started at all after a
reboot -- see "Boot-time safety net" below for how that's covered.

## Other X-series boards (the `boardType` field)

This connector's DI/DO channel abstraction isn't tied to the X26 board specifically --
BLIIOT's ARMxy BL460 carrier accepts a family of X-series IO daughterboards, and
`bliiot_gpio.json`'s `"gpio"` block can select which one is fitted:

```json
"gpio": {
  "boardType": "X23"
}
```

Omit it (or leave it out entirely, as every config predating this feature does,
including Kelvin26001's) and it defaults to `"X26"` -- so nothing changes for an existing
deployment unless you explicitly ask for a different board.

| boardType | DI | DO | Notes |
|---|---|---|---|
| `X26` | 8 | 4 | **Hardware-confirmed** (Kelvin26001) -- the only board actually tested on real hardware so far. |
| `X23` | 4 | 4 | Manual-derived, not hardware-confirmed. |
| `X28` | 12 | 0 | Manual-derived, not hardware-confirmed. |
| `X13` | 2 | 2 (labelled `DO3`/`DO4`) | Manual-derived, not hardware-confirmed. |
| `X14` | 4 | 0 | Manual-derived, not hardware-confirmed. |
| `X15` | 0 | 4 | Manual-derived, not hardware-confirmed. |

Every non-X26 mapping was derived from the manual's appendix ("9. 40-Pin Pin
Multiplexing Description", p.51), which gives a physical-pin-to-BCM table per connector
size (6-pin: X13/X14/X15/X16; 20-pin: X23/X26/X28), cross-referenced against each board's
own port-name table (manual section 2.2.1). This method was validated by reproducing
X26's already hardware-confirmed 12-channel mapping exactly from the 20-pin table alone
-- but every other board here is still a paper derivation until it's actually
smoke-tested. **Run `bliiot/test/gpio_smoke_test.py --board <type>` on the real board
before trusting it**, the same way the X26 DI polarity bug was originally caught by
testing rather than assuming. See `gpio_map.py`'s module docstring for the full method
and per-board sourcing.

Three board types are recognised but deliberately **not supported** by this connector,
and `boardType` will fail fast with a clear error (not a silent no-op) if you set one of
these:

* `X10`, `X20` -- RS485/RS232-only, no DI/DO at all. Use the gateway's built-in Modbus
  connector for these instead.
* `X16` -- exposes 4 raw GPIO lines with no DI/DO polarity/logic convention, so this
  connector's boolean DI/DO abstraction doesn't fit it.

CAN-equipped X-series boards (X11/X12/X21/X22/X24/X25/X27/X29) aren't in this list at
all -- the manual states they're "Not support[ed]" on the BL460 series entirely, so
there's nothing to map.

## Renaming channels for telemetry (the `key` field)

Every DI/DO channel can carry an optional `"key"` field in `bliiot_gpio.json`:

```json
"DI1": {"offset": 12, "activeLow": true, "bias": "as-is", "debounceMs": 50, "key": "Pump 1 Fault"}
```

That changes what shows up in ThingsBoard's Latest Telemetry -- `"Pump 1 Fault": false`
instead of `"DI1": false` -- so a dashboard can read meaningfully-named points instead of
raw channel identifiers. It's **telemetry-only**: the channel's internal name (`DI1`,
`DO1`, ...) never changes, so RPC calls (`setDo`'s `"channel"` param, `getDo`/`getDi`) and
the `<channel>_set` shared attribute for DO control keep addressing `DO1`/`DI1` regardless
of what `key` is set to. A channel with no `key` published under its internal name,
unchanged -- so this is fully backward compatible with configs that predate the feature.
Two channels sharing the same `key` fail fast at connector startup with a clear error
(check the gateway log) rather than silently overwriting each other's telemetry in
ThingsBoard.

## Installing

1. **Pre-install `gpiod`** rather than relying on the connector's automatic
   `TBUtility.install_package("gpiod")` fallback (which only triggers on first import and
   needs PyPI reachable). This built-in variant runs entirely under `thingsboard-gateway`'s
   own interpreter, so `gpiod` needs to be importable **there specifically** -- on a
   standard `.deb` install that's the gateway's own venv (typically
   `/var/lib/thingsboard_gateway/venv`), which does not automatically see system
   site-packages: `sudo /var/lib/thingsboard_gateway/venv/bin/pip install gpiod` (or
   rebuild that venv with `--system-site-packages` and then `sudo apt install
   python3-libgpiod`). A plain system-Python install alone is not enough for this
   variant -- unlike the extension variant, there's no separate smoke-test/safe-reset
   script here running under the system interpreter to make that sufficient. Confirm the
   version matches what `gpioget --version`/`gpioset --help` report for the CLI tools
   already validated on this box.

2. Copy/merge `thingsboard_gateway/config/bliiot_gpio.json` into the gateway's config
   directory (wherever `tb_gateway.json` and the other connector configs live, e.g.
   `/etc/thingsboard-gateway/config/` on a `.deb` install, or `thingsboard_gateway/config/`
   in a source checkout).

3. Add an entry to `tb_gateway.json`'s `"connectors"` array:

   ```json
   {
     "name": "BLIIOT X26 GPIO Connector",
     "type": "bliiot_gpio",
     "configuration": "bliiot_gpio.json"
   }
   ```

4. Make sure whatever user the gateway service runs as (`thingsboard_gateway` on a
   `.deb` install) can open `/dev/gpiochipN` -- on Raspberry Pi OS that usually means
   membership in the `gpio` group; check `ls -l /dev/gpiochip*` and `groups
   thingsboard_gateway` if the connector logs a permission error on startup.

5. Restart the gateway service (a brand new connector needs a full gateway restart, not
   just a connector reload -- same as any other connector per the gateway docs).

## Testing before you trust it

Run the smoke test directly on the box first, outside the gateway:

```
cd thingsboard-gateway   # this repo checkout
python3 bliiot/test/gpio_smoke_test.py
```

Read-only by default: it reports the gpiochip it found, the current state of every
channel on the board (X26's 8 DI + 4 DO by default), and forces the DOs safe again
before exiting. Add `--board X23` (or any other type from the table above) to test a
different board's pin map, and `--pulse DO1` (with something safe wired to that channel)
to test an actual energise/de-energise cycle. Only once this looks right on the real box
is it worth wiring the full connector into the gateway.

**This connector's polling-based DI monitoring uses the same `gpiod` primitives
(`request_lines`/`get_values`/`set_values`) as this project's own confirmed-working test
scripts, and its offline logic (RPC handling, DO safety, attribute updates) has been
exercised against a mocked `gpiod` -- but the connector itself has not yet been run
against the real box**, because at the time this was built, SSH/network access to the
freshly-reflashed OS was still being restored (see the migration log's "Current
status"). Treat the smoke test above as the first real-hardware checkpoint.

## RPC and attribute API

RPC methods (`method` / `params`):

| Method | Params | Reply |
|---|---|---|
| `setDo` | `{"channel": "DO1", "state": true}` | `{"success", "channel", "state", "message"}` |
| `setAllDo` | `{"state": false}` | `{"success", "channels": {"DO1": false, ...}}` |
| `getDo` | `{"channel": "DO1"}` or `{}` for all | `{"success", "channel"?, "state"?, "channels"?}` |
| `getDi` | `{"channel": "DI1"}` or `{}` for all | `{"success", "channel"?, "state"?, "channels"?}` |
| `getWanStatus` | `{}` | `{"success", "active_wan_interface"}` |

DO channels can also be driven with a **shared attribute** update instead of an RPC call
-- set `DO1_set` (channel name + the configurable `gpio.doAttributeUpdateSuffix`, default
`_set`) to `true`/`false` on the device and the connector applies it the same way `setDo`
does.

Telemetry keys are simply the channel names (`DI1`..`DI8`, `DO1`..`DO4`) as booleans,
published on change plus a full-state resend every `gpio.heartbeatIntervalSec` (default
60s). The active WAN path is published as the `active_wan_interface` device attribute
(`"ethernet"`/`"cellular"`), published on change **and** re-published unconditionally every
`gpio.heartbeatIntervalSec`, same as DI/DO. That heartbeat resend was added after a live
finding on Kelvin26001 (2026-09-03): the attribute used to be published on-change only, so
if the platform ever lost it independently of the interface actually changing (e.g. the
gateway device being deleted and recreated on the platform), it would stay blank/stale
until the connector process was restarted. The heartbeat resend bounds that to one
heartbeat interval, with no restart needed.

## Boot-time safety net

Because of the sticky-output behaviour above, install the independent oneshot too --
it does not depend on the gateway package or venv at all, so it keeps working even if
those are broken or not yet started:

```
sudo cp bliiot/systemd/bliiot-do-safe-reset.py /opt/bliiot/bliiot-do-safe-reset.py
sudo cp bliiot/systemd/bliiot-do-safe-reset.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable bliiot-do-safe-reset.service
```

It needs `python3-libgpiod` on the system Python (not just the gateway's venv) --
`sudo apt install python3-libgpiod` if `python3 -c "import gpiod"` fails at the system
level.

**Known residual gap:** this covers "box just booted" and the connector's own
`open()`/`close()` cover "connector started cleanly" / "connector shutting down
cleanly". Neither covers a live crash of the connector process followed by a
`systemd`-driven restart of the *gateway* service while the box stays up -- in that
window a DO line stays at whatever it was last set to. If that gap matters for whatever
is wired to these outputs, consider adding a lightweight external watchdog (e.g. a
timer unit that re-runs the safe-reset script if the gateway service has been in a
crash-loop for more than N seconds), which is not implemented here.

## Not yet in scope

RS485 (`ttyACM0`/`ttyACM1`) is not handled by this connector -- the downstream device
protocol on those ports hasn't been gathered yet (see the migration log's "Next task
scope"). The `gpio`/`wanStatus` config sections here are unrelated to and don't block
adding an RS485-based connector or extending this one later.
