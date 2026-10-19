"""Tell the pipeline who teaches you, once.

    python3 -m prep.setup --config config.yaml --set-teachers
    python3 -m prep.setup --config config.yaml --list-unknown
    python3 -m prep.setup --config config.yaml --set-teacher <id> "Name"
    python3 -m prep.setup --config config.yaml --list
    python3 -m prep.setup --config config.yaml --forget <id>
    python3 -m prep.setup --config config.yaml --export

Everything is grouped by the person who *set* each prep, not by the prep and
not by the class. That is the only thing that works: every class is shared
between at least two teachers, so a class id cannot tell them apart, and a
word in a title only works until someone names a prep differently. It also
means you answer once per teacher instead of once per prep.

Finished prep counts as much as current. Every teacher has set you something
at some point; only a few have anything due this week, so identifying from
current prep alone would leave most of them nameless. The default window is
four weeks back plus everything still to come.

The answers go to one file - the `learned_map_path` in your config, normally
~/prep/teachers.json. That is the file to copy to the mini; nothing here
needs to be duplicated in config.yaml on either machine.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as config_module
from . import identify
from .isotime import fromiso
from .scraper import RawAssignment, SessionExpired, capture, load_payload_file, parse

DEFAULT_WEEKS = 4
SAMPLE_TITLES = 3


@dataclass
class Person:
    """Someone who has set you prep, and what we know about them."""
    key: str                      # the creator id, or "class:<id>" as a fallback
    kind: str                     # "who" | "class"
    name_hint: str = ""           # displayName, when the tenant sends one
    class_ids: set = field(default_factory=set)
    titles: list = field(default_factory=list)
    current: int = 0
    past: int = 0
    first_due: datetime | None = None
    last_due: datetime | None = None
    teacher: str = ""             # already known, if it is

    @property
    def total(self) -> int:
        return self.current + self.past

    @property
    def lookup_key(self) -> str:
        """What --set-teacher takes, and what goes in the learned map."""
        return self.key[6:] if self.kind == "class" else self.key

    def summary(self) -> str:
        span = ""
        if self.first_due and self.last_due:
            span = (f"{self.first_due:%d %b} - {self.last_due:%d %b}"
                    if self.first_due.date() != self.last_due.date()
                    else f"{self.first_due:%d %b}")
        return (f"{self.total} prep ({self.current} still to come, "
                f"{self.past} done){f', {span}' if span else ''}")


def _parse_due(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = fromiso(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def gather(assignments: list[RawAssignment], learned: dict,
           weeks: int = DEFAULT_WEEKS,
           now: datetime | None = None) -> list[Person]:
    """Group recent and current prep by whoever set it."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(weeks=weeks)

    people = {str(k).lower(): v for k, v in (learned.get("people_map") or {}).items()}
    classes = {str(k).lower(): v for k, v in (learned.get("class_map") or {}).items()}
    archived = {str(k).lower() for k, v in (learned.get("archived") or {}).items() if v}

    found: dict[str, Person] = {}
    for item in assignments:
        due = _parse_due(item.due_at)
        if due is None or due < cutoff:
            continue
        if item.class_id and item.class_id.lower() in archived:
            continue

        # The creator is the identity that actually separates teachers. Fall
        # back to the class only when the tenant sends no creator at all -
        # and say which it is, because a class answer is weaker.
        if item.creator_id:
            key, kind = item.creator_id, "who"
            teacher = people.get(item.creator_id.lower(), "")
        elif item.creator_name:
            key, kind = item.creator_name, "who"
            teacher = people.get(item.creator_name.lower(), "")
        elif item.class_id:
            key, kind = f"class:{item.class_id}", "class"
            teacher = classes.get(item.class_id.lower(), "")
        else:
            continue

        person = found.get(key)
        if person is None:
            person = Person(key=key, kind=kind, teacher=teacher,
                            name_hint=item.creator_name or "")
            found[key] = person

        if item.class_id:
            person.class_ids.add(item.class_id)
        if len(person.titles) < SAMPLE_TITLES and item.title not in person.titles:
            person.titles.append(item.title)
        if due > now:
            person.current += 1
        else:
            person.past += 1
        person.first_due = min(person.first_due or due, due)
        person.last_due = max(person.last_due or due, due)

    # Unknown first, then whoever has most prep - the ones worth naming.
    return sorted(found.values(),
                  key=lambda p: (bool(p.teacher), -p.total, p.key))


def _collect(cfg, args) -> list[RawAssignment]:
    if args.payload_file:
        return parse(load_payload_file(args.payload_file))
    if args.no_refresh:
        dumps = sorted(Path(cfg.dump_dir).glob("payloads-*.json"))
        if not dumps:
            print(f"No saved capture in {cfg.dump_dir}. Drop --no-refresh to "
                  f"scrape, or pass --payload-file.")
            return []
        print(f"using {dumps[-1].name}")
        return parse(load_payload_file(dumps[-1]))
    print("capturing from Teams...")
    return parse(capture(cfg, dump=True))


def show_person(index: int, person: Person) -> None:
    marker = f"  [{index}]" if index else "     "
    label = person.name_hint or person.lookup_key
    print(f"\n{marker} {label[:52]}")
    if person.name_hint and person.lookup_key != person.name_hint:
        print(f"        id: {person.lookup_key}")
    print(f"        {person.summary()}")
    if person.kind == "class":
        print("        (no creator sent for these - this is a CLASS, so if it "
              "is shared\n         the answer will be wrong for one of them)")
    for title in person.titles:
        print(f"        - {title[:60]}")


def set_teacher(learned: dict, key: str, teacher: str,
                kind: str = "who") -> str:
    """Record one mapping. Returns a line describing what happened."""
    key, teacher = key.strip(), teacher.strip()
    if not key or not teacher:
        return "nothing to set"

    clashes = identify.existing_assignment(learned, teacher)
    bucket = "class_map" if kind == "class" else "people_map"
    learned.setdefault(bucket, {})[key] = teacher

    line = f"{key[:40]} -> {teacher}"
    if clashes:
        # Two ids for one teacher is usually a slip, but not always: a teacher
        # can genuinely have two accounts, or teach two of your classes. So it
        # is flagged for you to judge, never refused.
        line += (f"\n      ! {teacher} is already mapped to "
                 f"{', '.join(clashes)} - the same teacher twice, or a "
                 f"mistake?")
    return line


def interactive(people: list[Person], learned: dict) -> None:
    unknown = [p for p in people if not p.teacher]
    if not unknown:
        print("\nEveryone who has set you prep already has a name. Nothing to do.")
        return

    print(f"\n{len(unknown)} of {len(people)} still unnamed.\n")
    print("Type the teacher's name, or:")
    print("  Enter  skip this one        a  this class is finished (archive it)")
    print("  s      stop and save        ?  show more of their prep titles")

    for index, person in enumerate(unknown, 1):
        show_person(index, person)
        while True:
            try:
                answer = input("\n     teacher: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nstopping - what you have answered is saved.")
                return
            if answer == "?":
                for title in person.titles:
                    print(f"        - {title}")
                continue
            break

        if answer.lower() == "s":
            print("stopping - what you have answered is saved.")
            return
        if not answer:
            continue
        if answer.lower() == "a":
            for class_id in person.class_ids:
                learned.setdefault("archived", {})[class_id] = True
            print(f"      archived {len(person.class_ids)} class(es) - their "
                  f"prep will never be scheduled")
            continue

        print("      " + set_teacher(learned, person.lookup_key, answer,
                                     person.kind))


def print_unknown(people: list[Person], config_path: str) -> None:
    """A block you can fill in yourself, or paste somewhere for help with."""
    unknown = [p for p in people if not p.teacher]
    if not unknown:
        print("Everyone who has set you prep already has a name.")
        return

    print(f"# {len(unknown)} unidentified. Fill in the names and run these.")
    print("# Each id is one person; the titles are their recent prep.\n")
    for person in unknown:
        for title in person.titles:
            print(f"#   {title[:68]}")
        print(f"#   {person.summary()}")
        flag = "--set-class" if person.kind == "class" else "--set-teacher"
        print(f'python3 -m prep.setup --config {config_path} {flag} '
              f'{person.lookup_key} "TEACHER NAME"\n')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Tell the pipeline who teaches you",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Run --set-teachers once. Use --set-teacher later when a "
               "teacher changes.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--set-teachers", action="store_true",
                        help="go through everyone who has set you prep")
    parser.add_argument("--list-unknown", action="store_true",
                        help="print the unidentified ones as commands to fill in")
    parser.add_argument("--set-teacher", nargs=2, metavar=("ID", "NAME"),
                        help="name the person with this creator id")
    parser.add_argument("--set-class", nargs=2, metavar=("CLASS_ID", "NAME"),
                        help="name a whole class - only right if it has ONE teacher")
    parser.add_argument("--archive", metavar="CLASS_ID",
                        help="mark a class finished; its prep is never scheduled")
    parser.add_argument("--forget", metavar="ID", help="remove a mapping")
    parser.add_argument("--list", action="store_true", help="what is known")
    parser.add_argument("--export", action="store_true",
                        help="print the map, for copying to the other machine")
    parser.add_argument("--weeks", type=int, default=DEFAULT_WEEKS,
                        help=f"how far back to look (default {DEFAULT_WEEKS})")
    parser.add_argument("--no-refresh", action="store_true",
                        help="use the last capture instead of scraping")
    parser.add_argument("--payload-file", help="use a specific saved capture")
    args = parser.parse_args(argv)

    cfg = config_module.load(args.config)
    path = cfg.learned_map_path
    learned = identify.load_learned(path)

    # ---- the ones that need no capture -------------------------------
    if args.export:
        print(f"# {path}\n# copy this file to the other machine, or paste "
              f"this into it\n")
        print(json.dumps(learned, indent=2, sort_keys=True))
        return 0

    if args.list:
        print(f"learned map: {path}\n")
        for bucket, label in (("people_map", "people who set prep"),
                              ("class_map", "classes"),
                              ("title_codes", "title codes")):
            entries = learned.get(bucket) or {}
            if entries:
                print(f"{label}:")
                for key, teacher in sorted(entries.items(), key=lambda kv: kv[1]):
                    print(f"  {key[:44]:<44} -> {teacher}")
                print()
        archived = [k for k, v in (learned.get("archived") or {}).items() if v]
        if archived:
            print(f"archived classes ({len(archived)}, never scheduled):")
            for class_id in sorted(archived):
                print(f"  {class_id}")
        if not any(learned.get(b) for b in identify.EMPTY):
            print("(nothing learned yet - run --set-teachers)")
        return 0

    if args.set_teacher or args.set_class:
        key, name = args.set_teacher or args.set_class
        kind = "who" if args.set_teacher else "class"
        print("  " + set_teacher(learned, key, name, kind))
        identify.save_learned(path, learned)
        print(f"\nsaved to {path}")
        return 0

    if args.archive:
        learned.setdefault("archived", {})[args.archive] = True
        identify.save_learned(path, learned)
        print(f"archived {args.archive} - its prep will never be scheduled")
        return 0

    if args.forget:
        key = args.forget.strip()
        removed = False
        for bucket in ("people_map", "class_map", "title_codes", "archived"):
            entries = learned.get(bucket) or {}
            for candidate in [k for k in entries if k.lower() == key.lower()]:
                entries.pop(candidate)
                removed = True
        identify.save_learned(path, learned)
        print(f"forgot {key}" if removed else f"{key} was not recorded")
        return 0

    # ---- the ones that need prep -------------------------------------
    if not (args.set_teachers or args.list_unknown):
        parser.print_help()
        return 0

    try:
        assignments = _collect(cfg, args)
    except SessionExpired as exc:
        print(f"\nTeams session expired: {exc}")
        print(f"Sign in again: python3 -m prep.login --config {args.config}")
        return 1
    except Exception as exc:
        print(f"\nCapture failed: {exc}")
        return 1

    if not assignments:
        return 1

    people = gather(assignments, learned, weeks=args.weeks)
    if not people:
        print(f"\nNo prep due in the last {args.weeks} weeks or still to come. "
              f"Try --weeks {args.weeks * 3}.")
        return 1

    named = sum(1 for p in people if p.teacher)
    print(f"\n{len(people)} people have set you prep in the last {args.weeks} "
          f"weeks or have some due ({named} already named).")

    no_creator = [p for p in people if p.kind == "class"]
    if no_creator:
        print(f"\n{len(no_creator)} group(s) arrived with no creator, so they "
              f"are grouped by class\ninstead. If a class is shared, naming it "
              f"will be wrong for one of the teachers.\nRun python3 -m "
              f"prep.inspect to see what your tenant actually sends.")

    if args.list_unknown:
        print()
        print_unknown(people, args.config)
        return 0

    interactive(people, learned)
    identify.save_learned(path, learned)
    print(f"\nsaved to {path}")
    print(f"Copy that file to the mini so both machines agree, or run "
          f"--export there.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
