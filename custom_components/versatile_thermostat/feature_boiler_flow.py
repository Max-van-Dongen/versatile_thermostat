"""Opt-in shared boiler flow control and the version-1 SmartPI context."""

import asyncio
import time
from math import ceil, floor

from homeassistant.const import UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError
from vtherm_api.log_collector import get_vtherm_logger

from .boiler_flow import (
    FLOW_ENABLED,
    FLOW_ENTITY,
    FLOW_SENSOR,
    FlowSettings,
    choose_flow,
    finite,
)

_LOGGER = get_vtherm_logger(__name__)


class BoilerFlowManager:
    """Coordinate participating zones without importing the SmartPI plugin.

    Runs under the central boiler manager's lifecycle. The flow actuator is a
    Celsius number entity, distinct from a room thermostat's target temperature.
    Setting flow never constitutes an on/off decision.
    """

    def __init__(self, hass, entry):
        self.hass = hass
        self.enabled = bool(entry.get(FLOW_ENABLED, False))
        self.entity_id = entry.get(FLOW_ENTITY)
        self.sensor_id = entry.get(FLOW_SENSOR)
        self.settings = FlowSettings.from_entry(entry) if self.enabled else FlowSettings()
        self.context = None
        self.target = None
        self.can_heat = not self.enabled
        self.reason = "disabled"
        self._lock = asyncio.Lock()
        self._last_tick = None
        self._last_sent = None
        self._last_written_target = None
        self._was_active = False
        self._estimated_flow = self.settings.reference
        self._reference_since = None
        self.pulse = None
        if self.enabled and self.settings.pulse_enabled:
            from .feature_boiler_pulse import PulseRuntime

            self.pulse = PulseRuntime(self)

    @property
    def pulse_scheduler(self):
        return self.pulse.scheduler if self.pulse else None

    def context_for(self, zone):
        return self.pulse.context_for(zone, self.context) if self.pulse else self.context

    def plan_pulse(self, raw_demand, boiler_on):
        return self.pulse.plan(raw_demand, boiler_on) if self.pulse else raw_demand

    def pulse_permits_heat(self):
        return self.pulse is None or self.pulse.permits_heat()

    def _zones(self):
        component = self.hass.data.get("climate")
        return [zone for zone in list(component.entities) if getattr(zone, "is_used_by_central_boiler", False) is True] if component else []

    def input_entity_ids(self):
        """Room inputs and physical reports that can change a live flow decision."""
        entity_ids = {self.sensor_id} if self.sensor_id else set()
        for zone in self._zones():
            entity_ids.add(zone.entity_id)
            if self.pulse:
                for valve in getattr(zone, "activable_underlying_entities", ()) or ():
                    for entity_id in (valve.entity_id, getattr(valve, "closing_degree_entity_id", None)):
                        if entity_id and entity_id.startswith("number."):
                            entity_ids.add(entity_id)
        return entity_ids

    def _requests(self, zone_ids=None):
        requests = []
        for zone in self._zones():
            if zone_ids is not None and zone.entity_id not in zone_ids:
                continue
            # OFF, sleeping and cooling zones must not raise the heating flow.
            if str(getattr(zone, "hvac_mode", "off")) != "heat":
                continue
            if getattr(zone, "is_overpowering_detected", False) is True:
                continue
            algo = getattr(zone, "prop_algorithm", None)
            compatible = getattr(algo, "boiler_flow_protocol", None) == 1
            if not compatible:
                if getattr(zone, "is_device_active", False):
                    requests.append({"reference_required": True})
                continue
            request = dict(algo.boiler_heat_request(self.settings.opening))
            request["room_temperature"] = getattr(zone, "current_temperature", None)
            current = finite(request["room_temperature"])
            target = finite(getattr(zone, "target_temperature", None))
            # SmartPI already includes the room-temperature deficit in its heat
            # demand. Only faults add a coordinator-level reference override.
            if getattr(zone, "_safety_state", False) is True or current is None or target is None:
                request["reference_required"] = True
            requests.append(request)
        return requests

    def _measured_flow(self, now_wall):
        if not self.sensor_id:
            return None
        state = self.hass.states.get(self.sensor_id)
        if (state is None or state.attributes.get("unit_of_measurement") != UnitOfTemperature.CELSIUS
                or state.attributes.get("assumed_state") or state.attributes.get("restored")):
            return None
        reported = getattr(state, "last_reported", state.last_updated)
        timestamp = reported.timestamp()
        value = finite(state.state)
        if value is None or not 0 <= now_wall - timestamp <= 600 or not 5 <= value <= 100:
            return None
        return value

    def _publish(self, now, flow, learning, reason):
        self.reason = reason
        self.context = {
            "version": 1,
            "observed_at": now,
            "flow_temperature": flow,
            "reference_temperature": self.settings.reference,
            "exponent": self.settings.exponent,
            "learning_allowed": learning,
            "boiler_active": self._was_active,
            "reason": reason,
        }

    async def refresh(self, active: bool, *, force=False):
        """Select and write flow, then publish only successfully commanded state."""
        if not self.enabled:
            return
        async with self._lock:
            now = time.monotonic()
            previous_tick = self._last_tick
            elapsed = 0 if previous_tick is None else now - previous_tick
            self._last_tick = now
            self.can_heat = False
            state = self.hass.states.get(self.entity_id) if self.entity_id else None
            value = finite(state.state) if state else None
            if state is None or value is None or not self.entity_id.startswith("number.") or state.attributes.get("unit_of_measurement") != UnitOfTemperature.CELSIUS:
                self._publish(now, None, False, "flow_entity_unavailable")
                return
            device_min = finite(state.attributes.get("min"))
            device_max = finite(state.attributes.get("max"))
            step = finite(state.attributes.get("step"))
            if (
                device_min is None
                or device_max is None
                or step is None
                or step <= 0
                or device_min > self.settings.minimum
                or device_max < self.settings.maximum
                or abs(device_min + round((self.settings.reference - device_min) / step) * step - self.settings.reference) > 1e-6
            ):
                self._publish(now, None, False, "flow_entity_range_invalid")
                return

            measured = self._measured_flow(time.time())
            sensor_invalid = bool(self.sensor_id and measured is None)
            pulse = self.pulse.decision if self.pulse else None
            if active and self.pulse and not self.pulse.permits_heat():
                # Setting the flow number can itself enable the boiler.
                self._publish(now, measured, False, "waiting_for_valve")
                return
            if sensor_invalid and pulse is not None:
                # The measurement can disappear while waiting for the lock.
                self._publish(now, None, False, "flow_sensor_unavailable")
                return
            if sensor_invalid:
                desired, reason = self.settings.reference, "flow_sensor_unavailable"
            elif pulse is not None and (pulse.call_for_heat or not self.pulse.native_zones):
                desired, reason = pulse.flow_temperature, pulse.reason
            elif pulse is not None:
                # A native room can still request heat while the coordinated
                # radiators are coasting or travelling. Use only its demand.
                desired, reason = choose_flow(self.settings, self._requests(self.pulse.native_zones))
            else:
                desired, reason = choose_flow(self.settings, self._requests())

            starting = active and not self._was_active
            # Round on the device's grid, constrained to the configured bounds.
            low = ceil((self.settings.minimum - device_min) / step)
            high = floor((self.settings.maximum - device_min) / step)
            if low > high:
                self._publish(now, None, False, "flow_entity_range_invalid")
                return
            candidate = device_min + min(high, max(low, round((desired - device_min) / step))) * step
            candidate = round(candidate, 6)
            if active:
                # Apply every new grid value immediately. Allow the number a
                # few seconds to report an identical command before retrying.
                since_write = float("inf") if self._last_sent is None else now - self._last_sent
                should_write = (force or self._last_written_target != candidate or since_write >= 60
                                or (since_write >= 5 and abs(value - candidate) >= step / 2))
                if should_write:
                    try:
                        await self.hass.services.async_call(
                            "number",
                            "set_value",
                            {"value": candidate},
                            target={"entity_id": self.entity_id},
                            blocking=True,
                        )
                    except HomeAssistantError as err:
                        _LOGGER.warning("Cannot set boiler flow: %s", err)
                        self._publish(now, None, False, "flow_write_failed")
                        return
                    self._last_sent = now
                    self._last_written_target = candidate
                self.target = candidate
            else:
                # In particular, never overwrite a number=0 boiler OFF command.
                self._last_sent = None
                # This is only an idle learning baseline, never a command or the
                # next activation's setpoint. Each new cycle recalculates demand.
                self.target = self.settings.reference
            self.can_heat = True
            if active != self._was_active:
                # Time spent OFF cannot qualify a new heating period for learning.
                self._reference_since = None
            self._was_active = active

            if measured is not None:
                self._estimated_flow = measured
            elif active and not starting:
                # Conservative cooldown proxy: do not open valves instantly
                # while radiators may still contain hotter water.
                # On restart retain the last estimate: the new command has not
                # yet heated or cooled the water during the previous OFF interval.
                from math import exp

                alpha = 1 - exp(-max(elapsed, 0) / 180)
                self._estimated_flow += alpha * (self.target - self._estimated_flow)
            # At fixed reference flow, passive/cooling windows remain useful.
            # Requiring a hot sensor while OFF would deadlock bootstrap B learning.
            at_reference = abs(self.target - self.settings.reference) < 0.01 and not sensor_invalid and (not active or abs(self._estimated_flow - self.target) <= 2)
            if not at_reference:
                self._reference_since = None
            elif self._reference_since is None:
                self._reference_since = now
            settled = self._reference_since is not None and now - self._reference_since >= self.settings.settle_seconds
            coordinated_only = pulse is not None and not self.pulse.native_zones
            learning = settled and not sensor_invalid and not coordinated_only
            if coordinated_only:
                self._reference_since = None
            elif not active and pulse is None:
                reason = "idle"
            self._publish(now, self._estimated_flow, learning, reason)

    @property
    def diagnostics(self):
        return {
            "enabled": self.enabled,
            "setpoint_entity": self.entity_id,
            "temperature_sensor": self.sensor_id,
            "target_temperature": self.target,
            "can_heat": self.can_heat,
            "reason": self.reason,
            "context": self.context,
            **({"pulse": self.pulse.diagnostics} if self.pulse is not None else {}),
        }
