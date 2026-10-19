"""The separate Completed-tab test (prep/completed.py).

It must find the tab wherever Teams puts it, keep only prep that is actually
finished, and never be picked up by the normal pipeline.
"""
from __future__ import annotations

from datetime import datetime, timezone

from prep import completed
from prep.scraper import RawAssignment


class Target:
    def __init__(self, visible):
        self.visible = visible
        self.clicked = False

    @property
    def first(self):
        return self

    def is_visible(self, timeout=0):
        return self.visible

    def click(self, timeout=0):
        self.clicked = True


class Root:
    """A page or frame that has a Completed tab, or not."""

    def __init__(self, has_tab):
        self.tab = Target(has_tab)

    def get_by_role(self, role, name=None):
        return self.tab if role == "tab" else Target(False)

    def get_by_text(self, pattern):
        return Target(False)


class Page:
    def __init__(self, frames):
        self.frames = frames


def test_completed_tab_found_inside_the_edu_frame():
    outer, inner = Root(False), Root(True)
    assert completed.click_completed(Page([outer, inner])) == "tab inside a frame"
    assert inner.tab.clicked and not outer.tab.clicked


def test_no_completed_tab_says_so():
    assert completed.click_completed(Page([Root(False), Root(False)])) == ""


def test_label_matches_completed_not_lookalikes():
    assert completed.COMPLETED_LABEL.search("Completed")
    assert completed.COMPLETED_LABEL.search("completed (12)")
    assert not completed.COMPLETED_LABEL.search("Not completed")


def _item(due):
    return RawAssignment(assignment_id=due, title=due, class_ref="",
                         class_id="", creator_id="x", creator_name="",
                         assigned_at=None, due_at=due)


def test_only_finished_prep_inside_the_window():
    now = datetime(2026, 10, 7, tzinfo=timezone.utc)
    items = [_item("2026-09-20T08:00:00Z"),    # three weeks ago - outside
             _item("2026-09-28T08:00:00Z"),    # inside
             _item("2026-10-06T08:00:00Z"),    # inside
             _item("2026-10-09T08:00:00Z"),    # still to come - never
             _item("")]
    kept = completed.finished(items, weeks=2, now=now)
    assert [i.due_at[:10] for i in kept] == ["2026-09-28", "2026-10-06"]


def test_saved_under_a_name_the_pipeline_never_reads(tmp_path):
    class Cfg:
        dump_dir = tmp_path
    path = completed.save(Cfg, [])
    assert "/completed-" in path and "payloads-" not in path
