# BLIIOT X26 GPIO connector

A ThingsBoard IoT Gateway connector for the BLIIOT BL460AL-CM5002016-X26 IO board (8x DI,
4x DO), running under stock Raspberry Pi OS on the CM5 carrier board -- no vendor driver,
no I2C/SPI expander chip. It talks to the RP1 GPIO controller directly through
`libgpiod` v2, using the pin mapping and hardware behaviour confirmed on real hardware
during this project's OS migration (see the project's migration log for the full
history: chip index instability across reflashes, DO polarity, the "sticky output"
finding, etc.).

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

**DO polarity is handled for you.** The X26's DO bank is sink-type and its raw GPIO logic
is inverted (`raw 0 = energised/closed`, `raw 1 = de-energised/open`). Every RPC method,
shared attribute, and telemetry key this connector exposes talks in plain logical
`true = ON` / `false = OFF` terms -- the inversion is applied internally in
`gpio_map.do_state_to_raw()`/`raw_to_do_state()`. You never need to think about it
unless you're editing that module.

**DO outputs are "sticky"** -- a line keeps outputting its last value even after the
process holding it exits or is killed. This connector forces every DO channel to its
safe (OFF/open) state as soon as it requests the lines at startup, and again on a clean
`close()`/shutdown. That does **not** cover the gap between a hard crash and systemd
restarting the gateway service, nor the gap before the gateway has started at all after a
reboot -- see "Boot-time safety net" below for how that's covered.

## Installing

1. **Pre-install `gpiod`** rather than relying on the connector's automatic
   `TBUtility.install_package("gpiod")` fallback (which only triggers on first import and
   needs PyPI reachable) -- either `sudo apt install python3-libgpiod` or, inside the
   gateway's venv, `pip install gpiod`. Confirm the version matches what `gpioget
   --version`/`gpioset --help` report for the CLI tools already validated on this box.

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

Read-only by default: it reports the gpiochip it found, the current state of all 12
channels, and forces the DOs safe again before exiting. Add `--pulse DO1` (with something
safe wired to that channel) to test an actual energise/de-energise cycle. Only once this
looks right on the real box is it worth wiring the full connector into the gateway.

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
(`"ethernet"`/`"cellular"`), updated only on change.

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
