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

from thingsboard_gateway.connectors.hva_kelvin_v1_gpio.gpio_map import do_state_to_raw
from thingsboard_gateway.connectors.converter import Converter


class HVAKelvinV1GpioDownlinkConverter(Converter):
    """
    Translates a single requested DO write (from an RPC call or a shared attribute
    update, both normalised by the connector into the same shape before reaching here)
    into the raw gpiod line Value to write.

    Deliberately does NOT touch the gpiod line itself -- the connector owns the single
    long-lived LineRequest for all DO lines and is the only thing allowed to call
    set_value/set_values on it, so that every write goes through the one place that also
    tracks last-known state and can re-publish it. This converter is pure data shaping,
    consistent with how every other connector in this repo splits "decide what to write"
    (converter) from "actually write it" (connector).

    Expected `config` (from the device's "digitalOutputs" list in hva_kelvin_v1_gpio.json):
        {"DO1": {"offset": 24, "activeLow": true}, "DO2": {...}, ...}

    Expected `data`:
        {"channel": "DO1", "state": true}
    """

    def __init__(self, config, logger):
        self._log = logger

    def convert(self, config, data):
        if not data or 'channel' not in data or 'state' not in data:
            self._log.error('HVAKelvinV1 GPIO downlink request missing "channel" or "state": %s', data)
            return None

        channel = data['channel']
        channel_config = (config or {}).get(channel)
        if channel_config is None:
            self._log.error('HVAKelvinV1 GPIO downlink request for unknown DO channel "%s"', channel)
            return None

        requested_state = bool(data['state'])
        active_low = channel_config.get('activeLow', True)

        return {
            'channel': channel,
            'offset': channel_config['offset'],
            'state': requested_state,
            'value': do_state_to_raw(requested_state, active_low=active_low),
        }
