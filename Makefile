# Three phases, each answering a different question.
CONFIG ?= config.yaml
PY     ?= ./.venv/bin/python

.PHONY: help test phase-a phase-b phase-c phases check check-write live-write live-web refresh recheck web-demo

help:
	@echo "make phase-a      dummy data, no credentials, no network  - is the logic right?"
	@echo "make phase-b      credentials, and your current prep      - do the passwords work?"
	@echo "make phase-c      everything together, dry run            - do the parts integrate?"
	@echo "make phases       A then B then C, stopping at the first failure"
	@echo ""
	@echo "make test         just the offline unit suite (~3 seconds)"
	@echo "make check-write  phase C, but actually writes to the calendar"
	@echo "make live-write   book all current prep into the test calendar, then open the dashboard"
	@echo "make live-web     just the dashboard on the live test's data"
	@echo "make refresh      the normal scheduled run"
	@echo "make recheck      re-plan only what changed since the last check"
	@echo "make web-demo     the dashboard on dummy data, offline     - click around safely"

## Fails only when the code is wrong. Runs anywhere, in about three seconds.
test:
	$(PY) -m pytest -q

## No config, no credentials, no network. Start here on a new machine.
phase-a:
	$(PY) -m prep.phases A

## Signs into Teams and prints what is due. Needs a live session.
phase-b:
	$(PY) -m prep.phases B --config $(CONFIG)

## Capture, estimate, schedule, cross-check. Writes nothing.
phase-c:
	$(PY) -m prep.phases C --config $(CONFIG)

phases:
	$(PY) -m prep.phases all --config $(CONFIG)

## Old name for phase C, kept so existing habits still work.
check: phase-c

check-write:
	$(PY) -m prep.phases C --config $(CONFIG) --write

## Every current prep into the test calendar, from a scratch database.
## Then opens the dashboard on that same data; Ctrl+C stops it.
LIVE_STATE ?= $(HOME)/prep/live-test
live-write:
	PREP_LIVE=1 PREP_CONFIG=$(CONFIG) PREP_STATE_DIR=$(LIVE_STATE) \
		$(PY) -m pytest -s -q tests/test_live_write.py
	$(MAKE) live-web

## The dashboard on the live test's data, without booking again.
live-web:
	$(PY) -m prep.web --config $(CONFIG) --state-dir $(LIVE_STATE)

refresh:
	$(PY) -m prep.run --config $(CONFIG)

## Re-plan only what changed: finished, missed, re-estimated, unplanned.
## Quiet and offline when nothing has. The timer runs it every 5 minutes.
recheck:
	$(PY) -m prep.recheck --config $(CONFIG)

## The real dashboard on invented data. No config, no credentials, no network.
web-demo:
	$(PY) -m prep.demo
