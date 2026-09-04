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
thingsboard_gateway/config/bliiot_gpio.json   example connector config (default pin map, single device)
thingsboard_gateway/config/bliiot_gpio_multi_device_example.json
                                               worked example: duplicating DI/DO/WAN
                                               readings to a second, restricted device
bliiot/                                       this folder -- deployment helpers, not part
                                               of the installed Python package
  systemd/bliiot-do-safe-reset.py             standalone boot-time DO safety net
  systemd/bliiot-do-safe-reset.service        systemd unit for the above
  test/gpio_smoke_test.py                     stand-alone hardware smoke test (run first)
  test/offline_connector_test.py              offline (mocked gpiod) connector logic test --
                                               config parsing, reportOnChange, multi-device
                                               fan-out, per-channel RPC dispatch, no-modem case
  test/modbus_duplicate_device_test.py        proves the stock Modbus connector's own
                                               multi-device fan-out against a real local
                                               pymodbus TCP server (see "Duplicating one
                                               input to multiple ThingsBoard devices" below)
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

## Config format (2026-09-04 rewrite -- breaking change)

`bliiot_gpio.json` follows the same top-level shape as the gateway's own built-in
`bacnet` connector (`thingsboard_gateway/config/bacnet.json`) rather than this
connector's earlier bespoke shape. **There is no backward-compatible alias for the old
field names** -- this was a deliberate choice (a clean break, not a transition period),
so an old-format config must be replaced, not just left in place, or the connector will
not start. See "Migrating an existing config" below.

| Old (pre-2026-09-04) | New |
|---|---|
| `"gpio"."digitalInputs"` (dict of `{name: {offset, activeLow, bias, debounceMs}}`) | top-level `"timeseries"` (list of `{key, offset?, activeLow, bias, debounceMs}`) |
| `"gpio"."digitalOutputs"` (dict of `{name: {offset, activeLow}}`) | top-level `"serverSideRpc"` (list of `{key, offset?, activeLow, setMethod?, getMethod?, toggleMethod?}`) |
| `"wanStatus"` (object, `"enabled"` flag) | an entry in top-level `"attributes"` with `"source": "wanStatus"` -- presence of the entry *is* the enable flag, there is no separate `"enabled"` field |
| `"gpio"."heartbeatIntervalSec"` | `"gpio"."pollPeriod"` (still seconds) |
| n/a | `"gpio"."reportOnChange"` (new, default `true`) |
| `"devices"` (list, but only `devices[0]` was ever used) | `"devices"` (list, genuinely multi-device -- see "Duplicating one input to multiple devices" below) |

`"gpio"."pollIntervalMs"` (physical DI sampling/debounce rate) is unchanged and is
**not** the same thing as `"gpio"."pollPeriod"` -- see the "DI reporting" section below
for what each one governs.

Every standard channel key for the selected `boardType` (`DI1`..`DI8`, `DO1`..`DO4` on
X26) can be omitted from `"timeseries"`/`"serverSideRpc"` entirely and still gets its
board-default `offset` -- only list it if you're overriding something (`activeLow`,
`bias`, a non-default RPC method name, ...). A key that **isn't** one of the board's
standard channels (a custom/renamed channel -- see "Renaming channels" below) must
include an explicit `"offset"`; without one it's skipped at startup with a logged error
rather than crashing the connector.

### Renaming channels for telemetry

The old dedicated `"key"` rename sub-field (`{"offset": 12, ..., "key": "Pump 1 Fault"}`)
is gone -- in the new schema `"key"` already **is** the channel's own identity (matching
how BACnet's own `"key"` field works: it's simultaneously the address-lookup name and the
published telemetry/attribute field name). The equivalent of the old rename feature is to
give the renamed channel its own `"timeseries"`/`"serverSideRpc"` entry with an explicit
`"offset"`, instead of using one of the board's standard keys:

```json
{"key": "Pump 1 Fault", "offset": 12, "activeLow": true, "bias": "as-is", "debounceMs": 50}
```

This publishes telemetry as `"Pump 1 Fault": false` instead of `"DI1": false`, exactly
like before -- the only difference is that because `"Pump 1 Fault"` is no longer one of
the board's standard keys, its `"offset"` (BCM12, the physical pin DI1 was wired to) has
to be given explicitly rather than defaulted. For a renamed DO channel, the per-channel
RPC method names also default from the new key (`setPump 1 Fault` is legal JSON but an
awkward RPC method name in practice -- give it an explicit `"setMethod"`/`"getMethod"`/
`"toggleMethod"` too if you rename a DO channel). Two `"timeseries"` or two
`"serverSideRpc"` entries sharing the same resolved RPC method name fail fast at
connector startup with a clear error (check the gateway log) rather than silently
shadowing each other.

## Duplicating one input to multiple ThingsBoard devices

The same physical DI/DO channel or the WAN-status attribute can be surfaced on more than
one ThingsBoard device from a single connector instance -- the same pattern the built-in
BACnet connector already supports by listing the same point under two `"devices"` blocks
in `bacnet.json`. Each entry in `"devices"` may include its own `"timeseries"`/
`"attributes"`/`"serverSideRpc"` **subset** list of keys/channels; omitting one of those
lists for a device means "no restriction, sees/controls everything of that kind" (the
default, and what every config predating this feature effectively had). See
`thingsboard_gateway/config/bliiot_gpio_multi_device_example.json` for a full worked
example (a primary unrestricted device plus a second device restricted to `DI1`/`DI2`
telemetry and `DO1` control only), and `bliiot/test/offline_connector_test.py` (section
5, 7, 8, 9, 10) for the behaviour this is built on, exercised offline.

This has to happen inside **one** connector instance/one `gpiod` line request rather than
two independent connector processes each polling the same channel (which is how BACnet or
Modbus would do it): GPIO chardev lines are exclusively locked, so two separate
`gpiod.request_lines()` calls on the same offset from two different processes collide
with `OSError: [Errno 16] Device or resource busy` (confirmed the hard way during this
project's own install-time troubleshooting -- see the migration log). Modbus doesn't have
this constraint at all -- it's a bus/network protocol, so duplicating a Modbus register to
two devices is just two ordinary entries in `modbus.json`'s `"master.slaves"` list with
the same `host`/`port`/`unitId`/`address` and different `deviceName`, no special
connector-side mechanism needed. `bliiot/test/modbus_duplicate_device_test.py` proves
that end-to-end against the stock, unmodified Modbus connector and a real local Modbus
TCP test server.

**Telemetry visibility and RPC-control authorization are independent.** A device's
`"timeseries"` subset governs which channel *values* (DI or DO state -- both are just
telemetry) it receives; its separate `"serverSideRpc"` subset governs which DO channels
it may *control* via RPC or the `<key>_set` shared attribute. A device can be authorized
to flip `DO1` without ever seeing `DO1`'s state in its own telemetry feed, and vice versa
-- set both subsets deliberately if that separation matters for a given device.

## DI reporting: `reportOnChange` and `pollPeriod`

`"gpio"."reportOnChange"` (default `true`) controls whether a DI edge is published the
moment it's detected:

* `true` (default -- matches this connector's previous, only, behaviour): a DI edge
  publishes immediately, **and** the full DI+DO state is force-republished every
  `"gpio"."pollPeriod"` regardless of whether anything changed (the same heartbeat-resend
  reasoning as the WAN attribute below -- see the migration log's "active_wan_interface
  stale after device deletion" entry for why an unconditional resend matters even when
  on-change publishing is also active).
* `false`: DI changes are **never** published individually -- only the full DI+DO state,
  unconditionally, every `"gpio"."pollPeriod"`, matching BACnet's own simpler "always
  report everything on each poll cycle" model exactly. A DI edge still updates the
  connector's internal state immediately even with this off; it just doesn't trigger a
  publish of its own, so the next periodic publish is always accurate.

`"gpio"."pollIntervalMs"` (default 200ms) is a different, unrenamed setting: it's the
physical DI sampling/debounce rate, needed for debounce accuracy regardless of
`reportOnChange`. `"gpio"."pollPeriod"` is how often a full state re-report is forced.

## Migrating an existing config

There is no compatibility shim for the old field names -- an old-format
`bliiot_gpio.json` (`"digitalInputs"`/`"digitalOutputs"`/`"wanStatus"`/
`"heartbeatIntervalSec"`) makes the connector fail to start, not silently misbehave, but
it **will** stop the connector, so treat this like any other breaking config change:

1. Back up the current `bliiot_gpio.json` before touching it.
2. Rewrite it against the new shape (the table under "Config format" above maps every
   old field to its replacement 1:1) -- or start from
   `thingsboard_gateway/config/bliiot_gpio.json` in this delivery and reapply your
   site-specific overrides (non-default `boardType`, any custom/renamed channels,
   non-default WAN `interfaceNames`, etc.).
3. If you were relying on the old generic `setDo`/`getDo` RPC methods from an external
   system (a dashboard widget, a script, a rule chain action) rather than
   `<channel>_set` shared attributes, that caller needs updating too -- there is no
   `setDo`/`getDo` anymore, only the per-channel named methods (`setDO1`, etc.) or
   `setAllDo`/`getDi` for the channels that stayed generic.
4. Restart the gateway service after the config is in place -- a config-only edit is not
   picked up by a running connector.

There is nothing to migrate in ThingsBoard itself: telemetry/attribute keys for the
default (unrenamed) X26 channels are unchanged (`DI1`..`DI8`, `DO1`..`DO4`,
`active_wan_interface`), so existing dashboards and rule chains built against those keys
keep working once the connector is back up.

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

Before that, `bliiot/test/offline_connector_test.py` exercises the connector's config
parsing and RPC/telemetry/authorization logic end-to-end against a mocked `gpiod` and a
mocked ThingsBoard gateway boundary (no real hardware, no installed `thingsboard_gateway`
package needed) -- run it from the repo root after any change to `bliiot_gpio_connector.py`
or its config schema:

```
python3 bliiot/test/offline_connector_test.py
```

It covers the 2026-09-04 schema rewrite specifically: `timeseries`/`attributes`/
`serverSideRpc`/`devices` parsing, the offset-optional-for-standard-channels rule,
`reportOnChange` in both states, multi-device telemetry/attribute/RPC fan-out and
per-device authorization, per-channel RPC method dispatch, and the WAN loop's behaviour
when no interface holds the default route (4G modem not installed, or Ethernet down with
no modem fitted at all).

## RPC and attribute API

Each DO channel gets its **own** RPC method names (BACnet-exact convention: BACnet's own
`serverSideRpc` gives each capability, e.g. `set_state`, its own method name tied to one
object, rather than one generic method taking an address parameter) -- default
`set<key>`/`get<key>`/`toggle<key>` (e.g. `setDO1`/`getDO1`/`toggleDO1` for the standard
X26 channels), overridable per channel with `"setMethod"`/`"getMethod"`/`"toggleMethod"`
in that channel's `"serverSideRpc"` entry. `setAllDo`, `getDi` and `getWanStatus` remain
generic/global methods, now filtered to whatever the *requesting device* is authorized to
see/control (see "Duplicating one input to multiple ThingsBoard devices" above):

| Method | Params | Reply |
|---|---|---|
| `set<DOkey>` (e.g. `setDO1`) | `{"state": true}` | `{"success", "channel", "state", "message"}` |
| `get<DOkey>` (e.g. `getDO1`) | `{}` | `{"success", "channel", "state"}` |
| `toggle<DOkey>` (e.g. `toggleDO1`) | `{}` | `{"success", "channel", "state", "message"}` |
| `setAllDo` | `{"state": false}` | `{"success", "channels": {...}}` -- only the channels the requesting device's `serverSideRpc` subset authorizes |
| `getDi` | `{"channel": "DI1"}` or `{}` for all | `{"success", "channel"?, "state"?, "channels"?}` -- only channels the requesting device's `timeseries` subset includes |
| `getWanStatus` | `{}` | `{"success", "active_wan_interface"}` -- fails with a clear error if WAN reporting isn't configured at all, or if the requesting device's `attributes` subset excludes the key |

Calling a DO channel's method (or any method at all) from a device not authorized for
that channel returns `{"success": false, "error": "..."}` rather than silently no-oping
or crashing.

DO channels can also be driven with a **shared attribute** update instead of an RPC call
-- set `<key>_set` (e.g. `DO1_set`; channel key + the configurable
`gpio.doAttributeUpdateSuffix`, default `_set`) to `true`/`false` on the device and the
connector applies it the same way the RPC does, subject to the same per-device
`serverSideRpc` authorization check (an unauthorized device's shared-attribute write is
ignored with a logged warning, not silently accepted).

Telemetry keys are the configured `"timeseries"`/`"serverSideRpc"` channel keys (`DI1`..
`DI8`, `DO1`..`DO4` by default) as booleans, published on change (if
`"gpio"."reportOnChange"` is `true`, the default) plus an unconditional full-state resend
every `"gpio"."pollPeriod"` (default 60s) -- see "DI reporting" above. The active WAN path
is published as the configured WAN attribute's `"key"` (`active_wan_interface` by
default) with value `"ethernet"`/`"cellular"` (from `"interfaceNames"`) or the interface's
raw name if it isn't in that map, or `"unknown"` if no interface currently holds the
default route at all (Ethernet down and no cellular modem registered, or no modem
fitted). Published on change **and** re-published unconditionally every WAN attribute
entry's own `"pollPeriod"` (default 15s, independent of `"gpio"."pollPeriod"`). That
unconditional resend was added after a live finding on Kelvin26001 (2026-09-03): the
attribute used to be published on-change only, so if the platform ever lost it
independently of the interface actually changing (e.g. the gateway device being deleted
and recreated on the platform), it would stay blank/stale until the connector process was
restarted. The resend bounds that to one poll interval, with no restart needed. See
`bliiot/test/offline_connector_test.py` section 12 for the no-modem/no-default-route
case specifically.

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
scope"). The `gpio`/`timeseries`/`serverSideRpc`/`attributes` config sections here are
unrelated to and don't block adding an RS485-based connector or extending this one later.
