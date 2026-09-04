#!/usr/bin/env python3
"""
Offline (no real hardware, no gpiochip device node, no installed thingsboard_gateway
package required) verification for the built-in connectors/bliiot_gpio variant's (bliiot_gpio_connector.py) 2026-09-04 BACnet-convention
config schema rewrite (timeseries/attributes/serverSideRpc/devices, "gpio.reportOnChange",
"gpio.pollPeriod", per-channel RPC method naming).

Mocks gpiod (this sandbox/CI box has no real /dev/gpiochip*) and just enough of the
thingsboard_gateway package tree (Connector/Converter base classes, TBModuleLoader,
init_logger, TBUtility, ConvertedData) that the real connector.py and the real converter
files run completely unmodified, exactly as they do under the actual gateway -- nothing
about the connector's own logic is faked, only its environment.

Covers, in order:
  1. Config parsing: board-default DI/DO offsets, per-channel RPC method name defaults,
     the WAN "attributes" entry, and multi-device "devices" list parsing.
  2. The offset-optional-for-standard-channels / offset-required-for-custom-channels rule.
  3. RPC method name collision detection (startup ValueError).
  4. Duplicate device name detection (startup ValueError).
  5. Multi-device DI telemetry fan-out -- duplicating the same physical input to more than
     one ThingsBoard device (the capability requested to match what BACnet already does),
     including confirming a device's "timeseries" subset actually excludes channels not
     listed for it.
  6. "gpio.reportOnChange": false -- DI edges update internal state but are not published
     individually; only the next full periodic/forced publish carries them.
  7. DO writes: confirms telemetry-visibility ("timeseries" subset) and RPC-control-
     authorization ("serverSideRpc" subset) are independent -- a device can be authorized
     to control a channel via RPC without ever seeing its telemetry.
  8. server_side_rpc_handler: per-channel named RPC method dispatch (setDO1/getDO1/
     toggleDO1-style, BACnet-exact naming) plus per-device authorization for every method,
     including the remaining generic methods (setAllDo/getDi/getWanStatus).
  9. on_attributes_update: the "<key>_set" shared-attribute DO control path, same
     per-device authorization as the RPC path.
  10. WAN status attribute fan-out to multiple devices.
  11. WAN reporting fully disabled when no "attributes" entry has "source": "wanStatus".
  12. The 4G modem not being installed (or Ethernet down with no modem fitted at all):
     confirms `ip route show default` returning nothing is treated as "no interface holds
     the default route" (reported as the friendly value "unknown", not a stale/blank value
     and not an exception), and that a default route via an interface name this config's
     "interfaceNames" map doesn't recognise (e.g. a modem enumerating as "wwan0" instead of
     the assumed "usb0") still gets reported using its raw interface name rather than
     silently falling back to "unknown" or crashing the WAN status loop/thread.

  2026-09-04 second rewrite pass -- "destination is list membership" for DI/wanStatus, and
  the new daily wanTraffic feature:
  13. DI channel destination flexibility: a key in "attributes" only publishes attribute-
     only, "timeseries" only publishes timeseries-only, both publishes both, and a
     standard board key mentioned in neither list still defaults to timeseries-only
     (regression check). Confirms the "source"-tagged wanStatus entry placed in
     "timeseries" doesn't leak into di_config as a fake DI channel. __split_di_by_publish
     unit-tested directly. Conflicting explicit "offset" values across the two lists for
     the same key raises ValueError at startup.
  14. wanStatus destination flexibility: "timeseries" only, both lists (with the
     "attributes" list's copy authoritative for field values), and that each destination
     actually reaches the right storage (attributes vs telemetry).
  15. wanTraffic config parsing from either/both lists, defaults, and that it doesn't leak
     into di_config either.
  16. wanTraffic's pure helper functions against a fake NET_STATS_DIR (not the real
     /sys/class/net): __read_interface_counters, __read_all_traffic_counters,
     __compute_traffic_totals (including the no-4G-modem missing-interface case and a
     detected counter reset clamped to 0), __traffic_day_for at a non-zero resetHour.
  17. getWanTraffic RPC: not-configured case, and device "attributes"-subset filtering of
     the returned "sinceReset" totals.
  18. __wan_traffic_loop: seeds its baseline at startup (tolerating a missing interface),
     and stops cleanly on connector shutdown.

Run directly, no arguments, no pytest dependency:

    python3 bliiot/test/offline_connector_test.py

Exits 0 if every check passes, 1 (with a summary of what failed) otherwise.
"""
import importlib
import os
import logging
import sys
import threading
import time
import types
import traceback

FAILURES = []


def check(label, condition, detail=''):
    if condition:
        print(f'  PASS  {label}')
    else:
        print(f'  FAIL  {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


# ------------------------------------------------------------------ fake gpiod ----

class _Sym:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return f'<{self.name}>'


Value_ACTIVE = _Sym('ACTIVE')
Value_INACTIVE = _Sym('INACTIVE')


class Value:
    ACTIVE = Value_ACTIVE
    INACTIVE = Value_INACTIVE


class Direction:
    INPUT = _Sym('INPUT')
    OUTPUT = _Sym('OUTPUT')


class Bias:
    AS_IS = _Sym('AS_IS')
    DISABLED = _Sym('DISABLED')
    PULL_UP = _Sym('PULL_UP')
    PULL_DOWN = _Sym('PULL_DOWN')


class Edge:
    NONE = _Sym('NONE')
    BOTH = _Sym('BOTH')
    RISING = _Sym('RISING')
    FALLING = _Sym('FALLING')


class LineSettings:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeLineRequest:
    def __init__(self, chip_path, consumer, config):
        self.chip_path = chip_path
        self.consumer = consumer
        self.config = config
        self.values = {}
        for offset, settings in config.items():
            ov = getattr(settings, 'output_value', None)
            # Idle DI reads raw ACTIVE per the confirmed hardware behaviour this
            # connector's polarity handling is built around (see bliiot_gpio_map.py).
            self.values[offset] = ov if ov is not None else Value.ACTIVE
        self.fd = None

    def get_values(self, offsets):
        return [self.values[o] for o in offsets]

    def set_values(self, updates):
        self.values.update(updates)

    def release(self):
        pass


def fake_request_lines(chip_path, consumer=None, config=None):
    return FakeLineRequest(chip_path, consumer, config or {})


def fake_is_gpiochip_device(path):
    return True


fake_gpiod = types.ModuleType('gpiod')
fake_gpiod.LineSettings = LineSettings
fake_gpiod.request_lines = fake_request_lines
fake_gpiod.is_gpiochip_device = fake_is_gpiochip_device
fake_gpiod_line = types.ModuleType('gpiod.line')
fake_gpiod_line.Bias = Bias
fake_gpiod_line.Direction = Direction
fake_gpiod_line.Edge = Edge
fake_gpiod_line.Value = Value
fake_gpiod.line = fake_gpiod_line
sys.modules['gpiod'] = fake_gpiod
sys.modules['gpiod.line'] = fake_gpiod_line

# ------------------------------------------------------- fake thingsboard_gateway ----

tbg = types.ModuleType('thingsboard_gateway')
tbg.__path__ = []
sys.modules['thingsboard_gateway'] = tbg

connectors_pkg = types.ModuleType('thingsboard_gateway.connectors')
connectors_pkg.__path__ = []
sys.modules['thingsboard_gateway.connectors'] = connectors_pkg

connector_mod = types.ModuleType('thingsboard_gateway.connectors.connector')


class Connector:
    pass


connector_mod.Connector = Connector
sys.modules['thingsboard_gateway.connectors.connector'] = connector_mod

converter_mod = types.ModuleType('thingsboard_gateway.connectors.converter')


class Converter:
    pass


converter_mod.Converter = Converter
sys.modules['thingsboard_gateway.connectors.converter'] = converter_mod

tb_utility_pkg = types.ModuleType('thingsboard_gateway.tb_utility')
tb_utility_pkg.__path__ = []
sys.modules['thingsboard_gateway.tb_utility'] = tb_utility_pkg

tb_loader_mod = types.ModuleType('thingsboard_gateway.tb_utility.tb_loader')


class TBModuleLoader:
    _registry = {}

    @staticmethod
    def import_module(connector_type, class_name):
        return TBModuleLoader._registry.get(class_name)


tb_loader_mod.TBModuleLoader = TBModuleLoader
sys.modules['thingsboard_gateway.tb_utility.tb_loader'] = tb_loader_mod

tb_logger_mod = types.ModuleType('thingsboard_gateway.tb_utility.tb_logger')


def init_logger(gateway, name, log_level, enable_remote_logging=False,
                 is_connector_logger=False, is_converter_logger=False, attr_name=None):
    logger = logging.getLogger(name + ('.converter' if is_converter_logger else '.connector'))
    logger.setLevel(getattr(logging, str(log_level).upper(), logging.INFO))
    logger.stop = lambda: None
    return logger


tb_logger_mod.init_logger = init_logger
sys.modules['thingsboard_gateway.tb_utility.tb_logger'] = tb_logger_mod

tb_utility_mod = types.ModuleType('thingsboard_gateway.tb_utility.tb_utility')


class TBUtility:
    @staticmethod
    def install_package(name, version=None):
        pass


tb_utility_mod.TBUtility = TBUtility
sys.modules['thingsboard_gateway.tb_utility.tb_utility'] = tb_utility_mod

gateway_pkg = types.ModuleType('thingsboard_gateway.gateway')
gateway_pkg.__path__ = []
sys.modules['thingsboard_gateway.gateway'] = gateway_pkg
entities_pkg = types.ModuleType('thingsboard_gateway.gateway.entities')
entities_pkg.__path__ = []
sys.modules['thingsboard_gateway.gateway.entities'] = entities_pkg

converted_data_mod = types.ModuleType('thingsboard_gateway.gateway.entities.converted_data')


class ConvertedData:
    def __init__(self, device_name, device_type='default'):
        self.device_name = device_name
        self.device_type = device_type
        self.telemetry = []
        self.attributes = []

    def add_to_telemetry(self, entry):
        self.telemetry.append(dict(entry))

    def add_to_attributes(self, entry):
        self.attributes.append(dict(entry))

    @property
    def telemetry_datapoints_count(self):
        return sum(len({k: v for k, v in t.items() if k != 'ts'}) for t in self.telemetry)

    @property
    def attributes_datapoints_count(self):
        return sum(len(a) for a in self.attributes)


converted_data_mod.ConvertedData = ConvertedData
sys.modules['thingsboard_gateway.gateway.entities.converted_data'] = converted_data_mod

# ------------------------------------------------------------------ real imports ----

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
FORK_PKG_DIR = os.path.normpath(os.path.join(_THIS_DIR, '..', '..', 'thingsboard_gateway',
                                              'connectors', 'bliiot_gpio'))
bliiot_gpio_pkg = types.ModuleType('thingsboard_gateway.connectors.bliiot_gpio')
bliiot_gpio_pkg.__path__ = [FORK_PKG_DIR]
sys.modules['thingsboard_gateway.connectors.bliiot_gpio'] = bliiot_gpio_pkg

uplink_mod = importlib.import_module('thingsboard_gateway.connectors.bliiot_gpio.bliiot_gpio_uplink_converter')
downlink_mod = importlib.import_module('thingsboard_gateway.connectors.bliiot_gpio.bliiot_gpio_downlink_converter')
TBModuleLoader._registry['BliiotGpioUplinkConverter'] = uplink_mod.BliiotGpioUplinkConverter
TBModuleLoader._registry['BliiotGpioDownlinkConverter'] = downlink_mod.BliiotGpioDownlinkConverter

connector_module = importlib.import_module('thingsboard_gateway.connectors.bliiot_gpio.bliiot_gpio_connector')
BliiotGpioConnector = connector_module.BliiotGpioConnector


class FakeGateway:
    def __init__(self):
        self.devices_added = []
        self.storage = []
        self.rpc_replies = []

    def add_device(self, name, connector_info, device_type=None):
        self.devices_added.append((name, device_type))

    def send_to_storage(self, connector_name, connector_id, converted_data):
        self.storage.append(converted_data)

    def send_rpc_reply(self, device, req_id, content):
        self.rpc_replies.append((device, req_id, content))


def storage_for(gateway, device_name, kind='telemetry'):
    """Merge every telemetry (or attributes) dict sent to `device_name` into one dict."""
    merged = {}
    for cd in gateway.storage:
        if cd.device_name != device_name:
            continue
        for entry in getattr(cd, kind):
            for k, v in entry.items():
                if k != 'ts':
                    merged[k] = v
    return merged


def mangled(obj, name):
    return getattr(obj, f'_BliiotGpioConnector__{name}')


# =====================================================================================
print('--- 1. Basic config parsing (board defaults, method map, wan attr, devices) ---')

base_config = {
    'name': 'BLIIOT GPIO Test',
    'gpio': {
        'chip': 'test0',
        'pollIntervalMs': 20,
        'pollPeriod': 3600,
        'reportOnChange': True,
        'boardType': 'X26',
    },
    'timeseries': [],
    'serverSideRpc': [
        {'key': 'DO1'},
        {'key': 'DO2'},
        {'key': 'DO3'},
        {'key': 'DO4'},
    ],
    'attributes': [
        {'source': 'wanStatus', 'key': 'active_wan_interface', 'pollPeriod': 3600},
    ],
    'devices': [
        {'name': 'Device A - Full'},
        {'name': 'Device B - Restricted', 'timeseries': ['DI1', 'DI2'],
         'serverSideRpc': ['DO1'], 'attributes': []},
    ],
}

gw1 = FakeGateway()
conn1 = BliiotGpioConnector(gw1, base_config, 'bliiot_gpio')

check('DI channels default to X26 board offsets',
      set(mangled(conn1, 'di_config')) == {f'DI{i}' for i in range(1, 9)},
      mangled(conn1, 'di_config'))
check('DI1 offset defaults to board map (12)', mangled(conn1, 'di_config')['DI1']['offset'] == 12)
check('DO channels present', set(mangled(conn1, 'do_config')) == {'DO1', 'DO2', 'DO3', 'DO4'})
check('DO1 default RPC method names',
      mangled(conn1, 'do_config')['DO1']['setMethod'] == 'setDO1'
      and mangled(conn1, 'do_config')['DO1']['getMethod'] == 'getDO1'
      and mangled(conn1, 'do_config')['DO1']['toggleMethod'] == 'toggleDO1',
      mangled(conn1, 'do_config')['DO1'])

method_map = mangled(conn1, 'method_to_channel')
check('method map has 12 entries (4 channels x 3 actions)', len(method_map) == 12, method_map)
check('setDO1 -> (set, DO1)', method_map.get('setDO1') == ('set', 'DO1'))
check('toggleDO3 -> (toggle, DO3)', method_map.get('toggleDO3') == ('toggle', 'DO3'))

wan_attr = mangled(conn1, 'wan_attr')
check('WAN attribute parsed', wan_attr == {'key': 'active_wan_interface', 'pollPeriod': 3600,
                                            'interfaceNames': {'eth0': 'ethernet', 'usb0': 'cellular'},
                                            'destinations': frozenset({'attribute'})},
      wan_attr)

devices = mangled(conn1, 'devices')
check('two devices parsed', len(devices) == 2, devices)
dev_a = next(d for d in devices if d['name'] == 'Device A - Full')
dev_b = next(d for d in devices if d['name'] == 'Device B - Restricted')
check('Device A has no subset restriction (sees everything)',
      dev_a['timeseries'] is None and dev_a['serverSideRpc'] is None and dev_a['attributes'] is None)
check('Device B timeseries subset == {DI1, DI2}', dev_b['timeseries'] == {'DI1', 'DI2'}, dev_b['timeseries'])
check('Device B serverSideRpc subset == {DO1}', dev_b['serverSideRpc'] == {'DO1'}, dev_b['serverSideRpc'])
check('Device B attributes subset == empty set (explicitly no attributes)',
      dev_b['attributes'] == set(), dev_b['attributes'])

# =====================================================================================
print('\n--- 2. Custom channel offset-optional rule ---')

custom_cfg = dict(base_config)
custom_cfg['timeseries'] = [{'key': 'DI_CUSTOM_NO_OFFSET'}, {'key': 'DI_CUSTOM_WITH_OFFSET', 'offset': 26}]
gw_custom = FakeGateway()
conn_custom = BliiotGpioConnector(gw_custom, custom_cfg, 'bliiot_gpio')
di_cfg = mangled(conn_custom, 'di_config')
check('custom key with no offset is skipped (logged error, not fatal)',
      'DI_CUSTOM_NO_OFFSET' not in di_cfg)
check('custom key with explicit offset is kept',
      di_cfg.get('DI_CUSTOM_WITH_OFFSET', {}).get('offset') == 26)
check('standard board DI keys still present alongside custom key',
      'DI1' in di_cfg and di_cfg['DI1']['offset'] == 12)

# =====================================================================================
print('\n--- 3. Method name collision detection ---')

collision_cfg = dict(base_config)
collision_cfg['serverSideRpc'] = [
    {'key': 'DO1', 'setMethod': 'sharedMethod'},
    {'key': 'DO2', 'setMethod': 'sharedMethod'},
]
try:
    BliiotGpioConnector(FakeGateway(), collision_cfg, 'bliiot_gpio')
    check('duplicate RPC method name raises ValueError', False, 'no exception raised')
except ValueError as e:
    check('duplicate RPC method name raises ValueError', True, str(e))

# =====================================================================================
print('\n--- 4. Duplicate device name detection ---')

dup_cfg = dict(base_config)
dup_cfg['devices'] = [{'name': 'Same'}, {'name': 'Same'}]
try:
    BliiotGpioConnector(FakeGateway(), dup_cfg, 'bliiot_gpio')
    check('duplicate device name raises ValueError', False, 'no exception raised')
except ValueError as e:
    check('duplicate device name raises ValueError', True, str(e))

# =====================================================================================
print('\n--- 5. Multi-device telemetry fan-out (DI change) ---')

gw2 = FakeGateway()
conn2 = BliiotGpioConnector(gw2, base_config, 'bliiot_gpio')
mangled(conn2, 'init_gpio')()

di1_offset = mangled(conn2, 'di_config')['DI1']['offset']
di3_offset = mangled(conn2, 'di_config')['DI3']['offset']
di_request = mangled(conn2, 'di_request')

# Flip DI1 (in Device B's subset) -- both devices should see it.
di_request.values[di1_offset] = Value.INACTIVE
changes = mangled(conn2, 'poll_di_once')()
check('DI1 edge detected', 'DI1' in changes, changes)
mangled(conn2, 'fan_out_telemetry')(changes)
check('Device A (unrestricted) received DI1', storage_for(gw2, 'Device A - Full').get('DI1') is True)
check('Device B (subset incl. DI1) received DI1', storage_for(gw2, 'Device B - Restricted').get('DI1') is True)

gw2.storage.clear()
# Flip DI3 (NOT in Device B's subset) -- only Device A should see it.
di_request.values[di3_offset] = Value.INACTIVE
changes = mangled(conn2, 'poll_di_once')()
check('DI3 edge detected', 'DI3' in changes, changes)
mangled(conn2, 'fan_out_telemetry')(changes)
check('Device A received DI3', storage_for(gw2, 'Device A - Full').get('DI3') is True)
check('Device B did NOT receive DI3 (outside its timeseries subset)',
      'DI3' not in storage_for(gw2, 'Device B - Restricted'))

# =====================================================================================
print('\n--- 6. reportOnChange=False: no individual publish, state still tracked ---')

no_change_cfg = dict(base_config)
no_change_cfg['gpio'] = dict(base_config['gpio'])
no_change_cfg['gpio']['reportOnChange'] = False
no_change_cfg['gpio']['pollPeriod'] = 3600  # long enough that the periodic force-publish won't fire
no_change_cfg['gpio']['pollIntervalMs'] = 20
gw3 = FakeGateway()
conn3 = BliiotGpioConnector(gw3, no_change_cfg, 'bliiot_gpio')
mangled(conn3, 'init_gpio')()

di1_offset_3 = mangled(conn3, 'di_config')['DI1']['offset']
di_request_3 = mangled(conn3, 'di_request')

poll_thread = threading.Thread(target=mangled(conn3, 'poll_loop'), daemon=True)
poll_thread.start()
di_request_3.values[di1_offset_3] = Value.INACTIVE
time.sleep(0.3)
mangled(conn3, 'stopped').set()
poll_thread.join(timeout=2)

check('reportOnChange=False: no telemetry published on DI edge', len(gw3.storage) == 0, gw3.storage)
check('reportOnChange=False: DI state still tracked internally despite no publish',
      mangled(conn3, 'di_state').get('DI1') is True, mangled(conn3, 'di_state'))

gw3.storage.clear()
mangled(conn3, 'publish_full_state')()
check('a manual/periodic full-state publish still carries the un-published change',
      storage_for(gw3, 'Device A - Full').get('DI1') is True)

# =====================================================================================
print('\n--- 7. DO write -> telemetry visibility vs RPC-control authorization separation ---')

gw4 = FakeGateway()
conn4 = BliiotGpioConnector(gw4, base_config, 'bliiot_gpio')
mangled(conn4, 'init_gpio')()
gw4.storage.clear()

success, new_state, message = mangled(conn4, 'write_do')('DO1', True)
check('DO1 write succeeds', success, message)
check('Device A (unrestricted) sees DO1 telemetry', storage_for(gw4, 'Device A - Full').get('DO1') is True)
check('Device B does NOT see DO1 telemetry (DO1 not in its timeseries subset, even though it CAN control it)',
      'DO1' not in storage_for(gw4, 'Device B - Restricted'))

# =====================================================================================
print('\n--- 8. server_side_rpc_handler: per-channel method dispatch + per-device authorization ---')

gw5 = FakeGateway()
conn5 = BliiotGpioConnector(gw5, base_config, 'bliiot_gpio')
mangled(conn5, 'init_gpio')()
mangled(conn5, 'poll_di_once')()  # seed __di_state, same as run() does before serving any RPCs


def rpc(conn, gw, device, method, params=None, req_id=1):
    gw.rpc_replies.clear()
    conn.server_side_rpc_handler({'device': device, 'data': {'id': req_id, 'method': method,
                                                               'params': params or {}}})
    return gw.rpc_replies[-1][2] if gw.rpc_replies else None


reply = rpc(conn5, gw5, 'Device A - Full', 'setDO1', {'state': True})
check('setDO1 from Device A (unrestricted) succeeds', reply and reply.get('success') is True, reply)

reply = rpc(conn5, gw5, 'Device B - Restricted', 'setDO2', {'state': True})
check('setDO2 from Device B (not in its serverSideRpc subset {DO1}) is rejected',
      reply and reply.get('success') is False, reply)

reply = rpc(conn5, gw5, 'Device B - Restricted', 'setDO1', {'state': True})
check('setDO1 from Device B (IS in its serverSideRpc subset) succeeds', reply and reply.get('success') is True, reply)

reply = rpc(conn5, gw5, 'Device A - Full', 'toggleDO1')
check('toggleDO1 succeeds and flips state', reply and reply.get('success') is True and reply.get('state') is False,
      reply)

reply = rpc(conn5, gw5, 'Device A - Full', 'getDO1')
check('getDO1 reports current state', reply and reply.get('success') is True and reply.get('state') is False, reply)

reply = rpc(conn5, gw5, 'Device A - Full', 'bogusMethod')
check('unknown method name returns success=False with an error', reply and reply.get('success') is False, reply)

reply = rpc(conn5, gw5, 'Nonexistent Device', 'setDO1', {'state': True})
check('unknown device returns success=False with an error', reply and reply.get('success') is False, reply)

reply = rpc(conn5, gw5, 'Device B - Restricted', 'getWanStatus')
check('getWanStatus from Device B (attributes subset == empty set) is rejected',
      reply and reply.get('success') is False, reply)

reply = rpc(conn5, gw5, 'Device A - Full', 'getWanStatus')
check('getWanStatus from Device A (unrestricted) succeeds', reply and reply.get('success') is True, reply)

reply = rpc(conn5, gw5, 'Device B - Restricted', 'setAllDo', {'state': True})
check('setAllDo from Device B only touches its authorized subset (DO1)',
      reply and reply.get('success') is True and set(reply.get('channels', {})) == {'DO1'}, reply)

reply = rpc(conn5, gw5, 'Device A - Full', 'setAllDo', {'state': False})
check('setAllDo from Device A (unrestricted) touches all 4 DO channels',
      reply and set(reply.get('channels', {})) == {'DO1', 'DO2', 'DO3', 'DO4'}, reply)

reply = rpc(conn5, gw5, 'Device B - Restricted', 'getDi')
check('getDi from Device B (timeseries subset {DI1,DI2}) only returns that subset',
      reply and reply.get('success') is True and set(reply.get('channels', {})) <= {'DI1', 'DI2'}, reply)

reply = rpc(conn5, gw5, 'Device A - Full', 'getDi')
check('getDi from Device A (unrestricted) returns all 8 DI channels',
      reply and set(reply.get('channels', {})) == {f'DI{i}' for i in range(1, 9)}, reply)

# =====================================================================================
print('\n--- 9. on_attributes_update: shared-attribute DO control + per-device authorization ---')

gw6 = FakeGateway()
conn6 = BliiotGpioConnector(gw6, base_config, 'bliiot_gpio')
mangled(conn6, 'init_gpio')()

conn6.on_attributes_update({'device': 'Device A - Full', 'data': {'DO2_set': True}})
check('Device A can control DO2 via shared attribute (unrestricted)',
      mangled(conn6, 'do_state')['DO2'] is True, mangled(conn6, 'do_state'))

before = mangled(conn6, 'do_state')['DO2']
conn6.on_attributes_update({'device': 'Device B - Restricted', 'data': {'DO2_set': False}})
check('Device B CANNOT control DO2 via shared attribute (not in its serverSideRpc subset {DO1})',
      mangled(conn6, 'do_state')['DO2'] == before, mangled(conn6, 'do_state'))

conn6.on_attributes_update({'device': 'Device B - Restricted', 'data': {'DO1_set': True}})
check('Device B CAN control DO1 via shared attribute (in its serverSideRpc subset)',
      mangled(conn6, 'do_state')['DO1'] is True, mangled(conn6, 'do_state'))

# =====================================================================================
print('\n--- 10. WAN attribute fan-out ---')

gw7 = FakeGateway()
conn7 = BliiotGpioConnector(gw7, base_config, 'bliiot_gpio')
mangled(conn7, 'fan_out_attribute')({'active_wan_interface': 'ethernet'})
check('Device A (unrestricted attrs) receives WAN attribute',
      storage_for(gw7, 'Device A - Full', kind='attributes').get('active_wan_interface') == 'ethernet')
check('Device B (attributes subset == empty set) does NOT receive WAN attribute',
      'active_wan_interface' not in storage_for(gw7, 'Device B - Restricted', kind='attributes'))

# =====================================================================================
print('\n--- 11. No "attributes" wanStatus entry => WAN reporting fully disabled ---')

no_wan_cfg = dict(base_config)
no_wan_cfg['attributes'] = []
gw8 = FakeGateway()
conn8 = BliiotGpioConnector(gw8, no_wan_cfg, 'bliiot_gpio')
check('wan_attr is None when no "source": "wanStatus" entry is present', mangled(conn8, 'wan_attr') is None)
reply = rpc(conn8, gw8, 'Device A - Full', 'getWanStatus')
check('getWanStatus replies "not configured" when WAN reporting is disabled',
      reply and reply.get('success') is False, reply)

# =====================================================================================
print('\n--- 12. WAN detection when the 4G modem is not installed / no default route ---')
# Simulates the box with no cellular modem fitted (or the modem present but not yet
# registered on the network) AND Ethernet unplugged/down at the same time -- i.e. no
# interface holds the default route at all. `ip route show default` returns nothing in
# that case. This must not crash the WAN loop; it should report a clear "unknown" value
# rather than silently repeating the last-known interface or raising.
import types as _types

FakeCompleted = _types.SimpleNamespace


def _fake_run_no_default_route(*args, **kwargs):
    return FakeCompleted(returncode=0, stdout='', stderr='')


real_subprocess_run = connector_module.subprocess.run
connector_module.subprocess.run = _fake_run_no_default_route
try:
    interface = mangled(conn7, 'detect_default_route_interface')()
finally:
    connector_module.subprocess.run = real_subprocess_run
check('no default route (no modem, Ethernet down) => interface detection returns None, no exception',
      interface is None, interface)

wan_attr7 = mangled(conn7, 'wan_attr')
interface_names7 = wan_attr7['interfaceNames']
friendly = interface_names7.get(interface, interface) if interface else 'unknown'
check('friendly WAN value falls back to "unknown" (not a crash, not a stale value)', friendly == 'unknown', friendly)

gw7.storage.clear()
mangled(conn7, 'fan_out_attribute')({'active_wan_interface': friendly})
check('"unknown" WAN status is still published to ThingsBoard (visible, not silently dropped)',
      storage_for(gw7, 'Device A - Full', kind='attributes').get('active_wan_interface') == 'unknown')


def _fake_run_unmapped_interface(*args, **kwargs):
    # A real default route, but via an interface name this config's "interfaceNames"
    # doesn't recognise (e.g. a cellular modem enumerating as "wwan0" instead of the
    # configured "usb0" -- a real risk if the modem's driver/naming differs from what
    # was assumed when interfaceNames was written).
    return FakeCompleted(returncode=0, stdout='default via 10.0.0.1 dev wwan0 proto dhcp metric 700\n', stderr='')


connector_module.subprocess.run = _fake_run_unmapped_interface
try:
    interface = mangled(conn7, 'detect_default_route_interface')()
finally:
    connector_module.subprocess.run = real_subprocess_run
check('default route via an unrecognised interface name is still detected (raw name, no crash)',
      interface == 'wwan0', interface)
friendly = interface_names7.get(interface, interface) if interface else 'unknown'
check('unmapped interface name falls back to reporting the raw interface name (not "unknown", not blank)',
      friendly == 'wwan0', friendly)

# A full __wan_loop tick, mocked end-to-end, to confirm the loop body itself (not just
# the pieces tested above) survives a "no default route" reading without raising --
# run it in a real thread for one tick and make sure it's still alive afterwards.
wan_loop_cfg = dict(base_config)
gw9 = FakeGateway()
conn9 = BliiotGpioConnector(gw9, wan_loop_cfg, 'bliiot_gpio')
mangled(conn9, 'init_gpio')()
connector_module.subprocess.run = _fake_run_no_default_route
wan_attr9 = dict(mangled(conn9, 'wan_attr'))
wan_attr9['pollPeriod'] = 0.05
# __wan_attr is a private (name-mangled) attribute set in __init__; poke a faster
# pollPeriod into it the same way the other mangled-name pokes in this script work,
# then run the real loop body in a thread for a couple of ticks.
setattr(conn9, '_BliiotGpioConnector__wan_attr', wan_attr9)
wan_thread = threading.Thread(target=mangled(conn9, 'wan_loop'), daemon=True)
wan_thread.start()
time.sleep(0.2)
mangled(conn9, 'stopped').set()
wan_thread.join(timeout=2)
connector_module.subprocess.run = real_subprocess_run
check('__wan_loop survives repeated "no default route" ticks without raising (thread exits cleanly on stop)',
      not wan_thread.is_alive())
check('__wan_loop published "unknown" at least once while no interface held the default route',
      storage_for(gw9, 'Device A - Full', kind='attributes').get('active_wan_interface') == 'unknown')

# =====================================================================================
print('\n--- 13. DI channel destination flexibility (timeseries / attribute / both) ---')
# 2026-09-04 second rewrite pass: a DI channel's key can now be placed in "timeseries",
# "attributes", or both -- list membership decides the publish destination(s), exactly
# like BACnet. A standard board channel mentioned in NEITHER list must still default to
# timeseries-only (regression check against the schema shipped earlier the same day).

di_flex_cfg = dict(base_config)
di_flex_cfg['timeseries'] = [{'key': 'DI2'}, {'key': 'DI3'}]
di_flex_cfg['attributes'] = [
    {'source': 'wanStatus', 'key': 'active_wan_interface', 'pollPeriod': 3600},
    {'key': 'DI1'},
    {'key': 'DI3'},
]
gw13 = FakeGateway()
conn13 = BliiotGpioConnector(gw13, di_flex_cfg, 'bliiot_gpio')
di_cfg13 = mangled(conn13, 'di_config')

check('DI1 ("attributes" list only) publishes attribute-only',
      di_cfg13['DI1']['publish'] == frozenset({'attribute'}), di_cfg13['DI1'])
check('DI2 ("timeseries" list only, explicit) publishes timeseries-only',
      di_cfg13['DI2']['publish'] == frozenset({'timeseries'}), di_cfg13['DI2'])
check('DI3 (both lists) publishes both timeseries and attribute',
      di_cfg13['DI3']['publish'] == frozenset({'timeseries', 'attribute'}), di_cfg13['DI3'])
check('DI4 (mentioned in neither list) still defaults to timeseries-only (regression)',
      di_cfg13['DI4']['publish'] == frozenset({'timeseries'}), di_cfg13['DI4'])
check('the "source": "wanStatus" entry in "attributes" did not leak into di_config as a fake DI channel',
      'active_wan_interface' not in di_cfg13, di_cfg13)

ts_subset13, attr_subset13 = mangled(conn13, 'split_di_by_publish')(
    {'DI1': True, 'DI2': True, 'DI3': True, 'DI4': True})
check('__split_di_by_publish: DI1 only in the attribute subset',
      'DI1' not in ts_subset13 and attr_subset13.get('DI1') is True, (ts_subset13, attr_subset13))
check('__split_di_by_publish: DI2 only in the timeseries subset',
      ts_subset13.get('DI2') is True and 'DI2' not in attr_subset13, (ts_subset13, attr_subset13))
check('__split_di_by_publish: DI3 present in both subsets',
      ts_subset13.get('DI3') is True and attr_subset13.get('DI3') is True, (ts_subset13, attr_subset13))
check('__split_di_by_publish: DI4 (default) only in the timeseries subset',
      ts_subset13.get('DI4') is True and 'DI4' not in attr_subset13, (ts_subset13, attr_subset13))

conflict_cfg = dict(base_config)
conflict_cfg['timeseries'] = [{'key': 'DI5', 'offset': 99}]
conflict_cfg['attributes'] = [{'key': 'DI5', 'offset': 100}]
try:
    BliiotGpioConnector(FakeGateway(), conflict_cfg, 'bliiot_gpio')
    check('conflicting explicit "offset" across timeseries/attributes for the same key raises ValueError',
          False, 'no exception raised')
except ValueError as e:
    check('conflicting explicit "offset" across timeseries/attributes for the same key raises ValueError',
          True, str(e))

# =====================================================================================
print('\n--- 14. wanStatus destination flexibility (timeseries / attribute / both) ---')

wan_ts_only_cfg = dict(base_config)
wan_ts_only_cfg['attributes'] = []
wan_ts_only_cfg['timeseries'] = [{'source': 'wanStatus', 'key': 'active_wan_interface', 'pollPeriod': 3600}]
gw14a = FakeGateway()
conn14a = BliiotGpioConnector(gw14a, wan_ts_only_cfg, 'bliiot_gpio')
wan_attr14a = mangled(conn14a, 'wan_attr')
check('wanStatus in "timeseries" only => destinations == {timeseries}',
      wan_attr14a['destinations'] == frozenset({'timeseries'}), wan_attr14a)
check('a wanStatus entry placed in "timeseries" did not leak into di_config either',
      'active_wan_interface' not in mangled(conn14a, 'di_config'))

wan_both_cfg = dict(base_config)
wan_both_cfg['timeseries'] = [{'source': 'wanStatus', 'key': 'active_wan_interface'}]
wan_both_cfg['attributes'] = [{'source': 'wanStatus', 'key': 'active_wan_interface', 'pollPeriod': 3600}]
gw14b = FakeGateway()
conn14b = BliiotGpioConnector(gw14b, wan_both_cfg, 'bliiot_gpio')
wan_attr14b = mangled(conn14b, 'wan_attr')
check('wanStatus in both lists => destinations == {timeseries, attribute}',
      wan_attr14b['destinations'] == frozenset({'timeseries', 'attribute'}), wan_attr14b)
check('wanStatus in both lists: the "attributes" list copy is authoritative for field values (pollPeriod)',
      wan_attr14b['pollPeriod'] == 3600, wan_attr14b)

mangled(conn14b, 'fan_out_attribute')({wan_attr14b['key']: 'ethernet'})
check('wanStatus-as-attribute publishes into attributes storage',
      storage_for(gw14b, 'Device A - Full', kind='attributes').get('active_wan_interface') == 'ethernet')
gw14b.storage.clear()
mangled(conn14b, 'fan_out_telemetry')({wan_attr14b['key']: 'ethernet'})
check('wanStatus-as-timeseries publishes into telemetry storage',
      storage_for(gw14b, 'Device A - Full', kind='telemetry').get('active_wan_interface') == 'ethernet')

# =====================================================================================
print('\n--- 15. wanTraffic config parsing (either list, both, defaults) ---')

no_traffic_cfg = dict(base_config)
gw15a = FakeGateway()
conn15a = BliiotGpioConnector(gw15a, no_traffic_cfg, 'bliiot_gpio')
check('wan_traffic is None when no "source": "wanTraffic" entry is present',
      mangled(conn15a, 'wan_traffic') is None)

traffic_ts_cfg = dict(base_config)
traffic_ts_cfg['timeseries'] = [{'source': 'wanTraffic'}]
gw15b = FakeGateway()
conn15b = BliiotGpioConnector(gw15b, traffic_ts_cfg, 'bliiot_gpio')
wt15b = mangled(conn15b, 'wan_traffic')
check('wanTraffic in "timeseries" only, defaults applied',
      wt15b == {'interfaceNames': {'eth0': 'ethernet', 'usb0': 'cellular'}, 'pollIntervalSec': 60,
                'resetHour': 0, 'rxKeySuffix': '_rx_bytes', 'txKeySuffix': '_tx_bytes',
                'destinations': frozenset({'timeseries'})}, wt15b)
check('a wanTraffic entry placed in "timeseries" did not leak into di_config',
      'DI9' not in mangled(conn15b, 'di_config') and len(mangled(conn15b, 'di_config')) == 8)

traffic_both_cfg = dict(base_config)
traffic_both_cfg['timeseries'] = [{'source': 'wanTraffic'}]
traffic_both_cfg['attributes'] = [{'source': 'wanTraffic', 'pollIntervalSec': 30, 'resetHour': 6,
                                    'rxKeySuffix': '_in', 'txKeySuffix': '_out'}]
gw15c = FakeGateway()
conn15c = BliiotGpioConnector(gw15c, traffic_both_cfg, 'bliiot_gpio')
wt15c = mangled(conn15c, 'wan_traffic')
check('wanTraffic in both lists => destinations == {timeseries, attribute}',
      wt15c['destinations'] == frozenset({'timeseries', 'attribute'}), wt15c)
check('wanTraffic in both lists: the "attributes" list copy is authoritative for field values',
      wt15c['pollIntervalSec'] == 30 and wt15c['resetHour'] == 6
      and wt15c['rxKeySuffix'] == '_in' and wt15c['txKeySuffix'] == '_out', wt15c)

# =====================================================================================
print('\n--- 16. wanTraffic helper functions (fake NET_STATS_DIR, missing-interface case) ---')

import tempfile as _tempfile

_traffic_test_dir = _tempfile.mkdtemp(prefix='bliiot_net_stats_')


def _write_counters(iface, rx, tx):
    stats_dir = os.path.join(_traffic_test_dir, iface, 'statistics')
    os.makedirs(stats_dir, exist_ok=True)
    with open(os.path.join(stats_dir, 'rx_bytes'), 'w') as f:
        f.write(str(rx))
    with open(os.path.join(stats_dir, 'tx_bytes'), 'w') as f:
        f.write(str(tx))


_write_counters('eth0', 1000, 2000)
# usb0 (the configured "cellular" interface) is deliberately never created here --
# simulates the 4G modem not being installed/registered, same case already covered for
# WAN status detection in section 12.

real_net_stats_dir = connector_module.NET_STATS_DIR
connector_module.NET_STATS_DIR = _traffic_test_dir
try:
    eth0_counters = mangled(conn15b, 'read_interface_counters')('eth0')
    usb0_counters = mangled(conn15b, 'read_interface_counters')('usb0')
    all_counters16 = mangled(conn15b, 'read_all_traffic_counters')()
finally:
    connector_module.NET_STATS_DIR = real_net_stats_dir

check('__read_interface_counters reads real rx/tx bytes for an existing interface',
      eth0_counters == {'rx': 1000, 'tx': 2000}, eth0_counters)
check('__read_interface_counters returns None (not an exception) for a missing interface (no 4G modem)',
      usb0_counters is None, usb0_counters)
check('__read_all_traffic_counters reads every configured interface, missing ones as None',
      all_counters16 == {'eth0': {'rx': 1000, 'tx': 2000}, 'usb0': None}, all_counters16)

baseline16 = {'eth0': {'rx': 1000, 'tx': 2000}, 'usb0': {'rx': 500, 'tx': 500}}
current16 = {'eth0': {'rx': 1500, 'tx': 2400}, 'usb0': None}
totals16 = mangled(conn15b, 'compute_traffic_totals')(baseline16, current16, {'eth0': 'ethernet', 'usb0': 'cellular'},
                                                        '_rx_bytes', '_tx_bytes')
check('__compute_traffic_totals computes rx/tx deltas for a present interface',
      totals16 == {'ethernet_rx_bytes': 500, 'ethernet_tx_bytes': 400}, totals16)
check('__compute_traffic_totals skips an interface missing from the current reading (no 4G modem)',
      'cellular_rx_bytes' not in totals16 and 'cellular_tx_bytes' not in totals16, totals16)

reset_current16 = {'eth0': {'rx': 200, 'tx': 100}, 'usb0': {'rx': 600, 'tx': 700}}
reset_totals16 = mangled(conn15b, 'compute_traffic_totals')(baseline16, reset_current16,
                                                              {'eth0': 'ethernet', 'usb0': 'cellular'},
                                                              '_rx_bytes', '_tx_bytes')
check('__compute_traffic_totals clamps a detected counter reset (reboot/interface flap) to 0, not negative',
      reset_totals16['ethernet_rx_bytes'] == 0 and reset_totals16['ethernet_tx_bytes'] == 0, reset_totals16)

from datetime import datetime as _dt, timezone as _tz

day_midnight16 = mangled(conn15b, 'traffic_day_for')(_dt(2026, 9, 4, 5, 30, tzinfo=_tz.utc), 0)
day_offset16 = mangled(conn15b, 'traffic_day_for')(_dt(2026, 9, 4, 5, 30, tzinfo=_tz.utc), 6)
check('__traffic_day_for at resetHour=0 uses the calendar UTC date directly',
      day_midnight16 == _dt(2026, 9, 4, tzinfo=_tz.utc).date(), day_midnight16)
check('__traffic_day_for at resetHour=6: 05:30 UTC still belongs to the PREVIOUS day '
      '(before that day\'s 06:00 rollover)',
      day_offset16 == _dt(2026, 9, 3, tzinfo=_tz.utc).date(), day_offset16)

# =====================================================================================
print('\n--- 17. getWanTraffic RPC ---')

reply = rpc(conn15a, gw15a, 'Device A - Full', 'getWanTraffic')
check('getWanTraffic replies "not configured" when wanTraffic is not set up',
      reply and reply.get('success') is False, reply)

connector_module.NET_STATS_DIR = _traffic_test_dir
try:
    setattr(conn15b, '_BliiotGpioConnector__wan_traffic_baseline', {'eth0': {'rx': 0, 'tx': 0}})
    reply_full = rpc(conn15b, gw15b, 'Device A - Full', 'getWanTraffic')
    reply_restricted = rpc(conn15b, gw15b, 'Device B - Restricted', 'getWanTraffic')
finally:
    connector_module.NET_STATS_DIR = real_net_stats_dir

check('getWanTraffic (unrestricted device) returns "sinceReset" totals computed from live counters',
      reply_full and reply_full.get('success') is True
      and reply_full.get('sinceReset', {}).get('ethernet_rx_bytes') == 1000, reply_full)
check('getWanTraffic filters "sinceReset" by the requesting device\'s "attributes" subset '
      '(Device B\'s is explicitly empty => nothing)',
      reply_restricted and reply_restricted.get('success') is True
      and reply_restricted.get('sinceReset', {}) == {}, reply_restricted)

# =====================================================================================
print('\n--- 18. __wan_traffic_loop: seeds baseline at startup, survives a missing interface, stops cleanly ---')

loop_cfg = dict(base_config)
loop_cfg['timeseries'] = [{'source': 'wanTraffic', 'pollIntervalSec': 0.05, 'resetHour': 0}]
gw18 = FakeGateway()
conn18 = BliiotGpioConnector(gw18, loop_cfg, 'bliiot_gpio')
mangled(conn18, 'init_gpio')()
connector_module.NET_STATS_DIR = _traffic_test_dir
try:
    traffic_thread = threading.Thread(target=mangled(conn18, 'wan_traffic_loop'), daemon=True)
    traffic_thread.start()
    time.sleep(0.2)
    mangled(conn18, 'stopped').set()
    traffic_thread.join(timeout=2)
finally:
    connector_module.NET_STATS_DIR = real_net_stats_dir

check('__wan_traffic_loop thread exits cleanly on stop', not traffic_thread.is_alive())
baseline18 = mangled(conn18, 'wan_traffic_baseline')
check('__wan_traffic_loop seeds a baseline for the existing interface at startup',
      baseline18.get('eth0') == {'rx': 1000, 'tx': 2000}, baseline18)
check('__wan_traffic_loop tolerates a missing interface (no 4G modem) in the seeded baseline without crashing',
      'usb0' in baseline18 and baseline18['usb0'] is None, baseline18)

import shutil as _shutil

_shutil.rmtree(_traffic_test_dir, ignore_errors=True)

# =====================================================================================
print('\n' + ('=' * 70))
if FAILURES:
    print(f'{len(FAILURES)} CHECK(S) FAILED:')
    for f in FAILURES:
        print(f'  - {f}')
    sys.exit(1)
else:
    print('ALL CHECKS PASSED')
    sys.exit(0)
