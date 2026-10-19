"""prep.setup — naming the people who set your prep.

The thing this must get right is grouping by *who set it*, not by prep and
not by class: every class is shared, so a class answer is wrong for one of
the two teachers, and one answer per prep would be fifty answers.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from prep import identify, setup
from prep.scraper import RawAssignment

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def prep(title, creator="", class_id="cls-1", days=-3, name=""):
    due = NOW + timedelta(days=days)
    return RawAssignment(
        assignment_id=f"{title}-{days}", title=title, class_ref="",
        class_id=class_id, creator_id=creator, creator_name=name,
        assigned_at=(due - timedelta(days=4)).isoformat(),
        due_at=due.isoformat())


EMPTY = {"class_map": {}, "title_codes": {}, "people_map": {}, "archived": {}}


def test_two_teachers_sharing_a_class_are_separate_entries():
    """The whole reason this groups by creator."""
    people = setup.gather([
        prep("AAA Prep 5", creator="id-a", class_id="maths"),
        prep("EEE Prep 2", creator="id-e", class_id="maths"),
    ], dict(EMPTY), now=NOW)
    assert len(people) == 2
    assert {p.lookup_key for p in people} == {"id-a", "id-e"}


def test_one_teacher_many_preps_is_one_question():
    people = setup.gather(
        [prep(f"AAA Prep {n}", creator="id-a", days=-n) for n in range(1, 9)],
        dict(EMPTY), now=NOW)
    assert len(people) == 1
    assert people[0].total == 8
    assert len(people[0].titles) == setup.SAMPLE_TITLES, "showed every title"


def test_finished_and_current_prep_both_count():
    """Every teacher has set you something; few have anything due this week."""
    people = setup.gather([
        prep("done", creator="id-a", days=-10),
        prep("due soon", creator="id-a", days=+4),
    ], dict(EMPTY), now=NOW)
    assert people[0].past == 1
    assert people[0].current == 1


def test_prep_older_than_the_window_is_left_out():
    people = setup.gather([prep("ancient", creator="id-a", days=-200)],
                          dict(EMPTY), weeks=4, now=NOW)
    assert people == []


def test_the_window_can_be_widened():
    people = setup.gather([prep("last term", creator="id-a", days=-60)],
                          dict(EMPTY), weeks=12, now=NOW)
    assert len(people) == 1


def test_no_creator_falls_back_to_the_class_and_says_so():
    people = setup.gather([prep("Unnamed", creator="", class_id="mystery")],
                          dict(EMPTY), now=NOW)
    assert people[0].kind == "class"
    assert people[0].lookup_key == "mystery"


def test_an_archived_class_is_not_asked_about_again():
    learned = dict(EMPTY, archived={"old-latin": True})
    people = setup.gather([prep("Latin", creator="id-l", class_id="old-latin")],
                          learned, now=NOW)
    assert people == []


def test_someone_already_named_is_marked_as_known():
    learned = dict(EMPTY, people_map={"id-a": "Archer"})
    people = setup.gather([prep("AAA Prep 5", creator="id-a")], learned, now=NOW)
    assert people[0].teacher == "Archer"


def test_matching_a_known_id_ignores_case():
    learned = dict(EMPTY, people_map={"ID-A": "Archer"})
    people = setup.gather([prep("AAA Prep 5", creator="id-a")], learned, now=NOW)
    assert people[0].teacher == "Archer"


def test_unknown_people_are_listed_first():
    learned = dict(EMPTY, people_map={"id-a": "Archer"})
    people = setup.gather([
        prep("known", creator="id-a"),
        prep("unknown", creator="id-z"),
    ], learned, now=NOW)
    assert people[0].lookup_key == "id-z", "a named teacher came before an unnamed one"


def test_setting_a_teacher_writes_to_the_people_map():
    learned = dict(EMPTY, people_map={}, class_map={})
    setup.set_teacher(learned, "id-a", "Archer")
    assert learned["people_map"]["id-a"] == "Archer"
    assert learned["class_map"] == {}


def test_setting_a_class_writes_to_the_class_map():
    learned = dict(EMPTY, people_map={}, class_map={})
    setup.set_teacher(learned, "cls-1", "Archer", kind="class")
    assert learned["class_map"]["cls-1"] == "Archer"


def test_the_same_teacher_twice_is_flagged_not_refused():
    """A teacher can genuinely have two ids. Flag it; let him judge."""
    learned = dict(EMPTY, people_map={"id-a": "Archer"}, class_map={})
    line = setup.set_teacher(learned, "id-b", "archer")
    assert "already mapped" in line
    assert learned["people_map"]["id-b"] == "archer", "the mapping was refused"


def test_the_printable_block_is_runnable(capsys):
    people = setup.gather([prep("AAA Prep 5", creator="id-a")],
                          dict(EMPTY), now=NOW)
    setup.print_unknown(people, "config.yaml")
    out = capsys.readouterr().out
    assert "--set-teacher id-a" in out
    assert "TEACHER NAME" in out
    assert "AAA Prep 5" in out, "no way to tell whose id this is"


def test_a_class_fallback_offers_the_class_flag(capsys):
    people = setup.gather([prep("Unnamed", creator="", class_id="mystery")],
                          dict(EMPTY), now=NOW)
    setup.print_unknown(people, "config.yaml")
    assert "--set-class mystery" in capsys.readouterr().out


# -------------------------------------------------- the CLI, end to end

@pytest.fixture
def site(tmp_path: Path):
    (tmp_path / "config.yaml").write_text(f"""
db_path: {tmp_path}/p.db
dump_dir: {tmp_path}/dumps
export_path: {tmp_path}/p.txt
learned_map_path: {tmp_path}/teachers.json
teams: {{assignments_url: x, url_hints: [a], profile_dir: {tmp_path}/prof}}
calendar: {{caldav_url: x, username: u, password: p, prep_calendar: Prep}}
scheduling: {{windows: {{monday: {{start: "17:00", end: "21:30"}}}}}}
""")
    return tmp_path


def test_set_teacher_needs_no_capture(site, capsys):
    """Editing a mapping later must work offline - teachers change mid-year."""
    code = setup.main(["--config", str(site / "config.yaml"),
                       "--set-teacher", "id-a", "Archer"])
    assert code == 0
    saved = json.loads((site / "teachers.json").read_text())
    assert saved["people_map"]["id-a"] == "Archer"


def test_a_mapping_can_be_replaced(site):
    args = ["--config", str(site / "config.yaml"), "--set-teacher", "id-a"]
    setup.main(args + ["Archer"])
    setup.main(args + ["Barratt"])
    saved = json.loads((site / "teachers.json").read_text())
    assert saved["people_map"]["id-a"] == "Barratt", "could not correct a mistake"


def test_forget_removes_a_mapping(site, capsys):
    cfg = ["--config", str(site / "config.yaml")]
    setup.main(cfg + ["--set-teacher", "id-a", "Archer"])
    setup.main(cfg + ["--forget", "id-a"])
    saved = json.loads((site / "teachers.json").read_text())
    assert "id-a" not in saved["people_map"]


def test_archive_marks_a_class(site):
    cfg = ["--config", str(site / "config.yaml")]
    setup.main(cfg + ["--archive", "old-latin"])
    saved = json.loads((site / "teachers.json").read_text())
    assert saved["archived"]["old-latin"] is True


def test_export_prints_something_you_can_paste(site, capsys):
    cfg = ["--config", str(site / "config.yaml")]
    setup.main(cfg + ["--set-teacher", "id-a", "Archer"])
    capsys.readouterr()
    setup.main(cfg + ["--export"])
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{"):])
    assert payload["people_map"]["id-a"] == "Archer"


def test_list_shows_what_is_known(site, capsys):
    cfg = ["--config", str(site / "config.yaml")]
    setup.main(cfg + ["--set-teacher", "id-a", "Archer"])
    capsys.readouterr()
    setup.main(cfg + ["--list"])
    out = capsys.readouterr().out
    assert "Archer" in out and "id-a" in out


def test_the_map_written_is_the_one_the_pipeline_reads(site):
    """A setup command that writes somewhere the scheduler never looks is
    worse than no setup command."""
    from prep import config as config_module, pipeline
    setup.main(["--config", str(site / "config.yaml"),
                "--set-teacher", "id-a", "Archer"])
    cfg = config_module.load(site / "config.yaml")
    resolve = pipeline.teacher_matcher(cfg)
    assert resolve(prep("AAA Prep 5", creator="id-a")) == "Archer"
