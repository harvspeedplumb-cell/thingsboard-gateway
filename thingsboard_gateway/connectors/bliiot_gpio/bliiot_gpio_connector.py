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
Custom "extension"-style connector for the BLIIOT BL460AL-CM5002016-X26 IO board (8x DI,
4x DO), running under stock Raspberry Pi OS via libgpiod v2 against the RP1 gpiochip --
no vendor driver, no I2C/SPI expander involved. See gpio_map.py's module docstring
for the confirmed hardware facts this connector relies on (chip index instability across
reflashes, DI/DO polarity inversion, DO "sticky output" behaviour).

Also reports which WAN path (Ethernet vs the onboard 4G modem) currently holds the
default route as a device attribute, since the box is deployed with Ethernet-primary /
cellular-backup failover and that should be visible from ThingsBoard, not just by SSHing
into the box.

CONFIG FORMAT (2026-09-04 rewrite -- BREAKING CHANGE, no backward-compat aliases, by
explicit request): follows the naming/shape convention already used by this gateway's own
built-in `bacnet` connector (see thingsboard_gateway/config/bacnet.json), rather than the
bespoke dict-of-channels shape this connector used before:
  - DI channels are declared in a top-level "timeseries" LIST (was a "digitalInputs" dict).
  - The WAN-status feature is declared as an entry in a top-level "attributes" LIST (was a
    separate "wanStatus" object) -- one entry with "source": "wanStatus".
  - DO channels are declared in a top-level "serverSideRpc" LIST (was a "digitalOutputs"
    dict). Each entry gets its own named RPC methods (default "set<key>"/"get<key>"/
    "toggle<key>", e.g. "setDO1"/"getDO1"/"toggleDO1", overridable per entry) instead of
    the old generic setDo(channel=...)/getDo(channel=...) RPCs -- a deliberate, literal
    match to how BACnet's serverSideRpc gives each capability (e.g. "set_state") its own
    method name tied to one object, rather than one generic method taking an address
    parameter.
  - "gpio.heartbeatIntervalSec" is renamed "gpio.pollPeriod" (seconds) -- matches BACnet's
    own field name for "how often to (re)report everything", though BACnet's own pollPeriod
    is in milliseconds and conflates the physical read rate with the report rate; ours keeps
    "gpio.pollIntervalMs" (physical DI sampling rate, needed for debounce accuracy) as a
    separate, unrenamed field, since GPIO sampling and "how often to force a full re-report"
    are genuinely different concerns here in a way they aren't for a network-polled protocol
    like BACnet.
  - NEW "gpio.reportOnChange" (default true) makes DI change-triggered publishing optional.
    true (default, previous-equivalent behaviour): a DI edge publishes immediately, AND the
    full DI+DO state is force-republished every "gpio.pollPeriod" regardless of change.
    false: DI changes are never published individually -- only the full state, unconditionally,
    every "gpio.pollPeriod", matching BACnet's own simpler "always report everything on each
    poll cycle" model exactly.
  - "devices" is now a genuine LIST (previously only devices[0] was ever used). Each device
    may include its own "timeseries"/"attributes"/"serverSideRpc" SUBSET lists of keys/method
    -owning channels to scope what that device sees/controls; omitting a subset list for a
    device means "everything". This is what makes duplicating the same physical input to more
    than one ThingsBoard device possible (see test/duplicate_device_fanout_test.py) -- exactly
    the pattern BACnet already supports by listing the same object under two device blocks,
    except here it has to happen inside ONE connector instance/ONE gpiod line request, because
    GPIO chardev lines are exclusively locked: two independent connector processes each calling
    gpiod.request_lines() on the same offset would collide with "Device or resource busy"
    (confirmed the hard way during this project's own install-time troubleshooting -- see the
    migration log). BACnet/Modbus don't have this constraint since they're bus/network
    protocols, not an exclusively-lockable local character device.

This is the built-in "connectors/" variant: installed as part of the thingsboard_gateway
Python package itself (thingsboard_gateway/connectors/bliiot_gpio/), so gpio_map.py (this
variant's name for the sibling hardware-mapping helper module -- called bliiot_gpio_map.py
in the "extensions/" drop-in variant of this same connector) is importable as a normal
package-relative import, no sys.path shim needed. Kept functionally identical to the
extensions/ variant -- see that copy's module docstring for the sys.path-shim rationale
that applies there instead.
"""

import select
import subprocess
from datetime import timedelta
from random import choice
from string import ascii_lowercase
from threading import Event, Lock, Thread
from time import monotonic, sleep

from thingsboard_gateway.connectors.bliiot_gpio.gpio_map import (
    DEFAULT_BOARD_TYPE,
    GpioMapError,
    SAFE_DO_STATE,
    do_state_to_raw,
    find_rp1_gpiochip,
    get_board_offsets,
    raw_to_di_state,
    validate_offsets,
)
from thingsboard_gateway.connectors.connector import Connector
from thingsboard_gateway.tb_utility.tb_loader import TBModuleLoader
from thingsboard_gateway.tb_utility.tb_logger import init_logger
from thingsboard_gateway.tb_utility.tb_utility import TBUtility

try:
    import gpiod
    from gpiod.line import Bias, Direction, Edge
except ImportError:
    print("gpiod library not found - installing...")
    TBUtility.install_package("gpiod")
    import gpiod
    from gpiod.line import Bias, Direction, Edge

DEFAULT_UPLINK_CONVERTER = 'BliiotGpioUplinkConverter'
DEFAULT_DOWNLINK_CONVERTER = 'BliiotGpioDownlinkConverter'

_BIAS_MAP = {
    'as-is': Bias.AS_IS,
    'disabled': Bias.DISABLED,
    'pull-up': Bias.PULL_UP,
    'pull-down': Bias.PULL_DOWN,
}

_DO_ACTIONS = ('set', 'get', 'toggle')


class BliiotGpioConnector(Connector, Thread):

    def __init__(self, gateway, config, connector_type):
        Thread.__init__(self)
        self.daemon = True
        self.name = config.get('name',
                                'BLIIOT GPIO Connector ' + ''.join(choice(ascii_lowercase) for _ in range(5)))

        self.__config = config
        self.__id = config.get('id')
        self._connector_type = connector_type
        self.__gateway = gateway
        self.statistics = {'MessagesReceived': 0, 'MessagesSent': 0}

        log_level = config.get('logLevel', 'INFO')
        remote_logging = config.get('enableRemoteLogging', False)
        self.__log = init_logger(self.__gateway, self.name, log_level,
                                  enable_remote_logging=remote_logging, is_connector_logger=True)
        self.__converter_log = init_logger(self.__gateway, self.name, log_level,
                                            enable_remote_logging=remote_logging,
                                            is_converter_logger=True, attr_name=self.name)

        self.__stopped = Event()
        self._connected = False

        gpio_cfg = config.get('gpio', {})
        self.__chip_cfg = gpio_cfg.get('chip', 'auto')
        self.__chip_path = None
        self.__poll_interval_sec = max(gpio_cfg.get('pollIntervalMs', 200), 20) / 1000.0
        self.__poll_period_sec = gpio_cfg.get('pollPeriod', 60)
        self.__report_on_change = gpio_cfg.get('reportOnChange', True)
        # eventDriven uses libgpiod edge events (via select() on the request's fd) for
        # lower-latency DI updates instead of polling. It is OFF by default: it has not
        # been validated against real hardware yet (unlike the polling path, which only
        # relies on request_lines()/get_values(), the same primitives the project's own
        # confirmed-working test scripts used). Flip it on once it's been checked with
        # bliiot/test/gpio_smoke_test.py against the real box.
        self.__event_driven = gpio_cfg.get('eventDriven', False)
        self.__do_attr_suffix = gpio_cfg.get('doAttributeUpdateSuffix', '_set')

        # "boardType" selects which X-series daughterboard's DI/DO-to-BCM pin map to use
        # (see gpio_map.py's BOARD_PIN_MAPS and its module docstring). Omitting it
        # keeps every config written before this option existed working identically,
        # since it defaults to DEFAULT_BOARD_TYPE ("X26") -- this connector's original,
        # and so far only hardware-confirmed, board.
        board_type = gpio_cfg.get('boardType', DEFAULT_BOARD_TYPE)
        try:
            default_di_offsets, default_do_offsets = get_board_offsets(board_type)
        except GpioMapError as e:
            self.__log.error('[%s] %s', self.name, e)
            raise

        # Confirmed on real hardware 2026-09-03 (see gpio_map.py's module docstring): DI is
        # inverted the same way DO is, so this default is True, not False.
        self.__di_config = self.__build_timeseries_config(config.get('timeseries'), default_di_offsets)
        self.__do_config = self.__build_server_side_rpc_config(config.get('serverSideRpc'), default_do_offsets)
        validate_offsets({key: cfg['offset'] for key, cfg in self.__di_config.items()},
                          {key: cfg['offset'] for key, cfg in self.__do_config.items()})

        self.__di_offset_to_key = {cfg['offset']: key for key, cfg in self.__di_config.items()}
        self.__method_to_channel = self.__build_method_map()

        self.__wan_attr = self.__build_wan_attribute_config(config.get('attributes'))
        self.__last_wan_value = None

        self.__devices = self.__build_devices_config(config.get('devices'))
        # Kept for logging/backward-reference convenience -- the first/primary device.
        self.__device_name = self.__devices[0]['name']
        self.__device_type = self.__devices[0]['type']

        uplink_class_name = self.__devices[0]['converter']
        downlink_class_name = self.__devices[0]['downlink_converter']
        uplink_module = TBModuleLoader.import_module(self._connector_type, uplink_class_name)
        downlink_module = TBModuleLoader.import_module(self._connector_type, downlink_class_name)
        converter_init_config = {'deviceName': self.__device_name, 'deviceType': self.__device_type}

        self.__uplink_converter = None
        if uplink_module and not isinstance(uplink_module, list):
            self.__uplink_converter = uplink_module(converter_init_config, self.__converter_log)
        else:
            self.__log.error('[%s] Failed to load uplink converter "%s"', self.name, uplink_class_name)

        self.__downlink_converter = None
        if downlink_module and not isinstance(downlink_module, list):
            self.__downlink_converter = downlink_module(converter_init_config, self.__converter_log)
        else:
            self.__log.error('[%s] Failed to load downlink converter "%s"', self.name, downlink_class_name)

        self.__di_request = None
        self.__do_request = None
        self.__do_lock = Lock()
        self.__do_state = {key: SAFE_DO_STATE for key in self.__do_config}
        self.__di_state = {}

        self.__poll_thread = None
        self.__di_event_thread = None
        self.__wan_thread = None

    # ------------------------------------------------------------------ lifecycle ----

    def open(self):
        self.__stopped.clear()
        self.start()

    def run(self):
        try:
            self.__init_gpio()
        except Exception as e:
            self.__log.error('[%s] Failed to initialize GPIO, connector will not start: %s', self.name, e)
            self.__stopped.set()
            return

        for device in self.__devices:
            self.__gateway.add_device(device['name'], {'connector': self}, device_type=device['type'])
        self._connected = True
        self.__log.info('[%s] Connected. gpiochip=%s DI=%s DO=%s devices=%s', self.name, self.__chip_path,
                         list(self.__di_config), list(self.__do_config),
                         [d['name'] for d in self.__devices])

        self.__poll_di_once()  # seed self.__di_state before the first full-state publish
        self.__publish_full_state()

        self.__poll_thread = Thread(target=self.__poll_loop, name=f'{self.name} Poll', daemon=True)
        self.__poll_thread.start()

        if self.__event_driven:
            self.__di_event_thread = Thread(target=self.__event_loop, name=f'{self.name} DI Events', daemon=True)
            self.__di_event_thread.start()

        if self.__wan_attr is not None:
            self.__wan_thread = Thread(target=self.__wan_loop, name=f'{self.name} WAN Status', daemon=True)
            self.__wan_thread.start()

        while not self.__stopped.is_set():
            sleep(0.5)

    def close(self):
        self.__stopped.set()
        self._connected = False
        try:
            if self.__do_request is not None:
                self.__force_all_do_safe(log_prefix='Shutdown')
        except Exception as e:
            self.__log.exception('[%s] Error forcing DO channels safe on shutdown: %s', self.name, e)
        finally:
            self.__release_gpio()
        self.__log.info('[%s] connector has been stopped.', self.name)
        self.__log.stop()

    def get_id(self):
        return self.__id

    def get_name(self):
        return self.name

    def get_type(self):
        return self._connector_type

    def get_config(self):
        return self.__config

    def is_connected(self):
        return self._connected

    def is_stopped(self):
        return self.__stopped.is_set()

    # ------------------------------------------------------------------- gpio init ---

    def __build_timeseries_config(self, entries, defaults):
        """Parse the top-level "timeseries" list (DI channels) into {key: {offset,
        activeLow, bias, debounceMs}}. Any of the selected board's standard channel
        keys (DI1, DI2, ...) may be omitted from "timeseries" entirely and still gets
        its board-default offset; only a genuinely custom key needs an explicit "offset"."""
        entries = entries or []
        by_key = {}
        for entry in entries:
            key = entry.get('key')
            if not key:
                self.__log.error('[%s] "timeseries" entry with no "key", skipping: %s', self.name, entry)
                continue
            by_key[key] = entry

        result = {}
        for key, default_offset in defaults.items():
            overrides = by_key.pop(key, {})
            result[key] = {
                'offset': overrides.get('offset', default_offset),
                'activeLow': overrides.get('activeLow', True),
                'bias': overrides.get('bias', 'as-is'),
                'debounceMs': overrides.get('debounceMs', 50),
            }
        for key, entry in by_key.items():
            if 'offset' not in entry:
                self.__log.error('[%s] "timeseries" entry "%s" has no "offset" configured, skipping',
                                  self.name, key)
                continue
            result[key] = {
                'offset': entry['offset'],
                'activeLow': entry.get('activeLow', True),
                'bias': entry.get('bias', 'as-is'),
                'debounceMs': entry.get('debounceMs', 50),
            }
        return result

    def __build_server_side_rpc_config(self, entries, defaults):
        """Parse the top-level "serverSideRpc" list (DO channels) into {key: {offset,
        activeLow, setMethod, getMethod, toggleMethod}}. Same offset-optional rule as
        "timeseries" for the board's standard DO keys. Method names default to
        "set<key>"/"get<key>"/"toggle<key>" (e.g. "setDO1") and can be overridden per
        entry with explicit "setMethod"/"getMethod"/"toggleMethod" fields."""
        entries = entries or []
        by_key = {}
        for entry in entries:
            key = entry.get('key')
            if not key:
                self.__log.error('[%s] "serverSideRpc" entry with no "key", skipping: %s', self.name, entry)
                continue
            by_key[key] = entry

        result = {}
        for key, default_offset in defaults.items():
            overrides = by_key.pop(key, {})
            result[key] = self.__server_side_rpc_entry(key, overrides.get('offset', default_offset), overrides)
        for key, entry in by_key.items():
            if 'offset' not in entry:
                self.__log.error('[%s] "serverSideRpc" entry "%s" has no "offset" configured, skipping',
                                  self.name, key)
                continue
            result[key] = self.__server_side_rpc_entry(key, entry['offset'], entry)
        return result

    @staticmethod
    def __server_side_rpc_entry(key, offset, overrides):
        return {
            'offset': offset,
            'activeLow': overrides.get('activeLow', True),
            'setMethod': overrides.get('setMethod', f'set{key}'),
            'getMethod': overrides.get('getMethod', f'get{key}'),
            'toggleMethod': overrides.get('toggleMethod', f'toggle{key}'),
        }

    def __build_method_map(self):
        """Maps every configured RPC method name (per-channel, from "serverSideRpc") to
        (action, channel_key). Raises ValueError on a collision -- e.g. two DO channels
        both ending up with the same method name after an override -- caught at startup
        rather than discovered later as one channel silently shadowing another's RPC."""
        method_map = {}
        for key, cfg in self.__do_config.items():
            for action in _DO_ACTIONS:
                method = cfg[f'{action}Method']
                if method in method_map:
                    other_action, other_key = method_map[method]
                    raise ValueError(f'RPC method "{method}" is configured for both "{other_key}" ({other_action}) '
                                      f'and "{key}" ({action}) -- each method name must be unique. Set an explicit '
                                      f'"{action}Method" override on one of them.')
                method_map[method] = (action, key)
        return method_map

    def __build_wan_attribute_config(self, entries):
        """Parse the top-level "attributes" list for the (at most one) entry with
        "source": "wanStatus". Returns None if WAN-status reporting isn't configured at
        all (no such entry) -- this is what enables/disables the feature now, there is
        no separate "enabled" flag."""
        for entry in (entries or []):
            if entry.get('source') != 'wanStatus':
                continue
            return {
                'key': entry.get('key', 'active_wan_interface'),
                'pollPeriod': entry.get('pollPeriod', 15),
                'interfaceNames': entry.get('interfaceNames', {'eth0': 'ethernet', 'usb0': 'cellular'}),
            }
        return None

    def __build_devices_config(self, entries):
        """Parse the "devices" list. Each device may be given its own "timeseries"/
        "attributes"/"serverSideRpc" SUBSET lists (of keys/channel-keys) to scope what
        that device sees/controls -- omitting a subset list for a device means "all
        configured keys of that kind". This is what lets the same physical DI (or DO,
        or the WAN attribute) be duplicated to more than one ThingsBoard device: list it
        (unfiltered, or explicitly included) under two device entries here. Always
        returns at least one device (a single default one if "devices" is entirely
        absent), and the first entry is treated as "primary" for logging/converter
        class selection purposes."""
        entries = entries or [{}]
        if not entries:
            entries = [{}]
        all_ts_keys = set(self.__di_config) | set(self.__do_config)
        all_attr_keys = {self.__wan_attr['key']} if self.__wan_attr else set()
        all_rpc_keys = set(self.__do_config)

        devices = []
        seen_names = set()
        for i, entry in enumerate(entries):
            name = entry.get('name', self.__config.get('deviceName', 'BLIIOT X26 IO') if i == 0
                              else f'BLIIOT X26 IO {i + 1}')
            if name in seen_names:
                raise ValueError(f'Duplicate device name "{name}" in "devices" -- each device needs a unique name.')
            seen_names.add(name)

            ts_subset = entry.get('timeseries')
            attr_subset = entry.get('attributes')
            rpc_subset = entry.get('serverSideRpc')

            unknown_ts = set(ts_subset or []) - all_ts_keys
            if unknown_ts:
                self.__log.warning('[%s] Device "%s" lists unknown "timeseries" key(s) %s (not configured in the '
                                    'top-level "timeseries"/"serverSideRpc" lists)', self.name, name, sorted(unknown_ts))
            unknown_attr = set(attr_subset or []) - all_attr_keys
            if unknown_attr:
                self.__log.warning('[%s] Device "%s" lists unknown "attributes" key(s) %s', self.name, name,
                                    sorted(unknown_attr))
            unknown_rpc = set(rpc_subset or []) - all_rpc_keys
            if unknown_rpc:
                self.__log.warning('[%s] Device "%s" lists unknown "serverSideRpc" key(s) %s', self.name, name,
                                    sorted(unknown_rpc))

            devices.append({
                'name': name,
                'type': entry.get('type', 'default'),
                'converter': entry.get('converter', DEFAULT_UPLINK_CONVERTER),
                'downlink_converter': entry.get('downlink_converter', DEFAULT_DOWNLINK_CONVERTER),
                # None = "no restriction, everything of this kind" -- a set (possibly
                # empty) means "exactly this subset".
                'timeseries': set(ts_subset) if ts_subset is not None else None,
                'attributes': set(attr_subset) if attr_subset is not None else None,
                'serverSideRpc': set(rpc_subset) if rpc_subset is not None else None,
            })
        return devices

    def __init_gpio(self):
        self.__chip_path = find_rp1_gpiochip(self.__chip_cfg)

        di_line_settings = {}
        for key, cfg in self.__di_config.items():
            di_line_settings[cfg['offset']] = gpiod.LineSettings(
                direction=Direction.INPUT,
                bias=_BIAS_MAP.get(cfg.get('bias', 'as-is'), Bias.AS_IS),
                edge_detection=Edge.BOTH if self.__event_driven else Edge.NONE,
                debounce_period=timedelta(milliseconds=cfg.get('debounceMs', 50)),
            )
        self.__di_request = gpiod.request_lines(self.__chip_path, consumer='bliiot-gpio-connector-di',
                                                  config=di_line_settings)

        do_line_settings = {}
        for key, cfg in self.__do_config.items():
            do_line_settings[cfg['offset']] = gpiod.LineSettings(
                direction=Direction.OUTPUT,
                output_value=do_state_to_raw(SAFE_DO_STATE, active_low=cfg.get('activeLow', True)),
            )
        self.__do_request = gpiod.request_lines(self.__chip_path, consumer='bliiot-gpio-connector-do',
                                                  config=do_line_settings)

        # Belt-and-braces: explicitly re-assert the safe state right after the request
        # comes up, rather than relying solely on request_lines()'s initial output_value
        # -- see gpio_map.py's module docstring re: confirmed "sticky output" behaviour.
        self.__force_all_do_safe(log_prefix='Startup')

    def __release_gpio(self):
        for request in (self.__di_request, self.__do_request):
            if request is not None:
                try:
                    request.release()
                except Exception:
                    pass
        self.__di_request = None
        self.__do_request = None

    # --------------------------------------------------------------------- DI read ---

    def __poll_di_once(self):
        offsets = list(self.__di_offset_to_key.keys())
        if not offsets:
            return {}
        raw_values = self.__di_request.get_values(offsets)
        changes = {}
        for offset, raw_value in zip(offsets, raw_values):
            key = self.__di_offset_to_key[offset]
            active_low = self.__di_config[key].get('activeLow', True)
            state = raw_to_di_state(raw_value, active_low=active_low)
            if self.__di_state.get(key) != state:
                self.__di_state[key] = state
                changes[key] = state
        return changes

    def __poll_loop(self):
        last_publish = monotonic()
        while not self.__stopped.is_set():
            try:
                changes = self.__poll_di_once()
                if self.__report_on_change and changes:
                    self.__fan_out_telemetry(changes)
                now = monotonic()
                if now - last_publish >= self.__poll_period_sec:
                    self.__publish_full_state()
                    last_publish = now
            except Exception as e:
                self.__log.exception('[%s] Error while polling DI: %s', self.name, e)
            self.__stopped.wait(self.__poll_interval_sec)

    def __event_loop(self):
        """Experimental low-latency alternative to polling. See the note on
        self.__event_driven in __init__ before enabling this in production. Honours
        "gpio.reportOnChange" the same way the poll loop does -- if it's false, edge
        events still update self.__di_state (so the next periodic full publish is
        accurate) but don't trigger an immediate publish of their own."""
        try:
            fd = self.__di_request.fd
        except AttributeError:
            self.__log.warning('[%s] gpio.eventDriven=true but this gpiod version exposes no request.fd; '
                                'continuing with poll-only DI monitoring instead.', self.name)
            return
        while not self.__stopped.is_set():
            try:
                ready, _, _ = select.select([fd], [], [], 1.0)
                if not ready:
                    continue
                events = self.__di_request.read_edge_events()
                changes = {}
                for event in events:
                    key = self.__di_offset_to_key.get(event.line_offset)
                    if key is None:
                        continue
                    active_low = self.__di_config[key].get('activeLow', True)
                    is_rising = event.event_type == gpiod.EdgeEvent.Type.RISING_EDGE
                    raw_value = gpiod.line.Value.ACTIVE if is_rising else gpiod.line.Value.INACTIVE
                    state = raw_to_di_state(raw_value, active_low=active_low)
                    if self.__di_state.get(key) != state:
                        self.__di_state[key] = state
                        changes[key] = state
                if changes and self.__report_on_change:
                    self.__fan_out_telemetry(changes)
            except Exception as e:
                self.__log.exception('[%s] Error in DI edge-event loop (experimental -- set gpio.eventDriven=false '
                                      'to fall back to the validated polling path): %s', self.name, e)
                sleep(1)

    # -------------------------------------------------------------------- DO write ---

    def __write_do(self, channel, requested_state):
        if self.__downlink_converter is None:
            return False, None, 'Downlink converter not loaded'
        converter_config = {key: {'offset': cfg['offset'], 'activeLow': cfg.get('activeLow', True)}
                             for key, cfg in self.__do_config.items()}
        converted = self.__downlink_converter.convert(converter_config, {'channel': channel, 'state': requested_state})
        if converted is None:
            return False, None, f'Unknown DO channel "{channel}" or invalid request'

        with self.__do_lock:
            try:
                self.__do_request.set_values({converted['offset']: converted['value']})
            except Exception as e:
                self.__log.exception('[%s] Failed writing DO %s: %s', self.name, channel, e)
                return False, None, str(e)
            self.__do_state[channel] = converted['state']

        self.__fan_out_telemetry({channel: converted['state']})
        return True, converted['state'], 'ok'

    def __force_all_do_safe(self, log_prefix=''):
        with self.__do_lock:
            values = {}
            for key, cfg in self.__do_config.items():
                values[cfg['offset']] = do_state_to_raw(SAFE_DO_STATE, active_low=cfg.get('activeLow', True))
                self.__do_state[key] = SAFE_DO_STATE
            if self.__do_request is not None:
                self.__do_request.set_values(values)
        self.__log.info('[%s] %s: forced all DO channels to safe/inactive (OFF) state', self.name,
                         log_prefix or 'Safety')

    # ------------------------------------------------------------------ telemetry ----

    def __fan_out_telemetry(self, telemetry):
        """Publish `telemetry` ({key: state}) to every device whose "timeseries" subset
        includes each key (or has no subset restriction at all) -- the mechanism that
        lets the same physical DI/DO be duplicated to more than one ThingsBoard device."""
        if not telemetry or self.__uplink_converter is None:
            return
        for device in self.__devices:
            subset = device['timeseries']
            entry = telemetry if subset is None else {k: v for k, v in telemetry.items() if k in subset}
            if not entry:
                continue
            converted_data = self.__uplink_converter.convert(
                {'deviceName': device['name'], 'deviceType': device['type']},
                {'telemetry': entry})
            self.statistics['MessagesReceived'] += 1
            if converted_data and converted_data.telemetry_datapoints_count > 0:
                self.__gateway.send_to_storage(self.get_name(), self.get_id(), converted_data)
                self.statistics['MessagesSent'] += 1

    def __publish_full_state(self):
        snapshot = dict(self.__di_state)
        with self.__do_lock:
            snapshot.update(self.__do_state)
        self.__fan_out_telemetry(snapshot)

    # ------------------------------------------------------------------ WAN status ---

    def __fan_out_wan_attribute(self, value):
        if self.__uplink_converter is None or self.__wan_attr is None:
            return
        key = self.__wan_attr['key']
        for device in self.__devices:
            subset = device['attributes']
            if subset is not None and key not in subset:
                continue
            converted_data = self.__uplink_converter.convert(
                {'deviceName': device['name'], 'deviceType': device['type']},
                {'attributes': {key: value}})
            if converted_data and converted_data.attributes_datapoints_count > 0:
                self.__gateway.send_to_storage(self.get_name(), self.get_id(), converted_data)

    def __wan_loop(self):
        # NOTE: this attribute is pushed to the platform only when we (re)send it here --
        # unlike DI/DO telemetry, ThingsBoard has no way to "pull" it. If the platform-side
        # attribute is ever lost independently of the interface actually changing (the
        # gateway device being deleted and recreated on the platform, the attribute being
        # cleared by hand, a fresh device provisioned for the first time after this
        # connector has already been running, etc.), a pure on-change publish would never
        # notice and the attribute would just stay blank/stale until the connector process
        # restarts and __last_wan_value re-initialises to None. Confirmed live on
        # Kelvin26001 (2026-09-03): Harv deleted the "BLIIOT X26 IO" device on the platform
        # while the interface value hadn't changed since, active_wan_interface never came
        # back, and restarting thingsboard-gateway was the only thing that fixed it.
        # Fixed by also forcing an unconditional resend every this attribute's own
        # "pollPeriod", independent of whether the value changed, so the platform is
        # guaranteed to catch up within one interval even if it lost the attribute for a
        # reason this connector can't detect.
        wan_poll_period_sec = self.__wan_attr['pollPeriod']
        interface_names = self.__wan_attr['interfaceNames']
        last_heartbeat = monotonic()
        self.__log.debug('[%s] WAN status loop starting: pollPeriod=%s', self.name, wan_poll_period_sec)
        while not self.__stopped.is_set():
            try:
                interface = self.__detect_default_route_interface()
                friendly = interface_names.get(interface, interface) if interface else 'unknown'
                now = monotonic()
                changed = friendly != self.__last_wan_value
                due_for_heartbeat = (now - last_heartbeat) >= wan_poll_period_sec
                self.__log.debug('[%s] WAN poll tick: interface=%s friendly=%s last=%s changed=%s '
                                  'due_for_heartbeat=%s', self.name, interface, friendly,
                                  self.__last_wan_value, changed, due_for_heartbeat)
                if changed or due_for_heartbeat:
                    self.__last_wan_value = friendly
                    last_heartbeat = now
                    self.__fan_out_wan_attribute(friendly)
                    if changed:
                        self.__log.info('[%s] Active WAN interface changed to "%s" (%s)',
                                         self.name, friendly, interface)
                    else:
                        self.__log.debug('[%s] WAN interface heartbeat resend: "%s" (%s)',
                                          self.name, friendly, interface)
            except Exception as e:
                self.__log.exception('[%s] Error while checking WAN status: %s', self.name, e)
            self.__stopped.wait(wan_poll_period_sec)

    @staticmethod
    def __detect_default_route_interface():
        try:
            result = subprocess.run(['ip', 'route', 'show', 'default'],
                                     capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0 or not result.stdout.strip():
            return None
        # `ip route show default` lists routes in ascending route-metric order, so the
        # first line is always the interface currently holding the default route.
        first_line = result.stdout.strip().splitlines()[0].split()
        if 'dev' not in first_line:
            return None
        try:
            return first_line[first_line.index('dev') + 1]
        except (ValueError, IndexError):
            return None

    # --------------------------------------------------------------- ThingsBoard IO --

    def __device_by_name(self, name):
        for device in self.__devices:
            if device['name'] == name:
                return device
        return None

    def on_attributes_update(self, content):
        try:
            device_name = content.get('device')
            device = self.__device_by_name(device_name)
            if device is None:
                return
            for attr_name, value in (content.get('data') or {}).items():
                if not attr_name.endswith(self.__do_attr_suffix):
                    continue
                channel = attr_name[:-len(self.__do_attr_suffix)]
                if channel not in self.__do_config:
                    self.__log.warning('[%s] Shared attribute "%s" looks like a DO control attribute but "%s" is '
                                        'not a configured DO channel', self.name, attr_name, channel)
                    continue
                if device['serverSideRpc'] is not None and channel not in device['serverSideRpc']:
                    self.__log.warning('[%s] Device "%s" is not configured to control channel "%s" -- ignoring '
                                        'shared attribute "%s"', self.name, device_name, channel, attr_name)
                    continue
                success, _new_state, message = self.__write_do(channel, bool(value))
                if not success:
                    self.__log.error('[%s] Failed to apply shared attribute "%s"=%s: %s',
                                      self.name, attr_name, value, message)
        except Exception as e:
            self.__log.exception('[%s] Error handling attribute update: %s', self.name, e)

    def server_side_rpc_handler(self, content):
        try:
            device_name = content.get('device')
            device = self.__device_by_name(device_name)
            data = content.get('data') or {}
            req_id = data.get('id')
            method = data.get('method')
            params = data.get('params')
            if not isinstance(params, dict):
                params = {}

            if device is None:
                self.__reply(device_name, req_id, {'success': False, 'error': f'Unknown device "{device_name}"'})
                return

            if method == 'setAllDo':
                state = bool(params.get('state', False))
                allowed = self.__do_config if device['serverSideRpc'] is None else \
                    (k for k in self.__do_config if k in device['serverSideRpc'])
                results, overall = {}, True
                for channel in list(allowed):
                    success, new_state, _message = self.__write_do(channel, state)
                    results[channel] = new_state if success else None
                    overall = overall and success
                self.__reply(device_name, req_id, {'success': overall, 'channels': results})
                return

            if method == 'getDi':
                channel = params.get('channel')
                allowed = device['timeseries']
                with self.__do_lock:
                    pass  # no DO lock needed for DI, just keeping the block shape consistent
                if channel:
                    visible = allowed is None or channel in allowed
                    self.__reply(device_name, req_id, {'success': visible and channel in self.__di_state,
                                                         'channel': channel,
                                                         'state': self.__di_state.get(channel) if visible else None})
                else:
                    channels = self.__di_state if allowed is None else \
                        {k: v for k, v in self.__di_state.items() if k in allowed}
                    self.__reply(device_name, req_id, {'success': True, 'channels': dict(channels)})
                return

            if method == 'getWanStatus':
                if self.__wan_attr is None:
                    self.__reply(device_name, req_id, {'success': False, 'error': 'WAN status reporting is not configured'})
                    return
                key = self.__wan_attr['key']
                if device['attributes'] is not None and key not in device['attributes']:
                    self.__reply(device_name, req_id, {'success': False,
                                                         'error': f'Device "{device_name}" is not configured for "{key}"'})
                    return
                self.__reply(device_name, req_id, {'success': True, key: self.__last_wan_value})
                return

            channel_mapping = self.__method_to_channel.get(method)
            if channel_mapping is None:
                self.__log.warning('[%s] Unknown RPC method "%s"', self.name, method)
                self.__reply(device_name, req_id, {'success': False, 'error': f'Unknown method "{method}"'})
                return

            action, channel = channel_mapping
            if device['serverSideRpc'] is not None and channel not in device['serverSideRpc']:
                self.__reply(device_name, req_id,
                             {'success': False, 'error': f'Device "{device_name}" is not configured to control "{channel}"'})
                return

            if action == 'set':
                state = params.get('state')
                if state is None:
                    self.__reply(device_name, req_id, {'success': False, 'error': 'params must include "state"'})
                    return
                success, new_state, message = self.__write_do(channel, bool(state))
                self.__reply(device_name, req_id,
                             {'success': success, 'channel': channel, 'state': new_state, 'message': message})
            elif action == 'get':
                with self.__do_lock:
                    self.__reply(device_name, req_id, {'success': channel in self.__do_state,
                                                         'channel': channel, 'state': self.__do_state.get(channel)})
            elif action == 'toggle':
                with self.__do_lock:
                    current = self.__do_state.get(channel, SAFE_DO_STATE)
                success, new_state, message = self.__write_do(channel, not current)
                self.__reply(device_name, req_id,
                             {'success': success, 'channel': channel, 'state': new_state, 'message': message})
        except Exception as e:
            self.__log.exception('[%s] Error handling RPC request: %s', self.name, e)

    def __reply(self, device, req_id, content):
        if req_id is None:
            return
        try:
            self.__gateway.send_rpc_reply(device, req_id, content)
        except Exception as e:
            self.__log.exception('[%s] Failed to send RPC reply: %s', self.name, e)
