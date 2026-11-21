"""Coordinate full-open radiator pulses using room comfort bands. Times are seconds."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

OPEN_TIMEOUT = 180
RETRY_DELAY = 300
MAX_SAMPLE_GAP = 300
MAX_TREND_INTERVAL = 1200
MAX_COAST_TIME = 3600    # discard an unresolved peak before unrelated heat can be attributed
MIN_COAST_TIME = 180
PEAK_DROP = 0.05        # degrees below the highest post-close measurement
MIN_PULSE_RISE = 0.1    # measurable warming before ending a pulse that starts above its cutoff
START_MARGIN = 0.1      # one room-sensor step around the lower comfort limit
START_SETTLE_TIME = 120
VALVE_REOPEN_DELAY = 180
COOLING_RATE = -0.05 / 3600  # degrees per second
COMPANION_MARGIN = 0.05  # extra degrees of headroom to join an existing run
MAX_ROOMS = 256


@dataclass(frozen=True)
class PulseZone:
    key: str
    current: float
    target: float
    demand: float
    open_confirmed: bool
    recovery: bool = False
    observed_at: float | None = None
    slope_per_hour: float | None = None


@dataclass(frozen=True)
class PulseDecision:
    phase: str
    valves: frozenset[str]
    call_for_heat: bool
    flow_temperature: float
    reason: str
    recovery_zones: frozenset[str] = frozenset()


@dataclass
class CoastObservation:
    started: float
    cut_temperature: float
    peak: float
    last_temperature: float
    last_stamp: float | None
    from_cutoff: bool
    sampled_early: bool = False


@dataclass
class RoomHistory:
    target: float
    stamp: float
    temperature: float
    sampled_at: float
    slope: float = 0.0       # degrees per second
    has_trend: bool = False
    balance: float = 0.0     # seconds at full reference output
    retry_after: float = 0.0
    below_limit_since: float | None = None
    reopen_after: float = 0.0
    coast: CoastObservation | None = None
    pulse_low: float | None = None
    pulse_low_stamp: float | None = None

    @property
    def waiting_for_peak(self):
        return self.coast is not None and self.coast.from_cutoff


def _bounded(value, low, high):
    return min(high, max(low, value))


def _number(value):
    return type(value) in (float, int) and isfinite(value)


class PulseScheduler:
    """Latch individual rooms between their lower bound and predicted upper peak."""

    def __init__(self, minimum, maximum, overshoot=0.2, undershoot=0.1, exponent=1.3):
        self.minimum = float(minimum)
        self.maximum = float(maximum)
        self.overshoot = float(overshoot)
        self.undershoot = float(undershoot)
        self.exponent = float(exponent)
        self._residual = {}
        self.reset()

    def reset(self):
        """Discard physical observations and live commands on reload or a clock gap."""
        self._rooms = {}
        self._members = set()
        self._admission_reasons = {}
        self.room_decisions = {}
        self._recovery = set()
        self._heated = set()
        self._finished = set()
        self._opening_since = {}
        self.opening_timeouts = set()
        self._previous = {}
        self._last = None
        self._calling = False
        self._shared_heat_call = False
        self._saw_flame = False
        self._flame_off_at = None
        self._rest_until = 0.0

    @property
    def heat_balance_minutes(self):
        return {key: round(room.balance / 60, 2) for key, room in self._rooms.items()}

    def _observe(self, now, zones):
        elapsed = 0 if self._last is None else now - self._last
        if elapsed < 0 or elapsed > MAX_SAMPLE_GAP:
            self.reset()
            elapsed = 0
        for key in self._rooms.keys() - zones.keys():
            del self._rooms[key]
        for key, zone in zones.items():
            stamp = zone.observed_at if _number(zone.observed_at) else now
            room = self._rooms.get(key)
            if room is None or room.target != zone.target:
                self._rooms[key] = RoomHistory(zone.target, stamp, zone.current, now)
                self._heated.discard(key)
                continue
            demand, emitted = self._previous[key]
            room.balance = _bounded(room.balance + (emitted - demand) * elapsed, -3600, 3600)
            interval = stamp - room.stamp
            if interval < 0 or interval > MAX_TREND_INTERVAL:
                room.slope = 0.0
                room.has_trend = False
                room.stamp, room.temperature, room.sampled_at = stamp, zone.current, now
            elif interval >= 60:
                rate = _bounded((zone.current - room.temperature) / interval, -0.003, 0.003)
                # An earlier cooling observation must not label a room which is
                # now flat or warming as an opportunistic participant.
                if room.has_trend and room.slope * rate > 0:
                    rate = 0.5 * (room.slope + rate)
                room.slope, room.has_trend = rate, True
                room.stamp, room.temperature, room.sampled_at = stamp, zone.current, now
            elif now - room.sampled_at > MAX_SAMPLE_GAP:
                room.slope, room.has_trend = 0.0, False

            if room.coast is not None:
                self._observe_coast(now, zone, room)
        self._last = now

    def _observe_coast(self, now, zone, room):
        coast = room.coast
        age = now - coast.started
        stamp = zone.observed_at
        if _number(stamp) and coast.last_stamp is not None and stamp < coast.last_stamp:
            room.coast = None
            return
        if _number(stamp):
            if stamp == coast.last_stamp:
                return
            coast.last_stamp = stamp
        elif coast.last_stamp is not None or zone.current == coast.last_temperature:
            return

        # An expired observation cannot teach a peak. Wait for this first new
        # report before allowing a cold room to start again on the same reading.
        if age >= MAX_COAST_TIME:
            room.coast = None
            return
        coast.last_temperature = zone.current
        coast.peak = max(coast.peak, zone.current)
        if age <= MIN_COAST_TIME:
            coast.sampled_early = True
        if age < MIN_COAST_TIME or coast.peak - zone.current < PEAK_DROP - 1e-6:
            return
        room.coast = None
        rise = coast.peak - coast.cut_temperature
        # A sensor that first reports cold well after closure may have missed
        # the whole peak. It can request heat again without teaching a zero rise.
        if rise == 0 and not coast.sampled_early:
            return
        key = zone.key
        if key not in self._residual and len(self._residual) >= MAX_ROOMS:
            del self._residual[next(iter(self._residual))]
        self._residual[key] = 0.65 * self._residual.get(key, 0.1) + 0.35 * rise

    def _slope(self, zone):
        room = self._rooms[zone.key]
        if room.has_trend:
            return room.slope
        if _number(zone.slope_per_hour):
            return _bounded(zone.slope_per_hour / 3600, -0.003, 0.003)
        return 0.0

    def _cutoff_temperature(self, zone):
        # Only measured post-close warmth can justify a cutoff below the comfort band.
        band_reserve = max(0.0, self.overshoot + self.undershoot - 0.02)
        trend = max(0.0, self._slope(zone)) * 120 * (1 - zone.demand)
        reserve = max(self._residual.get(zone.key, min(0.1, band_reserve)), min(trend, band_reserve))
        return zone.target + self.overshoot - reserve

    def _at_peak(self, zone, margin=0.0):
        return zone.current + margin >= self._cutoff_temperature(zone) - 1e-6

    def _due(self, now, zone):
        room = self._rooms[zone.key]
        if now < room.retry_after or room.waiting_for_peak or zone.current >= zone.target + self.overshoot:
            return False
        lower = zone.target - self.undershoot
        cutoff = self._cutoff_temperature(zone)
        if cutoff > lower and zone.current >= cutoff - 1e-6:
            return False
        # Forecast three minutes for valve travel, boiler ignition and transit.
        forecast = zone.current + min(0.0, self._slope(zone)) * 180
        return zone.current <= zone.target and forecast <= lower + 1e-6

    def _should_close(self, zone):
        room = self._rooms[zone.key]
        if room.pulse_low is None or zone.current <= room.pulse_low:
            room.pulse_low = zone.current
            room.pulse_low_stamp = zone.observed_at
        if zone.current >= zone.target + self.overshoot:
            return True
        cutoff = self._cutoff_temperature(zone)
        if zone.current < cutoff - 1e-6:
            return False
        if cutoff > zone.target - self.undershoot:
            return True
        # A cold radiator can start above a learned cutoff. Give this pulse a
        # chance to deliver heat before expecting it to coast to the upper edge.
        fresh = room.pulse_low_stamp is None or (
            _number(zone.observed_at) and zone.observed_at > room.pulse_low_stamp
        )
        return (zone.key in self._heated and fresh
                and zone.current - room.pulse_low >= MIN_PULSE_RISE - 1e-6)

    def _start_delay(self, now, zone):
        """Qualify a marginal opening without ever delaying a closing command."""
        room = self._rooms[zone.key]
        lower = zone.target - self.undershoot
        if zone.current > lower + 1e-6:
            room.below_limit_since = None
        elif room.below_limit_since is None:
            room.below_limit_since = now

        if now < room.reopen_after:
            return "valve_reopen_delay"
        # A learned cutoff can be just one sensor step away from the opening
        # threshold. Repeated crossings must not keep reversing the motor.
        if (self._cutoff_temperature(zone) <= lower + START_MARGIN + 1e-6
                and zone.current > lower - START_MARGIN + 1e-6
                and (room.below_limit_since is None or now - room.below_limit_since < START_SETTLE_TIME)):
            return "waiting_for_stable_demand"
        return None

    def _release(self, key, now, zone=None, *, at_cutoff=False):
        if at_cutoff:
            room = self._rooms[key]
            room.reopen_after = now + VALVE_REOPEN_DELAY
            room.below_limit_since = None
        if zone is not None and key in self._heated:
            self._rooms[key].coast = CoastObservation(
                now, zone.current, zone.current, zone.current, zone.observed_at, from_cutoff=at_cutoff
            )
        self._members.remove(key)
        self._admission_reasons.pop(key, None)
        self._recovery.discard(key)
        self._heated.discard(key)
        self._opening_since.pop(key, None)

    def _burner_stop(self, now, water, burner):
        """Require CH-specific flame evidence and supply reaching the requested maximum."""
        if burner is True:
            if self._flame_off_at is not None:
                return "burner_restarted"
            self._saw_flame = True
        elif burner is False and self._saw_flame:
            if self._flame_off_at is None and _number(water) and water >= self.maximum - 2:
                self._flame_off_at = now
            if self._flame_off_at is not None and now - self._flame_off_at >= 5:
                return "burner_reached_flow"
        else:
            # An unavailable reading breaks the chain of evidence.
            self._saw_flame = False
            self._flame_off_at = None
        return None

    def _room_reason(self, now, zone):
        """Explain the most recent admission or the current constraint on admission."""
        key = zone.key
        if key in self._members:
            return self._admission_reasons[key]
        if now < self._rest_until:
            return "burner_rest"
        if now < self._rooms[key].retry_after:
            return "valve_opening_timeout"
        if self._rooms[key].waiting_for_peak or self._at_peak(zone):
            return "cutoff_reached"
        if key in self._finished:
            return "finished_this_run"
        if not self._members and not self._shared_heat_call:
            return "waiting_for_shared_call"
        if self._at_peak(zone, margin=COMPANION_MARGIN):
            return "insufficient_headroom"
        if not self._rooms[key].has_trend and not _number(zone.slope_per_hour):
            return "trend_unavailable"
        return "not_cooling"

    def update(self, now, zones, water=None, burner=None, boiler_on=False, outdoor=None, *, shared_heat_call=False):
        """A confirmed independent heat call can also admit eligible cooling companions."""
        self.opening_timeouts.clear()
        zones = {z.key: z for z in zones if _number(z.current) and _number(z.target)
                 and _number(z.demand) and 0 <= z.demand <= 1}
        self._observe(now, zones)
        if not self._members and not (self._shared_heat_call and shared_heat_call):
            self._finished.clear()
        self._shared_heat_call = shared_heat_call
        for key in self._members.copy():
            if key not in zones:
                self._finished.add(key)
                self._release(key, now)
                continue
            zone = zones[key]
            room = self._rooms[key]
            if zone.open_confirmed:
                self._opening_since[key] = now
                if self._calling and boiler_on and room.pulse_low is not None:
                    self._heated.add(key)
            if self._should_close(zone):
                self._finished.add(key)
                self._release(key, now, zone, at_cutoff=True)
            elif now - self._opening_since[key] >= OPEN_TIMEOUT:
                self.opening_timeouts.add(key)
                self._rooms[key].retry_after = now + RETRY_DELAY
                self._release(key, now)

        confirmed = {key for key in self._members if zones[key].open_confirmed}
        stop = None
        if confirmed and self._calling and boiler_on:
            stop = self._burner_stop(now, water, burner)
        else:
            self._saw_flame = False
            self._flame_off_at = None
        if stop:
            self._rest_until = now + RETRY_DELAY
            for key in self._members.copy():
                self._release(key, now, zones[key])

        # Keep observing closed rooms even during burner rest. Any warmer
        # reading breaks a pending two-minute run at the lower limit.
        candidates = zones.keys() - self._members
        delays = {key: reason for key in candidates if (reason := self._start_delay(now, zones[key]))}
        held = {}
        if now >= self._rest_until:
            due = {key for key in candidates if self._due(now, zones[key])
                   and (key not in self._finished
                        or zones[key].current <= zones[key].target - self.undershoot + 1e-6)}
            held = {key: delays[key] for key in due & delays.keys()}
            due -= delays.keys()
            if due or self._members or shared_heat_call:
                # One ignition can also serve cooling rooms anywhere inside
                # their band if there is room for a useful burst plus residual rise.
                companions = {key for key in candidates - self._finished - due
                              if now >= self._rooms[key].retry_after and not self._rooms[key].waiting_for_peak
                              and self._slope(zones[key]) <= COOLING_RATE
                              and not self._at_peak(zones[key], margin=COMPANION_MARGIN)}
                for key in due | companions:
                    if key in delays:
                        held[key] = delays[key]
                        continue
                    self._members.add(key)
                    if key in companions:
                        self._admission_reasons[key] = "joined_cooling"
                    elif zones[key].current <= zones[key].target - self.undershoot + 1e-6:
                        self._admission_reasons[key] = "lower_limit"
                    else:
                        self._admission_reasons[key] = "lower_limit_forecast"
                    self._opening_since[key] = now
                    self._rooms[key].coast = None
                    self._rooms[key].pulse_low = zones[key].current
                    self._rooms[key].pulse_low_stamp = zones[key].observed_at
        self._recovery.update(key for key in self._members if zones[key].recovery)
        confirmed = {key for key in self._members if zones[key].open_confirmed}
        self._calling = bool(confirmed)
        if self._calling:
            phase = "recovery" if self._recovery & confirmed else "heating"
            reason = "confirmed_open"
        elif self._members:
            phase, reason = "opening", "waiting_for_valve"
        else:
            phase, reason = "coast", stop or ("burner_rest" if now < self._rest_until else "room_comfort")

        # Heat credit is diagnostic only. Integrate this command over the next
        # interval with actual measured supply and acknowledged delivery.
        self._previous = {}
        self.room_decisions = {}
        for key, zone in zones.items():
            has_slope = self._rooms[key].has_trend or _number(zone.slope_per_hour)
            self.room_decisions[key] = {
                "reason": held.get(key) or self._room_reason(now, zone),
                "slope_per_hour": round(self._slope(zone) * 3600, 4) if has_slope else None,
                "lower_temperature": round(zone.target - self.undershoot, 3),
                "upper_temperature": round(zone.target + self.overshoot, 3),
                "cutoff_temperature": round(self._cutoff_temperature(zone), 3),
            }
            emitted = 0.0
            if key in confirmed and boiler_on and _number(water):
                headroom = max(1, self.maximum - zone.current)
                emitted = min(1.0, (max(0, water - zone.current) / headroom) ** self.exponent)
            self._previous[key] = (zone.demand, emitted)
        return PulseDecision(phase, frozenset(self._members), self._calling,
                             self.maximum if self._calling else 0.0, reason, frozenset(self._recovery))

    def _settings(self):
        return [self.minimum, self.maximum, self.overshoot, self.undershoot, self.exponent]

    def dump_learning(self):
        """Only measured room residual rises survive a restart."""
        return {"version": 2, "settings": self._settings(), "residual": dict(self._residual)}

    def restore_learning(self, data):
        if not isinstance(data, dict) or data.get("version") not in (1, 2):
            return
        if data.get("settings") != self._settings():
            return
        residual = data.get("residual")
        if isinstance(residual, dict):
            # Version one also included water-temperature trials. They have no
            # meaning for binary calls and are deliberately ignored.
            self._residual = {key: value for key, value in list(residual.items())[-MAX_ROOMS:]
                              if isinstance(key, str) and _number(value) and value >= 0}
