# Coordinated boiler flow and SmartPI valves

This custom fork adds **opt-in heating-water temperature control**. Install the
matching [SmartPI fork](https://github.com/Max-van-Dongen/vtherm_smartpi) as well.
The integration domains, existing entities and saved SmartPI models are retained.
This is a custom implementation, not an upstream VTherm release.

## What it does

SmartPI still decides each room's required heat. VTherm chooses one shared boiler
flow temperature that lets the most demanding room approach the configured valve
opening. All other rooms get independently compensated valve positions.

- Lower demand lowers flow temperature, down to the configured minimum.
- Higher demand raises it, up to the calibration reference.
- The most demanding room wins; demands are not averaged across rooms.
- Room-temperature deficits do not independently force reference temperature:
  SmartPI already includes them in its calculated heat demand. A stable room
  controller can therefore request cooler water while recovering toward target.
- Active bootstrap/calibration and incompatible active controllers also request
  the reference temperature.
- Upward changes go directly to the required temperature on the next controller
  update. Downward changes are limited to 1 C/min, subject to the number entity's
  step size while heating continues.
- Each new heating cycle starts directly at its current calculated temperature.
  A low-demand restart after an overshoot does not first heat the water to the
  reference. The same applies to the first activation with an already learned
  SmartPI model. Calibration, high demand and other reference overrides still win.
- The setpoint is reasserted every 60 seconds while heating. Zero-demand OFF
  commands and the existing activation delay remain effective. There is no new
  minimum-on time keeping the boiler running after the valves close.

The feature controls **central-heating water**, not domestic hot water or room
temperature. It requires a writable Celsius `number` entity for the boiler flow
setpoint. Existing activation/deactivation services still enable/disable heating.

## Install the two forks

1. Back up Home Assistant, including configuration and `.storage`, to preserve
   the existing integration settings and SmartPI calibration.
2. Download the default branches:
   [VTherm ZIP](https://github.com/Max-van-Dongen/versatile_thermostat/archive/refs/heads/main.zip)
   and [SmartPI ZIP](https://github.com/Max-van-Dongen/vtherm_smartpi/archive/refs/heads/master.zip).
3. From each ZIP, copy its corresponding `custom_components/versatile_thermostat`
   or `custom_components/vtherm_smartpi` directory over that directory under your
   Home Assistant configuration. Keep the directory names unchanged. Do not
   create a second copy of either integration under another name.
4. Restart Home Assistant. Keep the existing integrations and thermostat entries;
   do not delete/recreate them.
5. Prevent HACS from replacing these directories with upstream updates while
   using the forks. These source ZIPs are sufficient for manual installation;
   a GitHub release asset is not required.

The paired implementation is tested with Home Assistant 2026.11.0. The SmartPI
upstream regression suite is additionally run against its pinned 2026.8.2 test
environment. This fork is based on VTherm 10.6.6 and SmartPI 0.6.6.

## Configure

In **VTherm's central configuration > Central boiler**, enable coordinated boiler
flow. The feature is disabled by default.

| Setting | Meaning |
| --- | --- |
| Boiler water flow setpoint | The boiler's Celsius `number` entity, not a TRV opening or room-temperature helper. |
| Actual boiler flow sensor | Optional Celsius supply-water sensor. Sensors without a temperature device class are also selectable. A configured sensor must report within 10 minutes. |
| Minimum flow | Software lower bound, default 35 C. Use a value appropriate for your boiler and emitters. |
| Maximum and reference | Set **both to the fixed flow temperature at which your present SmartPI model learned**, e.g. both 65 C or both 50 C. They must match in this implementation. |
| Target valve opening | Default 70%. The most demanding room aims for this; quieter rooms can stay below it. Must be above the useful minimum of the valve curve. |
| Radiator exponent | Default 1.3. This is an approximate emission curve parameter. |
| Settling time | Default 300 seconds. Delays model learning at reference temperature; it does not force a reference-temperature startup hold. |

Each participating room must use SmartPI and have **Used by central boiler**
enabled. Direct valves and climates with direct valve regulation are supported.
SmartPI over switches cannot perform this compensation and requests reference
flow when active. A thermostat without the matching SmartPI fork also keeps
reference flow when active.

Keep the existing boiler ON/OFF services and a suitable boiler activation
threshold. A direct activation such as `number.boiler_flow/number.set_value/value:65`
is overridden with the current coordinated value; its OFF service is left intact.
If using scripts for ON/OFF, remove any fixed flow-temperature writes from those
scripts. There must be one controller of the water-temperature setpoint.

With SmartPI valve linearization, follow its existing valve configuration guidance:
avoid additional VTherm opening remapping, opening thresholds or filters that
prevent the commanded opening from reaching the valve. Flow compensation composes
with the existing SmartPI curve; it does not correct arbitrary hydraulic or valve
calibration errors.

## Full-open heating pulses

Install VTherm 10.6.6.post7 and SmartPI 0.6.6.post2 together, then restart Home Assistant.
In **VTherm central configuration > Central boiler**, enable coordinated flow, select a
Celsius **Actual boiler flow temperature sensor**, and enable **Group low heat demand
into full-open radiator pulses**. The option defaults to off for new and existing entries.

| Setting | Effect |
| --- | --- |
| Allowed rise above the room target | Default 0.2 C. The controller ends heating in time for residual radiator heat to bring the room towards this upper limit. |
| Allowed drop below the room target | Default 0.1 C. The controller uses this lower limit to decide when a room will need heat again. |
| Space-heating burner flame | Optional binary sensor that reports burner activity during space heating. A boiler-demand switch, supply temperature sensor or a flame signal shared with hot-water production cannot distinguish the burner events this feature uses. Leave the field empty if your boiler has no suitable feedback. |
| Space-heating gas flow rate | Alternative to the flame sensor. Use a near-real-time space-heating gas rate in m³/h or L/min that promptly returns to zero when the burner stops. A cumulative m³ meter, five-minute averages or a whole-house gas rate that includes cooking or hot-water draws cannot identify heating starts. |

For a 20.5 C setpoint the default comfort band is 20.4 to 20.7 C. Room sensors report
in steps and radiators continue to release heat after shutdown, so individual readings
may cross the band. Each participating valve opens fully for its pulse. The central
controller checks the reported opening before starting the boiler. A device that echoes
a requested position immediately provides weaker evidence of mechanical travel than
a device reporting actual valve position. Verify this timing on your TRVs when commissioning.

Temperature selection starts at **Minimum flow**. With suitable burner feedback the
controller can compare pulses for the same combination of rooms and try a warmer
temperature when repeated burner cutouts prevent a useful run. The configured flow
maximum bounds those trials. Without burner feedback, pulses can still group room
demand at minimum flow. The controller cannot measure the number of ignitions from
water temperature alone. Select at most one burner feedback source. If a template supplies
the flame or gas-rate signal, make it unavailable during domestic hot-water draws so
those interruptions cannot train the scheduler. A zero gas rate means a stopped
space-heating burner. Missing reports leave the burner state unknown.
Room comfort, other zones needing heat, and the active
radiators' capacity limit the achievable interval between heating cycles.

SmartPI continues to calculate demand at its calibration reference. Its ordinary
learning pauses while pulse control owns the valves. Disable the pulse checkbox to
return to ordinary coordinated flow.

On the central boiler binary sensor, inspect `central_boiler_manager.dynamic_flow.pulse`.
`phase` progresses from `coast` to `opening` to `heating`. During `opening`,
compare `requested_valves` with `reported_opening` (0 to 1). During `heating`,
`requested_flow` is the selected pulse temperature, and `burner` is the separate
flame observation when available. A `normal` phase and its `reason` explain when a
room recovery, calibration, incompatible controller or sensor condition gives
ordinary coordinated flow priority. The scheduler saves completed observations and
starts each new Home Assistant session from fresh valve and temperature reports.

## Heat units and learning

For each room, let `q` be SmartPI's heat demand at the reference flow temperature,
`C(v)` the existing valve curve's inverse (valve position to heat fraction),
`Tr` room temperature, `Tref` reference flow and `n` the radiator exponent:

```text
capacity(T) = ((T - Tr) / (Tref - Tr)) ** n
required_flow = Tr + (Tref - Tr) * (q / C(target_opening)) ** (1/n)
valve_command = C_inverse(q / capacity(actual_or_estimated_flow))
realized_heat = C(applied_valve_position) * capacity(actual_or_estimated_flow)
```

VTherm takes the maximum required flow across rooms, clamps it to the configured
bounds and limits only downward changes. A 100% reference-temperature demand
requests the maximum/reference immediately. A valve at 100% because the water
is cooler instead requests the temperature calculated from its reference heat
demand and preferred valve opening. If that temperature is 48 C, the setpoint
goes directly to 48 C rather than ramping up or automatically selecting maximum.
Calibration and other reference-required conditions still take priority.
The periodic controller update runs every 60 seconds, with demand events able to
trigger earlier updates; "immediately" means the next eligible update, not an
instantaneous change in water temperature. Compensation continues to follow the
measured or estimated water temperature as the circuit warms.

SmartPI saturates the valve at its allowed
opening and accounts for the reduced available heat in its anti-windup limit.
Feedback uses the same conversion as commands, so a larger valve opening at a
lower flow is not mistaken for more reference-temperature heat.

For an identity valve curve and a room at 20 C, 10% at 50 C becomes approximately
24.6% at 35 C. With SmartPI's default nonlinear curve, a 10% physical valve opening
corresponds to 30% linear heat demand, so the resulting opening is different.
Reaching the minimum flow can prevent reaching the preferred valve opening.

The power law uses supply-to-room temperature as a proxy for radiator temperature.
It does not measure water flow, return temperature or radiator watts. The existing
PI feedback corrects residual room-temperature error.

To protect calibration, **A/B learning, dead-time identification, feed-forward
trim updates and stagnation-triggered recalibration pause during modulation or
unreliable flow context**. Existing learned values and the user's learning-enabled
setting are preserved. Fixed-reference learning resumes after settling; passive
cooling measurements can still be learned at the reference setting. Manual
calibration requests bring the shared circuit back to reference first.

With an actual flow sensor, compensation follows the measured temperature.
Without one, a 180-second first-order estimate follows commanded temperature so
valves do not jump open immediately after a reduction. This estimate cannot prove
the boiler reached its requested temperature. A sensor is preferable.

On a restart, the new setpoint is chosen from current heat demand, while valve
compensation still follows the actual water temperature. Without a sensor, the
last estimate is retained conservatively across the OFF interval instead of
assuming the water instantly reached the new setpoint. Learning remains paused
at reduced flow. A reference-temperature restart must establish a fresh settling
period once the measured or estimated water is near reference; idle time does not
count toward heating-model settling.

## Diagnostics and failure behavior

The central boiler binary sensor exposes `central_boiler_manager.dynamic_flow`:
current target, reason, entity IDs and the context shared with SmartPI. SmartPI's
diagnostic sensor adds `power.boiler_flow`: capacity ratio, reference demand,
learning pause and reason. Existing valve diagnostics keep their meaning.

- Missing/invalid flow actuator or a failed service write prevents a new heating
  activation. A running boiler is deactivated by its existing OFF service.
- A missing/stale configured water sensor requests a direct return to reference
  and blocks learning. It does not assume the last sensor reading is current.
- An expired coordinator context holds the last compensation and blocks learning.
- An incompatible active room controller requests reference flow.
- Invalid room/target temperatures and a room's active safety state also request
  reference flow. SmartPI's own bootstrap/calibration overrides are retained.
- OFF, sleeping and cooling rooms do not raise the heating flow request.

The 60-second watchdog depends on Home Assistant running. It cannot provide
protection during a Home Assistant outage, nor can it make another controller stop
issuing competing commands.

## Commissioning and rollback

Initially observe one room and the diagnostics: target flow, measured flow if
available, physical valve opening, room temperature and boiler cycling. Then add
the other rooms and test simultaneous demand. This software has automated tests;
it has not been commissioned on your boiler or valves.

Lower flow and wider valves do not remove the boiler's minimum burner output. If
one room absorbs less heat than the boiler can produce at minimum modulation, the
burner can still cycle. This feature does not replace hydraulic balancing or the
boiler's minimum-flow provision.

To stop using the feature, disable it and restore the old fixed flow setpoint and
ON/OFF configuration. To roll back completely, restore the two previous integration
directories and restart Home Assistant. Retain `.storage`; restore its backup only
if you also want to undo later thermostat/model changes. Changing the reference or
valve curve after calibration requires recalibration at the new fixed reference.

## Development checks

The flow math and coordinator tests are in `tests/test_boiler_flow.py`. Paired
tests in `tests/test_boiler_flow_pair.py` run when the SmartPI checkout is alongside
this checkout, named `vtherm_smartpi`. They exercise two rooms with identity and
nonlinear valves, preserving heat demand as flow falls and raising flow when a
second room needs heat. Existing central-boiler, config-flow and valve suites
cover the disabled/default behavior.
