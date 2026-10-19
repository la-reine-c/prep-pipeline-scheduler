# prep-pipeline

Reads your homework out of Microsoft Teams Assignments, works out how long each
piece will actually take you, and books it into your calendar around your
lessons, rehearsals and everything else already in your diary.

It runs on a machine you own. Nothing about your school account leaves it.

```
Teams (your own browser session)
   │  Playwright, on a schedule
   ▼
SQLite  ──►  estimator  ──►  scheduler  ──►  iCloud "Prep" calendar (CalDAV)
   ▲                             ▲
   │                             │ busy time
dashboard                school timetable (ICS feed)  +  your blackout times
```

---

## Why it is built this way

**The scraper cannot move to the cloud.** Signing into a school Microsoft
tenant from a datacentre IP trips Entra ID risk detection, which at best means
constant re-authentication and at worst flags your account to IT. It has to run
from a machine on your home connection.

**Identity is the Teams assignment GUID**, never a name-plus-date composite. A
key containing the due date changes when a due date moves, which is precisely
the event the whole system exists to handle.

**SQLite is the source of truth.** The `prep.txt` export is a generated view.

**A failed timetable fetch stops the run.** Silently scheduling against an empty
timetable would book prep straight through your lessons, with total confidence.
Scheduling nothing is the better failure.

---

## Installation

**Time: about 40 minutes**, most of it unattended.

| Step | Hands-on | Waiting |
|---|---|---|
| Dependencies | 2 min | ~5 min (Chromium download dominates) |
| Apple app password + Prep calendar | 5 min | — |
| Alerts (email + Telegram bot) | 10 min | — |
| Sign into Teams once | 3 min | — |
| First capture and fitting the parser | 5 min | — |
| Naming your teachers | 5 min | — |
| systemd services | 3 min | — |

### Before you start

- A machine that is always on, with a desktop session available once for the
  Teams sign-in. Ubuntu (24.04 tested) on both the desktop and the mini.
  Chromium is Playwright's own build — Ubuntu's apt has none, and its snap is
  confined (see step 1).
- An iCloud calendar you own.
- Python 3.10 or newer.

### 1. Clone and install

```bash
sudo apt install -y python3-venv python3-pip
mkdir -p ~/prep && cp -r prep-pipeline/2-deploy-on-mini/. ~/prep/ && cd ~/prep
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m playwright install --with-deps chromium
```

Leave `chromium_path` empty. `sudo apt install chromium` has no candidate on
Ubuntu, and the snap (`chromium_path: /snap/bin/chromium`) only works with
the profile in a plain folder in your home directory — under `/tmp` it
reports success and keeps the profile in its own private `/tmp`, so the
sign-in never persists. The pipeline refuses that combination up front.

### 2. Make the calendar

In the Calendar app on your phone or Mac, create a calendar called **Prep**.
The run refuses to start without it — deliberately, so it can never write into
your school or family calendars.

### 3. Credentials

Get an app-specific password from [appleid.apple.com](https://appleid.apple.com)
→ Sign-In and Security → App-Specific Passwords. Your normal Apple password
will not work.

```bash
cp config.example.yaml config.yaml
nano .env
chmod 600 .env
```

```
ICLOUD_USER=you@icloud.com
ICLOUD_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
SOCS_FEED_URL=https://your-school-timetable-feed/...
PREP_WEB_TOKEN=a-long-random-string
ALERT_EMAIL_USER=you@gmail.com
ALERT_EMAIL_APP_PW=your-gmail-app-password
ALERT_EMAIL_TO=you@gmail.com
TELEGRAM_BOT_TOKEN=123456:AA...
TELEGRAM_CHAT_ID=987654321
```

Your timetable feed URL usually contains a personal key. **Treat it as a
credential** — it is in `.env`, and `.env` is gitignored, for that reason.

Keep `.env` next to `config.yaml`. systemd loads it on the mini, and every
command you run by hand (`make phase-c`, `python3 -m prep.run`) reads it
too. A variable you have already exported takes priority over the file.

Telegram, in three steps: message **@BotFather**, send `/newbot`; send your new
bot any message; then

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['result'][0]['message']['chat']['id'])"
```

Prove alerts work before you rely on them:

```bash
./.venv/bin/python -m prep.run --config config.yaml --test-alerts
```

### 4. Edit config.yaml

The parts that matter:

- **`windows`** — when you are willing to work. Defaults to weekday evenings
  and weekend daytimes.
- **`max_daily_minutes`** — total prep in any one day, across every subject.
- **`ignore_calendars`** — calendars that should not block time.
- **`web.port`** — check it is free with `sudo ss -tlnp`.

### 5. Sign into Teams, once

Needs a desktop session — use VNC if the machine is headless.

```bash
./.venv/bin/python -m prep.login --config config.yaml
```

Use this rather than a hand-typed Playwright one-liner. It opens the same
profile with the same browser and the same flags the scraper will use, and it
sets up the profile first — including **allowing third-party cookies**, which
a one-liner does not and which costs you a working session a few days later
(see below). It then reopens headless and checks the session survived, which
is the only thing that matters and is not what you tested by watching a
window.

In the window:

**Tick "Stay signed in".** That is the single most important click in the whole
setup: it is what lets the thing run for months without you.

**Then open Assignments yourself, and scroll to the bottom of the list.** The
app opens on Teams, not on your prep; on `teams.cloud.microsoft` the address
bar does not change when you get there, so the click is the only way in and
the URL will look identical afterwards. That is expected — `prep.login`
watches the requests, not the address.

#### Third-party cookies, and the session that dies a few days later

Teams renews its own access in a hidden iframe pointed at
`login.microsoftonline.com`. Inside the Teams page, that host is a **third
party** — so with third-party cookies blocked, the renewal fails silently.

The symptom arrives nowhere near the cause:

- you sign in, and everything works — the window renders, prep is captured,
  the sign-in cookie is written and is unexpired;
- a few days later every run reports a **dead session**, while that cookie is
  still sitting in the profile perfectly valid;
- signing in again fixes it for another few days, which is exactly what hides
  the cause.

`prep.login`, `prep.diagnose`, `prep.session` and Phase B now all write the
setting into the profile before Chromium starts and say where it stands:

```bash
./.venv/bin/python -m prep.session --config config.yaml
```
```
profile: /home/you/prep/profile
session: persistent, about 88 days left
cookies: third-party allowed (the browser profile)
```

It is the same setting the browser's own Settings page writes, in the one
profile that exists to read your own assignments. Nothing is spoofed and no
protection Teams relies on is weakened.

One case cannot be fixed from here: a **machine-wide policy** in
`/etc/chromium/policies/managed/` beats the profile's own setting. That is
reported as such rather than written over, because a preference the policy
will ignore, plus a message saying it worked, is worse than the truth.

### 6. First capture

```bash
./.venv/bin/python -m prep.run --config config.yaml --dump --dry-run
```

`--dry-run` reads your real calendar and timetable but writes nothing.

If it reports *no assignment payloads captured*, open a file in `dumps/`, find
the part holding your assignments, and narrow `url_hints` in `config.yaml` to
match that URL.

### 7. Tell it who teaches what

Teams sends a class GUID, usually with no teacher name attached, so this has to
be done once. You point at a prep you recognise:

```bash
./.venv/bin/python -m prep.inspect --payload-file dumps/payloads-*.json
./.venv/bin/python -m prep.identify --payload-file dumps/payloads-*.json
```

`inspect` reports which identifying fields your tenant actually sends, and says
outright whether the creator id can separate teachers who share a class.

`identify` then walks you through each unidentified class. It uses **every prep
ever captured, finished ones included** — every teacher has set you something at
some point, but only a few have anything due in any given week, so identifying
from current prep alone would leave most classes nameless.

It also flags classes that look finished: either Teams marks them archived, or
they have set nothing in over 120 days (long enough to survive a summer
holiday). Press `a` to confirm, and those preps are never scheduled.

If you map the same teacher to a second class it warns you — comparing names
case-insensitively, so `Garner` and `garner` are the same person. One teacher
really can take you twice, so it flags rather than refuses.

Non-interactively:

```bash
./.venv/bin/python -m prep.identify --payload-file dumps/x.json \
  --set "Core Practical 1=Garner" \
  --set "AAA=Archer" \
  --set "who:<creator-id>=Dalton" \
  --set "archived:<class-id>=yes"
```

#### Teachers whose prep is already finished

A normal capture opens Assignments on **Upcoming**, which only asks Teams for
prep due today or later. A teacher whose prep you finish between two runs is
never seen there. `prep.completed` is a separate check that also opens the
**Completed** tab and lists who set what you finished:

```bash
./.venv/bin/python -m prep.completed --config config.yaml            # last 2 weeks
./.venv/bin/python -m prep.completed --config config.yaml --weeks 6
```

It only reads and reports; name anyone it lists with `prep.setup
--set-teacher`. It is a copy of the capture, not a change to it, and nothing
in `prep.run` uses it. Its captures are saved as `completed-*.json`, never
`payloads-*.json`, so finished prep can never be scheduled.

### 8. Let it write to the calendar

```bash
./.venv/bin/python -m prep.run --config config.yaml
```

Check the Prep calendar on your phone. If the placement is wrong, change
`windows` rather than moving events by hand — the next run replaces them.

### 9. Run it on a schedule

`./install.sh` (in `2-deploy-on-mini/`) does this and everything above it on
the mini; its `README.md` lists every step. By hand:

```bash
for unit in systemd/prep-*.service systemd/prep-*.timer; do
  sed -e "s|PREP_USER|$(whoami)|g" -e "s|PREP_HOME|$HOME/prep|g" \
      "$unit" | sudo tee "/etc/systemd/system/$(basename "$unit")" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now prep-scrape.timer prep-recheck.timer prep-web.service
systemctl list-timers 'prep-*'
```

Stop the machine sleeping, or the runs silently never happen:

```bash
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

gunicorn now serves the dashboard on `127.0.0.1:8080`. Put nginx in front of
it for HTTPS — `2-deploy-on-mini/deploy/README.md` — and it is at
`https://<your-host>/?token=<your-token>`.

---

## Using it

Open the dashboard after finishing a prep and log how long it took. That is the
whole routine, and it is the only thing that makes the estimates good.

**"took" and "should be" are different.** *took* is a measurement, recorded
after the fact; it teaches the system about that teacher and changes all their
future estimates. *should be* is a correction to a prediction; it reschedules
this one prep and teaches nothing. If you correct a prep, still log what it
took afterwards.

### Your own tasks

**Add a task** on the dashboard puts anything Teams does not know about —
EPQ reading, mock revision, a UCAS draft — through the same estimator and
scheduler as prep, and books it into the Prep calendar straight away, around
lessons, blackouts and everything already booked.

- **Minutes** are optional. Given, they are stored as the correction
  (*should be*), so they win over any prediction. Left empty, the task is
  estimated like prep.
- **Teacher** is optional, and it decides where the task's logged time goes.
  Pick one only if they set the work: it then counts towards their estimates
  and patterns exactly like their Teams prep. Leave it as *my own task* and
  it stays out of every average, including the global mean that unlogged
  teachers are estimated from — one long evening on a personal project must
  not inflate the estimate for prep.
- **Start from** holds it back: nothing is booked before that date.
- If iCloud or the timetable feed is unreachable, the task is **saved but not
  booked**, the page says so, and the next scheduled run places it. A task
  that was planned but found no free slot is not retried every run (it would
  be re-reported each time); use its **Reschedule** button.
- **remove** deletes the task, its logged times and its calendar blocks. If
  iCloud cannot be reached, nothing is removed — deleting only the database
  row would strand its blocks in your diary. Prep from Teams cannot be
  removed here; the next capture would only bring it back.

Tasks live in the `assignments` table with `manual = 1` and a `task-` id, so
logging, corrections and the export treat them like any other prep.

### How it packs the week, and what a change moves

- **Soonest deadline first.** Whatever is being placed in one go is placed
  in deadline order. If a prep is still short and something due *later* holds
  evenings before its deadline, the later one gives them up and is booked
  again straight after. It has more days to find room in. This only happens
  when it actually gains time.
- **Sittings, not just blocks.** Blocks of the same prep that fall back to
  back become one sitting of up to `scheduling.max_sitting_minutes` (60),
  with no break and no flow buffer in the middle. Set it to `block_minutes`
  for the old one-block sittings.
- **To the minute.** An estimate is booked as whole blocks plus the
  remainder: 79 minutes books 79, not 90. A remainder shorter than
  `scheduling.min_block_minutes` (15) is rounded up to it, and no sitting is
  ever left shorter than that. The same rule lets the last 15-25 minutes of
  an evening be used.
- **Prep sits closer to prep.** `gap_minutes` (10) is breathing room around
  diary events. Between two prep sittings there is only
  `prep_gap_minutes` (5): a break, not time to get anywhere.
- **Spread, then fill.** A prep is spread across the days before its
  deadline first. Whatever is still unplaced then goes wherever there is
  room, so a day another prep has already filled cannot strand blocks.
- **The daily limit counts work**, not flow buffers. Two one-hour sittings
  are 120 minutes of work, so they fit a 120-minute day, though the second
  carries a 5-minute buffer. Each session records its buffer
  (`sessions.buffer_minutes`). Older rows without one are read as whole
  blocks plus a buffer, which is what they were.
- **A change moves only what it touches.** A blackout, an outside
  commitment or a correction re-plans the bookings that now clash, prep
  never booked, and prep short of time. Prep short of time is *topped up*:
  it keeps what it has and only gains. Everything else stays where it is.
  `pipeline.replan_all` still re-plans everything, but nothing on the
  dashboard calls it now.
- **In the background.** The dashboard saves your change and answers at
  once. The planning runs behind it, and the page says so, then refreshes
  itself with the result when it is done. If you have started typing in a
  form, it offers a link rather than refreshing. Progress is kept in
  `<db>.planning.json`, so both gunicorn workers report the same thing.

### Working late, and how you get there

- **Late evening costs more.** With `scheduling.late_from: "20:00"` and
  `late_step_minutes: 60`, time is used cheapest first: everything before
  20:00 up to the deadline, then 20:00-21:00, then 21:00-22:00. Late time
  is used only when nothing earlier before the deadline is free. That
  includes a later day: Tuesday at 18:00 beats Monday at 20:30 when the
  prep is not due until Thursday. Remove `late_from` for no penalty.
- **Car and public transport are timed apart.** `travel.times` is public
  transport and `travel.car_times` is by car, each either way round. An
  untimed journey is estimated from straight-line distance, calibrated
  separately for each mode from the journeys you have timed that way. A
  75-minute bus to school says nothing about a lift. With no car journey
  timed, a car is estimated at 1.8 minutes per straight-line km plus
  `car_overhead_minutes` (5).
- **Which mode a journey uses:**
  - the commitment you are going to, or leaving, says;
  - otherwise the school run (home <-> school) uses `school_mode`;
  - anything else uses `default_mode`.
  The dashboard's outside-commitment and blackout forms ask, defaulting to
  `default_mode`. For events you put straight into your calendar, write "by
  car" or "(car)", or "(bus)", "by train" or "public transport", in the
  title, location or notes.

### Did you do it? Missed sessions and the five-minute recheck

Every session that has ended appears under **Did you do these?** on the
dashboard, with **Done** and **Missed**. For `scheduling.confirm_grace_hours`
(24) after it ends, an unconfirmed session counts as done, so being slow to
say so costs nothing. After that it counts as missed. Missed time stops
counting towards the prep, and that prep alone is re-planned to make it up
before the deadline. Pressing **Missed** does the same at once. Logging how
long the whole prep took confirms all its sessions.

`prep.recheck` runs every five minutes (`prep-recheck.timer`) and re-plans
only when something changed:

- **finished**: a time was logged, so the prep's remaining bookings come out
  of the diary;
- **missed**: as above;
- **timing**: the estimate moved by at least one block since booking (a new
  logged time for that teacher, or a correction), so that prep is re-booked
  to the new length;
- **unplanned**: prep saved but never booked, because iCloud or the timetable
  was down on the run that found it.

With none of those it reads the database and stops, without touching the
network. Each session's state is the `status` column of `sessions`: `''`
not yet confirmed, `done`, `missed` (waiting to be re-booked), or `skipped`
(missed and re-booked). When the column is first added to an old database,
every session already in the past is marked `done`, so a term of history is
not suddenly "missed".

Dry runs (`prep.run --dry-run`, Phase C without `--write`, `prep.recheck
--dry-run`) plan on an in-memory copy of the database. They used to write to
the real one: the prep they saw was recorded as known, so the next real run
found nothing new and booked none of it, and their pretend `dry-run-*`
sessions counted as booked. Those leftovers are removed when the database is
opened, and the recheck books that prep properly.

### How long it thinks a prep will take

With nothing logged, `min(30 × days, 120)` minutes — a guess based only on how
much time you were given, not how much work was set.

Once a teacher has three or more logged times, it tracks them with a
random-walk filter in log space:

```
x_k = x_{k-1} + w,   w ~ N(0, Q)      the true duration drifts over the year
z_k = x_k + v,       v ~ N(0, R)      your logged time, noisily
```

Two departures from a textbook Kalman filter, both deliberate:

- **Huber-weighted updates.** A plain Kalman update is linear in the
  innovation, so one 300-minute evening drags the estimate hard. Down-weighting
  large innovations resists that. Measured: one such evening among five moves
  the estimate by about 4 minutes, where a plain mean moves by 51.
- **R is estimated from your own data**, using the robust spread of your log
  timings with a small-sample correction — not from the filter's residuals,
  which are far too small because the filter has already chased the data.

**It books a quantile, not the mean:**

```
t = exp(x + λ·√(P + R))
```

Booking the middle means overrunning half your preps by construction. At
λ = 0.85 the booked time covers roughly 80% of preps; measured across
variabilities from ±15% to ±80%, actual coverage is 74–85%.

---

## Commands

```bash
python -m prep.run    --config config.yaml                  # normal run
python -m prep.run    --config config.yaml --dry-run        # plan, write nothing
python -m prep.run    --config config.yaml --test-alerts    # prove alerts work
python -m prep.run    --config config.yaml --log "Core Practical:50"
python -m prep.recheck --config config.yaml                 # re-plan only what changed
python -m prep.recheck --config config.yaml --dry-run       # what it would change
python -m prep.web    --config config.yaml                  # dashboard (dev server)
python -m prep.inspect  --payload-file dumps/x.json         # what fields exist
python -m prep.identify --payload-file dumps/x.json         # who teaches what
python -m prep.completed --config config.yaml               # who set prep you have finished
python -m prep.classes  --payload-file dumps/x.json         # group by class
python -m prep.login  --config config.yaml                  # sign in, verify it stuck
python -m prep.session --config config.yaml                 # how long the session has left
python -m prep.session --config config.yaml --snapshot      # save a known-good profile
python -m prep.session --config config.yaml --restore       # put it back after corruption
python -m prep.diagnose --config config.yaml --expect 42    # why is the count that?
python -m prep.icloud  --config config.yaml                 # why will iCloud not sign in?
python -m prep.phases A                                     # dummy data, no credentials
python -m prep.demo                                         # dashboard on dummy data, offline
python -m prep.phases B --config config.yaml                # credentials + current prep
python -m prep.phases C --config config.yaml                # everything, end to end
python -m pytest -q                                         # offline suite, no network
```

Exit codes from a run: **2** dead Teams session, **3** capture failed,
**4** calendar unreachable, **5** timetable feed failed so nothing was scheduled.

---

## When Teams is unreachable

A dead session used to mean an empty diary — and you would find out the night
before something was due. It no longer does.

Your teachers are regular: one sets Monday due Friday, another Thursday due
Monday. After a few weeks that regularity is in the database, and it does not
stop being true because a session cookie expired. So when a capture fails, the
scheduler books **provisional** blocks where the prep almost certainly is,
using each teacher's learned pattern and your usual estimate for them.

They appear in the calendar as `Prep: Archer - expected prep (not yet in
Teams)`, so nothing is ambiguous, and the next successful capture withdraws
every one of them and replaces it with the real thing.

It is deliberately cautious:

- a teacher needs **three** past preps before any pattern is used
- and must be **60% regular** about their deadline day, or they are skipped
  entirely — inventing a pattern would book prep on days at random
- a prediction landing within a day of a deadline you already know about is
  dropped, because the real one is better in every way
- guesses obey the same lesson clashes, blackout times and daily limit as
  real prep
- the horizon is 10 days, not 21: a guess about Thursday is worth something,
  a guess about a fortnight away is not

A wrong provisional block costs you an hour in the diary you can ignore.
Scheduling nothing costs you the prep.

## Testing it: three phases

Each phase answers a different question, and they are separate so that a
failure tells you *where* the problem is rather than just that there is one.

```bash
make phase-a                       # or: python -m prep.phases A
make phase-b CONFIG=config.yaml
make phase-c CONFIG=config.yaml
make phases  CONFIG=config.yaml    # all three, stopping at the first failure
```

| | Question | Needs | Takes |
|---|---|---|---|
| **A** | Is the logic right? | nothing at all | ~5 s |
| **B** | Do the credentials work, and what is due? | Teams session, iCloud password, feed key | ~1 min |
| **C** | Do the parts work together? | everything in B | ~1 min |

**Phase A** runs on invented data — six made-up teachers across three shared
classes, eight weeks of history, a class that finished last year, one prep
whose author Teams never sent, and a diary full of lessons and rehearsals. No
config file, no password, no network, no browser. It checks the rules you
actually care about: 30 minutes a day capped at **120 per prep, not per week**;
a freak 300-minute evening damped rather than averaged in; a shared class split
correctly by who set the prep; an archived class never scheduled; nothing
booked over a lesson; no evening over the daily limit; every session finished
before its deadline. It ends by running the offline unit suite.

If Phase A fails, **the code is wrong** — nothing about your account, your wifi
or your school's settings can be blamed. That is the whole point of it needing
nothing.

**Phase B** signs in, captures, and prints your current prep: what is due, when,
who set it, how long it should take and how well-founded that estimate is. Then
it tests the iCloud connection, the timetable feed and your alerting. It reads
your database but never writes to it, and it never touches the calendar.

**Phase C** runs the real chain end to end — capture, parse, resolve teachers,
check archiving, merge the timetable, estimate, schedule, store, export, boot
the dashboard — and then cross-checks the parts against each other: the
calendar against the scheduler, the database against both, the daily totals
against the limit. It is a dry run unless you pass `--write`.

Both B and C capture fresh prep every time, so a test always runs against what
Teams says now. `--no-refresh` reuses the last capture when you are offline —
and Phase B says plainly that it has *not* tested the session when you do.

If the calendar is unreachable, Phase C stops rather than reporting a schedule
— with no diary to read, any plan would ignore every lesson you have.

```bash
make test      # just the unit suite: ~170 tests, ~3s, no network, no credentials
```

`make test` fails **only when the code is wrong**. If it ever needed a Teams
session it would fail on a train, fail when the session expired, and a failure
would stop meaning anything.

---

## Troubleshooting

```bash
systemctl status prep-scrape.service
journalctl -u prep-scrape -n 100 --no-pager
curl -s 127.0.0.1:8080/healthz
cat ~/prep/alerts.json          # problems alerted and not yet fixed
```

| Symptom | Cause | Fix |
|---|---|---|
| "sign-in needed" alert | Teams session expired | Redo step 5 |
| "sign-in needed" a few days after every sign-in | Third-party cookies blocked, so Teams cannot renew its own access | `prep.session` says `third-party BLOCKED`; it is written into the profile on the next `prep.login` |
| No assignments found | `url_hints` wrong | Rerun step 6 with `--dump` |
| Fewer assignments than you have | The list was not scrolled to the end — Teams only fetches what it is about to show | `prep.diagnose --expect N`; raise `scroll_passes` and `max_settle_ms` |
| Assignments never reached at all | The app opened on chat and nothing in the rail matched "Assignments" | Check the screenshot in `dumps/`; set `assignment_labels` if your school renamed it |
| "calendar 'Prep' not found" | Not created, or not syncing | Redo step 2 |
| iCloud sign-in fails with a correct app-specific password | The authenticated request went out over HTTP/3 and the datagram was too big for the path | Already fixed — the client now forces TCP. If it recurs: `python -m prep.icloud --config config.yaml` |
| iCloud rejects a password you just exported | Whitespace or quotes travelled with the value | `prep.icloud` prints the shape of what was actually read, without printing the secret |
| Prep at silly times | Wrong `windows` | Edit config, rerun |
| "NO FREE SLOT" | Genuinely no time before the deadline | Widen windows, or accept it |
| Everything stopped silently | Machine slept | `systemctl list-timers` |
| Browser window opens blank | GPU compositing failing on this hardware | Nothing to do: `prep.login` walks down its render ladder until one draws, then prints the config lines to keep it |

A persistent problem alerts **once**, then stays quiet for as long as it
lasts. When a run finds it fixed, the Telegram alert is edited to "Fixed: ..."
(other channels get a short "Fixed" message), and the next failure alerts
straight away.

---

## What is deliberately not here

- **No credential storage.** The browser profile holds a session token, which is
  narrower than a password, expires, and is revocable. Your password is never
  written anywhere.
- **No cloud copy of the database.** Two sources of truth would need syncing
  back before they could affect scheduling.
- **Blackouts are not guessed.** If you cannot work at a time, tell the
  dashboard; nothing tries to infer it.

## Known limits

- Every calendar except Prep counts as immovable. CalDAV does not usefully
  expose per-event colour, so "this one is movable" needs `ignore_calendars`.
- Booked minutes slightly exceed the estimate, because the flow buffer is added
  on top of each later sitting rather than taken out of the total.
- Prep set on paper never appears in Teams, so it is never scheduled.
- Assignments has no URL of its own. The address bar stays on the app root
  wherever you are in Teams, so the capture navigates by clicking, as a person
  does, and confirms it worked by whether assignment JSON arrived.
- Subscribed ICS calendars are invisible to CalDAV, which is why the timetable
  feed is fetched directly.

## Licence

MIT. Do what you like with it.
