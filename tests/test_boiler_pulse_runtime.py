"""Check the sensor and valve reports used by central pulse control."""

from datetime import datetime, timezone
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.versatile_thermostat.boiler_flow import (
    FLOW_ENABLED, FLOW_SENSOR, PULSE_ENABLED, PULSE_GAS_RATE,
)
from custom_components.versatile_thermostat.feature_boiler_flow import BoilerFlowManager
from custom_components.versatile_thermostat.valve_command import ValveCommandMixin

PULSE_MODULE = "custom_components.versatile_thermostat.feature_boiler_pulse"


@pytest.mark.parametrize(("rate", "unit", "state_class", "age", "assumed", "expected"), [
    ("0", "m³/h", "measurement", 3, False, False),
    ("0.005", "m³/h", "measurement", 89, False, True),
    ("1", "L/min", None, 0, False, True),
    ("1", "L/min", None, 91, False, None),
    ("1", "m³", "total_increasing", 0, False, None),
    ("1", "m³/h", "total", 0, False, None),
    ("1", "m³/h", "measurement", 0, True, None),
    ("unavailable", "L/min", None, 0, False, None),
    ("nan", "L/min", None, 0, False, None),
    ("-1", "L/min", None, 0, False, None),
])
async def test_burner_accepts_only_recent_ch_gas_rate(hass, rate, unit, state_class, age, assumed, expected):
    flow = BoilerFlowManager(hass, {FLOW_ENABLED: True, PULSE_ENABLED: True, PULSE_GAS_RATE: "sensor.ch_rate"})
    hass.states.async_set("sensor.ch_rate", rate, {
        "unit_of_measurement": unit, "state_class": state_class, "assumed_state": assumed,
    })
    reported = hass.states.get("sensor.ch_rate").last_reported.timestamp()
    with patch(PULSE_MODULE + ".time.time", return_value=reported + age):
        assert flow.pulse._burner() is expected


async def test_full_open_credit_uses_all_reported_degrees_and_caps(hass):
    class Zone(ValveCommandMixin):
        pass

    first = SimpleNamespace(entity_id="number.radiator", closing_degree_entity_id="number.closing",
                            is_initialized=True, max_opening_degree=100)
    second = SimpleNamespace(entity_id="number.radiator2", is_initialized=True, max_opening_degree=None)
    zone = Zone()
    zone.hass = hass
    zone.activable_underlying_entities = [first, second]
    attrs = {"min": 0, "max": 100}
    hass.states.async_set(first.entity_id, 100, attrs)
    hass.states.async_set(first.closing_degree_entity_id, 0, attrs)
    hass.states.async_set(second.entity_id, 64, {**attrs, "max": 128})
    assert zone.boiler_pulse_valve_feedback == 0.5
    hass.states.async_set(second.entity_id, 128, {**attrs, "max": 128})
    assert zone.boiler_pulse_valve_feedback == 1.0
    hass.states.async_set(first.closing_degree_entity_id, 15, attrs)
    assert zone.boiler_pulse_valve_feedback == 0.85
    first.max_opening_degree = 90
    assert zone.boiler_pulse_valve_feedback is None
    first.max_opening_degree = 100
    hass.states.async_set(first.entity_id, 100, {**attrs, "assumed_state": True})
    assert zone.boiler_pulse_valve_feedback is None


def test_pulse_and_handover_bypass_period_and_change_threshold():
    class Zone(ValveCommandMixin):
        is_sleeping = False
        _auto_regulation_period_min = 30
        _auto_regulation_dpercent = 10
        _valve_open_percent = 95
        now = _last_calculation_timestamp = datetime.now(timezone.utc)
        update_custom_attributes = Mock()
        async_write_ha_state = Mock()

        @property
        def valve_open_percent(self):
            return self._valve_open_percent

    zone = Zone()
    zone.prop_algorithm = SimpleNamespace(boiler_pulse_protocol=1, pulse_opening=1.0)
    zone.boiler_flow_context = {"pulse_opening": 1.0}
    assert zone.apply_valve_command_percent(1.0) == 1.0
    zone.prop_algorithm.pulse_opening = None
    zone.boiler_flow_context = {}
    assert zone.apply_valve_command_percent(0.95) == 0.95


async def test_stuck_valve_hands_back_to_normal_until_readback_moves(hass):
    flow = BoilerFlowManager(hass, {FLOW_ENABLED: True, PULSE_ENABLED: True, FLOW_SENSOR: "sensor.supply"})
    hass.states.async_set("sensor.supply", 34, {"unit_of_measurement": "°C"})
    algo = SimpleNamespace(boiler_pulse_protocol=1, boiler_heat_request=lambda _: {"demand": 0.15, "pulse_eligible": True})
    room = SimpleNamespace(
        entity_id="climate.test", is_ready=True, is_used_by_central_boiler=True, is_device_active=False,
        hvac_mode="heat", prop_algorithm=algo, target_temperature=20, current_temperature=19.8,
        last_temperature_measure=datetime.now(timezone.utc), valve_open_percent=100,
        boiler_pulse_valve_feedback=0.0, async_control_heating=AsyncMock(),
    )
    hass.data["climate"] = SimpleNamespace(entities=[room])
    with patch(PULSE_MODULE + ".time", wraps=time) as clock:
        clock.monotonic.return_value = 10
        assert not flow.plan_pulse(True, False)
        assert flow.pulse.decision.phase == "opening"
        await hass.async_block_till_done()
        clock.monotonic.return_value = 210
        assert not flow.plan_pulse(True, False)
        assert flow.pulse.decision.phase == "coast"
        await hass.async_block_till_done()
        assert not flow.plan_pulse(True, False)
        await hass.async_block_till_done()
        assert flow.pulse.decision is None
        assert not flow.plan_pulse(True, False)
        assert not flow.pulse_permits_heat()
        room.boiler_pulse_valve_feedback = 0.25
        assert not flow.plan_pulse(True, False)
        assert flow.pulse.decision.phase == "opening"
        await hass.async_block_till_done()
