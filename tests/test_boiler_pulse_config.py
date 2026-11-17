"""Configuration and options for full-open boiler pulses."""

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.versatile_thermostat.boiler_flow import (
    FLOW_ENABLED,
    FLOW_ENTITY,
    FLOW_SENSOR,
    PULSE_ENABLED,
    PULSE_FLAME,
    PULSE_GAS_RATE,
    PULSE_OVERSHOOT,
    PULSE_UNDERSHOOT,
    FlowSettings,
)
from custom_components.versatile_thermostat.config_flow import VersatileThermostatBaseConfigFlow
from custom_components.versatile_thermostat.config_schema import STEP_CENTRAL_BOILER_SCHEMA


def test_existing_entries_keep_continuous_flow():
    settings = FlowSettings.from_entry({FLOW_ENABLED: True})
    assert not settings.pulse_enabled
    assert settings.pulse_flame_sensor is None
    assert settings.pulse_gas_rate_sensor is None
    assert (settings.pulse_overshoot, settings.pulse_undershoot) == (0.2, 0.1)


@pytest.mark.parametrize("key", [PULSE_OVERSHOOT, PULSE_UNDERSHOOT])
@pytest.mark.parametrize("invalid", [0, 1.1, float("nan")])
def test_invalid_pulse_band_rejected(key, invalid):
    with pytest.raises(ValueError):
        FlowSettings.from_entry({PULSE_ENABLED: True, key: invalid})


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({PULSE_ENABLED: True}, {PULSE_ENABLED: "pulse_requires_flow"}),
        ({FLOW_ENABLED: True, PULSE_ENABLED: True}, {PULSE_ENABLED: "pulse_requires_flow"}),
        (
            {FLOW_ENABLED: True, PULSE_ENABLED: True, FLOW_SENSOR: "sensor.water", PULSE_FLAME: "binary_sensor.missing"},
            {PULSE_FLAME: "unknown_entity"},
        ),
    ],
)
async def test_pulse_form_reports_field_errors(hass, data, expected):
    flow = VersatileThermostatBaseConfigFlow({})
    flow.hass = hass
    result = await flow.async_step_central_boiler(data)
    assert result["errors"] == expected


@pytest.mark.parametrize("use_flame", [False, True])
async def test_pulse_config_accepts_celsius_sensor_with_optional_flame(hass, use_flame):
    flow = VersatileThermostatBaseConfigFlow({})
    flow.hass = hass
    celsius = "\N{DEGREE SIGN}C"
    hass.states.async_set("number.flow", 0, {"min": 0, "max": 90, "step": 0.01, "unit_of_measurement": celsius})
    hass.states.async_set("sensor.water", 37, {"unit_of_measurement": celsius})
    data = {FLOW_ENABLED: True, PULSE_ENABLED: True, FLOW_ENTITY: "number.flow", FLOW_SENSOR: "sensor.water"}
    if use_flame:
        hass.states.async_set("binary_sensor.ch_flame", "off")
        data[PULSE_FLAME] = "binary_sensor.ch_flame"
    settings = STEP_CENTRAL_BOILER_SCHEMA(data)
    with patch.object(flow, "generic_step", new_callable=AsyncMock) as next_step:
        await flow.async_step_central_boiler(settings)
        next_step.assert_awaited_once()
    assert FlowSettings.from_entry(settings).pulse_flame_sensor == data.get(PULSE_FLAME)


@pytest.mark.parametrize(
    ("unit", "with_flame", "error"),
    [("m\u00b3/h", False, None), ("L/min", False, None),
     ("m\u00b3", False, "pulse_gas_unit"), ("m\u00b3/h", True, "pulse_one_burner_sensor")],
)
async def test_pulse_gas_measurement_excludes_cumulative_and_duplicate_feedback(hass, unit, with_flame, error):
    flow = VersatileThermostatBaseConfigFlow({})
    flow.hass = hass
    celsius = "\N{DEGREE SIGN}C"
    hass.states.async_set("number.flow", 0, {"min": 0, "max": 90, "step": 0.01, "unit_of_measurement": celsius})
    hass.states.async_set("sensor.water", 37, {"unit_of_measurement": celsius})
    hass.states.async_set("sensor.gas", 0, {"unit_of_measurement": unit})
    data = {FLOW_ENABLED: True, PULSE_ENABLED: True, FLOW_ENTITY: "number.flow",
            FLOW_SENSOR: "sensor.water", PULSE_GAS_RATE: "sensor.gas"}
    if with_flame:
        hass.states.async_set("binary_sensor.flame", "off")
        data[PULSE_FLAME] = "binary_sensor.flame"
    settings = STEP_CENTRAL_BOILER_SCHEMA(data)
    with patch.object(flow, "generic_step", new_callable=AsyncMock) as next_step:
        result = await flow.async_step_central_boiler(settings)
        if error:
            assert result["errors"] == {PULSE_GAS_RATE: error}
            next_step.assert_not_awaited()
        else:
            next_step.assert_awaited_once()
            assert FlowSettings.from_entry(settings).pulse_gas_rate_sensor == "sensor.gas"
