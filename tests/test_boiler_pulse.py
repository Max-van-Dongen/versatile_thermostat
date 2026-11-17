"""Behavioral tests for the coordinated pulse scheduler (no HA I/O)."""

import json

import pytest

from custom_components.versatile_thermostat.boiler_pulse import PulseScheduler, PulseZone


BED = "bed"
KITCHEN = "kitchen"


def zone(current, *, key=BED, target=20.5, demand=0.1, opened=False):
    return PulseZone(key, current, target, demand, opened)


def tick(scheduler, now, *zones, water=35, burner=False, on=False):
    return scheduler.update(now, zones, water=water, burner=burner, boiler_on=on)


def test_preopens_only_interested_rooms_and_shuts_at_predicted_peak():
    s = PulseScheduler(35, 55)
    warm = zone(20.7, key=KITCHEN)
    first = tick(s, 0, zone(20.36), warm)
    assert first.valves == {BED}
    assert first.phase == "opening" and not first.call_for_heat
    open_only = tick(s, 30, zone(20.36, opened=True), warm)
    assert open_only.call_for_heat and open_only.flow_temperature == 35
    tick(s, 120, zone(20.51, opened=True), warm, burner=True, on=True)
    stop = tick(s, 180, zone(20.61, opened=True), warm, burner=True, on=True)
    assert stop.phase == "coast" and not stop.valves and not stop.call_for_heat
    assert tick(s, 220, zone(20.7), warm).phase == "coast"


def test_actuator_timeout_and_closed_valves_cannot_call_for_heat():
    s = PulseScheduler(35, 55)
    assert tick(s, 0, zone(20.3)).phase == "opening"
    assert tick(s, 181, zone(20.3)).phase == "coast"
    assert tick(s, 240, zone(20.3)).phase == "coast"
    assert tick(s, 482, zone(20.3)).phase == "opening"
    assert tick(s, 500, zone(20.3, opened=True)).call_for_heat
    assert not tick(s, 505, zone(20.3), on=True).call_for_heat
    assert not tick(s, 506, on=True).valves


def test_observed_cooling_starts_before_crossing_comfort_floor():
    s = PulseScheduler(35, 55)
    assert tick(s, 0, zone(20.55)).phase == "coast"
    assert tick(s, 120, zone(20.44)).phase == "opening"


def test_new_room_cannot_be_starved_by_a_continuing_pulse():
    s = PulseScheduler(35, 55)
    tick(s, 0, zone(20.3, opened=True), zone(20.7, key=KITCHEN))
    tick(s, 240, zone(20.35, opened=True), zone(20.6, key=KITCHEN), on=True, burner=True)
    join = tick(s, 260, zone(20.35, opened=True), zone(20.35, key=KITCHEN), on=True, burner=True)
    assert join.call_for_heat and join.valves == {BED, KITCHEN}
    finish = tick(s, 270, zone(20.6, opened=True), zone(20.35, key=KITCHEN), on=True, burner=True)
    assert finish.valves == {KITCHEN} and not finish.call_for_heat
    assert tick(s, 280, zone(20.6), zone(20.35, key=KITCHEN, opened=True)).call_for_heat


def test_target_change_interrupts_and_replans_for_new_temperature():
    s = PulseScheduler(35, 55)
    tick(s, 0, zone(20.4, opened=True))
    changed = tick(s, 5, zone(20.4, target=16, opened=True), on=True, burner=True)
    assert changed.phase == "coast" and not changed.valves
    assert tick(s, 10, zone(20.4, target=16)).phase == "coast"
    assert tick(s, 15, zone(20.4, target=21)).valves == {BED}


def test_delivered_balance_uses_actual_water_and_preceding_confirmed_interval():
    s = PulseScheduler(35, 55)
    start = zone(20, target=20.5, demand=0.1)
    opened = zone(20, target=20.5, demand=0.1, opened=True)
    tick(s, 0, start)
    tick(s, 60, opened, on=True, burner=True)
    assert s.heat_balance_minutes[BED] == -0.1
    tick(s, 120, opened, on=True, burner=True)
    assert s.heat_balance_minutes[BED] == round((15 / 35)**1.3 - 0.2, 2)


def run_early(s, t, *, burner_present=True, second_ignition=False):
    zone_open = zone(20.35, opened=True)
    start = tick(s, t, zone_open, burner=False if burner_present else None).flow_temperature
    tick(s, t+5, zone_open, water=start, burner=True if burner_present else None, on=True)
    tick(s, t+60, zone_open, water=start, burner=False if burner_present else None, on=True)
    stop = tick(s, t+70, zone_open, water=start, burner=second_ignition if burner_present else None, on=True)
    return start, stop


def test_repeated_confirmed_internal_stops_increase_only_next_comparable_pulse():
    s = PulseScheduler(35, 55)
    start, stop = run_early(s, 0)
    assert start == 35 and stop.reason == "burner_reached_flow"
    assert tick(s, 140, zone(20.35)).phase == "coast"
    start, stop = run_early(s, 375)
    assert start == 35 and not stop.call_for_heat
    tick(s, 550, zone(20.35))
    start, _ = run_early(s, 750)
    assert start == 37
    s.reset()
    # A different participant set or starting water temperature has its own profile.
    assert tick(s, 800, zone(20.35), zone(20.4, key=KITCHEN)).flow_temperature == 35
    s.reset()
    assert tick(s, 850, zone(20.35), water=51).flow_temperature == 35
    s.reset()
    assert tick(s, 900, zone(20.35), burner=None).flow_temperature == 35


def test_unknown_flame_or_water_cannot_teach_a_higher_flow():
    s = PulseScheduler(35, 55)
    flow, plan = run_early(s, 0, burner_present=False)
    assert flow == 35 and plan.call_for_heat
    for t in [120, 180, 240]:
        assert tick(s, t, zone(20.37, opened=True), water=49, burner=None, on=True).call_for_heat
    tick(s, 300, zone(20.7), water=49, burner=None, on=True)
    assert tick(s, 490, zone(20.35), burner=None).flow_temperature == 35
    assert s.dump_learning()["profiles"] == []


def test_reignition_cannot_be_scored_as_single_ignition_after_room_coasts_up():
    s = PulseScheduler(35, 55)
    _, stop = run_early(s, 0, second_ignition=True)
    assert stop.reason == "burner_restarted"
    tick(s, 220, zone(20.68))
    tick(s, 375, zone(20.35))
    samples = s.dump_learning()["profiles"][0][-1]
    assert len(samples) == 1 and samples[0][-1] is False


def test_learns_residual_rise_and_restores_only_learning():
    s = PulseScheduler(35, 55)
    tick(s, 0, zone(20.35, opened=True))
    tick(s, 30, zone(20.48, opened=True), on=True, burner=True)
    tick(s, 60, zone(20.60, opened=True), on=True, burner=True)
    tick(s, 240, zone(20.8))
    tick(s, 260, zone(20.7))
    saved = json.loads(json.dumps(s.dump_learning()))
    assert 0.1 < saved["residual"][BED] < 0.2
    restored = PulseScheduler(35, 55)
    restored.restore_learning(saved)
    assert tick(restored, 0, zone(20.68)).phase == "coast"
    tick(restored, 60, zone(20.35, opened=True))
    assert tick(restored, 120, zone(20.575, opened=True), on=True, burner=True).phase == "coast"


def test_profiles_compare_similar_coasts_and_probe_lower_once_sustainable():
    s = PulseScheduler(35, 55)
    def with_samples(samples):
        data = s.dump_learning()
        data["profiles"] = [[[[BED, 20.5]], 7, None, samples]]
        s.restore_learning(json.loads(json.dumps(data)))
        s.reset()
        return tick(s, 0, zone(20.35)).flow_temperature
    assert with_samples([(35, 900, True)] * 3) == 36
    assert with_samples([(35, 900, True)] * 3 + [(36, 950, True)] * 2) == 35
    assert with_samples([(35, 900, True)] * 3 + [(36, 1500, True)] * 2) == 36
    assert with_samples([(35, 900, True)] * 3 + [(36, 1500, True)] * 3) == 37
    assert with_samples([(35, 200, False)] * 2 + [(37, 1400, True)] * 3) == 36


@pytest.mark.parametrize("damaged", [None, [], {"version": 1}, {"version": 2},
                                     {"residual": ["invalid"], "profiles": [None]}])
def test_restore_tolerates_old_or_damaged_state(damaged):
    s = PulseScheduler(35, 55)
    s.restore_learning(damaged)
    assert tick(s, 0, zone(20.3)).flow_temperature == 35


def test_stalled_heating_below_floor_releases_to_ordinary_control_without_flame_sensor():
    s = PulseScheduler(35, 55)
    z = zone(20.3, opened=True)
    assert tick(s, 0, z, burner=None).call_for_heat
    for t in range(60, 1860, 60):
        assert tick(s, t, z, on=True, burner=None).call_for_heat
    decision = tick(s, 1860, z, on=True, burner=None)
    assert not decision.call_for_heat
    assert decision.reason == "insufficient_room_response"
    assert s.comfort_timeouts == {BED}
