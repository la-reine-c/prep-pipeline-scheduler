"""Group captured assignments by class, to build the classId -> teacher map.

Teams sends a class GUID but usually no class name, so a GUID has to be
identified once by a human. This prints each class with its assignment titles
as evidence, plus a ready-to-paste config block.

    python3 -m prep.classes --payload-file test-out/raw-payloads.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict

from .scraper import load_payload_file, parse

# Two to four capitals inside a title are often a teacher's initials.
CODE_RE = re.compile(r"\b([A-Z]{2,4})\b")

# Words that look like codes but are not teachers.
NOT_CODES = {
    "MCQ", "PPQ", "TBC", "RSVP", "HW", "PDF", "AS", "A2", "IB", "GCSE",
    "EPQ", "CP", "MS", "QRSTUV", "BBQ", "ICT", "PE", "RS", "DT", "PSHE",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the class -> teacher map")
    parser.add_argument("--payload-file", required=True)
    parser.add_argument("--min-code-hits", type=int, default=2,
                        help="how often a code must appear to be suggested")
    args = parser.parse_args(argv)

    assignments = parse(load_payload_file(args.payload_file))
    if not assignments:
        print("No assignments found in that file.")
        return 1

    by_class: dict[str, list] = defaultdict(list)
    for item in assignments:
        by_class[item.class_id or "(no class id)"].append(item)

    code_counts: dict[str, int] = defaultdict(int)
    shared_class_codes: list[set[str]] = []

    print(f"{len(assignments)} assignments across {len(by_class)} classes.\n")
    print("Identify each class below, then paste the config block at the end.\n")

    for class_id, items in sorted(by_class.items(), key=lambda kv: -len(kv[1])):
        name = next((i.class_ref for i in items if i.class_ref), "")
        header = f"{class_id}"
        if name:
            header += f"   ({name})"
        print("=" * 72)
        print(header)
        print(f"  {len(items)} assignments")

        local_codes: dict[str, int] = defaultdict(int)
        for item in items:
            for code in CODE_RE.findall(item.title):
                if code not in NOT_CODES:
                    local_codes[code] += 1
                    code_counts[code] += 1

        for item in sorted(items, key=lambda i: i.due_at or ""):
            due = (item.due_at or "")[:10]
            print(f"    {due}  {item.title[:60]}")

        if local_codes:
            shared_class_codes.append(set(local_codes))
            shown = ", ".join(f"{c} x{n}" for c, n in
                              sorted(local_codes.items(), key=lambda kv: -kv[1]))
            print(f"  possible teacher codes in titles: {shown}")
            if len(local_codes) >= 2:
                print("  -> several codes in one class: this class is probably "
                      "shared between teachers, so use title_codes for it")
        print()

    print("=" * 72)
    print("Paste into config.yaml under `scheduling:`, filling in the names.")
    print("A class taught by ONE teacher goes in class_map. A class shared by")
    print("several goes in title_codes instead, keyed on the code in the title.\n")

    print("  class_map:")
    for class_id, items in sorted(by_class.items(), key=lambda kv: -len(kv[1])):
        sample = items[0].title[:40].replace("\n", " ")
        print(f"    {class_id}: \"\"        # e.g. \"{sample}\"")

    # Frequency alone misses a teacher who has only set one prep. The stronger
    # signal is a single class carrying several distinct codes - that is a
    # shared class, and the codes are what tell the teachers apart.
    likely = {c: n for c, n in code_counts.items() if n >= args.min_code_hits}
    for codes in shared_class_codes:
        if len(codes) >= 2:
            for code in codes:
                likely.setdefault(code, code_counts[code])
    if likely:
        print("\n  title_codes:")
        for code, count in sorted(likely.items(), key=lambda kv: -kv[1]):
            print(f"    {code}: \"\"        # appears in {count} titles")

    print("\nLeave a line blank and delete it if it is not a real class "
          "(a year-group team, a notices channel).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
