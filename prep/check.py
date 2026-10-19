"""Backwards-compatible alias for Phase C.

`python3 -m prep.check` was the single live-verification command before the
work was split into phases. It now forwards to Phase C, which does the same
job and more, so there is one implementation rather than two that drift.

    python3 -m prep.check --config config.yaml
    python3 -m prep.phases C --config config.yaml     # the same thing
"""
from __future__ import annotations

import argparse
import sys

from . import phases
# Re-exported so existing code and tests that reach for these keep working.
from .phases import OK, WARN, FAIL, MARK, Report, phase_c  # noqa: F401
from .scheduler import CalDAVCalendar, DryRunCalendar      # noqa: F401


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture fresh prep and check everything against it "
                    "(this is Phase C)")
    parser.add_argument("--config", required=True)
    parser.add_argument("--no-refresh", action="store_true")
    parser.add_argument("--unit-tests", action="store_true",
                        help="also run Phase A, which includes the offline suite")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    forwarded = ["C", "--config", args.config]
    if args.no_refresh:
        forwarded.append("--no-refresh")
    if args.write:
        forwarded.append("--write")
    if args.verbose:
        forwarded.append("--verbose")

    code = phases.main(forwarded)
    if args.unit_tests and code == 0:
        code = phases.main(["A", "--quiet"])
    return code


if __name__ == "__main__":
    sys.exit(main())
