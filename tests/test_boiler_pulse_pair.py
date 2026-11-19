"""Pulse control from central demand through SmartPI and the boiler service."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from homeassistant.const import UnitOfTemperature
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import async_mock_service

from custom_components.versatile_thermostat.boiler_flow import (
    FLOW_ENABLED, FLOW_ENTITY, FLOW_MAX, FLOW_MIN, FLOW_REFERENCE, FLOW_SENSOR, PULSE_ENABLED,
    PULSE_OVERSHOOT, PULSE_UNDERSHOOT,
)
from custom_components.versatile_thermostat.const import (
    CONF_CENTRAL_BOILER_ACTIVATION_DELAY_SEC, CONF_CENTRAL_BOILER_ACTIVATION_SRV,
    CONF_CENTRAL_BOILER_DEACTIVATION_SRV, CONF_KEEP_ALIVE_BOILER_DELAY_SEC,
)

from .test_central_boiler_startup import MANAGER_MODULE, _build_manager
from .test_boiler_flow_pair import SmartPI, VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.const import AB_MIN_SAMPLES_B


class ReportedValveRoom:
    """SmartPI gets the central context; a separate readback simulates the TRV."""

    is_ready = True
    hvac_mode = "heat"
    is_used_by_central_boiler = True
    current_outdoor_temperature = 7.0
    target_temperature = 20.5

    def __init__(self, hass, manager, name, temp, demand):
        self.manager = manager
        self.entity_id = f"climate.{name}"
        self.current_temperature = temp
        self.last_temperature_measure = dt_util.utcnow()
        self.valve_open_percent = 0
        self.boiler_pulse_valve_feedback = 0.0
        self.reported_positions = None
        algo = self.prop_algorithm = SmartPI(hass, 5, 0, 0, name)
        algo.configure_valve_linearization(False, valve_mode=True)
        algo.est.learn_ok_count_a = 2
        algo.est.b_meas_hist.extend([algo.est.b] * AB_MIN_SAMPLES_B)
        algo._set_linear_output(demand)

    @property
    def is_device_active(self):
        return getattr(self, "reported_active", bool(self.boiler_pulse_valve_feedback))

    @property
    def boiler_pulse_valves_report_closed(self):
        positions = self.reported_positions
        if positions is None:
            positions = (self.boiler_pulse_valve_feedback,)
        return bool(positions) and all(position == 0 for position in positions)

    async def async_control_heating(self, force=False):
        context = self.manager.boiler_flow.context_for(self)
        self.prop_algorithm.set_boiler_context(context, self.current_temperature, VThermHvacMode_HEAT)
        self.valve_open_percent = round(100 * self.prop_algorithm.on_percent)


@pytest.fixture
def pulse_system(hass, request):
    manager = _build_manager(hass)
    manager.post_init({
        CONF_CENTRAL_BOILER_ACTIVATION_SRV: "number.flow/number.set_value/value:75",
        CONF_CENTRAL_BOILER_DEACTIVATION_SRV: "number.flow/number.set_value/value:0",
        CONF_CENTRAL_BOILER_ACTIVATION_DELAY_SEC: 30,
        CONF_KEEP_ALIVE_BOILER_DELAY_SEC: 60,
        FLOW_ENABLED: True, FLOW_ENTITY: "number.flow", FLOW_SENSOR: "sensor.water",
        FLOW_MIN: 50, FLOW_MAX: 75, FLOW_REFERENCE: 75, PULSE_ENABLED: True,
        **getattr(request, "param", {}),
    })
    manager._total_power_active_threshold_number_entity.native_value = 0
    manager._nb_active_device_threshold_number_entity.native_value = 1
    unit = {"unit_of_measurement": UnitOfTemperature.CELSIUS}
    hass.states.async_set("number.flow", 0, {**unit, "min": 0, "max": 90, "step": 0.01})
    hass.states.async_set("sensor.water", 31, unit)
    cold = ReportedValveRoom(hass, manager, "bedroom", 20.35, 0.1)
    warm = ReportedValveRoom(hass, manager, "kitchen", 20.75, 0.15)
    hass.data["climate"] = SimpleNamespace(entities=[cold, warm])
    calls = async_mock_service(hass, "number", "set_value")
    return manager, cold, warm, calls


def commands(calls):
    return [float(call.data["value"]) for call in calls]


@pytest.fixture
def kitchen_system(pulse_system):
    manager, living, kitchen, calls = pulse_system
    living.entity_id = "climate.living_room"
    living.current_temperature = 21.0
    living.prop_algorithm._set_linear_output(0.0)
    living.last_temperature_measure -= timedelta(days=1, seconds=1)
    living.boiler_pulse_valve_feedback = None
    living.reported_positions = (0, 0)
    kitchen.current_temperature = 20.7
    kitchen.prop_algorithm._set_linear_output(0.02)
    return manager, living, kitchen, calls


async def test_satisfied_stale_living_room_leaves_kitchen_free_to_coast_and_pulse(hass, kitchen_system):
    manager, living, kitchen, calls = kitchen_system
    flow = manager.boiler_flow
    kitchen.valve_open_percent = 2
    manager._nb_active_device_number_entity.native_value = 1
    with patch(MANAGER_MODULE + ".async_call_later", return_value=lambda: None) as timer:
        await manager.calculate_central_boiler_state(None)
        await hass.async_block_till_done()
        assert flow.pulse.diagnostics["excluded_zones"] == {living.entity_id: "room_measurement_stale"}
        assert flow.pulse.diagnostics["phase"] == "coast"
        assert "pulse_opening" not in (flow.context_for(living) or {})
        assert flow.context_for(kitchen)["pulse_opening"] == 0
        assert kitchen.valve_open_percent == 0
        assert kitchen.prop_algorithm.boiler_heat_request(1.0)["demand"] == pytest.approx(0.02)

        # Old closed positions permit the kitchen to prepare. They cannot confirm travel.
        kitchen.current_temperature = 20.4
        kitchen.boiler_pulse_valve_feedback = None
        kitchen.reported_positions = (0, 0)
        await manager.calculate_central_boiler_state(None)
        await hass.async_block_till_done()
        assert flow.pulse.diagnostics["phase"] == "opening"
        assert kitchen.valve_open_percent == 100
        await manager._keep_alive_boiler(None)
        assert not timer.called
        assert set(commands(calls)) <= {0}

        kitchen.boiler_pulse_valve_feedback = 1.0
        kitchen.reported_positions = (1, 1)
        await manager.calculate_central_boiler_state(None)
        timer.assert_called_once()
        await timer.call_args.args[2](None)
        assert manager.is_on is True
        assert commands(calls)[-1] == 75

        # End at 20.6 so residual radiator heat has room below the 20.7 cap.
        kitchen.current_temperature = 20.6
        await manager.calculate_central_boiler_state(None)
        await hass.async_block_till_done()
        assert flow.pulse.diagnostics["phase"] == "coast"
        assert not manager.is_on
        assert kitchen.valve_open_percent == 0
        assert commands(calls)[-1] == 0


@pytest.mark.parametrize("condition", ["demand", "cold", "active", "unknown", "conflicting", "safety"])
async def test_stale_living_room_requires_all_idle_evidence(hass, kitchen_system, condition):
    manager, living, kitchen, _ = kitchen_system
    if condition == "demand":
        living.prop_algorithm._set_linear_output(0.02)
    elif condition == "cold":
        living.current_temperature = living.target_temperature - 0.1
    elif condition == "active":
        living.reported_active = True
    elif condition == "unknown":
        living.reported_positions = (0, None)
    elif condition == "conflicting":
        living.reported_positions = (0, 0.5)
    else:
        living._safety_state = True
    await manager.calculate_central_boiler_state(None)
    await hass.async_block_till_done()
    diagnostics = manager.boiler_flow.pulse.diagnostics
    assert diagnostics["excluded_zones"] == {}
    assert diagnostics["blocking_zone"] == living.entity_id
    assert diagnostics["phase"] == "normal"
    assert diagnostics["reason"] == ("zone_safety" if condition == "safety" else "room_measurement_stale")
    assert "pulse_opening" not in (manager.boiler_flow.context_for(kitchen) or {})


@pytest.mark.parametrize("age", [timedelta(hours=1), timedelta(days=1, seconds=-1), timedelta(days=1)])
@pytest.mark.freeze_time("2026-11-19 15:45:00")
async def test_kitchen_report_through_24_hours_can_request_a_pulse(hass, kitchen_system, freezer, age):
    manager, _, kitchen, calls = kitchen_system
    kitchen.current_temperature = 18.9
    kitchen.safety_manager = SimpleNamespace(safety_delay_min=60, is_safety_detected=False)
    kitchen.last_temperature_measure = dt_util.utcnow() - age

    await manager.calculate_central_boiler_state(None)
    await hass.async_block_till_done()
    diagnostics = manager.boiler_flow.pulse.diagnostics
    assert diagnostics["room_temperature_timeout_sec"] == 86400
    assert diagnostics["blocking_zone"] is None
    assert diagnostics["requested_valves"] == [kitchen.entity_id]
    assert kitchen.valve_open_percent == 100
    assert not manager.is_on
    assert set(commands(calls)) <= {0}


@pytest.mark.freeze_time("2026-11-19 15:45:00")
async def test_cold_kitchen_resumes_on_fresh_report_after_timeout(hass, kitchen_system, freezer):
    manager, _, kitchen, _ = kitchen_system
    kitchen.current_temperature = 18.9
    kitchen.last_temperature_measure = dt_util.utcnow() - timedelta(days=1, seconds=1)

    await manager.calculate_central_boiler_state(None)
    await hass.async_block_till_done()
    flow = manager.boiler_flow
    assert flow.pulse.diagnostics["blocking_zone"] == kitchen.entity_id
    assert flow.pulse.diagnostics["reason"] == "room_measurement_stale"
    assert "pulse_opening" not in (flow.context_for(kitchen) or {})

    kitchen.last_temperature_measure = dt_util.utcnow()
    await manager.calculate_central_boiler_state(None)
    await hass.async_block_till_done()
    assert flow.pulse.diagnostics["blocking_zone"] is None
    assert flow.pulse.diagnostics["requested_valves"] == [kitchen.entity_id]
    assert kitchen.valve_open_percent == 100


@pytest.mark.parametrize("report", ["future", "missing_time", "missing_room", "missing_target"])
@pytest.mark.freeze_time("2026-11-19 15:45:00")
async def test_invalid_kitchen_measurement_is_unavailable_immediately(hass, kitchen_system, freezer, report):
    manager, _, kitchen, _ = kitchen_system
    if report == "future":
        kitchen.last_temperature_measure = dt_util.utcnow() + timedelta(seconds=1)
    elif report == "missing_time":
        kitchen.last_temperature_measure = None
    elif report == "missing_room":
        kitchen.current_temperature = None
    else:
        kitchen.target_temperature = None
    await manager.calculate_central_boiler_state(None)
    await hass.async_block_till_done()
    diagnostics = manager.boiler_flow.pulse.diagnostics
    assert diagnostics["reason"] == "room_measurement_unavailable"
    assert diagnostics["blocking_zone"] == kitchen.entity_id


async def test_full_open_pulse_obeys_readback_delay_and_coasts(hass, pulse_system):
    manager, bedroom, kitchen, calls = pulse_system
    model = bedroom.prop_algorithm.est.save_state()
    with patch(MANAGER_MODULE + ".async_call_later", return_value=lambda: None) as timer:
        await manager.calculate_central_boiler_state(None)
        await hass.async_block_till_done()
        assert bedroom.valve_open_percent == 100
        assert kitchen.valve_open_percent == 0
        assert bedroom.prop_algorithm.boiler_heat_request(1.0)["demand"] == pytest.approx(0.1)
        assert bedroom.prop_algorithm._flow_learning_blocked
        assert set(commands(calls)) <= {0}

        # The count can report demand before the physical opening report arrives.
        manager._nb_active_device_number_entity.native_value = 1
        await manager._keep_alive_boiler(None)
        assert set(commands(calls)) <= {0}
        assert not timer.called

        bedroom.boiler_pulse_valve_feedback = 1.0
        await manager.calculate_central_boiler_state(None)
        timer.assert_called_once()
        await manager._keep_alive_boiler(None)
        assert set(commands(calls)) <= {0}
        await timer.call_args.args[2](None)
        assert manager.is_on is True
        assert commands(calls)[-1] == 75

        # Even before the devices-count sensor updates, reaching the cap
        # withdraws boiler demand and the watchdog must keep the number at zero.
        bedroom.current_temperature = 20.75
        await manager.calculate_central_boiler_state(None)
        assert manager.is_on is False
        assert commands(calls)[-1] == 0
        await hass.async_block_till_done()
        assert bedroom.valve_open_percent == 0
        calls.clear()
        await manager._keep_alive_boiler(None)
        assert set(commands(calls)) <= {0}
    assert bedroom.prop_algorithm.est.save_state() == model


@pytest.mark.parametrize("guard_kick", [False, True])
@pytest.mark.freeze_time("2026-11-18 10:00:00")
async def test_large_kitchen_setpoint_change_opens_only_the_cold_room(hass, kitchen_system, guard_kick, freezer):
    manager, living, kitchen, calls = kitchen_system
    living.last_temperature_measure = dt_util.utcnow()
    living.boiler_pulse_valve_feedback = 0.0
    flow = manager.boiler_flow
    model = kitchen.prop_algorithm.est.save_state()
    manager._nb_active_device_number_entity.native_value = 1
    with patch(MANAGER_MODULE + ".async_call_later", return_value=lambda: None) as timer:
        await manager.calculate_central_boiler_state(None)
        await hass.async_block_till_done()
        assert flow.pulse.diagnostics["phase"] == "coast"

        kitchen.target_temperature = 30.0
        kitchen.prop_algorithm.guards._guard_kick_active = guard_kick
        await manager.calculate_central_boiler_state(None)
        await hass.async_block_till_done()
        assert kitchen.valve_open_percent == 100
        assert living.valve_open_percent == 0
        assert flow.pulse.diagnostics["requested_valves"] == [kitchen.entity_id]
        await manager._keep_alive_boiler(None)
        assert not timer.called
        assert set(commands(calls)) <= {0}

        manager._nb_active_device_number_entity.native_value = 0
        kitchen.boiler_pulse_valve_feedback = 1.0
        await manager.calculate_central_boiler_state(None)
        assert not timer.called
        manager._nb_active_device_number_entity.native_value = 1
        await manager.calculate_central_boiler_state(None)
        timer.assert_called_once()
        await timer.call_args.args[2](None)
        assert flow.pulse.diagnostics["phase"] == "recovery"
        assert manager.is_on
        assert commands(calls)[-1] == 75
        assert kitchen.prop_algorithm.boiler_heat_request(1.0)["demand"] == pytest.approx(0.02)
        assert kitchen.prop_algorithm.est.save_state() == model

        kitchen.prop_algorithm.guards._guard_kick_active = False
        kitchen.target_temperature = 21.5
        freezer.tick(timedelta(milliseconds=500))
        await manager.calculate_central_boiler_state(None)
        assert manager.is_on
        assert commands(calls)[-1] == 75
        timer.assert_called_once()

        kitchen.target_temperature = 20.5
        await manager.calculate_central_boiler_state(None)
        await hass.async_block_till_done()
        assert kitchen.valve_open_percent == 0
        assert not manager.is_on
        assert commands(calls)[-1] == 0


async def test_each_room_stops_independently_and_restarts_at_floor_with_zero_demand(hass, pulse_system):
    manager, bedroom, kitchen, calls = pulse_system
    kitchen.current_temperature = 20.39
    kitchen.prop_algorithm._set_linear_output(0.02)
    with patch(MANAGER_MODULE + ".async_call_later", return_value=lambda: None) as timer:
        await manager.calculate_central_boiler_state(None)
        await hass.async_block_till_done()
        assert bedroom.valve_open_percent == kitchen.valve_open_percent == 100
        assert set(commands(calls)) <= {0}
        bedroom.boiler_pulse_valve_feedback = 1.0
        kitchen.boiler_pulse_valve_feedback = 1.0
        manager._nb_active_device_number_entity.native_value = 2
        await manager.calculate_central_boiler_state(None)
        await timer.call_args.args[2](None)
        assert commands(calls)[-1] == 75

        bedroom.current_temperature = 20.75
        await manager.calculate_central_boiler_state(None)
        await hass.async_block_till_done()
        assert bedroom.valve_open_percent == 0
        assert kitchen.valve_open_percent == 100
        assert manager.is_on
        assert commands(calls)[-1] == 75

        kitchen.current_temperature = 20.75
        await manager.calculate_central_boiler_state(None)
        await hass.async_block_till_done()
        assert bedroom.valve_open_percent == kitchen.valve_open_percent == 0
        assert not manager.is_on
        assert commands(calls)[-1] == 0

        manager._nb_active_device_number_entity.native_value = 0
        bedroom.boiler_pulse_valve_feedback = kitchen.boiler_pulse_valve_feedback = 0.0
        bedroom.prop_algorithm._set_linear_output(0.0)
        bedroom.current_temperature = 20.5
        await manager.calculate_central_boiler_state(None)
        await hass.async_block_till_done()
        assert bedroom.valve_open_percent == 0
        bedroom.current_temperature = 20.4
        await manager.calculate_central_boiler_state(None)
        await hass.async_block_till_done()
        assert bedroom.prop_algorithm.boiler_heat_request(1.0)["demand"] == 0
        assert bedroom.valve_open_percent == 100
        assert kitchen.valve_open_percent == 0
        assert commands(calls)[-1] == 0
        bedroom.boiler_pulse_valve_feedback = 1.0
        manager._nb_active_device_number_entity.native_value = 1
        await manager.calculate_central_boiler_state(None)
        assert timer.call_count == 2
        await timer.call_args.args[2](None)
        assert commands(calls)[-1] == 75
        assert set(commands(calls)) <= {0, 75}


@pytest.mark.parametrize("pulse_system", [{PULSE_ENABLED: False}], indirect=True)
async def test_disabled_pulses_keep_proportional_room_and_flow_control(hass, pulse_system):
    manager, _, kitchen, calls = pulse_system
    kitchen.current_temperature = 20.39
    kitchen.prop_algorithm._set_linear_output(0.02)
    manager._nb_active_device_number_entity.native_value = 1
    with patch(MANAGER_MODULE + ".async_call_later", return_value=lambda: None) as timer:
        await manager.calculate_central_boiler_state(None)
        await timer.call_args.args[2](None)
        await kitchen.async_control_heating()
    assert "pulse_opening" not in (manager.boiler_flow.context_for(kitchen) or {})
    assert 0 < kitchen.valve_open_percent < 100
    assert 50 <= commands(calls)[-1] < 75


async def test_capped_room_keeps_ordinary_heat_before_and_after_kitchen_pulse(hass, pulse_system):
    manager, bedroom, kitchen, calls = pulse_system
    bedroom.prop_algorithm._max_on_percent = 0.6
    await bedroom.async_control_heating()
    bedroom.boiler_pulse_valve_feedback = bedroom.valve_open_percent / 100
    manager._nb_active_device_number_entity.native_value = 1
    kitchen.target_temperature = 30.0
    with patch(MANAGER_MODULE + ".async_call_later", return_value=lambda: None) as timer:
        await manager.calculate_central_boiler_state(None)
        await hass.async_block_till_done()
        await timer.call_args.args[2](None)
        assert "pulse_opening" not in (manager.boiler_flow.context_for(bedroom) or {})
        assert 0 < bedroom.valve_open_percent < 100
        assert kitchen.valve_open_percent == 100
        assert 50 <= commands(calls)[-1] < 75

        kitchen.boiler_pulse_valve_feedback = 1.0
        await manager.calculate_central_boiler_state(None)
        assert commands(calls)[-1] == 75
        kitchen.target_temperature = 20.5
        await manager.calculate_central_boiler_state(None)
        await hass.async_block_till_done()
        assert kitchen.valve_open_percent == 0
        assert manager.is_on
        assert 50 <= commands(calls)[-1] < 75
        timer.assert_called_once()


@pytest.mark.parametrize("pulse_system", [{PULSE_OVERSHOOT: 0.5, PULSE_UNDERSHOOT: 0.5}], indirect=True)
@pytest.mark.freeze_time("2026-11-18 10:00:00")
async def test_only_cooling_room_joins_a_native_heat_call(hass, pulse_system, freezer):
    manager, bedroom, kitchen, calls = pulse_system
    bedroom.prop_algorithm._max_on_percent = 0.6
    await bedroom.async_control_heating()
    bedroom.boiler_pulse_valve_feedback = bedroom.valve_open_percent / 100
    assert 0 < bedroom.boiler_pulse_valve_feedback < 1
    manager._nb_active_device_number_entity.native_value = 1
    kitchen.target_temperature = 20.0
    kitchen.current_temperature = 19.8
    kitchen.prop_algorithm._set_linear_output(0.0)
    with patch(MANAGER_MODULE + ".async_call_later", return_value=lambda: None) as timer:
        await manager.calculate_central_boiler_state(None)
        await hass.async_block_till_done()
        await timer.call_args.args[2](None)
        assert manager.is_on
        assert kitchen.valve_open_percent == 0
        assert 50 <= commands(calls)[-1] < 75

        freezer.tick(timedelta(minutes=5))
        kitchen.current_temperature = 19.7
        kitchen.last_temperature_measure = dt_util.utcnow()
        await manager.calculate_central_boiler_state(None)
        await hass.async_block_till_done()
        assert manager.boiler_flow.pulse.diagnostics["requested_valves"] == [kitchen.entity_id]
        assert kitchen.prop_algorithm.boiler_heat_request(1.0)["demand"] == 0
        assert kitchen.valve_open_percent == 100
        assert 50 <= commands(calls)[-1] < 75

        kitchen.boiler_pulse_valve_feedback = 1.0
        await manager.calculate_central_boiler_state(None)
        assert commands(calls)[-1] == 75
        kitchen.current_temperature = 20.4
        await manager.calculate_central_boiler_state(None)
        await hass.async_block_till_done()
        assert kitchen.valve_open_percent == 0
        assert manager.is_on
        assert 50 <= commands(calls)[-1] < 75
        assert "pulse_opening" not in (manager.boiler_flow.context_for(bedroom) or {})
        timer.assert_called_once()


@pytest.mark.parametrize("pulse_system", [{PULSE_OVERSHOOT: 0.5, PULSE_UNDERSHOOT: 0.5}], indirect=True)
@pytest.mark.parametrize(("kitchen_now", "first_finished"), [
    (19.7, "bedroom"), (19.7, "kitchen"), (19.8, None), (19.9, None),
])
@pytest.mark.freeze_time("2026-11-18 10:00:00")
async def test_cooling_room_joins_at_first_ignition(hass, pulse_system, freezer, kitchen_now, first_finished):
    manager, bedroom, kitchen, calls = pulse_system
    bedroom.target_temperature = kitchen.target_temperature = 20.0
    bedroom.current_temperature = 20.0
    kitchen.current_temperature = 19.8
    kitchen.prop_algorithm._set_linear_output(0.0)
    with patch(MANAGER_MODULE + ".async_call_later", return_value=lambda: None) as timer:
        await manager.calculate_central_boiler_state(None)
        await hass.async_block_till_done()
        assert bedroom.valve_open_percent == kitchen.valve_open_percent == 0

        # At 0.1 C per five minutes the kitchen is ten minutes from its 19.5 C floor.
        freezer.tick(timedelta(minutes=5))
        bedroom.current_temperature = 18.0
        kitchen.current_temperature = kitchen_now
        bedroom.last_temperature_measure = kitchen.last_temperature_measure = dt_util.utcnow()
        await manager.calculate_central_boiler_state(None)
        await hass.async_block_till_done()
        assert bedroom.valve_open_percent == 100
        assert kitchen.prop_algorithm.boiler_heat_request(1.0)["demand"] == 0.0
        if first_finished is None:
            assert kitchen.valve_open_percent == 0
            return
        assert kitchen.valve_open_percent == 100
        assert set(commands(calls)) <= {0}

        bedroom.boiler_pulse_valve_feedback = kitchen.boiler_pulse_valve_feedback = 1.0
        manager._nb_active_device_number_entity.native_value = 2
        await manager.calculate_central_boiler_state(None)
        await timer.call_args.args[2](None)
        assert commands(calls)[-1] == 75

        finish, remaining = (bedroom, kitchen) if first_finished == "bedroom" else (kitchen, bedroom)
        finish.current_temperature = 20.4
        await manager.calculate_central_boiler_state(None)
        await hass.async_block_till_done()
        assert finish.valve_open_percent == 0
        assert remaining.valve_open_percent == 100
        assert manager.is_on
        assert commands(calls)[-1] == 75

        finish.boiler_pulse_valve_feedback = 0.0
        remaining.current_temperature = 20.4
        await manager.calculate_central_boiler_state(None)
        await hass.async_block_till_done()
        assert remaining.valve_open_percent == 0
        assert not manager.is_on
        assert commands(calls)[-1] == 0
        timer.assert_called_once()


@pytest.mark.parametrize("guard", ["guard_cut", "valve_limit", "safety"])
async def test_large_setpoint_change_respects_native_controls(hass, kitchen_system, guard):
    manager, _, kitchen, _ = kitchen_system
    kitchen.target_temperature = 30.0
    if guard == "guard_cut":
        kitchen.prop_algorithm.guards._guard_cut_active = True
    elif guard == "valve_limit":
        kitchen.prop_algorithm._max_on_percent = 0.6
    else:
        kitchen._safety_state = True
    await manager.calculate_central_boiler_state(None)
    await hass.async_block_till_done()
    await kitchen.async_control_heating()
    assert "pulse_opening" not in (manager.boiler_flow.context_for(kitchen) or {})
    assert kitchen.valve_open_percent < 100


async def test_lost_valve_before_delayed_start_blocks_burn(hass, pulse_system):
    manager, bedroom, _, calls = pulse_system
    with patch(MANAGER_MODULE + ".async_call_later", return_value=lambda: None) as timer:
        await manager.calculate_central_boiler_state(None)
        await hass.async_block_till_done()
        manager._nb_active_device_number_entity.native_value = 1
        bedroom.boiler_pulse_valve_feedback = 1.0
        await manager.calculate_central_boiler_state(None)
        delayed_start = timer.call_args.args[2]
        bedroom.boiler_pulse_valve_feedback = None
        await delayed_start(None)
        await manager._keep_alive_boiler(None)
    assert not manager.is_on
    assert set(commands(calls)) <= {0}


@pytest.mark.parametrize("cause", ["room_stale", "old_plugin", "water_unavailable"])
async def test_ineligible_room_or_sensor_releases_forced_opening(hass, pulse_system, cause):
    manager, bedroom, _, calls = pulse_system
    await manager.calculate_central_boiler_state(None)
    await hass.async_block_till_done()
    assert bedroom.valve_open_percent == 100
    if cause == "room_stale":
        bedroom.last_temperature_measure -= timedelta(days=1, seconds=1)
    elif cause == "old_plugin":
        bedroom.prop_algorithm.boiler_pulse_protocol = 0
    else:
        hass.states.async_set("sensor.water", "unavailable", {"unit_of_measurement": UnitOfTemperature.CELSIUS})
    await manager.calculate_central_boiler_state(None)
    await hass.async_block_till_done()
    assert "pulse_opening" not in (manager.boiler_flow.context_for(bedroom) or {})
    assert bedroom.valve_open_percent < 100
    assert set(commands(calls)) <= {0}
