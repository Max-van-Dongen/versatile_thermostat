"""Pure, bounded flow-temperature selection for a shared radiator circuit.

The version-1 SmartPI contract expresses demand in reference-temperature heat
units. Valve positions must never be substituted for these heat units.
"""

from dataclasses import dataclass
from math import isfinite

FLOW_ENABLED = "boiler_flow_enabled"
FLOW_ENTITY = "boiler_flow_entity"
FLOW_SENSOR = "boiler_flow_sensor"
FLOW_MIN = "boiler_flow_min"
FLOW_MAX = "boiler_flow_max"
FLOW_REFERENCE = "boiler_flow_reference"
FLOW_OPENING = "boiler_flow_target_opening"
FLOW_EXPONENT = "boiler_flow_exponent"
FLOW_SETTLE = "boiler_flow_settle_seconds"
PULSE_ENABLED = "boiler_pulse_enabled"
PULSE_FLAME = "boiler_pulse_flame_sensor"
PULSE_GAS_RATE = "boiler_pulse_gas_rate_sensor"
PULSE_GAS_RATE_UNITS = ("m\u00b3/h", "L/min")
PULSE_OVERSHOOT = "boiler_pulse_overshoot"
PULSE_UNDERSHOOT = "boiler_pulse_undershoot"
FLOW_KEYS = (
    FLOW_ENABLED, FLOW_ENTITY, FLOW_SENSOR, FLOW_MIN, FLOW_MAX, FLOW_REFERENCE,
    FLOW_OPENING, FLOW_EXPONENT, FLOW_SETTLE, PULSE_ENABLED, PULSE_FLAME, PULSE_GAS_RATE,
    PULSE_OVERSHOOT, PULSE_UNDERSHOOT,
)


def finite(value):
    """Return a finite float, or None for absent/invalid entity data."""
    try:
        result = float(value)
        return result if isfinite(result) else None
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class FlowSettings:
    """All temperatures are degrees Celsius; opening is a fraction."""

    minimum: float = 35.0
    maximum: float = 65.0
    reference: float = 65.0
    opening: float = 0.70
    exponent: float = 1.3
    settle_seconds: float = 300.0
    pulse_enabled: bool = False
    pulse_flame_sensor: str | None = None
    pulse_overshoot: float = 0.2
    pulse_undershoot: float = 0.1
    pulse_gas_rate_sensor: str | None = None

    def __post_init__(self):
        values = (self.minimum, self.maximum, self.reference, self.opening, self.exponent, self.settle_seconds)
        if not all(isfinite(v) for v in values):
            raise ValueError("Flow settings must be finite")
        # Never operate above the learned reference: normalized power remains
        # in SmartPI's established 0..1 domain.
        if not 25 <= self.minimum <= self.maximum <= self.reference <= 85:
            raise ValueError("Require 25 <= minimum <= maximum <= reference <= 85")
        if self.maximum != self.reference:
            raise ValueError("Maximum must equal the calibration reference")
        if not 0.10 <= self.opening <= 1 or not 1 <= self.exponent <= 2:
            raise ValueError("Invalid target opening or radiator exponent")
        if not 60 <= self.settle_seconds <= 3600:
            raise ValueError("Settling time must be 60..3600 seconds")
        if not (0.1 <= self.pulse_overshoot <= 1 and 0.1 <= self.pulse_undershoot <= 1):
            raise ValueError("Pulse temperature limits must be 0.1..1 Celsius")

    @classmethod
    def from_entry(cls, data):
        return cls(
            float(data.get(FLOW_MIN, 35)),
            float(data.get(FLOW_MAX, 65)),
            float(data.get(FLOW_REFERENCE, 65)),
            float(data.get(FLOW_OPENING, 70)) / 100,
            float(data.get(FLOW_EXPONENT, 1.3)),
            float(data.get(FLOW_SETTLE, 300)),
            pulse_enabled=bool(data.get(PULSE_ENABLED, False)),
            pulse_flame_sensor=data.get(PULSE_FLAME),
            pulse_gas_rate_sensor=data.get(PULSE_GAS_RATE),
            pulse_overshoot=float(data.get(PULSE_OVERSHOOT, 0.2)),
            pulse_undershoot=float(data.get(PULSE_UNDERSHOOT, 0.1)),
        )


def choose_flow(settings: FlowSettings, requests: list[dict]) -> tuple[float, str]:
    """The most demanding room determines the shared flow temperature.

    Each request supplies linear heat demand and the unscaled valve curve's
    heat fraction at the desired valve opening. Unknown/mixed controllers and
    calibration use the reference temperature.
    """
    desired = settings.minimum
    for request in requests:
        if request.get("reference_required"):
            return settings.reference, "reference_required"
        room = finite(request.get("room_temperature"))
        demand = finite(request.get("demand"))
        capacity = finite(request.get("target_capacity"))
        if room is None or demand is None or capacity is None or not 0 <= demand <= 1 or not 0 < capacity <= 1 or not -20 <= room < settings.minimum - 1:
            return settings.reference, "invalid_demand"
        if demand > 0:
            required = room + (settings.reference - room) * (demand / capacity) ** (1 / settings.exponent)
            desired = max(desired, required)
    return min(settings.maximum, desired), "modulating"


def slew_flow(current: float, desired: float, elapsed: float) -> float:
    """Meet higher demand immediately; fall at 1 C/min without downtime jumps."""
    if desired >= current:
        return desired
    minutes = min(max(elapsed, 0), 60) / 60
    return max(current - minutes, desired)
