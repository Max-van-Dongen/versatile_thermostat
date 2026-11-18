"""A cached or acknowledged TRV setpoint must not replace a newer room request."""

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.versatile_thermostat.const import (
    CONF_AUTO_REGULATION_MODE,
    CONF_AUTO_REGULATION_VALVE,
    CONF_CLOSING_DEGREE_LIST,
    CONF_OPENING_DEGREE_LIST,
    CONF_PROP_FUNCTION,
    CONF_TPI_COEF_EXT,
    CONF_TPI_COEF_INT,
    PROPORTIONAL_FUNCTION_TPI,
)
from .commons import (
    DOMAIN,
    MockConfigEntry,
    PARTIAL_CLIMATE_NOT_REGULATED_CONFIG,
    create_and_register_mock_climate,
    create_and_register_mock_number,
    create_thermostat,
)


@pytest.mark.parametrize("report", ["startup", "unavailable", "unknown", "late_ack"])
async def test_manual_target_survives_stale_feedback(
    hass, skip_hass_states_is_state, skip_turn_on_off_heater, skip_send_event, report,
):
    """A genuine subsequent dial turn and an explicit preset selection still apply."""
    physical_id = "climate.mock_climate"
    await create_and_register_mock_climate(hass, "mock_climate", "MockClimateName", {})
    await create_and_register_mock_number(hass, "mock_opening", "MockOpening", value=0, min=0, max=100)
    await create_and_register_mock_number(hass, "mock_closing", "MockClosing", value=100, min=0, max=100)
    config = PARTIAL_CLIMATE_NOT_REGULATED_CONFIG | {
        CONF_AUTO_REGULATION_MODE: CONF_AUTO_REGULATION_VALVE,
        CONF_OPENING_DEGREE_LIST: ["number.mock_opening"],
        CONF_CLOSING_DEGREE_LIST: ["number.mock_closing"],
        CONF_PROP_FUNCTION: PROPORTIONAL_FUNCTION_TPI,
        CONF_TPI_COEF_INT: 0.3,
        CONF_TPI_COEF_EXT: 0.1,
    }
    entry = MockConfigEntry(domain=DOMAIN, title="Room", unique_id="setpoint_feedback", data=config)
    room = await create_thermostat(hass, entry, "climate.theoverclimatemockname", {"comfort": 20.5})
    under = room.underlying_entity(0)
    room.set_follow_underlying_temp_change(True)
    await hass.async_block_till_done()

    async def command(service, **data):
        await hass.services.async_call("climate", service, {"entity_id": room.entity_id, **data}, blocking=True)

    callbacks = []

    def capture_debounce(_hass, delay, callback):
        assert delay == 5
        callbacks.append(callback)
        return lambda: None

    try:
        attrs = dict(hass.states.get(physical_id).attributes)
        with patch.object(under, "hass_services_async_call", new_callable=AsyncMock) as send:
            await command("set_hvac_mode", hvac_mode="heat")
            await command("set_preset_mode", preset_mode="comfort")
            await command("set_temperature", temperature=23)
            assert send.await_args.args[:2] == ("climate", "set_temperature")
            assert send.await_args.args[2]["temperature"] == 23
            old_context = send.await_args.kwargs.get("context")
            await command("set_temperature", temperature=30)
            assert room.target_temperature == 30
            assert under.last_sent_temperature == 30

            with patch(
                "custom_components.versatile_thermostat.thermostat_climate.async_call_later",
                side_effect=capture_debounce,
            ):
                if report == "startup":
                    hass.states.async_remove(physical_id)
                elif report == "late_ack":
                    hass.states.async_set(physical_id, "heat", attrs | {"temperature": 30})
                else:
                    hass.states.async_set(physical_id, report, attrs)
                await hass.async_block_till_done()
                report_context = old_context if report == "late_ack" else None
                hass.states.async_set(physical_id, "heat", attrs | {"temperature": 23}, context=report_context)
                await hass.async_block_till_done()
                for callback in callbacks:
                    await callback(None)
                assert room.target_temperature == 30
                assert room.preset_mode == "none"
                assert room._underlying_tracking_attempts == 0
                assert not callbacks

                # The same temperature with an independent context is a dial gesture.
                hass.states.async_set(physical_id, "heat", attrs | {"temperature": 30})
                await hass.async_block_till_done()
                hass.states.async_set(physical_id, "heat", attrs | {"temperature": 23})
                await hass.async_block_till_done()
                assert len(callbacks) == 1
                await callbacks.pop()(None)
                assert room.target_temperature == 23
                await command("set_preset_mode", preset_mode="comfort")
                assert room.target_temperature == 20.5
                assert room.requested_state.target_temperature == 23
    finally:
        room.remove_thermostat()
