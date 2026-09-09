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

from asyncio import Queue
from unittest.mock import AsyncMock, MagicMock

from tests.unit.connectors.bacnet.bacnet_base_test import BacnetBaseTestCase


class FakeObjectIterator:
    """Stands in for application.ObjectIterator, replaying a fixed sequence of
    (results, config) chunks the way a real device with more objects than fit in a
    single ReadPropertyMultiple request would - see ObjectIterator.get_limit()."""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def get_next(self):
        results, config = self._chunks.pop(0)
        all_done = len(self._chunks) == 0
        return results, config, all_done


class BacnetReadMultiplePropertiesTestCase(BacnetBaseTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.connector._AsyncBACnetConnector__data_to_convert_queue = Queue(1_000_000)
        self.connector._AsyncBACnetConnector__application = MagicMock()
        self.connector._AsyncBACnetConnector__stopped = False

    async def test_single_chunk_read_queues_one_item(self):
        """Sanity check: a device whose whole poll fits in one RPM request still
        results in exactly one queued item."""
        chunk = ([('obj1', 'presentValue', None, 1.23)], [{'objectId': 1}])
        self.connector._AsyncBACnetConnector__application.get_device_values = AsyncMock(
            return_value=FakeObjectIterator([chunk])
        )

        await self.connector._AsyncBACnetConnector__read_multiple_properties(self.device)

        queue = self.connector._AsyncBACnetConnector__data_to_convert_queue
        self.assertEqual(queue.qsize(), 1)
        device, config, results = queue.get_nowait()
        self.assertIs(device, self.device)
        self.assertEqual(config, [{'objectId': 1}])
        self.assertEqual(results, [('obj1', 'presentValue', None, 1.23)])

    async def test_multi_chunk_read_is_merged_into_a_single_queue_item(self):
        """Regression test for https://github.com/thingsboard/thingsboard-gateway/issues/2112.

        When a device's objects don't fit in a single ReadPropertyMultiple request (because
        of the device's advertised max APDU length, see ObjectIterator.get_limit()), the read
        is split across several chunks. Each chunk used to be queued for conversion
        separately, which meant uplink_converter.convert() ran once per chunk and produced a
        separate telemetry message (with its own timestamp) for a single logical poll - the
        exact "data from the same device call arriving in different telemetry messages"
        symptom reported in #2104 and reopened in #2112. All chunks from one poll must be
        merged into a single queue item so they become a single telemetry message.
        """
        chunks = [
            ([('obj1', 'presentValue', None, 1.0)], [{'objectId': 1}]),
            ([('obj2', 'presentValue', None, 2.0)], [{'objectId': 2}]),
            ([('obj3', 'presentValue', None, 3.0)], [{'objectId': 3}]),
        ]
        self.connector._AsyncBACnetConnector__application.get_device_values = AsyncMock(
            return_value=FakeObjectIterator(chunks)
        )

        await self.connector._AsyncBACnetConnector__read_multiple_properties(self.device)

        queue = self.connector._AsyncBACnetConnector__data_to_convert_queue
        self.assertEqual(queue.qsize(), 1, "all chunks from a single poll must be merged into one queue item")

        device, config, results = queue.get_nowait()
        self.assertIs(device, self.device)
        self.assertEqual(config, [{'objectId': 1}, {'objectId': 2}, {'objectId': 3}])
        self.assertEqual(results, [
            ('obj1', 'presentValue', None, 1.0),
            ('obj2', 'presentValue', None, 2.0),
            ('obj3', 'presentValue', None, 3.0),
        ])

    async def test_empty_chunks_are_skipped_and_nothing_is_queued_if_all_empty(self):
        chunks = [([], []), ([], [])]
        self.connector._AsyncBACnetConnector__application.get_device_values = AsyncMock(
            return_value=FakeObjectIterator(chunks)
        )

        await self.connector._AsyncBACnetConnector__read_multiple_properties(self.device)

        queue = self.connector._AsyncBACnetConnector__data_to_convert_queue
        self.assertEqual(queue.qsize(), 0)

    async def test_no_iterator_does_not_raise(self):
        self.connector._AsyncBACnetConnector__application.get_device_values = AsyncMock(return_value=None)

        await self.connector._AsyncBACnetConnector__read_multiple_properties(self.device)

        queue = self.connector._AsyncBACnetConnector__data_to_convert_queue
        self.assertEqual(queue.qsize(), 0)
