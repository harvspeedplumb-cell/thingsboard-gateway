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

from thingsboard_gateway.connectors.converter import Converter
from thingsboard_gateway.gateway.entities.converted_data import ConvertedData


class HVAKelvinV1GpioUplinkConverter(Converter):
    """
    Turns the connector's already-decoded GPIO/WAN readings into a ConvertedData object.

    Unlike most uplink converters in this repo, there is no raw byte/frame parsing to do
    here -- the connector already knows exactly which DI/DO channel changed and what its
    logical state is, since it owns the gpiod line requests directly. This converter's
    job is just to shape that into the gateway's telemetry/attribute data model, so the
    device-facing config (deviceName/deviceType/reportStrategy) still flows through the
    normal converter interface instead of being hardcoded into the connector.

    Expected `data` shape (produced by the connector):
        {
            "telemetry": {"DI1": True, "DO2": False, ...},   # optional
            "attributes": {"active_wan_interface": "ethernet", ...},  # optional
            "ts": 1234567890123,                              # optional, ms epoch
        }
    """

    def __init__(self, config, logger):
        self._log = logger
        self._device_name = config.get('deviceName')
        self._device_type = config.get('deviceType', 'default')

    def convert(self, config, data) -> ConvertedData:
        device_name = (config or {}).get('deviceName', self._device_name)
        device_type = (config or {}).get('deviceType', self._device_type)
        converted_data = ConvertedData(device_name=device_name, device_type=device_type)

        if not data:
            return converted_data

        telemetry = data.get('telemetry') or {}
        attributes = data.get('attributes') or {}
        ts = data.get('ts')

        try:
            if telemetry:
                entry = dict(telemetry)
                if ts is not None:
                    entry['ts'] = ts
                converted_data.add_to_telemetry(entry)
            if attributes:
                converted_data.add_to_attributes(attributes)
        except Exception as e:
            self._log.exception('Failed converting HVAKelvinV1 GPIO data to ConvertedData: %s', e)

        return converted_data
