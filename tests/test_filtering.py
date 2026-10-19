"""The tracking filter: drift, outlier resistance, and honest booking."""
from __future__ import annotations

import math
import random
import statistics
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from prep import estimator, filtering
from prep.config import EstimatorConfig
from prep.filtering import FilterConfig, huber_weight, run, steady_state_gain

TZ = ZoneInfo("Europe/London")
CFG = FilterConfig()


def test_huber_weight_is_one_inside_the_bend_and_falls_outside():
    assert huber_weight(0.0, 1.5) == 1.0
    assert huber_weight(1.4, 1.5) == 1.0
    assert huber_weight(3.0, 1.5) == pytest.approx(0.5)
    assert huber_weight(-6.0, 1.5) == pytest.approx(0.25)


def test_one_wild_evening_barely_moves_the_estimate():
    """The whole point: a distracted evening must not become the new normal."""
    clean = run([42, 38, 45, 40, 44], CFG)
    spiked = run([42, 38, 300, 40, 44], CFG)

    assert spiked.rejected >= 1
    assert abs(spiked.minutes - clean.minutes) < 10, (
        f"outlier moved the estimate by {spiked.minutes - clean.minutes:.0f} min")
    # A plain mean would be wrecked by the same data.
    assert statistics.mean([42, 38, 300, 40, 44]) > 90


def test_a_genuine_sustained_increase_is_followed():
    """Damping an outlier must not mean ignoring a real trend."""
    state = run([30, 32, 31, 35, 38, 42, 45, 48, 52, 55], CFG)
    flat_mean = statistics.mean([30, 32, 31, 35, 38, 42, 45, 48, 52, 55])
    assert state.minutes > flat_mean, "the filter lags as badly as a flat mean"
    assert state.minutes > 43


def test_booked_time_exceeds_the_central_estimate():
    """Booking the middle means overrunning half the time."""
    state = run([40, 44, 38, 42, 41], CFG)
    assert state.session_minutes(CFG) > state.minutes


def test_booking_covers_about_eighty_percent_of_next_preps():
    """The claim the quantile makes has to actually hold."""
    random.seed(11)
    for true_sd in (0.2, 0.3, 0.45):
        covered = trials = 0
        for _ in range(1500):
            mu = math.log(random.uniform(25, 90))
            history = [math.exp(random.gauss(mu, true_sd)) for _ in range(6)]
            state = run(history, CFG)
            following = math.exp(random.gauss(mu, true_sd))
            covered += following <= state.session_minutes(CFG)
            trials += 1
        rate = covered / trials
        assert 0.70 <= rate <= 0.92, f"log-sd {true_sd}: covered {rate:.0%}"


def test_a_consistent_person_gets_a_tighter_booking_than_an_erratic_one():
    steady = run([40, 41, 40, 39, 41, 40], CFG)
    erratic = run([20, 65, 30, 70, 25, 60], CFG)
    assert (steady.session_minutes(CFG) - steady.minutes) < \
           (erratic.session_minutes(CFG) - erratic.minutes), \
        "buffer did not widen for the more variable history"


def test_estimates_stay_positive_however_odd_the_data():
    for history in ([1], [1, 1, 1], [1, 500, 1, 500], [720, 1, 720]):
        state = run(history, CFG)
        assert state.minutes > 0
        assert state.session_minutes(CFG) > 0
        assert math.isfinite(state.session_minutes(CFG))


def test_empty_or_invalid_history_returns_nothing():
    assert run([], CFG) is None
    assert run([0, -5, None], CFG) is None


def test_steady_state_gain_is_a_sane_ewma():
    """A constant-gain filter is an EWMA; the smoothing must be reasonable."""
    gain = steady_state_gain(CFG)
    assert 0.1 < gain < 0.6, f"gain {gain:.2f} is not a sensible smoothing"


# ------------------------------------------------- integration

def test_estimator_switches_to_tracking_once_there_is_history():
    cfg = EstimatorConfig()
    assigned = datetime(2026, 9, 18, 9, 0, tzinfo=TZ)
    due = datetime(2026, 9, 25, 8, 30, tzinfo=TZ)

    thin = estimator.estimate(cfg, "Dalton", assigned, due,
                              {"Dalton": (2, 40.0)}, 40.0,
                              history={"Dalton": [38, 42]})
    assert thin.source == "shrunk"

    tracked = estimator.estimate(cfg, "Dalton", assigned, due,
                                 {"Dalton": (4, 40.0)}, 40.0,
                                 history={"Dalton": [38, 42, 41, 39]})
    assert tracked.source == "tracked"
    assert tracked.minutes >= tracked.central
    assert tracked.spread > 0


def test_tracked_estimate_never_exceeds_the_cap():
    cfg = EstimatorConfig(cap_minutes=120)
    est = estimator.estimate(
        cfg, "Garner", None, datetime(2026, 9, 25, 8, 30, tzinfo=TZ),
        {"Garner": (6, 400.0)}, 400.0,
        history={"Garner": [380, 420, 410, 390, 405, 415]})
    assert est.minutes <= 120
    assert est.central <= 120


def test_confidence_reports_tracking_once_the_filter_is_running():
    assert estimator.confidence("B", {"B": (4, 40.0)}, 40.0,
                                history={"B": [38, 42, 41, 39]}) == "tracked"
    assert estimator.confidence("B", {"B": (2, 40.0)}, 40.0,
                                history={"B": [38, 42]}) == "shrunk"
    assert estimator.confidence("B", {"B": (4, 40.0)}, 40.0, overridden=True,
                                history={"B": [38, 42, 41, 39]}) == "corrected"


def test_history_order_is_preserved_from_the_database(tmp_path):
    """The filter is time-ordered; shuffled history would track nonsense."""
    from prep.scraper import RawAssignment
    from prep.store import Store
    import time as time_module

    store = Store(tmp_path / "p.db")
    store.upsert([RawAssignment("a" * 36, "S1", "", "c", "", "",
                                None, "2026-09-25T08:30:00Z")],
                 lambda item: "Dalton")
    for minutes in (30, 35, 40, 45, 50):
        store.record_timing("a" * 36, minutes)
        time_module.sleep(0.002)

    assert store.teacher_timings()["Dalton"] == [30, 35, 40, 45, 50]
    store.close()


def test_end_to_end_the_schedule_reflects_tracked_times(tmp_path):
    """Logging real times must change what actually gets booked."""
    import json
    from prep import config as config_module, pipeline
    from prep.scheduler import FakeCalendar
    from prep.scraper import load_payload_file, parse
    from prep.store import Change, Store

    (tmp_path / "payloads.json").write_text(json.dumps([{
        "url": "a", "body": {"value": [
            {"id": "1" * 36, "classId": "c", "displayName": "Sheet",
             "assignedDateTime": "2026-09-18T09:00:00Z",
             "dueDateTime": "2026-09-25T08:30:00Z"}]}}]))
    (tmp_path / "config.yaml").write_text(f"""
db_path: {tmp_path}/p.db
dump_dir: {tmp_path}/d
export_path: {tmp_path}/p.txt
timezone: Europe/London
teams: {{assignments_url: x, url_hints: [a], profile_dir: {tmp_path}/prof}}
calendar: {{caldav_url: x, username: u, password: p, prep_calendar: Prep}}
scheduling:
  windows:
    monday:    {{start: "17:00", end: "21:30"}}
    tuesday:   {{start: "17:00", end: "21:30"}}
    wednesday: {{start: "17:00", end: "21:30"}}
    thursday:  {{start: "17:00", end: "21:30"}}
    friday:    {{start: "17:00", end: "21:00"}}
    saturday:  {{start: "09:30", end: "18:00"}}
    sunday:    {{start: "09:30", end: "20:00"}}
  class_map: {{c: Dalton}}
""")
    cfg = config_module.load(tmp_path / "config.yaml")
    store = Store(cfg.db_path)
    changes = store.upsert(parse(load_payload_file(tmp_path / "payloads.json")),
                           pipeline.teacher_matcher(cfg))
    now = datetime(2026, 9, 18, 16, 0, tzinfo=TZ)

    first = pipeline.reschedule(cfg, store, FakeCalendar(), changes, now=now)
    assert first[0].minutes == 120          # the cold-start pattern guess

    # Now tell it Dalton preps really take about 40 minutes.
    for minutes in (38, 42, 40, 41):
        store.record_timing("1" * 36, minutes)

    row = store.assignment("1" * 36)
    again = pipeline.reschedule(
        cfg, store, FakeCalendar(),
        [Change("due_changed", "1" * 36, row["label"], "Dalton",
                row["due_at"], row["due_at"])], now=now)

    assert again[0].estimate_source == "tracked"
    assert 40 <= again[0].minutes <= 70, (
        f"booked {again[0].minutes} min after learning it takes ~40")
    assert again[0].minutes > 40, "no buffer at all was added above the centre"
    store.close()


def test_calibration_chart_is_not_skewed_by_one_bad_evening(tmp_path):
    """The chart must not contradict the estimator that damped the outlier."""
    from prep.scraper import RawAssignment
    from prep.store import Store

    store = Store(tmp_path / "p.db")
    store.upsert([RawAssignment(f"{n}" * 36, f"Prep {n}", "", "c", "", "",
                                None, f"2026-09-2{n}T08:30:00Z")
                  for n in range(1, 6)], lambda item: "Garner")
    for n, minutes in zip(range(1, 6), (52, 61, 300, 49, 55)):
        store.record_estimate(f"{n}" * 36, 55, "tracked")
        store.record_timing(f"{n}" * 36, minutes)

    row = store.calibration()[0]
    assert row["mean_actual"] == 55, (
        f"chart says {row['mean_actual']} - the 300-minute evening skewed it")
    assert abs(row["mean_predicted"] - row["mean_actual"]) < 10
    store.close()
