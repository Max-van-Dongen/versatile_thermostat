"""Behavioral tests for binary coordinated heating without Home Assistant I/O."""

import json

import pytest

from custom_components.versatile_thermostat.boiler_pulse import PulseScheduler, PulseZone


def zone(current, key="bed", *, target=20, demand=0.1, opened=False, recovery=False,
         stamp=None, slope=None):
    return PulseZone(key, current, target, demand, opened, recovery, stamp, slope)


def tick(scheduler, now, *zones, water=50, burner=None, on=False):
    return scheduler.update(now, zones, water=water, burner=burner, boiler_on=on)


@pytest.mark.parametrize("demand", [0, 0.01, 0.5, 1])
@pytest.mark.parametrize("target,current", [(20, 19.8), (30, 18.1)])
def test_maximum_for_every_confirmed_call_and_zero_while_closed(target, current, demand):
    s = PulseScheduler(50, 75, overshoot=0.7, undershoot=0.2)
    closed = zone(current, target=target, demand=demand)
    first = tick(s, 0, closed, zone(20.1, "warm", demand=1, opened=True))
    assert first.phase == "opening" and first.valves == {"bed"}
    assert not first.call_for_heat and first.flow_temperature == 0
    opened = zone(current, target=target, demand=demand, opened=True)
    heat = tick(s, 15, opened)
    assert heat.call_for_heat and heat.flow_temperature == 75
    assert tick(s, 30, opened, on=True).flow_temperature == 75
    stop = tick(s, 40, closed, on=True)
    assert not stop.call_for_heat and stop.flow_temperature == 0


def test_no_call_in_band_from_pi_demand_or_recovery_hint_alone():
    s = PulseScheduler(50, 75, overshoot=0.5, undershoot=0.5)
    for t in range(0, 1500, 60):
        decision = tick(s, t, zone(19.7, demand=1, opened=True, recovery=True))
        assert not decision.valves and decision.flow_temperature == 0


def test_forecast_at_zero_pi_admits_a_solo_room_before_the_lower_edge():
    s = PulseScheduler(50, 75, overshoot=0.5, undershoot=0.5)
    assert tick(s, 0, zone(19.8, demand=0)).phase == "coast"
    assert tick(s, 120, zone(19.63, demand=0)).phase == "opening"


@pytest.mark.parametrize("last,current,joins", [
    (19.8, 19.7, True), (20.2, 20.1, True), (19.7, 19.7, False),
    (19.6, 19.7, False), (20.48, 20.41, False),
])
def test_cooling_companion_can_share_a_new_run_anywhere_inside_band(last, current, joins):
    s = PulseScheduler(50, 75, overshoot=0.5, undershoot=0.5)
    assert not tick(s, 0, zone(20), zone(last, "other", demand=0)).valves
    plan = tick(s, 300, zone(18, opened=True, recovery=True),
                zone(current, "other", demand=0))
    assert plan.valves == ({"bed", "other"} if joins else {"bed"})
    assert plan.flow_temperature == 75 and plan.recovery_zones == {"bed"}


def test_cooling_companion_waits_for_a_legitimate_primary_call():
    s = PulseScheduler(50, 75, overshoot=0.5, undershoot=0.5)
    tick(s, 0, zone(19.8, "kitchen", demand=0))
    assert not tick(s, 300, zone(19.7, "kitchen", demand=0)).valves


@pytest.mark.parametrize("shared,last,joins", [(True, 19.8, True), (True, 19.7, False), (False, 19.8, False)])
def test_native_call_can_admit_a_cooling_companion(shared, last, joins):
    s = PulseScheduler(50, 75, overshoot=0.5, undershoot=0.5)
    tick(s, 0, zone(last, demand=0))
    plan = s.update(300, [zone(19.7, demand=0)], shared_heat_call=shared)
    assert plan.valves == ({"bed"} if joins else set())
    assert not plan.call_for_heat and plan.flow_temperature == 0
    confirmed = s.update(315, [zone(19.7, demand=0, opened=True)], shared_heat_call=shared)
    assert confirmed.call_for_heat is joins
    assert confirmed.flow_temperature == (75 if joins else 0)


def test_finished_companion_keeps_its_cutoff_for_the_same_native_call():
    s = PulseScheduler(50, 75, overshoot=0.5, undershoot=0.5)

    def plan(t, temperature, opened=False, shared=True):
        return s.update(t, [zone(temperature, demand=0, opened=opened)], shared_heat_call=shared)

    plan(0, 19.8)
    assert plan(300, 19.7, opened=True).call_for_heat
    assert not plan(320, 20.45, opened=True).valves
    plan(400, 20.32)
    assert not plan(600, 20.2).valves
    assert not plan(620, 20.2, shared=False).valves
    assert plan(640, 20.19).valves == {"bed"}


def test_midrun_cooling_join_independent_cut_and_suppressed_opportunistic_rejoin():
    s = PulseScheduler(50, 75, overshoot=0.5, undershoot=0.5)
    primary = zone(18, opened=True, recovery=True)
    assert tick(s, 0, primary, zone(19.8, "kitchen", demand=0)).valves == {"bed"}
    assert tick(s, 300, primary, zone(19.7, "kitchen", demand=0)).valves == {"bed", "kitchen"}
    assert tick(s, 320, primary, zone(19.72, "kitchen", opened=True), on=True).valves == {"bed", "kitchen"}
    cut = tick(s, 350, primary, zone(20.41, "kitchen", opened=True), on=True)
    assert cut.valves == {"bed"} and cut.flow_temperature == 75
    # Same run, well above the floor and now cooling: do not repeatedly reopen.
    tick(s, 420, primary, zone(20.3, "kitchen"))
    assert tick(s, 620, primary, zone(20.15, "kitchen")).valves == {"bed"}
    # A subsequent true comfort floor still takes precedence in a long primary run.
    assert tick(s, 650, primary, zone(19.5, "kitchen")).valves == {"bed", "kitchen"}


def test_startup_uses_fresh_external_slope_until_local_measurements_take_over():
    s = PulseScheduler(50, 75, overshoot=0.5, undershoot=0.5)
    primary = zone(18, opened=True)
    assert tick(s, 0, primary, zone(19.7, "other", slope=-0.4)).valves == {"bed", "other"}
    s.reset()
    room = zone(19.7, "other", stamp=1000)
    tick(s, 0, primary, room)
    # A fresh flat local pair supersedes a contradictory external estimate.
    assert tick(s, 90, primary, zone(19.7, "other", stamp=1090, slope=-0.4)).valves == {"bed"}


def test_repeated_sensor_stamp_does_not_keep_cooling_trend_alive():
    s = PulseScheduler(50, 75, overshoot=0.5, undershoot=0.5)
    tick(s, 0, zone(20), zone(19.8, "other", stamp=1000))
    tick(s, 200, zone(20), zone(19.7, "other", stamp=1200))
    tick(s, 480, zone(20), zone(19.7, "other", stamp=1200))
    assert tick(s, 550, zone(18, opened=True), zone(19.7, "other", stamp=1200)).valves == {"bed"}


def test_target_changes_and_valve_feedback_are_reconciled_in_same_decision():
    s = PulseScheduler(50, 75, overshoot=0.5, undershoot=0.5)
    primary = zone(18, opened=True, recovery=True)
    tick(s, 0, primary, zone(20.1, "other"))
    updated = tick(s, 15, primary, zone(20.1, "other", target=30, recovery=True))
    assert updated.valves == {"bed", "other"} and updated.flow_temperature == 75
    swapped = tick(s, 30, zone(18, target=16, opened=True),
                   zone(20.1, "other", target=30, recovery=True))
    assert swapped.valves == {"other"} and swapped.flow_temperature == 0
    restored = tick(s, 60, zone(18, target=16, opened=True),
                    zone(20.1, "other", target=30, opened=True))
    assert restored.phase == "recovery" and restored.recovery_zones == {"other"}
    assert restored.flow_temperature == 75


def test_slow_response_does_not_throttle_long_confirmed_run_or_change_native_demand():
    s = PulseScheduler(50, 75, overshoot=0.7, undershoot=0.2)
    cold = zone(20, target=30, demand=0, opened=True, recovery=True)
    for now in range(0, 7200, 60):
        plan = tick(s, now, cold, on=now > 0)
        assert plan.valves == {"bed"} and plan.phase == "recovery" and plan.flow_temperature == 75
        assert cold.demand == 0


def test_pi_can_anticipate_peak_with_room_warming_but_never_defeat_lower_edge():
    def run(demand):
        s = PulseScheduler(50, 75, overshoot=0.5, undershoot=0.5)
        tick(s, 0, zone(19.4, demand=demand, opened=True))
        return tick(s, 300, zone(20.27, demand=demand, opened=True), on=True)
    assert not run(0).call_for_heat
    assert run(1).call_for_heat


def test_actuator_timeout_and_unavailable_room_cannot_authorize_heat():
    s = PulseScheduler(50, 75)
    assert tick(s, 0, zone(19)).phase == "opening"
    assert not tick(s, 181, zone(19)).call_for_heat and s.opening_timeouts == {"bed"}
    assert not tick(s, 240, zone(19, opened=True)).valves and not s.opening_timeouts
    assert tick(s, 482, zone(19)).phase == "opening"
    assert tick(s, 500, zone(19, opened=True)).call_for_heat
    assert tick(s, 510, zone(float("nan"), opened=True)).flow_temperature == 0
    assert not tick(s, 520).call_for_heat


@pytest.mark.parametrize("restart,reason", [(False, "burner_reached_flow"), (True, "burner_restarted")])
def test_confirmed_ch_cycle_at_maximum_requests_five_minute_rest(restart, reason):
    s = PulseScheduler(50, 75)
    cold = zone(18, target=30, opened=True, recovery=True)
    tick(s, 0, cold)
    tick(s, 10, cold, on=True, burner=True)
    tick(s, 60, cold, on=True, burner=False, water=74)
    stop = tick(s, 70, cold, on=True, burner=restart, water=74)
    assert stop.reason == reason and stop.flow_temperature == 0
    assert tick(s, 300, cold).reason == "burner_rest"
    assert tick(s, 370, cold).flow_temperature == 75


@pytest.mark.parametrize("burner,on", [(None, True), (False, True), (True, False)])
def test_supply_temperature_alone_cannot_infer_internal_burner_stop(burner, on):
    s = PulseScheduler(50, 75)
    cold = zone(18, opened=True)
    for t, water in [(0, 50), (20, 76), (70, 74), (120, 55)]:
        assert tick(s, t, cold, on=on, burner=burner, water=water).flow_temperature == 75


def test_measured_heat_balance_is_for_the_preceding_interval_only():
    s = PulseScheduler(35, 55)
    tick(s, 0, zone(20, target=20.5))
    opened = zone(20, target=20.5, opened=True)
    tick(s, 60, opened, water=35, on=True)
    assert s.heat_balance_minutes["bed"] == -0.1
    tick(s, 120, opened, water=35, on=True)
    assert s.heat_balance_minutes["bed"] == round((15 / 35)**1.3 - 0.2, 2)


def test_post_cut_rise_persists_and_legacy_flow_trials_are_ignored():
    s = PulseScheduler(50, 75, overshoot=0.5, undershoot=0.5)
    tick(s, 0, zone(19.4, opened=True))
    tick(s, 30, zone(19.6, opened=True), on=True)
    assert not tick(s, 60, zone(20.4, opened=True), on=True).call_for_heat
    tick(s, 240, zone(20.65))
    tick(s, 260, zone(20.55))
    saved = json.loads(json.dumps(s.dump_learning()))
    assert 0.1 < saved["residual"]["bed"] < 0.25
    assert "profiles" not in saved
    saved.update(version=1, profiles=[[[["bed", 20]], 7, 1, [[50, 4000, True]]]])
    restored = PulseScheduler(50, 75, overshoot=0.5, undershoot=0.5)
    restored.restore_learning(saved)
    assert restored.dump_learning()["residual"] == s.dump_learning()["residual"]
    assert tick(restored, 0, zone(19.4, opened=True)).flow_temperature == 75
    assert not tick(restored, 30, zone(20.36, opened=True), on=True).call_for_heat


@pytest.mark.parametrize("damaged", [None, [], {"version": 3}, {"version": 1, "residual": []}])
def test_loading_damaged_history_cannot_lower_requested_temperature(damaged):
    s = PulseScheduler(50, 75)
    s.restore_learning(damaged)
    assert tick(s, 0, zone(19.5, opened=True)).flow_temperature == 75
