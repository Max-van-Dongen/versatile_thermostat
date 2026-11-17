"""Coordinate space-heating pulses with fully open radiators. All times are seconds."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from math import floor, isfinite
from statistics import median

OPEN_TIMEOUT = 180
RETRY_DELAY = 300
MAX_SAMPLE_GAP = 300
MAX_PROFILES = 96
MAX_SAMPLES = 32


@dataclass(frozen=True)
class PulseZone:
    key: str
    current: float
    target: float
    demand: float
    open_confirmed: bool


@dataclass(frozen=True)
class PulseDecision:
    phase: str
    valves: frozenset[str]
    call_for_heat: bool
    flow_temperature: float
    reason: str


@dataclass
class RoomHistory:
    target: float
    anchor: float
    temperature: float
    slope: float = 0.0       # degrees per second
    balance: float = 0.0     # seconds at full reference output
    retry_after: float = 0.0
    underheated_since: float | None = None
    cut_temperature: float | None = None
    cut_at: float = 0.0
    peak: float = 0.0


@dataclass
class Trial:
    profile: tuple
    targets: dict[str, float]
    flow: float
    started: float
    valid: bool
    saw_flame: bool = False
    cut_early: bool = False
    off_at: float | None = None
    multiple_ignitions: bool = False
    ended: float | None = None
    peaks: dict[str, float] = field(default_factory=dict)
    confirmed: set[str] = field(default_factory=set)


def _bounded(value, low, high):
    return min(high, max(low, value))


def _number(value):
    return type(value) in (float, int) and isfinite(value)


class PulseScheduler:
    """Request heat only while a participating radiator is confirmed open."""

    def __init__(self, minimum, maximum, overshoot=0.2, undershoot=0.1, exponent=1.3):
        self.minimum = float(minimum)
        self.maximum = float(maximum)
        self.overshoot = float(overshoot)
        self.undershoot = float(undershoot)
        self.exponent = float(exponent)
        self._profiles = {}
        self._residual = {}
        self.reset()

    def reset(self):
        """Abort any in-flight experiment and discard transient heat credit."""
        self._rooms = {}
        self.opening_timeouts = set()
        self.comfort_timeouts = set()
        self._members = set()
        self._opening_since = {}
        self._trial = None
        self._previous = {}
        self._last = None
        self._rest_until = 0.0

    @property
    def heat_balance_minutes(self):
        return {key: round(room.balance / 60, 2) for key, room in self._rooms.items()}

    def _decision(self, phase, reason):
        flow = self._trial.flow if self._members else self.minimum
        if phase != "heating":
            # The next integration interval starts with the command we are
            # returning. Residual radiator emission is captured by the coast peak.
            self._previous = {key: (demand, 0.0) for key, (demand, _) in self._previous.items()}
        return PulseDecision(phase, frozenset(self._members), phase == "heating", flow, reason)

    def _observe(self, now, zones, water, boiler_on):
        """Integrate the preceding sample interval, never retroactively credit a new opening."""
        elapsed = 0 if self._last is None else now - self._last
        if elapsed < 0 or elapsed > MAX_SAMPLE_GAP:
            self.reset()
            elapsed = 0
        for key in self._rooms.keys() - zones.keys():
            del self._rooms[key]
        for key, zone in zones.items():
            room = self._rooms.get(key)
            if room is None or room.target != zone.target:
                room = self._rooms[key] = RoomHistory(zone.target, now, zone.current)
            else:
                demand, emitted = self._previous[key]
                room.balance = _bounded(room.balance + (emitted - demand) * elapsed, -3600, 3600)
                interval = now - room.anchor
                if interval >= 60:
                    slope = _bounded((zone.current - room.temperature) / interval, -0.003, 0.003)
                    room.slope = 0.5 * room.slope + 0.5 * slope
                    room.temperature, room.anchor = zone.current, now
            if room.cut_temperature is not None:
                room.peak = max(room.peak, zone.current)
                # Watch residual rise even after the valve closes. Discard unfinished
                # observations when another burst starts or the target is changed.
                if now - room.cut_at >= 900 or (
                    now - room.cut_at >= 180 and zone.current < room.peak - 0.04
                ):
                    rise = _bounded(room.peak - room.cut_temperature, 0, 1)
                    self._residual[key] = 0.65 * self._residual.get(key, 0.1) + 0.35 * rise
                    room.cut_temperature = None
            if (key in self._members and zone.open_confirmed and boiler_on
                    and zone.current <= zone.target - self.undershoot
                    and room.slope <= 0.1 / 3600):
                if room.underheated_since is None:
                    room.underheated_since = now
                elif now - room.underheated_since >= 1800:
                    self.comfort_timeouts.add(key)
            else:
                room.underheated_since = None
        self._previous = {}
        for key, z in zones.items():
            emitted = 0.0
            if boiler_on and z.open_confirmed and key in self._members and _number(water):
                delta = max(1, self.maximum - z.current)
                emitted = min(1.0, (max(0, water - z.current) / delta) ** self.exponent)
            self._previous[key] = (z.demand, emitted)
        self._last = now

    def _at_peak(self, zone):
        # Peaks after stopping are identified independently of the SmartPI model.
        # Reserve room for residual heat while allowing a pulse near the lower edge.
        reserve = min(self.overshoot + self.undershoot - 0.02, self._residual.get(zone.key, 0.1))
        return zone.current >= zone.target + self.overshoot - reserve - 1e-6

    def _due(self, now, zone):
        room = self._rooms[zone.key]
        if now < room.retry_after or self._at_peak(zone):
            return False
        floor_temp = zone.target - self.undershoot
        if zone.current <= floor_temp + 1e-6:
            return True
        # Allow three minutes for valve travel, boiler start and water transit.
        forecast = zone.current + min(0.0, room.slope) * 180
        return zone.demand > 0 and zone.current <= zone.target and forecast <= floor_temp

    def _profile_key(self, members, zones, water, outdoor):
        # Compare the same emitter combination at similar absolute water,
        # outdoor, and room-target temperatures. Unknown conditions have their own bin.
        return (
            tuple(sorted((key, round(zones[key].target * 2) / 2) for key in members)),
            floor(water / 5) if _number(water) else None,
            floor(outdoor / 5) if _number(outdoor) else None,
        )

    def _select_flow(self, profile):
        history = self._profiles.get(profile, ())
        if not history:
            return self.minimum
        groups = {}
        for temp, coast, success in history:
            groups.setdefault(temp, []).append((coast, success))
        # Two failed attempts since the last success justify trying hotter water.
        def failed(temp):
            recent = groups.get(temp, [])[-2:]
            return len(recent) == 2 and not any(ok for _, ok in recent)

        qualified = {t: median(c for c, ok in obs[-5:] if ok)
                     for t, obs in groups.items()
                     if any(ok for _, ok in obs[-5:]) and not failed(t)}
        last = history[-1][0]
        if not qualified:
            return min(self.maximum, last + 2) if failed(last) else last
        longest = max(qualified.values())
        # Within two minutes or 10% of the best observed coast, prefer lower flow.
        tolerance = max(120, longest * 0.10)
        best = min(t for t, coast in qualified.items() if coast >= longest - tolerance)
        # Repeat a new probe before trusting a single weather/noise-dependent result.
        if len(groups[last]) == 1:
            return last
        if all(t == best and ok for t, _, ok in list(history)[-3:]) and len(history) >= 3:
            # Probe near a known good setting only after comparable successes.
            for candidate in [max(self.minimum, best - 1), min(self.maximum, best + 1)]:
                if candidate != best and len(groups.get(candidate, ())) < 2:
                    return candidate
            # A flat first degree does not establish that all higher settings
            # coast equally long. Occasionally explore past measured neighbors.
            if len(history) >= 8 and all(t == best for t, _, _ in list(history)[-5:]):
                candidates = sorted({min(self.maximum, t + 2) for t in qualified})
                for candidate in candidates:
                    if candidate > best and len(groups.get(candidate, ())) < 2:
                        return candidate
        return best

    def _record(self, now, zones):
        trial = self._trial
        if not trial or trial.ended is None or not trial.valid or not trial.saw_flame:
            return
        coast = now - trial.ended
        if not 0 <= coast <= 4 * 3600:
            return
        if any(key not in zones or zones[key].target != target for key, target in trial.targets.items()):
            return
        # Reject excess overshoot. An internal stop can still yield a successful
        # single ignition if residual heat carried the room to the planned peak.
        under_cap = all(trial.peaks[k] <= target + self.overshoot + 0.051
                        for k, target in trial.targets.items())
        enough = all(trial.peaks[k] >= target + self.overshoot - 0.11
                     for k, target in trial.targets.items())
        if not under_cap or (not trial.cut_early and not enough):
            return
        if trial.profile not in self._profiles and len(self._profiles) >= MAX_PROFILES:
            del self._profiles[next(iter(self._profiles))]
        self._profiles.setdefault(trial.profile, deque(maxlen=MAX_SAMPLES)).append(
            (trial.flow, coast, enough and not trial.multiple_ignitions)
        )

    def _end_room(self, key, now, zone):
        room = self._rooms[key]
        room.cut_temperature = room.peak = zone.current
        room.cut_at = now

    def _end(self, now, zones, reason):
        for key in self._members & zones.keys() & self._trial.confirmed:
            self._end_room(key, now, zones[key])
        self._members.clear()
        self._trial.ended = now
        return self._decision("coast", reason)

    def update(self, now, zones, water=None, burner=None, boiler_on=False, outdoor=None):
        """The caller filters unsafe zones and supplies only a CH-specific burner observation.

        Missing flame observations disable flow adaptation. The pulse envelope
        still works at minimum flow. 'boiler_on' is the acknowledged external
        request; it must not be used as the flame signal.
        """
        self.opening_timeouts.clear()
        self.comfort_timeouts.clear()
        zones = {z.key: z for z in zones if _number(z.current) and _number(z.target)
                 and _number(z.demand) and 0 <= z.demand <= 1}
        self._observe(now, zones, water, boiler_on)
        trial = self._trial
        if trial:
            if any(k not in zones or zones[k].target != t for k, t in trial.targets.items()):
                trial.valid = False
            for key in trial.targets.keys() & zones.keys():
                trial.peaks[key] = max(trial.peaks[key], zones[key].current)
        if self._members:
            if self.comfort_timeouts:
                trial.valid = False
                return self._end(now, zones, "insufficient_room_response")
            # A setpoint edit invalidates the pulse designed for the old target.
            if any(zones[k].target != trial.targets[k] for k in self._members & zones.keys()):
                return self._end(now, zones, "changed_room_target")
            # A different room can reach its comfort floor during a long pulse.
            # Admit it immediately, while keeping the current flow; a different
            # emitter combination is not evidence about the original group.
            for key in zones.keys() - self._members:
                if self._due(now, zones[key]):
                    trial.valid = False
                    self._members.add(key)
                    self._opening_since[key] = now
                    trial.targets[key] = zones[key].target
                    trial.peaks[key] = zones[key].current
                    self._rooms[key].cut_temperature = None
            for key in self._members.copy():
                if key not in zones or self._at_peak(zones[key]):
                    if key in zones and key in trial.confirmed:
                        self._end_room(key, now, zones[key])
                    self._members.remove(key)
                elif zones[key].open_confirmed:
                    self._opening_since[key] = now
                    if boiler_on:
                        trial.confirmed.add(key)
                elif now - self._opening_since[key] >= OPEN_TIMEOUT:
                    trial.valid = False
                    self.opening_timeouts.add(key)
                    self._rooms[key].retry_after = now + RETRY_DELAY
                    self._members.remove(key)
            if not self._members:
                return self._end(now, zones, "pulse_complete")
            if burner is None or not _number(water):
                trial.valid = False
            if boiler_on and burner is True:
                if trial.off_at is not None:
                    # A second ignition within the requested pulse.
                    trial.cut_early = True
                    trial.multiple_ignitions = True
                    self._rest_until = now + RETRY_DELAY
                    return self._end(now, zones, "burner_restarted")
                trial.saw_flame = True
            elif boiler_on and burner is False and trial.saw_flame:
                if trial.off_at is not None or (_number(water) and water >= trial.flow - 2):
                    if trial.off_at is None:
                        trial.off_at = now
                    if now - trial.off_at >= 5:
                        trial.cut_early = True
                        self._rest_until = now + RETRY_DELAY
                        return self._end(now, zones, "burner_reached_flow")
                else:
                    # Unknown cause, e.g. DHW interruption. Do not train on it.
                    trial.valid = False
            if any(zones[k].open_confirmed for k in self._members):
                return self._decision("heating", "confirmed_open")
            return self._decision("opening", "waiting_for_valve")

        due = {k for k, z in zones.items() if self._due(now, z)}
        if not due:
            return self._decision("coast", "room_comfort")
        if now < self._rest_until and not any(
            zones[k].current < zones[k].target - self.undershoot - 0.3 for k in due
        ):
            return self._decision("coast", "burner_rest")
        members = due | {
            k for k, z in zones.items() if z.demand > 0 and not self._at_peak(z)
            and self._rooms[k].balance <= 0 and now >= self._rooms[k].retry_after
        }
        if trial and trial.ended is not None:
            # A newly participating room can shorten the coast for reasons that
            # have nothing to do with the previous water-temperature setting.
            if due <= trial.targets.keys():
                self._record(now, zones)
        profile = self._profile_key(members, zones, water, outdoor)
        flow = self._select_flow(profile) if type(burner) is bool and _number(water) else self.minimum
        self._members = members
        self._opening_since = dict.fromkeys(members, now)
        self._trial = Trial(profile, {k: zones[k].target for k in members}, flow, now,
                            not boiler_on and type(burner) is bool and _number(water),
                            peaks={k: zones[k].current for k in members})
        for k in members:
            self._rooms[k].cut_temperature = None
        if any(zones[k].open_confirmed for k in members):
            return self._decision("heating", "confirmed_open")
        return self._decision("opening", "waiting_for_valve")

    def dump_learning(self):
        """Portable bounded history. Physical state, timers and credits never persist."""
        return {
            "version": 1,
            "settings": [self.minimum, self.maximum, self.overshoot, self.undershoot, self.exponent],
            "residual": dict(self._residual),
            "profiles": [[list(members), water, outside, list(samples)]
                         for (members, water, outside), samples in self._profiles.items()],
        }

    def restore_learning(self, data):
        if not isinstance(data, dict) or data.get("version") != 1:
            return
        if data.get("settings") != [self.minimum, self.maximum, self.overshoot, self.undershoot, self.exponent]:
            return
        residual = data.get("residual")
        if isinstance(residual, dict):
            self._residual = {k: v for k, v in list(residual.items())[-256:]
                              if isinstance(k, str) and _number(v) and 0 <= v <= 1}
        profiles = data.get("profiles")
        if not isinstance(profiles, list):
            return
        self._profiles.clear()
        for item in profiles[-MAX_PROFILES:]:
            try:
                members, water, outside, rows = item
                keys = tuple((k, t) for k, t in members)
                if not (0 < len(keys) <= 128 and all(isinstance(k, str) and _number(t) for k, t in keys)):
                    continue
                if any(v is not None and type(v) is not int for v in (water, outside)):
                    continue
                samples = deque(maxlen=MAX_SAMPLES)
                for temp, seconds, ok in rows[-MAX_SAMPLES:]:
                    if (_number(temp) and self.minimum <= temp <= self.maximum
                            and _number(seconds) and 0 <= seconds <= 4 * 3600 and type(ok) is bool):
                        samples.append((temp, seconds, ok))
                if samples:
                    self._profiles[(keys, water, outside)] = samples
            except (TypeError, ValueError, KeyError):
                continue
