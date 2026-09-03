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
Custom connector for the BLIIOT BL460AL-CM5002016-X26 IO board (8x DI, 4x DO), running
under stock Raspberry Pi OS via libgpiod v2 against the RP1 gpiochip -- no vendor driver,
no I2C/SPI expander involved. See gpio_map.py's module docstring for the confirmed
hardware facts this connector relies on (chip index instability across reflashes, DI/DO
polarity inversion, DO "sticky output" behaviour).

Also reports which WAN path (Ethernet vs the onboard 4G modem) currently holds the
default route as a device attribute, since the box is deployed with Ethernet-primary /
cellular-backup failover and that should be visible from ThingsBoard, not just by SSHing
into the box.
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
        self.__heartbeat_interval_sec = gpio_cfg.get('heartbeatIntervalSec', 60)
        # eventDriven uses libgpiod edge events (via select() on the request's fd) for
        # lower-latency DI updates instead of polling. It is OFF by default: it has not
        # been validated against real hardware yet (unlike the polling path, which only
        # relies on request_lines()/get_values(), the same primitives the project's own
        # confirmed-working test scripts used). Flip it on once it's been checked with
        # bliiot/test/gpio_smoke_test.py against the real box.
        self.__event_driven = gpio_cfg.get('eventDriven', False)
        self.__do_attr_suffix = gpio_cfg.get('doAttributeUpdateSuffix', '_set')

        # "boardType" selects which X-series daughterboard's DI/DO-to-BCM pin map to use
        # (see gpio_map.py's BOARD_PIN_MAPS and its module docstring). Omitting it keeps
        # every config written before this option existed working identically, since it
        # defaults to DEFAULT_BOARD_TYPE ("X26") -- this connector's original, and so far
        # only hardware-confirmed, board.
        board_type = gpio_cfg.get('boardType', DEFAULT_BOARD_TYPE)
        try:
            default_di_offsets, default_do_offsets = get_board_offsets(board_type)
        except GpioMapError as e:
            self.__log.error('[%s] %s', self.name, e)
            raise

        # Confirmed on real hardware 2026-09-03 (see gpio_map.py's module docstring): DI is
        # inverted the same way DO is, so this default is True, not False.
        self.__di_config = self.__build_channel_config(gpio_cfg.get('digitalInputs'), default_di_offsets,
                                                         default_active_low=True)
        self.__do_config = self.__build_channel_config(gpio_cfg.get('digitalOutputs'), default_do_offsets,
                                                         default_active_low=True)
        validate_offsets({name: cfg['offset'] for name, cfg in self.__di_config.items()},
                          {name: cfg['offset'] for name, cfg in self.__do_config.items()})
        self.__telemetry_key_by_name = self.__build_telemetry_key_map()

        self.__di_offset_to_name = {cfg['offset']: name for name, cfg in self.__di_config.items()}

        wan_cfg = config.get('wanStatus', {})
        self.__wan_enabled = wan_cfg.get('enabled', True)
        self.__wan_poll_interval_sec = wan_cfg.get('pollIntervalSec', 15)
        self.__wan_interface_names = wan_cfg.get('interfaceNames', {'eth0': 'ethernet', 'usb0': 'cellular'})
        self.__wan_attribute_key = wan_cfg.get('attributeKey', 'active_wan_interface')
        self.__last_wan_value = None

        devices_cfg = config.get('devices') or [{}]
        device_cfg = devices_cfg[0]
        self.__device_name = device_cfg.get('name', config.get('deviceName', 'BLIIOT X26 IO'))
        self.__device_type = device_cfg.get('type', 'default')

        uplink_class_name = device_cfg.get('converter', DEFAULT_UPLINK_CONVERTER)
        downlink_class_name = device_cfg.get('downlink_converter', DEFAULT_DOWNLINK_CONVERTER)
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
        self.__do_state = {name: SAFE_DO_STATE for name in self.__do_config}
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

        self.__gateway.add_device(self.__device_name, {'connector': self}, device_type=self.__device_type)
        self._connected = True
        self.__log.info('[%s] Connected. gpiochip=%s DI=%s DO=%s', self.name, self.__chip_path,
                         list(self.__di_config), list(self.__do_config))

        self.__poll_di_once()  # seed self.__di_state before the first full-state publish
        self.__publish_full_state()

        self.__poll_thread = Thread(target=self.__poll_loop, name=f'{self.name} Poll', daemon=True)
        self.__poll_thread.start()

        if self.__event_driven:
            self.__di_event_thread = Thread(target=self.__event_loop, name=f'{self.name} DI Events', daemon=True)
            self.__di_event_thread.start()

        if self.__wan_enabled:
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

    def __build_channel_config(self, user_cfg, defaults, default_active_low):
        user_cfg = user_cfg or {}
        result = {}
        for name, default_offset in defaults.items():
            overrides = user_cfg.get(name, {})
            result[name] = {
                'offset': overrides.get('offset', default_offset),
                'activeLow': overrides.get('activeLow', default_active_low),
                'bias': overrides.get('bias', 'as-is'),
                'debounceMs': overrides.get('debounceMs', 50),
                'key': overrides.get('key', name),
            }
        for name, overrides in user_cfg.items():
            if name in result:
                continue
            if 'offset' not in overrides:
                self.__log.error('[%s] Channel "%s" has no "offset" configured, skipping', self.name, name)
                continue
            result[name] = {
                'offset': overrides['offset'],
                'activeLow': overrides.get('activeLow', default_active_low),
                'bias': overrides.get('bias', 'as-is'),
                'debounceMs': overrides.get('debounceMs', 50),
                'key': overrides.get('key', name),
            }
        return result

    def __build_telemetry_key_map(self):
        """Maps each channel's internal name (e.g. "DI1", used for RPC/attribute
        addressing and never renamed) to the telemetry key it should be published
        under -- the optional per-channel "key" config field if set, else the
        internal name itself, unchanged. Raises ValueError on a duplicate key
        (two channels -- DI, DO, or a mix -- mapped to the same telemetry key would
        silently overwrite each other in ThingsBoard, so this is caught at startup
        rather than discovered later on a dashboard)."""
        mapping = {}
        seen_keys = {}
        for name, cfg in {**self.__di_config, **self.__do_config}.items():
            key = cfg.get('key', name)
            if key in seen_keys:
                raise ValueError(f'Channels "{seen_keys[key]}" and "{name}" are both configured with '
                                  f'telemetry key "{key}" -- each channel needs a unique "key"')
            seen_keys[key] = name
            mapping[name] = key
        return mapping

    def __init_gpio(self):
        self.__chip_path = find_rp1_gpiochip(self.__chip_cfg)

        di_line_settings = {}
        for name, cfg in self.__di_config.items():
            di_line_settings[cfg['offset']] = gpiod.LineSettings(
                direction=Direction.INPUT,
                bias=_BIAS_MAP.get(cfg.get('bias', 'as-is'), Bias.AS_IS),
                edge_detection=Edge.BOTH if self.__event_driven else Edge.NONE,
                debounce_period=timedelta(milliseconds=cfg.get('debounceMs', 50)),
            )
        self.__di_request = gpiod.request_lines(self.__chip_path, consumer='bliiot-gpio-connector-di',
                                                  config=di_line_settings)

        do_line_settings = {}
        for name, cfg in self.__do_config.items():
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
        offsets = list(self.__di_offset_to_name.keys())
        if not offsets:
            return {}
        raw_values = self.__di_request.get_values(offsets)
        changes = {}
        for offset, raw_value in zip(offsets, raw_values):
            name = self.__di_offset_to_name[offset]
            active_low = self.__di_config[name].get('activeLow', True)
            state = raw_to_di_state(raw_value, active_low=active_low)
            if self.__di_state.get(name) != state:
                self.__di_state[name] = state
                changes[name] = state
        return changes

    def __poll_loop(self):
        last_heartbeat = monotonic()
        while not self.__stopped.is_set():
            try:
                changes = self.__poll_di_once()
                if changes:
                    self.__send_telemetry(changes)
                now = monotonic()
                if now - last_heartbeat >= self.__heartbeat_interval_sec:
                    self.__publish_full_state()
                    last_heartbeat = now
            except Exception as e:
                self.__log.exception('[%s] Error while polling DI: %s', self.name, e)
            self.__stopped.wait(self.__poll_interval_sec)

    def __event_loop(self):
        """Experimental low-latency alternative to polling. See the note on
        self.__event_driven in __init__ before enabling this in production."""
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
                    name = self.__di_offset_to_name.get(event.line_offset)
                    if name is None:
                        continue
                    active_low = self.__di_config[name].get('activeLow', True)
                    is_rising = event.event_type == gpiod.EdgeEvent.Type.RISING_EDGE
                    raw_value = gpiod.line.Value.ACTIVE if is_rising else gpiod.line.Value.INACTIVE
                    state = raw_to_di_state(raw_value, active_low=active_low)
                    if self.__di_state.get(name) != state:
                        self.__di_state[name] = state
                        changes[name] = state
                if changes:
                    self.__send_telemetry(changes)
            except Exception as e:
                self.__log.exception('[%s] Error in DI edge-event loop (experimental -- set gpio.eventDriven=false '
                                      'to fall back to the validated polling path): %s', self.name, e)
                sleep(1)

    # -------------------------------------------------------------------- DO write ---

    def __write_do(self, channel, requested_state):
        if self.__downlink_converter is None:
            return False, None, 'Downlink converter not loaded'
        converter_config = {name: {'offset': cfg['offset'], 'activeLow': cfg.get('activeLow', True)}
                             for name, cfg in self.__do_config.items()}
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

        self.__send_telemetry({channel: converted['state']})
        return True, converted['state'], 'ok'

    def __force_all_do_safe(self, log_prefix=''):
        with self.__do_lock:
            values = {}
            for name, cfg in self.__do_config.items():
                values[cfg['offset']] = do_state_to_raw(SAFE_DO_STATE, active_low=cfg.get('activeLow', True))
                self.__do_state[name] = SAFE_DO_STATE
            if self.__do_request is not None:
                self.__do_request.set_values(values)
        self.__log.info('[%s] %s: forced all DO channels to safe/inactive (OFF) state', self.name,
                         log_prefix or 'Safety')

    # ------------------------------------------------------------------ telemetry ----

    def __send_telemetry(self, telemetry):
        if not telemetry or self.__uplink_converter is None:
            return
        # Internal channel names (DI1/DO1/...) never change -- they're what RPC calls and
        # the <channel>_set shared attributes address. Only the outgoing telemetry key is
        # ever renamed, via each channel's optional "key" config field (see
        # __build_telemetry_key_map()); a channel with no "key" set publishes under its
        # internal name unchanged, so existing configs/dashboards keep working as-is.
        telemetry = {self.__telemetry_key_by_name.get(name, name): state for name, state in telemetry.items()}
        converted_data = self.__uplink_converter.convert(
            {'deviceName': self.__device_name, 'deviceType': self.__device_type},
            {'telemetry': telemetry})
        self.statistics['MessagesReceived'] += 1
        if converted_data and converted_data.telemetry_datapoints_count > 0:
            self.__gateway.send_to_storage(self.get_name(), self.get_id(), converted_data)
            self.statistics['MessagesSent'] += 1

    def __publish_full_state(self):
        snapshot = dict(self.__di_state)
        with self.__do_lock:
            snapshot.update(self.__do_state)
        self.__send_telemetry(snapshot)

    # ------------------------------------------------------------------ WAN status ---

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
        # Fixed the same way DI/DO already handle this (see __poll_loop's heartbeat): also
        # force an unconditional resend every heartbeatIntervalSec, independent of whether
        # the value changed, so the platform is guaranteed to catch up within one heartbeat
        # even if it lost the attribute for a reason this connector can't detect.
        last_heartbeat = monotonic()
        while not self.__stopped.is_set():
            try:
                interface = self.__detect_default_route_interface()
                friendly = self.__wan_interface_names.get(interface, interface) if interface else 'unknown'
                now = monotonic()
                changed = friendly != self.__last_wan_value
                due_for_heartbeat = (now - last_heartbeat) >= self.__heartbeat_interval_sec
                if changed or due_for_heartbeat:
                    self.__last_wan_value = friendly
                    last_heartbeat = now
                    converted_data = self.__uplink_converter.convert(
                        {'deviceName': self.__device_name, 'deviceType': self.__device_type},
                        {'attributes': {self.__wan_attribute_key: friendly}}) if self.__uplink_converter else None
                    if converted_data and converted_data.attributes_datapoints_count > 0:
                        self.__gateway.send_to_storage(self.get_name(), self.get_id(), converted_data)
                        if changed:
                            self.__log.info('[%s] Active WAN interface changed to "%s" (%s)',
                                             self.name, friendly, interface)
                        else:
                            self.__log.debug('[%s] WAN interface heartbeat resend: "%s" (%s)',
                                              self.name, friendly, interface)
            except Exception as e:
                self.__log.exception('[%s] Error while checking WAN status: %s', self.name, e)
            self.__stopped.wait(self.__wan_poll_interval_sec)

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

    def on_attributes_update(self, content):
        try:
            if content.get('device') != self.__device_name:
                return
            for attr_name, value in (content.get('data') or {}).items():
                if not attr_name.endswith(self.__do_attr_suffix):
                    continue
                channel = attr_name[:-len(self.__do_attr_suffix)]
                if channel not in self.__do_config:
                    self.__log.warning('[%s] Shared attribute "%s" looks like a DO control attribute but "%s" is '
                                        'not a configured DO channel', self.name, attr_name, channel)
                    continue
                success, _new_state, message = self.__write_do(channel, bool(value))
                if not success:
                    self.__log.error('[%s] Failed to apply shared attribute "%s"=%s: %s',
                                      self.name, attr_name, value, message)
        except Exception as e:
            self.__log.exception('[%s] Error handling attribute update: %s', self.name, e)

    def server_side_rpc_handler(self, content):
        try:
            device = content.get('device')
            data = content.get('data') or {}
            req_id = data.get('id')
            method = data.get('method')
            params = data.get('params')
            if not isinstance(params, dict):
                params = {}

            if method == 'setDo':
                channel, state = params.get('channel'), params.get('state')
                if channel is None or state is None:
                    self.__reply(device, req_id,
                                 {'success': False, 'error': 'params must include "channel" and "state"'})
                    return
                success, new_state, message = self.__write_do(channel, bool(state))
                self.__reply(device, req_id,
                             {'success': success, 'channel': channel, 'state': new_state, 'message': message})

            elif method == 'setAllDo':
                state = bool(params.get('state', False))
                results, overall = {}, True
                for channel in self.__do_config:
                    success, new_state, _message = self.__write_do(channel, state)
                    results[channel] = new_state if success else None
                    overall = overall and success
                self.__reply(device, req_id, {'success': overall, 'channels': results})

            elif method == 'getDo':
                channel = params.get('channel')
                with self.__do_lock:
                    if channel:
                        self.__reply(device, req_id, {'success': channel in self.__do_state,
                                                       'channel': channel, 'state': self.__do_state.get(channel)})
                    else:
                        self.__reply(device, req_id, {'success': True, 'channels': dict(self.__do_state)})

            elif method == 'getDi':
                channel = params.get('channel')
                if channel:
                    self.__reply(device, req_id, {'success': channel in self.__di_state,
                                                   'channel': channel, 'state': self.__di_state.get(channel)})
                else:
                    self.__reply(device, req_id, {'success': True, 'channels': dict(self.__di_state)})

            elif method == 'getWanStatus':
                self.__reply(device, req_id, {'success': True, self.__wan_attribute_key: self.__last_wan_value})

            else:
                self.__log.warning('[%s] Unknown RPC method "%s"', self.name, method)
                self.__reply(device, req_id, {'success': False, 'error': f'Unknown method "{method}"'})
        except Exception as e:
            self.__log.exception('[%s] Error handling RPC request: %s', self.name, e)

    def __reply(self, device, req_id, content):
        if req_id is None:
            return
        try:
            self.__gateway.send_rpc_reply(device, req_id, content)
        except Exception as e:
            self.__log.exception('[%s] Failed to send RPC reply: %s', self.name, e)
