"""Valve command filter shared by the VTherms driving a valve opening"""

from datetime import datetime
import time

from vtherm_api.log_collector import get_vtherm_logger

from .boiler_flow import finite

_LOGGER = get_vtherm_logger(__name__)


class ValveCommandMixin:
    """Convert a proportional command into the retained valve opening.

    Used by ThermostatOverValve and ThermostatOverClimateValve. The
    CycleScheduler calls apply_valve_command_percent for every valve command
    it receives (TPI or plugin algorithm), so the valve follows the command
    path and the cycle callbacks report the value actually retained.

    The host class provides valve_open_percent, is_sleeping, now,
    do_recalculate_later(), update_custom_attributes() and
    async_write_ha_state().
    """

    _valve_open_percent: int | None
    _last_calculation_timestamp: datetime | None
    _auto_regulation_dpercent: float | None
    _auto_regulation_period_min: int | None
    _pulse_command_applied = False

    @property
    def boiler_pulse_valve_feedback(self) -> float | None:
        """Least reported opening across this zone, including paired closing degrees.

        Read device entities independently of the thermostat's desired percentage.
        Some TRVs expose only the reported position setting, not a stroke sensor.
        """
        positions = self._boiler_pulse_positions(require_fresh=True)
        return min(positions) if positions else None

    @property
    def boiler_pulse_valves_report_closed(self) -> bool:
        """All available degree entities report their fully closed endpoints.

        An unchanged closed number may be old. This check can exclude an idle
        room or begin valve travel, but cannot confirm an available heat load.
        """
        positions = self._boiler_pulse_positions(require_fresh=False)
        return bool(positions) and max(positions) == 0.0

    def _boiler_pulse_positions(self, *, require_fresh: bool) -> list[float]:
        positions = []
        for under in self.activable_underlying_entities or ():
            readings = self._reported_valve_positions(under, require_fresh=require_fresh, require_full_travel=True)
            if not readings:
                return []
            positions.extend(readings)
        return positions

    @property
    def boiler_pulse_has_reported_load(self) -> bool:
        """A radiator in this zone reports an opening usable by ordinary control."""
        for under in self.activable_underlying_entities or ():
            readings = self._reported_valve_positions(under, require_fresh=True, require_full_travel=False)
            if readings and min(readings) > 0:
                return True
        return False

    def _reported_valve_positions(self, under, *, require_fresh, require_full_travel) -> list[float]:
        if not under.is_initialized or not under.entity_id:
            return []
        positions = []
        for entity_id, closing in ((under.entity_id, False), (getattr(under, "closing_degree_entity_id", None), True)):
            if not entity_id:
                continue
            state = self.hass.states.get(entity_id)
            if state is None or state.attributes.get("assumed_state") or state.attributes.get("restored"):
                return []
            reported = getattr(state, "last_reported", state.last_updated)
            age = time.time() - reported.timestamp()
            if age < 0 or (require_fresh and age > 1800):
                return []
            value = finite(state.state)
            maximum = finite(state.attributes.get("max"))
            if value is None or maximum is None or maximum <= 0 or not 0 <= value <= maximum:
                return []
            if require_full_travel and not closing and under.max_opening_degree is not None and under.max_opening_degree < maximum:
                return []
            fraction = value / maximum
            positions.append(1.0 - fraction if closing else fraction)
        return positions

    def apply_valve_command_percent(
        self,
        on_percent: float,
        force: bool = False,
    ) -> float:
        """Accept a requested valve command and return the retained ratio.

        This is the single conversion point between a proportional algorithm's
        continuous output and the integer command exposed by the thermostat.
        While sleeping the valve is fully open: the dpercent filter is bypassed
        but the regulation period still applies unless force is set.
        """
        if self.is_sleeping:
            return self._retain_valve_open_percent(100, force, filter_dpercent=False)

        # Only bypass filters when the algorithm has actually accepted the
        # per-zone pulse directive. VTherm safety/clamps keep their authority.
        algo = getattr(self, "prop_algorithm", None)
        if getattr(algo, "boiler_pulse_protocol", None) == 1:
            context = self.boiler_flow_context
            pulse = context.get("pulse_opening") if context else None
            if pulse in (0.0, 1.0) and algo.pulse_opening == pulse and on_percent == pulse:
                self._pulse_command_applied = True
                return self._retain_valve_open_percent(int(100 * pulse), True, filter_dpercent=False)

        if self._auto_regulation_period_min is None or self._auto_regulation_dpercent is None:
            _LOGGER.warning(
                "%s - auto_regulation_period_min or auto_regulation_dpercent is not set. Keeping current valve command.",
                self,
            )
            return self.valve_open_percent / 100.0

        new_valve_percent = round(max(0, min(on_percent, 1)) * 100)
        if self._pulse_command_applied:
            # Resume the ordinary output immediately. An old forced opening
            # must not survive handover just because its regulation period runs.
            self._pulse_command_applied = False
            if new_valve_percent < self._auto_regulation_dpercent:
                new_valve_percent = 0
            return self._retain_valve_open_percent(new_valve_percent, True, filter_dpercent=False)
        return self._retain_valve_open_percent(new_valve_percent, force)

    def _retain_valve_open_percent(
        self,
        new_valve_percent: int,
        force: bool,
        filter_dpercent: bool = True,
    ) -> float:
        """Apply the dpercent and period filters, then store the valve opening.

        Returns the retained ratio (0.0 to 1.0).
        """
        if filter_dpercent:
            # Issue 533 - don't filter with dtemp if valve should be close. Else it will never close
            if new_valve_percent < self._auto_regulation_dpercent:
                new_valve_percent = 0

            # The first command is never filtered by dpercent
            if self._valve_open_percent is not None:
                dpercent = new_valve_percent - self.valve_open_percent
                if new_valve_percent > 0 and -1 * self._auto_regulation_dpercent <= dpercent < self._auto_regulation_dpercent:
                    _LOGGER.debug(
                        "%s - do not calculate TPI because regulation_dpercent (%.1f) is not exceeded",
                        self,
                        dpercent,
                    )
                    return self.valve_open_percent / 100.0

        if self._valve_open_percent == new_valve_percent:
            _LOGGER.debug("%s - no change in valve_open_percent.", self)
            return self.valve_open_percent / 100.0

        now = self.now
        if self._last_calculation_timestamp is not None:
            period = (now - self._last_calculation_timestamp).total_seconds() / 60
            if not force and period < self._auto_regulation_period_min:
                _LOGGER.info(
                    "%s - do not apply valve command because regulation_period (%d) is not exceeded",
                    self,
                    period,
                )
                self.do_recalculate_later()
                return self.valve_open_percent / 100.0

        self._valve_open_percent = new_valve_percent

        # Valve open percent is sent to underlyings by CycleScheduler
        # in start_cycle (called from control_heating)

        self._last_calculation_timestamp = now

        self.update_custom_attributes()
        self.async_write_ha_state()
        return self.valve_open_percent / 100.0
