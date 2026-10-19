"""Teach the pipeline who teaches what, by naming one prep at a time.

Editing GUIDs by hand is miserable and easy to get wrong. Instead you point at
a prep you recognise and say who set it; the class behind it is learned from
that. One example per class is enough, because the class GUID is stable.

    python3 -m prep.identify --payload-file dumps/latest.json
    python3 -m prep.identify --payload-file x.json --set "Core Practical 1=Garner"
    python3 -m prep.identify --payload-file x.json --set "AAA=Archer" --set "CCC=Carver"
    python3 -m prep.identify --show

A key that matches a title (or part of one) maps that prep's whole class.
A key that looks like a teacher code maps the code instead, which is what you
want when several teachers share one class.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from datetime import datetime, timezone

from . import config as config_module
from .isotime import fromiso
from .scraper import RawAssignment, load_payload_file, parse

# A class with no prep for this long is probably last year's. It is a hint for
# you to confirm, never a decision the code makes on its own.
DORMANT_DAYS = 60

CODE_RE = re.compile(r"^[A-Z]{2,4}$")


EMPTY = {"class_map": {}, "title_codes": {}, "people_map": {},
         "archived": {}}


def load_learned(path: Path) -> dict:
    if not path.exists():
        return {k: {} for k in EMPTY}
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {k: {} for k in EMPTY}
    for key in EMPTY:
        data.setdefault(key, {})
    return data


def save_learned(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def _parse_due(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = fromiso(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def summarise_class(items: list[RawAssignment],
                    now: datetime | None = None) -> dict:
    """What this class looks like: how much prep, how recent, still running?

    Past prep matters as much as current here. Every teacher has set you
    something at some point, but only a few have anything due this week - so
    identifying from current prep alone would leave most classes nameless.
    """
    now = now or datetime.now(timezone.utc)
    dues = [d for d in (_parse_due(item.due_at) for item in items) if d]

    past = [d for d in dues if d <= now]
    current = [d for d in dues if d > now]
    latest = max(dues) if dues else None
    # Clamped at zero: a class with prep still to come has been silent for no
    # days, not a negative number of them.
    days_since = max((now - latest).days, 0) if latest else None

    stated = {item.archived for item in items if item.archived is not None}
    if stated:
        archived = any(stated)          # the tenant told us outright
        basis = "stated by Teams"
    elif days_since is not None and days_since > DORMANT_DAYS:
        archived = True
        basis = f"no prep for {days_since} days"
    else:
        archived = False
        basis = ""

    return {
        "total": len(items),
        "past": len(past),
        "current": len(current),
        "first": min(dues) if dues else None,
        "latest": latest,
        "days_since": days_since,
        "likely_archived": archived,
        "archive_basis": basis,
        "stated_archived": bool(stated) and any(stated),
    }


def existing_assignment(learned: dict, teacher: str) -> list[str]:
    """Where this teacher is already mapped. Case-insensitive on purpose."""
    wanted = teacher.strip().lower()
    found = []
    for bucket, label in (("class_map", "class"), ("title_codes", "code"),
                          ("people_map", "person")):
        for key, value in (learned.get(bucket) or {}).items():
            if str(value).strip().lower() == wanted:
                found.append(f"{label} {key}")
    return found


def find_by_title(assignments: list[RawAssignment], needle: str) -> list[RawAssignment]:
    lowered = needle.lower().strip()
    return [a for a in assignments if lowered in a.title.lower()]


def apply_set(assignments: list[RawAssignment], learned: dict,
              key: str, teacher: str) -> str:
    """Record one mapping. Returns a line describing what was learned."""
    key = key.strip()
    teacher = teacher.strip()

    # The same teacher on two class ids is usually a slip - but not always, so
    # it is flagged for you to judge rather than refused. Case is ignored:
    # "Garner" and "garner" are the same person.
    clashes = existing_assignment(learned, teacher)
    warning = ""
    if clashes:
        warning = (f"\n      ! {teacher} is already mapped to "
                   f"{', '.join(clashes)} - the same teacher twice, "
                   f"or a mistake?")

    # "archived:<class id>" retires a class you have finished with.
    if key.lower().startswith("archived:"):
        class_id = key[9:].strip()
        keep = teacher.strip().lower() not in ("no", "false", "0", "")
        learned.setdefault("archived", {})[class_id] = keep
        count = sum(1 for a in assignments
                    if a.class_id.lower() == class_id.lower())
        verb = "archived" if keep else "un-archived"
        return f"class {class_id[:30]} {verb} ({count} preps will be ignored)"

    # "who:<id or name>" maps the person who SET the prep. This is the only
    # thing that works when a class is shared, because it is per assignment.
    if key.lower().startswith("who:"):
        person = key[4:].strip()
        learned["people_map"][person] = teacher
        hits = sum(1 for a in assignments
                   if person in (a.creator_id, a.creator_name))
        return f"person {person[:20]} -> {teacher} ({hits} preps)"

    if CODE_RE.match(key):
        learned["title_codes"][key.upper()] = teacher
        hits = sum(1 for a in assignments
                   if key.upper() in re.findall(r"[A-Za-z]{2,4}", a.title.upper()))
        return f"code {key.upper()} -> {teacher} (matches {hits} titles)"

    matches = find_by_title(assignments, key)
    if not matches:
        return f"NO MATCH for '{key}' - nothing learned"

    class_ids = {a.class_id for a in matches if a.class_id}
    if not class_ids:
        return f"'{key}' matched {len(matches)} prep(s) but none carry a class id"
    if len(class_ids) > 1:
        return (f"'{key}' matched preps in {len(class_ids)} different classes - "
                f"be more specific")

    class_id = class_ids.pop()
    learned["class_map"][class_id] = teacher
    siblings = sum(1 for a in assignments if a.class_id == class_id)
    return (f"'{matches[0].title[:40]}' -> {teacher}; "
            f"learned the whole class ({siblings} preps)" + warning)


def interactive(assignments: list[RawAssignment], learned: dict) -> None:
    by_class: dict[str, list[RawAssignment]] = defaultdict(list)
    for item in assignments:
        by_class[item.class_id or "(none)"].append(item)

    unmapped = [(cid, items) for cid, items in by_class.items()
                if cid != "(none)" and cid.lower() not in
                {key.lower() for key in learned["class_map"]}]
    if not unmapped:
        print("Every class already has a teacher. Nothing to do.")
        return

    print(f"{len(unmapped)} class(es) still unidentified.\n")
    print("Past prep counts: every teacher has set you something, even if")
    print("nothing is due from them this week.\n")
    print("Type a name, Enter to skip, 'a' to mark the class archived, "
          "'s' to stop.\n")

    for class_id, items in sorted(
            unmapped, key=lambda kv: (summarise_class(kv[1])["latest"] is None,
                                      -(summarise_class(kv[1])["current"]),
                                      summarise_class(kv[1])["days_since"] or 0)):
        summary = summarise_class(items)
        codes: dict[str, int] = defaultdict(int)
        creators: set[str] = set()
        for item in items:
            for code in re.findall(r"\b[A-Z]{2,4}\b", item.title):
                codes[code] += 1
            if item.creator_id or item.creator_name:
                creators.add(item.creator_id or item.creator_name)

        print("-" * 70)
        print(f"{class_id}")
        print(f"  {summary['total']} prep total: "
              f"{summary['current']} still to come, {summary['past']} already gone")
        if summary["latest"]:
            print(f"  most recent due {summary['latest']:%d %b %Y}"
                  + (f" ({summary['days_since']} days ago)"
                     if summary["days_since"] and summary["days_since"] > 0 else ""))

        if summary["likely_archived"]:
            print(f"  ** LOOKS ARCHIVED ** ({summary['archive_basis']}) - "
                  "press 'a' to confirm, or name it anyway")

        print()
        for item in sorted(items, key=lambda i: i.due_at or "")[:8]:
            marker = " " if (_parse_due(item.due_at) or datetime.now(timezone.utc)) \
                > datetime.now(timezone.utc) else "."
            print(f"   {marker} {(item.due_at or '')[:10]}  {item.title[:54]}")
        if len(items) > 8:
            print(f"     ... and {len(items) - 8} more")

        if len(creators) > 1:
            print(f"\n  {len(creators)} different people set prep in this class,")
            print("  so it is shared - answer per person:")
            for person in sorted(creators):
                answer = input(f"    {person[:34]} teaches as: ").strip()
                if answer.lower() == "s":
                    return
                if answer:
                    for line in [apply_set(assignments, learned,
                                           f"who:{person}", answer)]:
                        print(f"      {line}")
            continue

        if len(codes) >= 2:
            listed = ", ".join(sorted(codes))
            print(f"\n  Several codes in one class ({listed}) - answer per code:")
            for code in sorted(codes):
                answer = input(f"    {code} is taught by: ").strip()
                if answer.lower() == "s":
                    return
                if answer:
                    print(f"      {apply_set(assignments, learned, code, answer)}")
            continue

        answer = input("\n  Who teaches this class? ").strip()
        if answer.lower() == "s":
            return
        if answer.lower() == "a":
            learned.setdefault("archived", {})[class_id] = True
            print("      marked archived - it will be left out of scheduling")
            continue
        if answer:
            clashes = existing_assignment(learned, answer)
            learned["class_map"][class_id] = answer
            if clashes:
                print(f"      ! {answer} is already mapped to "
                      f"{', '.join(clashes)} - the same teacher twice, "
                      f"or a mistake?")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Teach the pipeline who teaches what")
    parser.add_argument("--config", help="config file, to locate the learned map")
    parser.add_argument("--payload-file", help="a captured payload to work from")
    parser.add_argument("--set", action="append", metavar="KEY=TEACHER", default=[],
                        help="'part of a prep title=Teacher', or 'CODE=Teacher'")
    parser.add_argument("--map-file", default="~/prep/teachers.json")
    parser.add_argument("--show", action="store_true", help="print what is known")
    parser.add_argument("--forget", metavar="KEY", help="remove a class id or code")
    args = parser.parse_args(argv)

    path = Path(args.map_file).expanduser()
    if args.config:
        try:
            path = config_module.load(args.config).learned_map_path
        except Exception:
            pass

    learned = load_learned(path)

    if args.forget:
        key = args.forget.strip()
        removed = (learned["class_map"].pop(key, None)
                   or learned["title_codes"].pop(key.upper(), None)
                   or learned["people_map"].pop(key, None)
                   or learned["people_map"].pop(key.replace("who:", ""), None))
        save_learned(path, learned)
        print(f"forgot {key}" if removed else f"{key} was not recorded")
        return 0

    if args.show or not (args.payload_file or args.set):
        print(f"learned map: {path}\n")
        if learned["class_map"]:
            print("classes:")
            for class_id, teacher in sorted(learned["class_map"].items()):
                print(f"  {class_id}  ->  {teacher}")
        if learned["title_codes"]:
            print("\ncodes:")
            for code, teacher in sorted(learned["title_codes"].items()):
                print(f"  {code:<6} ->  {teacher}")
        if learned["people_map"]:
            print("\npeople who set prep:")
            for person, teacher in sorted(learned["people_map"].items()):
                print(f"  {person[:38]:<38} ->  {teacher}")
        if not any(learned[k] for k in EMPTY):
            print("(nothing learned yet)")
        return 0

    assignments = parse(load_payload_file(args.payload_file)) if args.payload_file else []

    if args.set:
        for pair in args.set:
            if "=" not in pair:
                print(f"skipping '{pair}' - expected KEY=TEACHER")
                continue
            key, teacher = pair.split("=", 1)
            print("  " + apply_set(assignments, learned, key, teacher))
        save_learned(path, learned)
        print(f"\nsaved to {path}")
        return 0

    interactive(assignments, learned)
    save_learned(path, learned)
    print(f"\nsaved to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
