"""Opt-in shared boiler flow control and the version-1 SmartPI context."""

import asyncio
import time
from math import ceil, floor

from homeassistant.exceptions import HomeAssistantError
from vtherm_api.log_collector import get_vtherm_logger

from .boiler_flow import (
    FLOW_ENABLED,
    FLOW_ENTITY,
    FLOW_SENSOR,
    FlowSettings,
    choose_flow,
    finite,
    slew_flow,
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
        self._changed_at = time.monotonic()
        self._last_sent = None
        self._last_written_target = None
        self._was_active = False
        self._estimated_flow = self.settings.reference
        self._ramp_target = None
        self._reference_since = None

    def _zones(self):
        component = self.hass.data.get("climate")
        return [zone for zone in list(component.entities) if getattr(zone, "is_used_by_central_boiler", False) is True] if component else []

    def _requests(self):
        requests = []
        for zone in self._zones():
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
            if getattr(zone, "_safety_state", False) is True or current is None or target is None or target - current >= 0.5:
                request["reference_required"] = True
            requests.append(request)
        return requests

    def _measured_flow(self, now_wall):
        if not self.sensor_id:
            return None
        state = self.hass.states.get(self.sensor_id)
        if state is None or state.attributes.get("unit_of_measurement") != "°C":
            return None
        reported = getattr(state, "last_reported", state.last_updated)
        age = now_wall - reported.timestamp()
        value = finite(state.state)
        return value if value is not None and 0 <= age <= 600 and 5 <= value <= 100 else None

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
            # Event storms must not repeatedly consume the ramp allowance.
            if not force and previous_tick is not None and elapsed < 5 and active == self._was_active:
                return
            self._last_tick = now
            self.can_heat = False
            state = self.hass.states.get(self.entity_id) if self.entity_id else None
            value = finite(state.state) if state else None
            if state is None or value is None or not self.entity_id.startswith("number.") or state.attributes.get("unit_of_measurement") != "°C":
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
            desired, reason = choose_flow(self.settings, self._requests())
            if sensor_invalid:
                desired, reason = self.settings.reference, "flow_sensor_unavailable"

            startup = self.target is None or (active and not self._was_active)
            if startup:
                # A cold circuit must establish a known baseline before modulating.
                self.target = self.settings.reference
                self._estimated_flow = measured if measured is not None else self.target
                self._changed_at = now
                self._ramp_target = self.target
            warmup = now - self._changed_at < self.settings.settle_seconds and startup
            if not startup and active:
                # Hold the reference for the first settling period after activation.
                if now - getattr(self, "_activated_at", now) < self.settings.settle_seconds:
                    desired, reason = self.settings.reference, "startup_settling"
                candidate = slew_flow(self._ramp_target if self._ramp_target is not None else self.target, desired, elapsed)
            else:
                candidate = self.target
            if startup:
                self._activated_at = now
                reason = "startup_settling"

            ramp_candidate = candidate

            # Round on the device's grid, constrained to the configured bounds.
            low = ceil((self.settings.minimum - device_min) / step)
            high = floor((self.settings.maximum - device_min) / step)
            if low > high:
                self._publish(now, None, False, "flow_entity_range_invalid")
                return
            candidate = device_min + min(high, max(low, round((candidate - device_min) / step))) * step
            candidate = round(candidate, 6)
            if active:
                should_write = force or self._last_sent is None or now - self._last_sent >= 60 or self._last_written_target != candidate or abs(value - candidate) >= step / 2
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
                if candidate != self.target:
                    self._changed_at = now
                self.target = candidate
                self._ramp_target = ramp_candidate
            else:
                # In particular, never overwrite a number=0 boiler OFF command.
                self._last_sent = None
                # The next activation starts at reference. Reset only the logical
                # target while OFF so passive learning/manual calibration cannot
                # remain blocked indefinitely after a modulated heating period.
                self.target = self.settings.reference
                self._ramp_target = self.target
            self.can_heat = True
            self._was_active = active

            if measured is not None:
                self._estimated_flow = measured
            elif active:
                # Conservative cooldown proxy: do not open valves instantly
                # while radiators may still contain hotter water.
                from math import exp

                alpha = 1 - exp(-max(elapsed, 0) / 180)
                self._estimated_flow += alpha * (self.target - self._estimated_flow)
            # At fixed reference flow, passive/cooling windows remain useful.
            # Requiring a hot sensor while OFF would deadlock bootstrap B learning.
            at_reference = abs(self.target - self.settings.reference) < 0.01 and not sensor_invalid and (not active or measured is None or abs(measured - self.target) <= 2)
            if not at_reference:
                self._reference_since = None
            elif self._reference_since is None:
                self._reference_since = now
            settled = self._reference_since is not None and now - self._reference_since >= self.settings.settle_seconds
            learning = settled and not sensor_invalid
            if not active:
                reason = "idle"
            elif warmup:
                reason = "startup_settling"
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
        }
