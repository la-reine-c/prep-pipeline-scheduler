"""Identifying classes from past and current prep, and retiring old ones."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from prep import config as config_module, identify, pipeline
from prep.identify import (apply_set, existing_assignment, load_learned,
                           save_learned, summarise_class)
from prep.scheduler import FakeCalendar
from prep.scraper import RawAssignment, parse
from prep.store import Store

TZ = ZoneInfo("Europe/London")
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def prep(title: str, due: str, class_id: str = "cls",
         creator: str = "", archived=None) -> RawAssignment:
    return RawAssignment(f"{title}-id", title, "", class_id, creator, "",
                         None, due, archived=archived)


# --------------------------------------------------- past vs current

def test_finished_and_upcoming_prep_are_counted_separately():
    """Identification leans on past prep; scheduling only cares about future."""
    summary = summarise_class([
        prep("Old sheet", "2026-05-01T08:30:00Z"),
        prep("Older sheet", "2026-04-20T08:30:00Z"),
        prep("Due soon", "2026-09-28T08:30:00Z"),
    ], now=NOW)
    assert summary["total"] == 3
    assert summary["past"] == 2
    assert summary["current"] == 1


def test_a_class_with_only_finished_prep_is_still_summarised():
    """Every teacher has set something; few have anything due this week."""
    summary = summarise_class([prep("Vocab", "2026-05-01T08:00:00Z")], now=NOW)
    assert summary["total"] == 1 and summary["current"] == 0
    assert summary["days_since"] > 100


# --------------------------------------------------- archive detection

def test_the_tenants_own_archive_flag_is_believed():
    summary = summarise_class(
        [prep("Recent", "2026-09-24T08:30:00Z", archived=True)], now=NOW)
    assert summary["likely_archived"] is True
    assert summary["stated_archived"] is True
    assert "Teams" in summary["archive_basis"]


def test_a_long_dormant_class_is_flagged_without_a_tenant_flag():
    summary = summarise_class([
        prep("Latin vocab", "2026-04-29T08:00:00Z"),
        prep("Prose paper", "2026-05-01T08:00:00Z"),
    ], now=NOW)
    assert summary["likely_archived"] is True
    assert summary["stated_archived"] is False
    assert "no prep for" in summary["archive_basis"]


def test_a_class_quiet_over_the_holidays_is_not_flagged():
    """A summer break must not retire a class you are still taking."""
    summary = summarise_class([
        prep("End of term", "2026-07-10T08:30:00Z"),
        prep("New term", "2026-09-22T08:30:00Z"),
    ], now=NOW)
    assert summary["likely_archived"] is False


def test_tenant_saying_not_archived_beats_the_dormancy_guess():
    summary = summarise_class(
        [prep("Old but live", "2026-04-01T08:30:00Z", archived=False)], now=NOW)
    assert summary["likely_archived"] is False


# --------------------------------------------------- duplicate teachers

def test_the_same_teacher_on_two_classes_is_flagged():
    learned = {"class_map": {"class-a": "Garner"}, "title_codes": {},
               "people_map": {}, "archived": {}}
    assert existing_assignment(learned, "Garner") == ["class class-a"]

    message = apply_set([prep("Titration", "2026-09-28T08:30:00Z", "class-b")],
                        learned, "Titration", "Garner")
    assert "already mapped" in message
    # It warns, but still records - one teacher really can take you twice.
    assert learned["class_map"]["class-b"] == "Garner"


def test_the_duplicate_check_ignores_case():
    learned = {"class_map": {"class-a": "Garner"}, "title_codes": {},
               "people_map": {}, "archived": {}}
    assert existing_assignment(learned, "garner")
    assert existing_assignment(learned, "  GARNER  ")

    message = apply_set([prep("Titration", "2026-09-28T08:30:00Z", "class-b")],
                        learned, "Titration", "gARNER")
    assert "already mapped" in message


def test_a_clash_is_found_across_every_kind_of_mapping():
    learned = {"class_map": {}, "title_codes": {"AAA": "Archer"},
               "people_map": {"t-9": "Dalton"}, "archived": {}}
    assert existing_assignment(learned, "archer") == ["code AAA"]
    assert existing_assignment(learned, "DALTON") == ["person t-9"]


def test_a_genuinely_new_teacher_raises_no_warning():
    learned = {"class_map": {"class-a": "Garner"}, "title_codes": {},
               "people_map": {}, "archived": {}}
    message = apply_set([prep("Essay", "2026-09-28T08:30:00Z", "class-b")],
                        learned, "Essay", "Hughes")
    assert "already mapped" not in message


# --------------------------------------------------- archiving

def test_archiving_a_class_is_recorded(tmp_path: Path):
    learned = {"class_map": {}, "title_codes": {}, "people_map": {}, "archived": {}}
    items = [prep("Latin vocab", "2026-05-01T08:00:00Z", "latin-class")]
    message = apply_set(items, learned, "archived:latin-class", "yes")
    assert learned["archived"]["latin-class"] is True
    assert "archived" in message


def test_a_class_can_be_un_archived():
    learned = {"class_map": {}, "title_codes": {}, "people_map": {},
               "archived": {"latin-class": True}}
    apply_set([], learned, "archived:latin-class", "no")
    assert learned["archived"]["latin-class"] is False


def test_archived_classes_are_never_scheduled(tmp_path: Path):
    """The point of the whole feature."""
    (tmp_path / "payloads.json").write_text(json.dumps([{
        "url": "a", "body": {"value": [
            {"id": "1" * 36, "classId": "live-class", "displayName": "Current work",
             "assignedDateTime": "2026-09-21T09:00:00Z",
             "dueDateTime": "2026-09-28T08:30:00Z"},
            {"id": "2" * 36, "classId": "OLD-class", "displayName": "GCSE leftover",
             "assignedDateTime": "2026-09-21T09:00:00Z",
             "dueDateTime": "2026-09-29T08:30:00Z"},
        ]}}]))
    learned = tmp_path / "teachers.json"
    # Deliberately different case from the payload, to prove matching is not
    # case-sensitive.
    learned.write_text(json.dumps({"archived": {"old-class": True},
                                   "class_map": {}, "title_codes": {},
                                   "people_map": {}}))
    (tmp_path / "config.yaml").write_text(f"""
db_path: {tmp_path}/p.db
dump_dir: {tmp_path}/d
export_path: {tmp_path}/p.txt
learned_map_path: {learned}
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
""")
    cfg = config_module.load(tmp_path / "config.yaml")
    assert "old-class" in cfg.scheduling.archived_classes

    store = Store(cfg.db_path)
    from prep.scraper import load_payload_file
    changes = store.upsert(parse(load_payload_file(tmp_path / "payloads.json")),
                           pipeline.teacher_matcher(cfg))
    calendar = FakeCalendar()
    outcomes = pipeline.reschedule(cfg, store, calendar, changes,
                                   now=datetime(2026, 9, 21, 16, 0, tzinfo=TZ))

    problems = {o.change.label: o.problem for o in outcomes}
    assert problems["GCSE leftover"] == "archived class, skipped"
    assert not problems["Current work"]
    for session in calendar.events.values():
        assert "GCSE leftover" not in session.label
    store.close()


def test_an_un_archived_class_is_scheduled_again(tmp_path: Path):
    learned = tmp_path / "teachers.json"
    learned.write_text(json.dumps({"archived": {"old-class": False},
                                   "class_map": {}, "title_codes": {},
                                   "people_map": {}}))
    (tmp_path / "config.yaml").write_text(f"""
db_path: {tmp_path}/p.db
dump_dir: {tmp_path}/d
export_path: {tmp_path}/p.txt
learned_map_path: {learned}
teams: {{assignments_url: x, url_hints: [a], profile_dir: {tmp_path}/prof}}
calendar: {{caldav_url: x, username: u, password: p, prep_calendar: Prep}}
scheduling:
  windows: {{monday: {{start: "17:00", end: "21:30"}}}}
""")
    cfg = config_module.load(tmp_path / "config.yaml")
    assert "old-class" not in cfg.scheduling.archived_classes


def test_the_learned_map_round_trips(tmp_path: Path):
    path = tmp_path / "teachers.json"
    learned = load_learned(path)
    learned["class_map"]["c1"] = "Archer"
    learned["archived"]["c2"] = True
    save_learned(path, learned)

    again = load_learned(path)
    assert again["class_map"]["c1"] == "Archer"
    assert again["archived"]["c2"] is True
    assert set(again) >= {"class_map", "title_codes", "people_map", "archived"}


def test_days_silent_is_never_negative():
    """A class with prep still to come has been silent for zero days."""
    summary = summarise_class([prep("Due next week", "2026-10-02T08:30:00Z")],
                              now=NOW)
    assert summary["days_since"] == 0
    assert summary["likely_archived"] is False


def test_a_class_with_no_dates_at_all_is_not_wrongly_archived():
    summary = summarise_class([prep("Undated", "")], now=NOW)
    assert summary["days_since"] is None
    assert summary["likely_archived"] is False
