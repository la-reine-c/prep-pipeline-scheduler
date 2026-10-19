"""Report which identifying fields your tenant actually sends.

When every class is shared between teachers, neither the class id nor a title
code is enough on its own. The thing that would settle it is a per-assignment
creator id. This says whether yours has one.

    python3 -m prep.inspect --payload-file test-out/raw-payloads.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from .scraper import CREATOR_KEYS, GUID_RE, _walk, load_payload_file, parse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="What can identify a teacher here?")
    parser.add_argument("--payload-file", required=True)
    parser.add_argument("--show-keys", action="store_true",
                        help="list every field name seen on assignment objects")
    args = parser.parse_args(argv)

    payloads = load_payload_file(args.payload_file)
    assignments = parse(payloads)
    if not assignments:
        print("No assignments parsed from that file.")
        return 1

    total = len(assignments)
    print(f"{total} assignments.\n")

    with_creator_id = [a for a in assignments if a.creator_id]
    with_creator_name = [a for a in assignments if a.creator_name]
    classes = {a.class_id for a in assignments if a.class_id}

    def line(label: str, count: int, note: str = "") -> None:
        mark = "yes" if count else "NO "
        print(f"  [{mark}] {label:<34} {count}/{total}  {note}")

    print("Signals available:")
    line("creator id (who set it)", len(with_creator_id),
         "<- best: works for shared classes" if with_creator_id else
         "<- absent, so shared classes cannot be split this way")
    line("creator name", len(with_creator_name))
    line("class id", sum(1 for a in assignments if a.class_id),
         f"({len(classes)} distinct classes)")
    line("class name", sum(1 for a in assignments if a.class_ref))

    codes: dict[str, int] = defaultdict(int)
    for item in assignments:
        for code in re.findall(r"\b[A-Z]{2,4}\b", item.title):
            codes[code] += 1
    line("teacher-style codes in titles", sum(codes.values()),
         f"({', '.join(sorted(codes)[:8])})" if codes else "")

    # THE decisive question. Class id and title codes both fail when one class
    # is shared between teachers; a per-assignment creator is the only thing
    # that can split them. This says outright whether yours does.
    if with_creator_id or with_creator_name:
        print("\nCan the creator tell apart teachers who share a class?")
        by_class: dict[str, set] = defaultdict(set)
        class_sizes: dict[str, int] = defaultdict(int)
        for item in assignments:
            key = item.class_id or "(no class id)"
            class_sizes[key] += 1
            who = item.creator_id or item.creator_name
            if who:
                by_class[key].add(who)

        shared = {c: people for c, people in by_class.items() if len(people) > 1}
        single = {c: people for c, people in by_class.items() if len(people) == 1}

        for class_id, people in sorted(by_class.items(),
                                       key=lambda kv: -len(kv[1])):
            mark = "SPLIT " if len(people) > 1 else "single"
            print(f"  [{mark}] {class_id[:38]:<38} "
                  f"{len(people)} creator(s), {class_sizes[class_id]} preps")

        print()
        if shared:
            print(f"  YES - {len(shared)} class(es) have several distinct creators,")
            print("  so the creator id separates teachers that a class map cannot.")
        elif single and len(single) > 1:
            print("  INCONCLUSIVE - every class here shows one creator. Either your")
            print("  classes are not really shared, or this capture is too small.")
            print("  Capture again after several teachers have each set prep.")
        else:
            print("  INCONCLUSIVE - not enough classes in this capture to tell.")

        # If two classes share a creator, one person teaches you twice - fine,
        # but worth knowing before you map them.
        everyone: dict[str, set] = defaultdict(set)
        for class_id, people in by_class.items():
            for person in people:
                everyone[person].add(class_id)
        multi = {p: c for p, c in everyone.items() if len(c) > 1}
        if multi:
            print(f"\n  Note: {len(multi)} creator(s) set prep in more than one "
                  "class - the same teacher teaching you twice.")

    if with_creator_id:
        print("\nDistinct people who set prep:")
        by_creator: dict[tuple[str, str], list] = defaultdict(list)
        for item in assignments:
            if item.creator_id or item.creator_name:
                by_creator[(item.creator_id, item.creator_name)].append(item)
        for (creator_id, creator_name), items in sorted(
                by_creator.items(), key=lambda kv: -len(kv[1])):
            shown = creator_name or creator_id
            print(f"\n  {shown}   ({len(items)} preps)")
            if creator_name and creator_id:
                print(f"    id: {creator_id}")
            for item in items[:4]:
                print(f"      {item.title[:56]}")
        print("\nName each one once:")
        for (creator_id, creator_name), items in by_creator.items():
            key = creator_id or creator_name
            example = items[0].title[:34]
            print(f'  python3 -m prep.identify --payload-file {args.payload_file} \\')
            print(f'      --set "who:{key}=TEACHER"     # e.g. "{example}"')
    else:
        print("\nNo creator field found, so a shared class cannot be split by it.")
        print("Check whether the raw payload has a field I am not reading:")
        keys: dict[str, int] = defaultdict(int)
        for payload in payloads:
            for node in _walk(payload.get("body")):
                if any(k in node for k in ("dueDateTime", "dueDate", "due")):
                    for key, value in node.items():
                        keys[f"{key}: {type(value).__name__}"] += 1
        interesting = [k for k in keys
                       if any(word in k.lower() for word in
                              ("by", "user", "teacher", "owner", "author",
                               "creat", "modif", "staff"))]
        if interesting:
            print("  candidates:")
            for key in sorted(interesting):
                print(f"    {key}  (on {keys[key]} objects)")
            print("\n  Send these field names to Claude.")
        else:
            print("  Nothing obvious. Run with --show-keys and send the list.")

    if args.show_keys:
        keys = defaultdict(int)
        for payload in payloads:
            for node in _walk(payload.get("body")):
                if any(k in node for k in ("dueDateTime", "dueDate", "due")):
                    for key, value in node.items():
                        keys[f"{key}: {type(value).__name__}"] += 1
        print("\nAll fields on assignment-shaped objects:")
        for key in sorted(keys):
            print(f"    {key}  (x{keys[key]})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
