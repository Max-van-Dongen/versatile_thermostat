"""Exercise both forks together when the SmartPI checkout is alongside VTherm."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, PropertyMock, patch

import pytest

paired_checkout = Path(__file__).resolve().parents[2] / "vtherm_smartpi"
if paired_checkout.is_dir():
    sys.path.insert(0, str(paired_checkout))
SmartPI = pytest.importorskip("custom_components.vtherm_smartpi.algo").SmartPI
from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.const import SmartPIPhase
from custom_components.versatile_thermostat.boiler_flow import FLOW_ENABLED, FLOW_ENTITY, FLOW_MIN, FLOW_MAX, FLOW_REFERENCE
from custom_components.versatile_thermostat.feature_boiler_flow import BoilerFlowManager


@pytest.mark.parametrize("linearization", [False, True])
async def test_two_rooms_lower_flow_preserve_heat_and_raise_for_new_demand(hass, linearization):
    algos = [SmartPI(hass, 5, 0, 0, name) for name in ("Bedroom", "Office")]
    zones = []
    for algo, demand in zip(algos, (0.1, 0.05)):
        algo.configure_valve_linearization(linearization, valve_mode=True)
        algo._set_linear_output(demand)
        zones.append(
            SimpleNamespace(
                is_used_by_central_boiler=True,
                hvac_mode="heat",
                is_overpowering_detected=False,
                prop_algorithm=algo,
                current_temperature=20,
                target_temperature=20.1,
                is_device_active=True,
            )
        )
    hass.data["climate"] = SimpleNamespace(entities=zones)
    hass.states.async_set("number.flow", 50, {"min": 0, "max": 80, "step": 1, "unit_of_measurement": "\u00b0C"})
    manager = BoilerFlowManager(
        hass,
        {
            FLOW_ENABLED: True,
            FLOW_ENTITY: "number.flow",
            FLOW_MIN: 30,
            FLOW_MAX: 50,
            FLOW_REFERENCE: 50,
        },
    )
    original = [algo.on_percent for algo in algos]
    models = [algo.est.save_state() for algo in algos]
    with (
        patch.object(SmartPI, "phase", new_callable=PropertyMock, return_value=SmartPIPhase.STABLE),
        patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock),
    ):
        for tick in range(1000, 4001, 60):
            with patch("time.monotonic", return_value=tick):
                await manager.refresh(True)
                for algo in algos:
                    algo.set_boiler_context(manager.context, 20, VThermHvacMode_HEAT)
                    # The real actuator-boundary conversion and feedback inverse.
                    algo._set_committed_actuator_output(algo.on_percent)
        assert manager.target == 30
        for algo, prior, model, demand in zip(algos, original, models, (0.1, 0.05)):
            assert algo.on_percent > prior
            assert algo.linear_committed_on_percent == pytest.approx(demand)
            assert algo.est.save_state() == model
            assert algo._flow_learning_blocked
        # A colder target alone must not override SmartPI's calculated demand.
        zones[1].target_temperature = 22
        with patch("time.monotonic", return_value=4060):
            await manager.refresh(True)
        assert manager.target == 30
        assert manager.reason == "modulating"
        # Full reference-temperature heat demand still raises the flow directly.
        algos[1]._set_linear_output(1)
        with patch("time.monotonic", return_value=4120):
            await manager.refresh(True)
        assert manager.target == 50
        assert manager.reason == "modulating"


async def test_unsupported_active_controller_forces_reference(hass):
    hass.data["climate"] = SimpleNamespace(
        entities=[
            SimpleNamespace(
                is_used_by_central_boiler=True,
                hvac_mode="heat",
                is_device_active=True,
                is_overpowering_detected=False,
                prop_algorithm=object(),
            )
        ]
    )
    manager = BoilerFlowManager(hass, {FLOW_ENABLED: True, FLOW_ENTITY: "number.flow"})
    assert manager._requests() == [{"reference_required": True}]
