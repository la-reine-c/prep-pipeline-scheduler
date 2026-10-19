# Testing it in phases

Three phases, each answering a different question. Run them in order: a later
phase is meaningless if an earlier one failed.

| | Question it answers | Needs | Takes |
|---|---|---|---|
| **A** | Is the logic right? | nothing at all | ~5 seconds |
| **B** | Do the credentials work, and what is due? | Teams session, iCloud app password, feed key | ~1 minute |
| **C** | Do the parts work together? | everything Phase B needs | ~1 minute |

---

## Setup on Ubuntu

The same on the desktop and on the mini — both run Ubuntu (24.04 tested).

```bash
cd prep-pipeline/1-test-on-ubuntu      # or 2-deploy-on-mini on the mini

sudo apt install -y python3-venv python3-pip
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium
```

`--with-deps` installs the libraries Chromium needs, through `sudo apt`. On a
desktop they are usually there already; on a minimal server install they are
not, and the symptom is a browser that "exits unexpectedly" with nothing
useful in the message.

If pip refuses with `externally-managed-environment`, you skipped the venv.
Ubuntu protects its own Python; use the venv rather than
`--break-system-packages`.

### If the venv will not create

```
The virtual environment was not created successfully because ensurepip is
not available
```

means `python3-venv` is not installed — `sudo apt install -y python3-venv`.
Without sudo, build it without pip and let the system pip fill it in:

```bash
python3 -m venv --without-pip .venv
pip3 --python .venv/bin/python install pip
.venv/bin/python -m pip install -r requirements.txt
```

A venv is tied to the interpreter that built it. One copied from another
machine — Kali's Python 3.14 onto Ubuntu's 3.12, say — does not work; delete
it and build a new one.

### Phase A needs almost nothing

**Phase A's only third-party import is PyYAML.** Playwright, caldav, requests
and icalendar are all imported lazily, inside the functions that use them, so
none of them load on a run that never touches a browser or a network. If the
venv is fighting you, this gets you testing immediately:

```bash
sudo apt install -y python3-yaml
python3 -m prep.phases A --no-unit-tests
```

Add `python3-flask python3-pytest` and drop the flag to run the unit suite as
well. Phase B and C do need the full install, because they really do open a
browser and talk to iCloud.

### The browser: Playwright's own, not the snap

Phase B and C need Chromium and a config file:

```bash
cp config.example.yaml config.yaml
$EDITOR config.yaml                  # leave chromium_path empty
```

**Leave `chromium_path` empty** and Playwright's own build is used — the one
the install step above downloaded. Ubuntu has no Chromium in apt:
`apt install chromium` has no installation candidate, and `chromium-browser`
is a stub that installs the snap. The snap does work, with
`chromium_path: /snap/bin/chromium`, but it is confined, and measured on
24.04 it fails in two ways you would not guess:

- **profile under `/tmp`** — it launches, reports success, and writes the
  profile into the snap's private `/tmp`. You sign in, and the session never
  reaches the folder the pipeline reads. Every run starts logged out.
- **profile in a hidden folder** (`~/.anything`) — it refuses to start with
  *profile is already in use*, sending you looking for a second browser that
  does not exist.

Both are refused up front now, with the reason. A plain folder in your home
directory, like the default `~/prep/profile`, is fine either way.

Then sign into Teams once, in the profile the scraper will use:

```bash
python3 -m prep.login --config config.yaml
```

This opens a visible Chromium using the same profile directory, the same
browser and the same flags as the scraper, all read from your config. Sign in,
**tick "Stay signed in"**, open Assignments, **scroll to the bottom of the
list**, then press Enter. It reopens headless afterwards and checks the
session survived — which is the only thing that matters, and is not what you
tested by watching a window.

**If the window opens blank, it fixes itself.** `prep.login` works down a
ladder of render settings — `software` (the default), then `swiftshader`, then
`x11` on Linux — reopening the window until one actually draws, and prints the
`config.yaml` lines to make the working one permanent. If nothing drew it
drops a rung on its own; if something drew it asks you once whether you can
see it, because a screenshot is captured through a different path from the
window on screen and only you can tell. A 3060Ti under X, an ARM box on a
Mac's GPU, a VM with no GPU: same command, no flags.

Two things about the window are worth knowing before they confuse you:

**The address bar does not change when you reach Assignments.** Teams opens on
chat, and on `teams.cloud.microsoft` the URL stays on the app root however far
into your prep you click. `assignments_url` only decides where the app
*starts*. Getting to the list is a click — the same one you make — and the
capture makes it for itself, then confirms it worked by whether assignment
JSON arrived rather than by reading the address.

**Scroll to the end of the list.** Teams virtualises it: prep that has never
been scrolled into view has never been requested, so it cannot be seen here.
The capture scrolls for itself too, and keeps scrolling while the page still
moves rather than stopping at a fixed number of passes.

### Third-party cookies

`prep.login` prints a line about these before it opens the window, and it is
worth understanding, because blocking them produces a failure that arrives
days after the mistake:

```
cookies:  third-party allowed (the browser profile)
```

Teams renews its own access in a hidden iframe on `login.microsoftonline.com`,
which is a **third party** to the Teams page around it. Blocked, that renewal
fails silently. The sign-in works, prep is captured, the cookie is written —
and then a few days later every run reports a dead session while that cookie
is still perfectly valid. Signing in again buys another few days, which is
what hides the cause.

The setting is written into your profile before Chromium starts — the same
one the browser's own Settings page writes, in the one profile that exists to
read your own assignments. A machine-wide policy in
`/etc/chromium/policies/managed/` beats it, and is reported rather than
written over.

It needs a desktop session. Over SSH, use `ssh -X`.

**Ticking "Stay signed in" is the single thing that decides whether this runs
unattended.** Without it Microsoft issues a cookie that dies when the browser
closes, and every scheduled run starts logged out — and you cannot tell by
looking, because a window showing your assignments looks identical either
way. `prep.login` checks afterwards and says which you got. To ask again
later:

```bash
python3 -m prep.session --config config.yaml
python3 -m prep.session --config config.yaml --verbose   # cookie names, no values
```

Once it says `persistent`, take a snapshot:

```bash
python3 -m prep.session --config config.yaml --snapshot
```

Most lost sessions are not lost to authentication. A bad shutdown, a full
disk or a Chromium upgrade that rewrites the profile costs you a sign-in for
a purely mechanical reason. A snapshot turns that into `--restore`. It
refuses to snapshot a session-only or expired profile, because restoring one
would look like it worked and then fail at the next run.

A snapshot holds a live session cookie, so it is as sensitive as the profile
itself: 0700, same machine, never committed.

### Signing in again when you are not at the machine

The session will occasionally die while you are somewhere else. The answer is
to reach the machine's screen, not to re-type your password into something
else:

1. **The Cloudflare Tunnel** the installer sets up, with Cloudflare Access in
   front so only you can get through it.
2. **A VNC server on the machine** (`x11vnc` with noVNC), bound to the
   loopback and reached only through the tunnel at `/screen/`, never
   directly from the internet (`deploy/remote-access.md`).
3. From your phone, connect and run `python3 -m prep.login --config
   config.yaml`. You sign in **on Microsoft's own page**, in the real browser,
   on the machine that needs the session.

Your password goes from your fingers into Microsoft's login form and nowhere
else. It never passes through this code, this dashboard, or any file.

**The dashboard will never ask for your school username or password.** It
tells you the session needs attention and gives you the command; it does not
take credentials. Any page that looks like this one and asks for them is not
this one — that is worth knowing precisely because you are the person who
would otherwise be tempted to build it.

---

## Phase A — the logic, on invented data

```bash
python3 -m prep.phases A
```

**No config file. No password. No network. No browser.** If this needs
anything from you, that is itself a bug.

It builds a world that looks like yours but is entirely made up: six invented
teachers across three shared classes, eight weeks of history, a class that
finished last year, a class silent for five months, one prep whose author
Teams never sent, and a diary full of lessons, orchestra and a Saturday
fixture. Then it checks the things that actually matter:

- every prep parsed, with a real id rather than a fallback key
- a **shared class split into two teachers** by who set each prep — the thing
  a class GUID cannot do
- a prep with no creator left unresolved rather than guessed at
- a class silent for months **flagged for you to confirm**, never archived on
  its own
- the same teacher mapped twice **caught** (a duplicate is planted on purpose)
- 30 minutes a day, **capped at 120 per prep — not 120 a week**
- a freak 300-minute evening **damped, not averaged in**
- the archived class never scheduled; prep past its deadline skipped
- nothing booked over a lesson or rehearsal
- no evening over the daily limit; every session finished before its deadline
- a time you said you cannot work left free
- the database agreeing with what was booked, and a second run finding nothing
  new

It finishes by running the offline unit suite (~170 tests). `--no-unit-tests`
skips that; `--quiet` drops the schedule listing.

**No warnings expected.** The dummy diary is deliberately tight. Before
partial sittings and work-only daily limits, one prep only got part of its
time, and Phase A warned about it. It now fits.
If a change ever brings a warning back:

```
[warn] Every prep got all the time it needs - 1 placed short
       FFF Prep 10: 30 of 79 min - the diary ran out before the deadline
```

that is the check working. A prep that finds *some* time looks like a
success in any count of what was scheduled, and it is the quiet way a deadline
gets missed — so it gets its own line.

**If Phase A fails, the code is wrong.** Not your session, not your wifi, not
your school's settings. That is why it is built to need nothing.

---

## Phase B — the credentials, and what is due right now

```bash
python3 -m prep.phases B --config config.yaml
```

Opens Chromium, gets itself to the Assignments view with your saved session,
and captures what Teams sends. Three of its checks are about *how* it got
there, because the payloads alone cannot tell you and each one is a way a
capture comes back short while looking perfectly healthy:

```
[ ok ] Reached the Assignments view - clicked through to it (treeitem)
[ ok ] Assignment list seen to the end - 6 scroll(s), it would go no further
[ ok ] Assignment data had stopped arriving - quiet for the last 14s
```

- **Reached the Assignments view** says which route worked: the app opened
  straight onto it, a click got there (and whether that click was in the
  rail, its "More apps" overflow, or inside a frame), a **rail shortcut**
  (`Ctrl+1`…`Ctrl+9` jump to the Nth app in the rail — it tries each and
  watches for assignment data, so nobody has to remember which number theirs
  is), or the URL fragment had to be re-applied. Clicking is the normal
  answer in a window you can see; the keyboard is what rescues a **headless**
  run, where the rail is often present but not as anything clickable.
- **Assignment list seen to the end** is asked of the page, not assumed: the
  capture scrolls until nothing moves. A WARN here means prep below that
  point was never requested — and would be missing with no other sign.
- **Assignment data had stopped arriving** distinguishes "finished" from
  "ran out of time", which look identical in the result.

Then it prints your current prep:

```
       due                  in  teacher        est  basis     title
       -------------------------------------------------------------------
       Mon 28 Sep 09:00     2d  Barratt        94m  tracked   BBB Prep 9
       Wed 30 Sep 09:00     4d  Dalton        76m  tracked   DDD Prep 9
       Fri 02 Oct 09:00     6d  ?             120m  pattern   Read chapter 4
```

`basis` is how well-founded the estimate is:

| | meaning |
|---|---|
| `corrected` | you set this one by hand |
| `tracked` | enough of your own timings for the filter to follow this teacher |
| `measured` | five or more timings, averaged |
| `shrunk` | a few timings, pulled towards the overall average |
| `global` | no timings for this teacher, using everyone's average |
| `pattern` | no timings at all — the min(30 × days, 120) guess |

Everything starting at `pattern` is normal on a new install. It improves as
you log how long things really took.

Then it checks iCloud, the timetable feed and your alerting. It **reads** your
database and **writes nothing anywhere** — not to the database, not to the
calendar.

Add `--test-alerts` to actually send a test message on every configured
channel, so you know what a dead session will look like when it happens.

### If Phase B fails

| Line | What it means |
|---|---|
| `Teams session alive - redirected to sign-in` | The saved session expired. Run the browser once with `headless: false`, sign in, tick "Stay signed in". |
| `Reached the Assignments view - never reached it` | The app opened on chat and nothing in the rail, its overflow or the frames was named like Assignments. Look at the screenshot in your dump directory; if your school renamed the app, put its name in `assignment_labels`. |
| `Assignment list seen to the end - still scrolling` | The capture stopped with more list below it, so that prep was never requested. Raise `max_settle_ms`, and `scroll_passes` with it. |
| `Assignment data had stopped arriving - gave up ... still coming` | The clock ran out mid-load. This count is short; raise `max_settle_ms` and watch it with `prep.diagnose`. |
| `Third-party cookies allowed - BLOCKED` | Teams cannot renew its own access, so this session will start failing within days. `prep.login` writes the setting; a *managed policy* has to be changed by hand. |
| `no assignment payloads captured` | Either the app never left chat, or the page loaded and nothing matched `url_hints`. A screenshot of what the browser saw is saved to your dump directory — look at it first. Run `python3 -m prep.inspect` to see what the tenant actually sends. |
| Fewer prep than you know you have | The list was not scrolled to its end. `python3 -m prep.diagnose --config config.yaml --expect N` says whether it was still moving when the clock ran out. |
| Chromium window opens but stays blank | GPU acceleration failing | `disable_gpu: true` under `teams:` — it is the default, so check it was not turned off |
| `Calendar sign-in accepted - FAIL` | iCloud answered and **refused the credentials**. Either you used your ordinary Apple password (it cannot work over CalDAV), or the value picked up whitespace or quotes, or the app-specific password belongs to a different Apple ID. `python3 -m prep.icloud --config config.yaml` says which. |
| `Calendar reachable - FAIL` | iCloud **never answered**. This says nothing about your password — do not go and generate another one. It was already retried; check the network, then run `prep.icloud`. |
| `Calendar reachable - calendar 'Prep' not found` | The sign-in worked. Create the calendar in the Calendar app with exactly that name and let it sync. |
| `Busy time found in the next week - 0 blocks` | Normal if your timetable is a *subscribed* calendar — CalDAV cannot see those. That is what the ICS feed is for. |
| `Teams sends who set each prep - 0 of N` | Your tenant does not send `createdBy`. Teachers who share a class cannot then be told apart; `prep.inspect` says what is available instead. |

`--no-refresh` reuses the last capture. Phase B will then say
`Teams session alive - NOT TESTED - read from disk`, because reading a file
proves nothing about your credentials, which is the entire job of this phase.

---

## Phase C — everything together

```bash
python3 -m prep.phases C --config config.yaml
```

Runs the real chain end to end:

```
capture → parse → resolve teachers → archive checks → timetable feed
        → estimate → schedule → store → export → dashboard
```

and then cross-checks the parts **against each other**, which is the bit that
catches integration bugs rather than logic bugs:

- the calendar was told about exactly the sessions the scheduler planned
- the database recorded every booking the calendar was given
- no evening exceeds the daily limit once *everything already scheduled* is
  counted, not just this run's work
- the dashboard renders, and refuses a request with no token
- the reschedule report is small enough to survive Telegram's 4096-character
  limit, or gets truncated at a line boundary rather than failing to send

It is a **dry run** unless you pass `--write`. A dry run still reads your real
diary, so the plan it prints is the plan you would actually get.

If the calendar cannot be reached, Phase C **stops** instead of printing a
schedule. With no diary to read, any plan would ignore every lesson you have,
and a confident wrong answer is worse than none.

---

## All three

```bash
python3 -m prep.phases all --config config.yaml
```

Stops at the first phase that fails, because a later one would only repeat the
same failure. A *warning* does not stop it.

## Exit codes

`0` passed, possibly with warnings. `1` something failed. `2` you asked for
Phase B or C without `--config`.
