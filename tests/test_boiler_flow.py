"""Flow coordinator invariants and Home Assistant actuator behavior."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.versatile_thermostat.boiler_flow import (
    FLOW_ENABLED,
    FLOW_ENTITY,
    FLOW_SENSOR,
    FLOW_MIN,
    FLOW_MAX,
    FLOW_REFERENCE,
    FlowSettings,
    choose_flow,
    slew_flow,
)
from custom_components.versatile_thermostat.feature_boiler_flow import BoilerFlowManager


def request(demand=0.1, room=20, capacity=0.7, **extra):
    return dict(demand=demand, room_temperature=room, target_capacity=capacity, **extra)


def test_low_demand_reduces_flow_with_a_floor():
    settings = FlowSettings(minimum=30, maximum=50, reference=50)
    temperature, _ = choose_flow(settings, [request()])
    assert temperature == 30
    temperature, _ = choose_flow(settings, [request(0.4)])
    assert temperature == pytest.approx(20 + 30 * (0.4 / 0.7) ** (1 / 1.3))


def test_most_demanding_room_wins_and_saturation_is_bounded():
    s = FlowSettings()
    assert choose_flow(s, [request(0.1), request(0.8)])[0] == 65
    assert choose_flow(s, [request(0.4), request(0.1)])[0] == choose_flow(s, [request(0.4)])[0]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), None, "unavailable", -1, 2])
def test_invalid_demands_fall_back_to_reference(bad):
    assert choose_flow(FlowSettings(), [request(bad)]) == (65, "invalid_demand")


def test_calibration_holds_reference_and_only_downward_ramp_is_limited():
    assert choose_flow(FlowSettings(), [request(0.05, reference_required=True)])[0] == 65
    assert slew_flow(50, 35, 60) == 49
    assert slew_flow(35, 65, 60) == 65
    assert slew_flow(35, 65, 3600) == 65
    assert slew_flow(35, 48, 0) == 48
    assert slew_flow(50, 35, 3600) == 49
    assert slew_flow(50, 35, 0) == 50


@pytest.mark.parametrize("kwargs", [{"minimum": 70}, {"reference": 50}, {"minimum": float("nan")}, {"opening": 0}, {"exponent": 0}])
def test_invalid_settings_rejected(kwargs):
    with pytest.raises(ValueError):
        FlowSettings(**kwargs)


def build(hass, **extra):
    hass.states.async_set("number.flow", "65", {"min": 0, "max": 80, "step": 1, "unit_of_measurement": "�C"})
    manager = BoilerFlowManager(hass, {FLOW_ENABLED: True, FLOW_ENTITY: "number.flow", **extra})
    manager._requests = lambda: [request()]
    return manager


async def test_idle_never_writes_and_keepalive_recovers_external_override(hass):
    manager = build(hass)
    manager._requests = lambda: [request(1)]
    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as call:
        with patch("custom_components.versatile_thermostat.feature_boiler_flow.time.monotonic", return_value=1000):
            await manager.refresh(False)
            call.assert_not_called()
            await manager.refresh(True)
            assert call.call_args.args[2] == {"value": 65}
        manager._requests = lambda: [request()]
        with patch("custom_components.versatile_thermostat.feature_boiler_flow.time.monotonic", return_value=1360):
            await manager.refresh(True)
            assert call.call_args.args[2] == {"value": 64}
        call.reset_mock()
        with patch("custom_components.versatile_thermostat.feature_boiler_flow.time.monotonic", return_value=1420):
            await manager.refresh(True)
            call.assert_awaited_once()
        call.reset_mock()
        with patch("custom_components.versatile_thermostat.feature_boiler_flow.time.monotonic", return_value=1480):
            await manager.refresh(False)
            call.assert_not_called()


async def test_failed_write_does_not_publish_compensated_context(hass):
    from homeassistant.exceptions import HomeAssistantError

    manager = build(hass)
    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock, side_effect=HomeAssistantError("offline")):
        await manager.refresh(True)
    assert not manager.can_heat
    assert manager.context["flow_temperature"] is None
    assert not manager.context["learning_allowed"]


async def test_unavailable_and_invalid_unit_are_rejected(hass):
    manager = build(hass)
    for state, unit in [("unavailable", "�C"), ("nan", "�C"), ("65", "�F")]:
        hass.states.async_set("number.flow", state, {"unit_of_measurement": unit})
        await manager.refresh(True, force=True)
        assert not manager.can_heat
        assert manager.reason == "flow_entity_unavailable"


async def test_sensor_failure_restores_reference_without_learning(hass):
    manager = build(hass, **{FLOW_SENSOR: "sensor.flow"})
    manager.target = 35
    manager._was_active = True
    manager._last_tick = 1000
    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        with patch("custom_components.versatile_thermostat.feature_boiler_flow.time.monotonic", return_value=1060):
            await manager.refresh(True)
    assert manager.target == 65
    assert manager.reason == "flow_sensor_unavailable"
    assert manager.context["learning_allowed"] is False


async def test_measured_flow_is_used_and_reference_learning_requires_settling(hass):
    manager = build(hass, **{FLOW_SENSOR: "sensor.flow"})
    manager._requests = lambda: [request(1)]
    hass.states.async_set("sensor.flow", 45, {"unit_of_measurement": "�C"})
    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        with patch("custom_components.versatile_thermostat.feature_boiler_flow.time.monotonic", return_value=1000):
            await manager.refresh(True)
            assert manager.context["flow_temperature"] == 45
            assert not manager.context["learning_allowed"]
        hass.states.async_set("sensor.flow", 65, {"unit_of_measurement": "�C"})
        with patch("custom_components.versatile_thermostat.feature_boiler_flow.time.monotonic", return_value=1360):
            await manager.refresh(True)
            assert not manager.context["learning_allowed"]
        with patch("custom_components.versatile_thermostat.feature_boiler_flow.time.monotonic", return_value=1720):
            await manager.refresh(True)
            assert manager.context["learning_allowed"]


async def test_frequent_events_accumulate_sub_step_ramp_changes(hass):
    manager = build(hass)
    manager.target = 50
    manager._ramp_target = 50
    manager._was_active = True
    manager._last_tick = 1000
    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        for tick in range(1005, 1061, 5):
            with patch("custom_components.versatile_thermostat.feature_boiler_flow.time.monotonic", return_value=tick):
                await manager.refresh(True)
    assert manager.target == 49


@pytest.mark.parametrize("demand, expected", [(1.0, 55), (0.7 * (28 / 35) ** 1.3, 48)])
async def test_rising_demand_sets_required_flow_immediately_using_measured_water(hass, demand, expected):
    manager = build(hass, **{FLOW_MAX: 55, FLOW_REFERENCE: 55, FLOW_SENSOR: "sensor.flow"})
    manager._requests = lambda: [request(demand)]
    manager.target = 40
    manager._ramp_target = 40
    manager._was_active = True
    manager._last_tick = 1000
    hass.states.async_set("sensor.flow", 40, {"unit_of_measurement": "�C"})
    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as calls:
        with patch("custom_components.versatile_thermostat.feature_boiler_flow.time.monotonic", return_value=1005):
            await manager.refresh(True)
    assert manager.target == expected
    assert calls.call_args.args[2] == {"value": expected}
    # Raising the command does not pretend that the circuit is already hotter.
    assert manager.context["flow_temperature"] == 40
    assert not manager.context["learning_allowed"]


@pytest.mark.parametrize("demand, expected", [(0.1, 35), (1.0, 65)])
async def test_number_activation_obeys_delay_and_closed_valves_switch_off(hass, demand, expected):
    from .test_central_boiler_startup import _build_manager, _set_demand, MANAGER_MODULE
    from custom_components.versatile_thermostat.const import (
        CONF_CENTRAL_BOILER_ACTIVATION_SRV,
        CONF_CENTRAL_BOILER_DEACTIVATION_SRV,
        CONF_CENTRAL_BOILER_ACTIVATION_DELAY_SEC,
    )

    build(hass)
    manager = _build_manager(hass, power=2000)
    manager.post_init(
        {
            FLOW_ENABLED: True,
            FLOW_ENTITY: "number.flow",
            CONF_CENTRAL_BOILER_ACTIVATION_SRV: "number.flow/number.set_value/value:65",
            CONF_CENTRAL_BOILER_DEACTIVATION_SRV: "number.flow/number.set_value/value:0",
            CONF_CENTRAL_BOILER_ACTIVATION_DELAY_SEC: 60,
        }
    )
    manager.boiler_flow._requests = lambda: [request(demand)]
    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as calls, patch(MANAGER_MODULE + ".async_call_later", return_value=lambda: None) as timer:
        await manager.calculate_central_boiler_state(None)
        calls.assert_not_called()
        await timer.call_args.args[2](None)
        assert manager.is_on is True
        assert calls.call_args.kwargs["service_data"]["value"] == expected
        _set_demand(manager, 0)
        await manager.calculate_central_boiler_state(None)
        assert manager.is_on is False
        assert calls.call_args.kwargs["service_data"]["value"] == "0"
        count = calls.call_count
        await manager.boiler_flow.refresh(False, force=True)
        assert calls.call_count == count


async def test_flow_sensor_timestamp_must_be_fresh(hass):
    manager = build(hass, **{FLOW_SENSOR: "sensor.flow"})
    hass.states.async_set("sensor.flow", 50, {"unit_of_measurement": "�C"})
    now = datetime.now(timezone.utc).timestamp()
    assert manager._measured_flow(now) == 50
    assert manager._measured_flow(now + 601) is None


async def test_config_rejects_missing_entity_and_invalid_reference(hass):
    from custom_components.versatile_thermostat.config_flow import VersatileThermostatBaseConfigFlow

    flow = VersatileThermostatBaseConfigFlow({})
    flow.hass = hass
    for data in [{FLOW_ENABLED: True}, {FLOW_ENABLED: True, FLOW_REFERENCE: 50}]:
        result = await flow.async_step_central_boiler(data)
        assert result["errors"] == {"base": "boiler_flow_invalid"}


async def test_demand_disappearing_during_flow_write_cannot_enable_boiler(hass):
    from .test_central_boiler_startup import _build_manager, _set_demand
    from custom_components.versatile_thermostat.const import (
        CONF_CENTRAL_BOILER_ACTIVATION_SRV,
        CONF_CENTRAL_BOILER_DEACTIVATION_SRV,
    )

    build(hass)
    manager = _build_manager(hass, power=2000)
    manager.post_init(
        {
            FLOW_ENABLED: True,
            FLOW_ENTITY: "number.flow",
            CONF_CENTRAL_BOILER_ACTIVATION_SRV: "number.flow/number.set_value/value:65",
            CONF_CENTRAL_BOILER_DEACTIVATION_SRV: "number.flow/number.set_value/value:0",
        }
    )

    async def close_valves(*args, **kwargs):
        if len(args) > 2 and args[2].get("value") == 35:
            _set_demand(manager, 0)

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock, side_effect=close_valves) as calls:
        await manager.calculate_central_boiler_state(None)
        assert manager.is_on is False
        assert calls.call_count == 2
        assert calls.call_args.kwargs["service_data"]["value"] == "0"
        assert calls.call_args.kwargs["blocking"] is True


async def test_reference_must_be_representable_on_actuator_grid(hass):
    manager = build(hass)
    hass.states.async_set("number.flow", 64, {"min": 0, "max": 80, "step": 2, "unit_of_measurement": "�C"})
    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as calls:
        await manager.refresh(True)
        calls.assert_not_called()
        assert manager.can_heat is False
        assert manager.reason == "flow_entity_range_invalid"


async def test_idle_after_modulation_can_resume_passive_learning_without_a_flow_write(hass):
    manager = build(hass)
    manager.target = 35
    manager._was_active = True
    manager._reference_since = None
    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as calls:
        with patch("custom_components.versatile_thermostat.feature_boiler_flow.time.monotonic", return_value=1000):
            await manager.refresh(False)
            assert manager.target == 65
            assert not manager.context["learning_allowed"]
        with patch("custom_components.versatile_thermostat.feature_boiler_flow.time.monotonic", return_value=1300):
            await manager.refresh(False)
            assert manager.context["learning_allowed"]
            assert not manager.context["boiler_active"]
        calls.assert_not_called()


@pytest.mark.parametrize("demand, expected", [(0.01, 35), (0.1, 35), (0.7 * (28 / 35) ** 1.3, 48), (1.0, 55)])
@pytest.mark.parametrize("use_sensor", [False, True])
async def test_restart_uses_current_demand_without_reheating_to_reference(hass, demand, expected, use_sensor):
    settings = {FLOW_MAX: 55, FLOW_REFERENCE: 55}
    if use_sensor:
        settings[FLOW_SENSOR] = "sensor.flow"
        hass.states.async_set("sensor.flow", 55, {"unit_of_measurement": "�C"})
    manager = build(hass, **settings)
    manager._requests = lambda: [request(1)]
    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as calls:
        with patch("custom_components.versatile_thermostat.feature_boiler_flow.time.monotonic", return_value=1000):
            await manager.refresh(True)
            assert manager.target == 55
        calls.reset_mock()
        with patch("custom_components.versatile_thermostat.feature_boiler_flow.time.monotonic", return_value=1060):
            await manager.refresh(False)
            calls.assert_not_called()
        manager._requests = lambda: [request(demand)]
        if use_sensor:
            hass.states.async_set("sensor.flow", 42, {"unit_of_measurement": "�C"})
        with patch("custom_components.versatile_thermostat.feature_boiler_flow.time.monotonic", return_value=1120):
            await manager.refresh(True)
            assert manager.target == expected
            calls.assert_awaited_once()
            assert calls.call_args.args[2] == {"value": expected}
            # Residual heat remains in the water even when the new target is low.
            assert manager.context["flow_temperature"] == (42 if use_sensor else 55)
            assert not manager.context["learning_allowed"]
        with patch("custom_components.versatile_thermostat.feature_boiler_flow.time.monotonic", return_value=1180):
            await manager.refresh(True)
            assert manager.target == expected
            assert all(call.args[2]["value"] == expected for call in calls.call_args_list)


async def test_first_low_demand_activation_also_starts_at_minimum(hass):
    manager = build(hass)
    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as calls:
        await manager.refresh(True)
    assert manager.target == 35
    assert calls.call_args.args[2] == {"value": 35}
    assert not manager.context["learning_allowed"]


@pytest.mark.parametrize("missing_sensor", [False, True])
async def test_restart_preserves_calibration_and_sensor_failure_reference_overrides(hass, missing_sensor):
    settings = {FLOW_MAX: 55, FLOW_REFERENCE: 55}
    if missing_sensor:
        settings[FLOW_SENSOR] = "sensor.missing"
    manager = build(hass, **settings)
    manager._requests = lambda: [request(0.1, reference_required=not missing_sensor)]
    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as calls:
        await manager.refresh(False)
        calls.assert_not_called()
        await manager.refresh(True)
        assert manager.target == 55
        assert calls.call_args.args[2] == {"value": 55}
    assert manager.reason == ("flow_sensor_unavailable" if missing_sensor else "reference_required")
    assert not manager.context["learning_allowed"]


async def test_restart_resets_learning_settle_time_after_long_idle(hass):
    manager = build(hass, **{FLOW_SENSOR: "sensor.flow"})
    manager._requests = lambda: [request(1)]
    hass.states.async_set("sensor.flow", 65, {"unit_of_measurement": "�C"})
    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        for tick, active, learning in [(1000, False, False), (1360, False, True), (1420, True, False), (1720, True, True)]:
            with patch("custom_components.versatile_thermostat.feature_boiler_flow.time.monotonic", return_value=tick):
                await manager.refresh(active)
                assert manager.context["learning_allowed"] is learning


async def test_reference_learning_waits_for_estimated_water_to_warm(hass):
    manager = build(hass)
    manager._requests = lambda: [request(1)]
    manager._estimated_flow = 35
    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        for tick in range(1000, 1481, 60):
            with patch("custom_components.versatile_thermostat.feature_boiler_flow.time.monotonic", return_value=tick):
                await manager.refresh(True)
                assert not manager.context["learning_allowed"]
        # The estimate enters the reference band, then gets its own settling time.
        with patch("custom_components.versatile_thermostat.feature_boiler_flow.time.monotonic", return_value=1540):
            await manager.refresh(True)
            assert manager.context["flow_temperature"] >= 63
            assert not manager.context["learning_allowed"]
        with patch("custom_components.versatile_thermostat.feature_boiler_flow.time.monotonic", return_value=1840):
            await manager.refresh(True)
            assert manager.context["learning_allowed"]
