"""Prep turned in on Teams is finished; prep long overdue leaves the page."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from prep.scraper import RawAssignment, complete_lists, parse, turned_in
from prep.store import Store

NOW = datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc)

# The two lists the Assignments page asks for, as Teams sends them.
UPCOMING = ("https://assignments.edu.cloud.microsoft/api/v1.0/edu/me/work?%24filter="
            "%28%20%28%20status%20eq%20microsoft.education.assignments.api."
            "educationAssignmentStatus%27assigned%27%20and%20isCompleted%20eq%20false"
            "%20%29%20%29%20and%20dueDateTime%20ge%202026-10-12T23%3A00%3A00.000Z"
            "&%24top=20&%24orderby=dueDateTime%20asc")
PAST_DUE = ("https://assignments.edu.cloud.microsoft/api/v1.0/edu/me/work?%24filter="
            "status%20eq%20microsoft.education.assignments.api."
            "educationAssignmentStatus%27assigned%27%20and%20isCompleted%20eq%20false"
            "%20and%20dueDateTime%20le%202026-10-13T08%3A59%3A00.000Z"
            "&%24top=20&%24orderby=dueDateTime%20desc")


def work(aid: str, due: str, completed: bool = False) -> dict:
    return {"id": aid, "displayName": f"prep {aid[:4]}", "dueDateTime": due,
            "classId": "c" * 36, "isCompleted": completed}


def guid(n: int) -> str:
    return f"{n:08d}-0000-0000-0000-000000000000"


def store_with(*items: dict) -> Store:
    store = Store(":memory:")
    store.upsert(parse([{"url": UPCOMING, "body": {"value": list(items)}}]),
                 lambda item: "")
    return store


def test_a_list_sent_whole_covers_its_due_dates():
    ranges = complete_lists([{"url": UPCOMING, "body": {"value": []}}])
    assert ranges == [(datetime(2026, 10, 12, 23, tzinfo=timezone.utc), None)]


def test_a_paged_list_proves_nothing():
    # The past-due list comes a page at a time. Prep missing from page one
    # may be on page two.
    paged = {"url": PAST_DUE, "body": {"value": [], "@odata.nextLink": "more"}}
    assert complete_lists([paged]) == []
    whole = {"url": PAST_DUE, "body": {"value": []}}
    assert complete_lists([whole]) == [
        (None, datetime(2026, 10, 13, 8, 59, tzinfo=timezone.utc))]


def test_pages_fetched_in_order_cover_back_to_the_oldest_on_them():
    # As Teams sent it on 13/10: newest first, two empty pages, then April.
    # Prep due yesterday would have been on one of these pages.
    def page(token, items, following):
        url = PAST_DUE + (f"&%24skiptoken={token}" if token else "")
        body = {"value": items}
        if following:
            body["@odata.nextLink"] = PAST_DUE + f"&%24skiptoken={following}"
        return {"url": url, "body": body}

    april = [work(guid(9), "2026-05-01T08:00:00Z"), work(guid(8), "2026-04-24T07:00:00Z")]
    ranges = complete_lists([page(None, [], "AbC1"), page("AbC1", [], "dEf2"),
                             page("dEf2", april, "gHi3")])
    assert ranges == [(datetime(2026, 4, 24, 7, 0, 1, tzinfo=timezone.utc),
                       datetime(2026, 10, 13, 8, 59, tzinfo=timezone.utc))]

    store = store_with(work(guid(1), "2026-10-12T15:00:00Z"))
    assert store.mark_handed_in(set(), NOW, ranges) == [guid(1)]

    # A page missing from the middle breaks the chain: nothing after it counts.
    assert complete_lists([page(None, [], "AbC1"), page("dEf2", april, "gHi3")]) == []


def test_other_requests_are_not_lists_of_work():
    assert complete_lists([{"url": "https://x/edu/me/settings/viewed",
                            "body": {"value": []}}]) == []


def test_prep_missing_from_a_whole_list_was_handed_in():
    store = store_with(work(guid(1), "2026-10-15T07:00:00Z"),
                       work(guid(2), "2026-10-16T07:00:00Z"))
    gone = store.mark_handed_in({guid(2)}, NOW,
                                [(datetime(2026, 10, 12, 23, tzinfo=timezone.utc), None)])
    assert gone == [guid(1)]
    assert guid(1) in store.logged_ids()
    assert [row["assignment_id"] for row in store.upcoming(now=NOW)] == [guid(2)]


def test_prep_outside_every_whole_list_is_left_alone():
    # Due yesterday: only the past-due list would show it, so its absence
    # from the upcoming list says nothing.
    store = store_with(work(guid(1), "2026-10-12T15:00:00Z"))
    assert store.mark_handed_in(
        set(), NOW, [(datetime(2026, 10, 12, 23, tzinfo=timezone.utc), None)]) == []
    assert store.mark_handed_in(set(), NOW, []) == []


def test_your_own_tasks_are_never_handed_in_by_teams():
    store = Store(":memory:")
    task = store.add_task("UCAS draft", "2026-10-20T17:00:00+01:00", minutes=60)
    assert store.mark_handed_in(set(), NOW, [(None, None)]) == []
    assert task not in store.logged_ids()


def test_prep_listed_again_is_open_again():
    # Returned for another go: it is back on Teams' list.
    store = store_with(work(guid(1), "2026-10-15T07:00:00Z"))
    store.mark_handed_in(set(), NOW, [(None, None)])
    store.upsert(parse([{"url": UPCOMING, "body": {
        "value": [work(guid(1), "2026-10-15T07:00:00Z")]}}]), lambda item: "")
    assert guid(1) not in store.logged_ids()


def test_teams_own_completed_flag_counts():
    [item] = parse([{"url": UPCOMING, "body": {
        "value": [work(guid(1), "2026-10-15T07:00:00Z", completed=True)]}}])
    assert turned_in(item)
    submitted = RawAssignment(guid(2), "t", "", "", "", "", None, None,
                              raw={"submissions": [{"status": "submitted"}]})
    assert turned_in(submitted)
    working = RawAssignment(guid(3), "t", "", "", "", "", None, None,
                            raw={"isCompleted": False,
                                 "submissions": [{"status": "working"}]})
    assert not turned_in(working)


def test_past_due_prep_leaves_the_page_after_a_week():
    store = store_with(work(guid(1), "2026-04-22T08:00:00Z"),
                       work(guid(2), (NOW - timedelta(days=3)).isoformat()),
                       work(guid(3), "2026-10-15T07:00:00Z"))
    shown = [row["assignment_id"] for row in store.upcoming(now=NOW)]
    assert shown == [guid(3), guid(2)]


def test_a_short_logged_time_finishes_prep_but_teaches_nothing():
    store = store_with(work(guid(1), "2026-10-15T07:00:00Z"),
                       work(guid(2), "2026-10-16T07:00:00Z"))
    store.conn.execute("UPDATE assignments SET teacher = 'T'")
    store.record_timing(guid(1), 20)
    store.record_timing(guid(2), 45)
    assert {guid(1), guid(2)} <= store.logged_ids()
    assert store.teacher_timings() == {"T": [45.0]}
    assert store.global_mean() == 45
