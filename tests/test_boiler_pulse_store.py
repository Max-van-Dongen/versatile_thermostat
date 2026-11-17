"""Persistence of completed pulse observations across central config reloads."""

from types import SimpleNamespace
from unittest.mock import Mock

from custom_components.versatile_thermostat.boiler_flow import FlowSettings
from custom_components.versatile_thermostat.boiler_pulse_store import PulseLearningStore


async def test_pulse_learning_reload_uses_only_matching_actuator(hass):
    def flow(entity):
        scheduler = Mock()
        scheduler.dump_learning.return_value = {}
        return SimpleNamespace(entity_id=entity, sensor_id="sensor.water",
                               settings=FlowSettings(pulse_enabled=True), pulse_scheduler=scheduler)

    original = flow("number.flow")
    writer = PulseLearningStore(hass, "test-central", original)
    await writer.async_load()
    original.pulse_scheduler.dump_learning.return_value = {"residual": {"climate.bedroom": 0.15}}
    writer.track()
    await writer.async_flush()

    same = flow("number.flow")
    await PulseLearningStore(hass, "test-central", same).async_load()
    same.pulse_scheduler.restore_learning.assert_called_once_with({"residual": {"climate.bedroom": 0.15}})

    replaced = flow("number.other_boiler")
    await PulseLearningStore(hass, "test-central", replaced).async_load()
    replaced.pulse_scheduler.restore_learning.assert_not_called()
