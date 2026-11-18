"""Translate Home Assistant zone state to the shared boiler pulse scheduler."""

import time
from datetime import datetime

from homeassistant.const import UnitOfTemperature

from .boiler_flow import PULSE_GAS_RATE_UNITS, finite
from .boiler_pulse import PulseScheduler, PulseZone


class PulseRuntime:
    """Coordinate eligible zones through their ordinary SmartPI valve controls."""

    def __init__(self, flow):
        self.flow = flow
        settings = flow.settings
        self.scheduler = PulseScheduler(
            settings.minimum, settings.maximum, overshoot=settings.pulse_overshoot,
            undershoot=settings.pulse_undershoot, exponent=settings.exponent,
        )
        self.decision = None
        self.reason = "starting"
        self.openings = {}
        self.feedback = {}
        self.blocking_zone = None
        self.excluded_zones = {}
        self.native_zones = {}
        self.observed_at = None
        self.water = None
        self.boiler_on = False
        self.save_learning = None
        self._pending = {}
        self._published = {}
        self._stuck = {}
        self._awaiting_handover = False

    def _inputs(self, zones, now_wall):
        """Separate rooms under native regulation from healthy binary valve controls."""
        inputs = []
        feedback = {}
        outdoor = None
        self.excluded_zones = {}
        self.native_zones = {}
        for zone in zones:
            self.blocking_zone = zone.entity_id
            if not getattr(zone, "is_ready", False):
                return [], {}, None, "zone_not_ready"
            if str(zone.hvac_mode) != "heat":
                continue
            if getattr(zone, "temperature_unit", UnitOfTemperature.CELSIUS) != UnitOfTemperature.CELSIUS:
                return [], {}, None, "room_temperature_unit"
            if (getattr(zone, "is_overpowering_detected", False) is True
                    or getattr(zone, "is_inhibited", False) is True
                    or ("on" in (getattr(zone, "window_state", None), getattr(zone, "window_auto_state", None))
                        and not getattr(zone, "is_window_bypass", False))):
                if getattr(zone, "is_device_active", False):
                    return [], {}, None, "zone_guard"
                continue
            algo = getattr(zone, "prop_algorithm", None)
            if getattr(algo, "boiler_pulse_protocol", None) != 1:
                self.native_zones[zone.entity_id] = "incompatible_zone"
                continue
            safety = getattr(zone, "safety_manager", None)
            if getattr(zone, "_safety_state", False) is True or getattr(safety, "is_safety_detected", False) is True:
                return [], {}, None, "zone_safety"
            room, target = finite(zone.current_temperature), finite(zone.target_temperature)
            observed = zone.last_temperature_measure
            delay = finite(getattr(safety, "safety_delay_min", None))
            age_limit = min(1800, delay * 60) if delay and delay > 0 else 1800
            if room is None or target is None or not isinstance(observed, datetime):
                return [], {}, None, "room_measurement_unavailable"
            age = now_wall - observed.timestamp()
            request = algo.boiler_heat_request(self.flow.settings.opening)
            demand = finite(request.get("demand"))
            if not 0 <= age <= age_limit:
                # Leave a satisfied, closed room under its own thermostat's
                # control. No pulse decision or heat credit uses its old sample.
                if (age > age_limit and room >= target and demand == 0
                        and not request.get("reference_required")
                        and zone.valve_open_percent == 0 and zone.is_device_active is False
                        and getattr(zone, "boiler_pulse_valves_report_closed", False) is True):
                    self.excluded_zones[zone.entity_id] = "room_measurement_stale"
                    continue
                return [], {}, None, "room_measurement_unavailable"
            if demand is None or not 0 <= demand <= 1:
                return [], {}, None, "invalid_demand"
            if request.get("reference_required"):
                self.native_zones[zone.entity_id] = "reference_required"
                continue
            deficit = target - room
            undershoot = self.flow.settings.pulse_undershoot
            recovery = (deficit >= max(0.6, undershoot + 0.4)
                        or (deficit > 0 and request.get("pulse_eligible") is not True
                            and request.get("recovery_eligible") is True))
            if (request.get("pulse_eligible") is not True
                    and not (recovery and request.get("recovery_eligible") is True)):
                self.native_zones[zone.entity_id] = "normal_control_required"
                continue
            key = zone.entity_id
            position = finite(getattr(zone, "boiler_pulse_valve_feedback", None))
            if key in self._stuck:
                if position is None or self._stuck[key] == position:
                    self.native_zones[key] = "valve_opening_timeout"
                    continue
                del self._stuck[key]
            if position is None:
                reported_closed = getattr(zone, "boiler_pulse_valves_report_closed", False) is True
                if not reported_closed and room >= target and demand == 0 and not getattr(zone, "is_device_active", False):
                    continue
                # Prepare a confirmed-closed valve even if the idle reports
                # are old. Only fresh full-open reports can authorize heat.
                awaiting_report = self.decision is not None and key in self.decision.valves
                if not reported_closed and (not awaiting_report or getattr(zone, "boiler_pulse_has_reported_load", False) is True):
                    self.native_zones[key] = "valve_feedback_unavailable"
                    continue
            feedback[key] = position
            confirmed = zone.valve_open_percent == 100 and position == 1.0
            window = getattr(zone, "window_manager", None)
            slope = finite(window.slope_for_measurement(observed)) if window is not None and age <= 300 else None
            inputs.append(PulseZone(key=key, current=room, target=target, demand=demand, open_confirmed=confirmed,
                                    recovery=recovery, observed_at=observed.timestamp(), slope_per_hour=slope))
            if outdoor is None:
                outdoor = finite(getattr(zone, "current_outdoor_temperature", None))
        self.blocking_zone = None
        bypass = None if inputs else ("native_control" if self.native_zones else "no_eligible_rooms")
        return inputs, feedback, outdoor, bypass

    @property
    def burner_sensor(self):
        return self.flow.settings.pulse_flame_sensor or self.flow.settings.pulse_gas_rate_sensor

    def _burner(self):
        sensor = self.burner_sensor
        state = self.flow.hass.states.get(sensor) if sensor else None
        if state is None or state.attributes.get("assumed_state"):
            return None
        if self.flow.settings.pulse_flame_sensor:
            return state.state == "on" if state.state in ("on", "off") else None
        attributes = state.attributes
        reported = getattr(state, "last_reported", state.last_updated)
        if (attributes.get("unit_of_measurement") not in PULSE_GAS_RATE_UNITS
                or attributes.get("state_class") in ("total", "total_increasing")
                or not 0 <= time.time() - reported.timestamp() <= 90):
            return None
        rate = finite(state.state)
        return rate > 0 if rate is not None and rate >= 0 else None

    def stop(self):
        """Release transient requests before replacing the manager or listeners."""
        for task in self._pending.values():
            task.cancel()
        self._pending.clear()
        self._published.clear()
        self.openings.clear()
        self.feedback.clear()
        self.blocking_zone = None
        self.excluded_zones.clear()
        self.native_zones.clear()
        self._stuck.clear()
        self.scheduler.reset()
        self.decision = None
        self._awaiting_handover = False

    def _set_openings(self, zones, openings):
        previous = self._published
        self.openings = openings
        # Wake the controller when a delivered-heat input changes. The
        # half-degree step avoids running a room cycle for sensor noise.
        self._published = {
            key: (opening, key in self.decision.recovery_zones, self.feedback[key], self.boiler_on, round(self.water * 2))
            for key, opening in openings.items()
        }
        for zone in zones:
            key = zone.entity_id
            if previous.get(key) == self._published.get(key) or (key in self._pending and not self._pending[key].done()):
                continue
            task = self.flow.hass.async_create_task(self._apply_zone(zone, key))
            self._pending[key] = task
            task.add_done_callback(lambda done, key=key: self._zone_applied(key, done))

    def _zone_applied(self, key, task):
        if self._pending.get(key) is task:
            del self._pending[key]

    async def _apply_zone(self, zone, key):
        while zone.is_ready:
            inputs = self._published.get(key)
            await zone.async_control_heating(force=False)
            if inputs == self._published.get(key):
                break

    def set_boiler_on(self, active):
        """Refresh delivery estimates after the central service succeeds."""
        if self.boiler_on != active:
            self.boiler_on = active
            self._set_openings(self.flow._zones(), self.openings)

    def plan(self, raw_demand, boiler_on):
        now = time.monotonic()
        now_wall = time.time()
        self.observed_at = now
        self.boiler_on = boiler_on is True
        self.water = self.flow._measured_flow(now_wall)
        zones = self.flow._zones()
        inputs, self.feedback, outdoor, bypass = self._inputs(zones, now_wall)
        if self.water is None or bypass:
            self.reason = "water_unavailable" if self.water is None else bypass
            if self.openings:
                self._awaiting_handover = True
            self.scheduler.reset()
            self.decision = None
            self._set_openings(zones, {})
            if self._awaiting_handover:
                # The raw active-device count can still reflect the last
                # forced-open valve until its ordinary control cycle returns.
                if self._pending:
                    return False
                self._awaiting_handover = False
            return raw_demand and self._native_load_active(zones, all_rooms=True)

        self._awaiting_handover = False
        native_heat_call = raw_demand and self._native_load_active(zones)
        self.decision = self.scheduler.update(
            now=now, zones=inputs, water=self.water, burner=self._burner(),
            boiler_on=boiler_on is True, outdoor=outdoor, shared_heat_call=native_heat_call,
        )
        if self.scheduler.opening_timeouts:
            for zone in zones:
                key = zone.entity_id
                if key in self.scheduler.opening_timeouts:
                    position = self.feedback.get(key)
                    if position is None and getattr(zone, "boiler_pulse_valves_report_closed", False):
                        position = 0.0
                    self._stuck[key] = position
        if self.save_learning is not None:
            self.save_learning()
        self.reason = self.decision.reason
        self._set_openings(zones, {zone.key: float(zone.key in self.decision.valves) for zone in inputs})
        return raw_demand and (self.decision.call_for_heat or self._native_load_active(zones))

    def _native_load_active(self, zones, *, all_rooms=False):
        """A released room may use the boiler through its ordinary controller."""
        for zone in zones:
            key = zone.entity_id
            if ((not all_rooms and key not in self.native_zones) or key in self.openings or key in self._pending
                    or not getattr(zone, "is_ready", False) or str(zone.hvac_mode) != "heat"):
                continue
            if getattr(zone, "is_device_active", False) is not True:
                continue
            command = getattr(zone, "valve_open_percent", None)
            # Native relays and ordinary climates use their normal activity
            # signal. Native valve zones also need positive device readback.
            if command is None:
                return True
            if command > 0:
                reported = getattr(zone, "boiler_pulse_has_reported_load", None)
                if reported is None:
                    position = finite(getattr(zone, "boiler_pulse_valve_feedback", None))
                    reported = position is not None and position > 0
                if reported is True:
                    return True
        return False

    def permits_heat(self):
        if self.decision is None:
            return not self._awaiting_handover and self._native_load_active(self.flow._zones(), all_rooms=True)
        zones = self.flow._zones()
        if not self.decision.call_for_heat:
            return self._native_load_active(zones)
        return any(
            zone.entity_id in self.decision.valves
            and str(zone.hvac_mode) == "heat" and zone.valve_open_percent == 100
            and zone.boiler_pulse_valve_feedback == 1.0
            for zone in zones
        )

    def context_for(self, zone, context):
        if self.decision is None or zone.entity_id not in self.openings:
            return context
        return {
            **(context or {}), "version": 1, "observed_at": self.observed_at,
            "pulse_opening": self.openings[zone.entity_id],
            "pulse_recovery": zone.entity_id in self.decision.recovery_zones,
            "pulse_valve_feedback": self.feedback[zone.entity_id],
            "flow_temperature": self.water, "learning_allowed": False,
            "boiler_active": self.boiler_on,
            "reference_temperature": self.flow.settings.reference,
            "exponent": self.flow.settings.exponent, "reason": self.reason,
        }

    @property
    def diagnostics(self):
        return {
            "phase": self.decision.phase if self.decision else "normal",
            "reason": self.reason,
            "blocking_zone": self.blocking_zone,
            "excluded_zones": dict(self.excluded_zones),
            "native_zones": dict(self.native_zones),
            "requested_valves": sorted(self.decision.valves) if self.decision else [],
            "recovery_zones": sorted(self.decision.recovery_zones) if self.decision else [],
            "reported_opening": self.feedback,
            "requested_flow": self.decision.flow_temperature if self.decision else None,
            "burner": self._burner(), "heat_balance_minutes": self.scheduler.heat_balance_minutes,
        }
