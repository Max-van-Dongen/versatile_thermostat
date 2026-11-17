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
- A room at least 0.5 C below its target requests the reference temperature.
- Active bootstrap/calibration and incompatible active controllers also request
  the reference temperature.
- Upward changes are limited to 3 C/min and downward changes to 1 C/min,
  subject to the number entity's step size. The reference is used on activation
  for the configured settling period.
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
| Settling time | Default 300 seconds. Model learning is only allowed at a settled reference temperature. |

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
bounds and applies the rate limit. SmartPI saturates the valve at its allowed
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

## Diagnostics and failure behavior

The central boiler binary sensor exposes `central_boiler_manager.dynamic_flow`:
current target, reason, entity IDs and the context shared with SmartPI. SmartPI's
diagnostic sensor adds `power.boiler_flow`: capacity ratio, reference demand,
learning pause and reason. Existing valve diagnostics keep their meaning.

- Missing/invalid flow actuator or a failed service write prevents a new heating
  activation. A running boiler is deactivated by its existing OFF service.
- A missing/stale configured water sensor requests a gradual return to reference
  and blocks learning. It does not assume the last sensor reading is current.
- An expired coordinator context holds the last compensation and blocks learning.
- An incompatible active room controller requests reference flow.
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
