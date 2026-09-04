#!/usr/bin/env python3
"""
Real (not mocked) proof that the gateway's stock, UNMODIFIED Modbus connector already
supports duplicating one physical input to more than one ThingsBoard device -- the same
capability requested for the BLIIOT GPIO connector (see bliiot/test/offline_connector_test.py
in the "extensions/" delivery, and thingsboard_gateway/connectors/bliiot_gpio's own
"devices"/"timeseries" subset mechanism) and that BACnet already provides by listing the
same object under two "devices" blocks in bacnet.json.

Unlike BLIIOT GPIO, Modbus needs NO special connector-side fan-out mechanism at all: it's
a bus/network protocol, not an exclusively-lockable local GPIO chardev line, so two
independent "slaves" entries in thingsboard_gateway/config/modbus.json's "master.slaves"
LIST can simply be given identical "host"/"port"/"unitId"/"address" and different
"deviceName" values -- each is just an ordinary, independent device block. This script
proves that by running a real local pymodbus TCP test server (not mocked) and a real,
completely unmodified AsyncModbusConnector against it, with two "slaves" entries that are
identical except for "deviceName", both reading the exact same holding register. Only the
ThingsBoard *gateway* boundary is faked (add_device/send_to_storage/send_rpc_reply) --
the same scope of mocking the BLIIOT GPIO offline test uses, and for the same reason:
there's no live ThingsBoard platform to talk to in an offline/CI check.

Requires the gateway's own normal dependencies (pymodbus, jsonpath-rw, simplejson -- all
already in this repo's requirements.txt/requirements-full.txt; nothing extra to install)
and this repo checkout on PYTHONPATH. Run from the repo root:

    python3 bliiot/test/modbus_duplicate_device_test.py

Exits 0 if both device blocks report the same value from the same register on every poll,
1 otherwise (with the mismatch printed).
"""
import logging
import os
import socket
import sys
import threading
import time
import warnings
from unittest.mock import MagicMock

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(_THIS_DIR, '..', '..'))
sys.path.insert(0, REPO_ROOT)

warnings.filterwarnings('ignore', message='BinaryPayloadDecoder is deprecated')

from pymodbus.datastore import ModbusSequentialDataBlock, ModbusSlaveContext, ModbusServerContext  # noqa: E402
from pymodbus.server import StartTcpServer  # noqa: E402

from thingsboard_gateway.connectors.modbus.modbus_connector import AsyncModbusConnector  # noqa: E402

UNIT_ID = 1
REGISTER_ADDRESS = 10
DEVICE_A = 'Duplicate Test Device A'
DEVICE_B = 'Duplicate Test Device B'


def _free_tcp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _run_server(port):
    values = list(range(200))  # register N reads back as N by default -- easy to eyeball
    store = ModbusSlaveContext(
        di=ModbusSequentialDataBlock(0, [0] * 200),
        co=ModbusSequentialDataBlock(0, [0] * 200),
        hr=ModbusSequentialDataBlock(0, values),
        ir=ModbusSequentialDataBlock(0, [0] * 200),
    )
    context = ModbusServerContext(slaves={UNIT_ID: store}, single=False)
    StartTcpServer(context=context, address=('127.0.0.1', port))


def _slave_block(port, device_name):
    return {
        'host': '127.0.0.1', 'port': port, 'type': 'tcp', 'method': 'socket',
        'unitId': UNIT_ID, 'deviceName': device_name, 'deviceType': 'default',
        'timeout': 10, 'byteOrder': 'BIG', 'wordOrder': 'BIG',
        'retries': 3, 'retryOnEmpty': True, 'retryOnInvalid': True,
        'pollPeriod': 300, 'connectAttemptTimeMs': 3000, 'connectAttemptCount': 3,
        'waitAfterFailedAttemptsMs': 5000,
        'attributes': [], 'attributeUpdates': [], 'rpc': [],
        'timeseries': [
            {'tag': 'shared_value', 'type': '16uint', 'functionCode': 3,
             'objectsCount': 1, 'address': REGISTER_ADDRESS},
        ],
    }


def _extract_values(published_entries):
    """`published_entries` is the list of ConvertedData.telemetry lists captured from every
    gateway.send_to_storage() call for one device -- each is a list of TelemetryEntry
    objects whose own `.values` is a {DatapointKey: value} dict. Flatten it."""
    found = []
    for publish in published_entries:
        for telemetry_entry in publish:
            values_dict = getattr(telemetry_entry, 'values', None)
            if isinstance(values_dict, dict):
                found.extend(values_dict.values())
    return found


def main():
    port = _free_tcp_port()
    server_thread = threading.Thread(target=_run_server, args=(port,), daemon=True)
    server_thread.start()
    time.sleep(1.5)  # let the TCP listener come up

    config = {
        'master': {'slaves': [_slave_block(port, DEVICE_A), _slave_block(port, DEVICE_B)]},
        'slave': None,
    }

    gateway = MagicMock()
    gateway.get_devices.return_value = []
    gateway.available_connections = {}
    # init_logger() wires gateway.remote_handler in as a logging Handler; a bare MagicMock
    # stand-in (with a MagicMock .level) breaks stdlib logging's level comparisons even
    # with remote logging left off, so give it the same concrete values this repo's own
    # Modbus integration tests use for the same reason (see
    # tests/integration/connectors/modbus/test_modbus_connector.py's setUp()).
    gateway.remote_handler = MagicMock()
    gateway.remote_handler.level = logging.DEBUG
    gateway.remote_handler.loggers = {}

    connector = AsyncModbusConnector(gateway, config, 'modbus')
    connector.open()
    try:
        time.sleep(3)  # a handful of poll cycles at pollPeriod=300ms
    finally:
        connector.close()
        time.sleep(0.5)

    by_device = {}
    for call in gateway.send_to_storage.call_args_list:
        args, kwargs = call
        converted_data = args[-1] if args else kwargs.get('data')
        name = getattr(converted_data, 'device_name', None)
        by_device.setdefault(name, []).append(getattr(converted_data, 'telemetry', []))

    values_a = _extract_values(by_device.get(DEVICE_A, []))
    values_b = _extract_values(by_device.get(DEVICE_B, []))

    print(f'{DEVICE_A} received {len(values_a)} readings: {values_a[:5]}{" ..." if len(values_a) > 5 else ""}')
    print(f'{DEVICE_B} received {len(values_b)} readings: {values_b[:5]}{" ..." if len(values_b) > 5 else ""}')

    # The point isn't any particular register value -- it's that BOTH device blocks,
    # reading the SAME host/port/unitId/address from the SAME live Modbus register,
    # consistently report the SAME value as each other on every poll. That is exactly
    # "duplicating one physical input to multiple ThingsBoard devices", achieved here with
    # nothing but two ordinary entries in "master.slaves" -- no code changes needed.
    ok_a = len(values_a) > 0 and len(set(values_a)) == 1
    ok_b = len(values_b) > 0 and len(set(values_b)) == 1
    ok_match = ok_a and ok_b and set(values_a) == set(values_b)

    print(f'Device A reported a single, consistent value: {ok_a}')
    print(f'Device B reported a single, consistent value: {ok_b}')
    print(f"Device A's and Device B's readings match each other (same physical register): {ok_match}")

    if ok_match:
        print('\nPASS: one physical Modbus register was fanned out to two separate '
              "ThingsBoard devices using only two ordinary 'slaves' entries -- the stock "
              'Modbus connector needs no code changes to do this.')
        return 0
    print('\nFAIL')
    return 1


if __name__ == '__main__':
    sys.exit(main())
