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


import logging
import unittest
from unittest.mock import MagicMock

from bacpypes3.basetypes import BinaryPV, DailySchedule, PriorityValue, TimeValue
from bacpypes3.constructeddata import AnyAtomic, ArrayOf
from bacpypes3.primitivedata import Boolean, CharacterString, Null, ObjectIdentifier, Real, Unsigned

from thingsboard_gateway.connectors.bacnet.bacnet_uplink_converter import AsyncBACnetUplinkConverter
from thingsboard_gateway.connectors.bacnet.constants import SUPPORTED_OBJECTS_TYPES
from thingsboard_gateway.connectors.bacnet.entities.ede_connector_config import EDEConnectorConfig


def _result(object_type, object_id, property_id, value):
    """Shape of one decoded ReadPropertyMultiple result, as produced by Application.decode_tag_list()."""
    return ObjectIdentifier(f"{object_type},{object_id}"), property_id, None, value


def _item(object_type, object_id, property_id, key):
    return {'type': 'timeseries', 'objectType': object_type, 'objectId': object_id,
            'propertyId': property_id, 'key': key}


class MultiStateAndScheduleConversionTest(unittest.TestCase):
    def setUp(self):
        logger = logging.getLogger('Bacnet test')
        logger.trace = logger.debug
        config = MagicMock(device_name='test device', device_type='default', report_strategy=None)
        self.converter = AsyncBACnetUplinkConverter(config, logger)

    def _convert(self, items, results):
        converted = self.converter.convert(items, results)
        values = {}
        for entry in converted.telemetry:
            values.update({k.key if hasattr(k, 'key') else k: v for k, v in entry.values.items()})
        return values

    def test_multistate_present_values_are_numeric(self):
        items = [_item('multiStateInput', 1, 'presentValue', 'msi'),
                 _item('multiStateOutput', 1, 'presentValue', 'mso'),
                 _item('multiStateValue', 1, 'presentValue', 'msv')]
        results = [_result('multiStateInput', 1, 'presentValue', Unsigned(2)),
                   _result('multiStateOutput', 1, 'presentValue', Unsigned(3)),
                   _result('multiStateValue', 1, 'presentValue', Unsigned(4))]

        self.assertEqual({'msi': 2, 'mso': 3, 'msv': 4}, self._convert(items, results))

    def test_multistate_state_text(self):
        state_text = ArrayOf(CharacterString)(['Off', 'Low', 'High'])
        values = self._convert([_item('multiStateValue', 1, 'stateText', 'states')],
                               [_result('multiStateValue', 1, 'stateText', state_text)])

        self.assertEqual({'states': "['Off', 'Low', 'High']"}, values)

    def test_schedule_present_value_unwraps_real(self):
        values = self._convert([_item('schedule', 1, 'presentValue', 'sched')],
                               [_result('schedule', 1, 'presentValue', AnyAtomic(Real(21.0)))])

        self.assertEqual({'sched': 21.0}, values)

    def test_schedule_present_value_unwraps_unsigned(self):
        values = self._convert([_item('schedule', 2, 'presentValue', 'sched')],
                               [_result('schedule', 2, 'presentValue', AnyAtomic(Unsigned(3)))])

        self.assertEqual({'sched': 3}, values)

    def test_schedule_present_value_binary_matches_binary_objects(self):
        values = self._convert([_item('schedule', 3, 'presentValue', 'sched')],
                               [_result('schedule', 3, 'presentValue', AnyAtomic(BinaryPV('active')))])

        self.assertEqual({'sched': '1'}, values)

    def test_schedule_null_present_value_is_skipped(self):
        values = self._convert([_item('schedule', 4, 'presentValue', 'sched')],
                               [_result('schedule', 4, 'presentValue', AnyAtomic(Null(())))])

        self.assertEqual({}, values)

    def test_weekly_schedule(self):
        day = DailySchedule(daySchedule=[TimeValue(time=(7, 0, 0, 0), value=Real(21.0))])
        weekly = ArrayOf(DailySchedule)([day])
        values = self._convert([_item('schedule', 1, 'weeklySchedule', 'weekly')],
                               [_result('schedule', 1, 'weeklySchedule', weekly)])

        self.assertEqual({'weekly': "[[('07:00:00.00', '21.0')]]"}, values)

    def test_priority_array_entries_not_duplicated(self):
        priority_array = ArrayOf(PriorityValue)([PriorityValue(null=())] * 7 +
                                                [PriorityValue(unsigned=2)] +
                                                [PriorityValue(null=())] * 8)
        values = self._convert([_item('multiStateOutput', 1, 'priorityArray', 'pa')],
                               [_result('multiStateOutput', 1, 'priorityArray', priority_array)])

        self.assertEqual({'pa': str([None] * 7 + ['2'] + [None] * 8)}, values)

    def test_existing_value_formats_unchanged(self):
        items = [_item('analogValue', 1, 'presentValue', 'av'),
                 _item('binaryValue', 1, 'presentValue', 'bv'),
                 _item('analogValue', 1, 'outOfService', 'oos')]
        results = [_result('analogValue', 1, 'presentValue', Real(21.456)),
                   _result('binaryValue', 1, 'presentValue', BinaryPV('active')),
                   _result('analogValue', 1, 'outOfService', Boolean(False))]

        self.assertEqual({'av': 21.46, 'bv': '1', 'oos': '0'}, self._convert(items, results))


class MultiStateAndScheduleSupportedTypesTest(unittest.TestCase):
    def test_supported_object_types(self):
        for object_type in ('multiStateInput', 'multiStateOutput', 'multiStateValue', 'schedule'):
            self.assertIn(object_type, SUPPORTED_OBJECTS_TYPES.values())

    def test_ede_object_type_codes(self):
        objects = [{'object-type': code, 'object-instance': '1', 'object-name': name}
                   for code, name in (('13', 'msi'), ('14', 'mso'), ('17', 'sched'), ('19', 'msv'))]

        config = EDEConnectorConfig._get_device_objects(objects)

        self.assertEqual(['multiStateInput', 'multiStateOutput', 'schedule', 'multiStateValue'],
                         [item['objectType'] for item in config])


if __name__ == '__main__':
    unittest.main()
