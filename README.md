# prep-pipeline-scheduler

Reads your homework ("prep") out of Microsoft Teams Assignments, works out how
long each piece will take you, and books it into your iCloud calendar around
lessons, rehearsals, travel and everything else already in your diary.

```
Teams (your own browser session)
   │  Playwright, on a schedule
   ▼
SQLite  ──►  estimator  ──►  scheduler  ──►  iCloud "Prep" calendar (CalDAV)
   ▲                             ▲
   │                             │ busy time
dashboard                school timetable (ICS feed)  +  your iCloud calendars
```

It runs on an always-on Linux machine at home: an hourly full run
(07:05–22:05), a five-minute recheck, and a small web dashboard where you
log how long prep took, mark sessions done or missed, and add things you
can't work around.

This page covers **what you need** and **how to set it up**. The full
installer reference is [INSTALL.md](INSTALL.md); the technical design is
[REFERENCE.md](REFERENCE.md); [PHASES.md](PHASES.md) explains every check.

---

## Minimum specs

### The computer

| | Minimum | Notes |
|---|---|---|
| **OS** | Linux with systemd: **Ubuntu 22.04 or 24.04** | Both are in use. The installer uses `apt` and systemd, so other Debian-based distributions may work but are untested. **macOS and Windows are not supported** by the installer |
| **CPU** | 64-bit **x86_64**, 2 cores | Runs fine on a 2009 Intel Core 2 Duo (2 cores, 2.66 GHz). ARM (e.g. Raspberry Pi) is untested: the installer warns, and you may need the Chromium snap instead of Playwright's |
| **RAM** | **4 GB** | Tested on a machine with 3.6 GB usable. Chromium opens Teams once an hour and needs the headroom; less is untested |
| **Disk** | **2 GB free** (5 GB recommended) | Measured: Playwright's Chromium ~660 MB, Python environment ~230 MB, Teams browser profile ~250 MB, profile snapshots ~170 MB, plus apt packages and logs |
| **Python** | **3.10** or newer | 3.10 is the oldest in use; the system Python on Ubuntu 22.04/24.04 is fine |
| **systemd** | any recent | **250+** only if you want `--encrypt-secrets` (Ubuntu 22.04 has 249, so not there) |
| **Power** | always on, never sleeping | A machine asleep at 07:05 looks exactly like a week with no prep. The installer turns sleep off |
| **Network** | always-on internet | Wired or reliable Wi-Fi. If the timetable feed can't be fetched, nothing is booked that run, on purpose |
| **Screen** | a desktop session, occasionally | Signing into Teams opens a real browser window: once at install, and again whenever Microsoft ends the session. A monitor, or VNC from another device ([deploy/remote-access.md](deploy/remote-access.md)) |
| **Access** | a user account with `sudo` | Run the installer as that user, not as root |

### Accounts and data

| You need | Why |
|---|---|
| A **Microsoft Teams for Education** account with the **Assignments** app | where the prep is read from. You sign in yourself in the browser window; your Microsoft password is never stored |
| An **iCloud account with two-factor authentication** and an **app-specific password** | bookings are written over CalDAV, which doesn't accept your normal Apple password. Make one at appleid.apple.com → Sign-In and Security → App-Specific Passwords |
| Two iCloud calendars: one for prep (e.g. **Prep**) and one called **Personal** | prep is booked into the first; the dashboard adds your other commitments to the second. Create both in the Calendar app first |
| Your **timetable as an ICS feed URL** | so prep is never booked over lessons. The setting is called `feeds.socs`, but any ICS link works. Without it the scheduler refuses to book |
| **For remote access (the default): a Cloudflare account and a domain** whose DNS is on Cloudflare | the dashboard is served through a Cloudflare Tunnel. The free plan is enough, but you need a domain. Don't have one? Use `--cert selfsigned` (home network only) or `--cert tailscale` |
| Optional: a **Gmail app password** or a **Telegram bot** | alerts when something breaks (e.g. the Teams sign-in has expired) |

UK-specific: travel times between places are worked out from **UK
postcodes** (looked up on api.postcodes.io). Elsewhere, enter journey times
by hand in `config.yaml` (`travel.times`) and turn `lookup_postcodes` off.

---

## Setting it up

Allow about 40 minutes, most of it unattended (package and browser
downloads). Do this **at the machine's own desktop**, not over SSH, because
step 5 opens a browser window.

### 1. Get the code

```bash
git clone https://github.com/la-reine-c/prep-pipeline-scheduler.git
cd prep-pipeline-scheduler
```

(No git? `sudo apt install git`, or download the ZIP from GitHub and unzip
it.)

### 2. Make your config

```bash
cp config.example.yaml config.yaml
chmod 600 config.yaml
```

Open `config.yaml` and set at least:

- `timezone`: e.g. `Europe/London`
- `calendar.prep_calendar`: the iCloud calendar prep goes into (the example
  says `Prep-Pi-Test`, a test calendar)
- `travel.home.postcode`, and `travel.school.name` / `postcode`
- `travel.times.home-school`: your journey in minutes, door to door
- `scheduling` → the evening and weekend windows you're willing to work

Every setting is explained in the comments in the file.

### 3. Put your secrets in `.env`

Create `.env` next to `install.sh` (the installer copies it into place):

```bash
cat > .env <<'EOF'
ICLOUD_USER=you@icloud.com
ICLOUD_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
SOCS_FEED_URL=https://your-school-timetable-feed.example/...
CLOUDFLARE_TUNNEL_TOKEN=
EOF
chmod 600 .env
```

The dashboard token is generated for you. Email or Telegram alert settings
also go here (see [LINUX.md](LINUX.md), section 3). **Never commit `.env`
or `config.yaml`**: `.gitignore` already excludes them.

### 4. Set up the Cloudflare Tunnel

In the Cloudflare dashboard: **Zero Trust → Networks → Tunnels → Create a
tunnel** (Cloudflared). Copy its token into `CLOUDFLARE_TUNNEL_TOKEN` in
`.env`. Give it a public hostname such as `prep.your-domain.com`, pointing at
**HTTP `localhost:8088`**. Then add a **Cloudflare Access** application for
that hostname that lets in only your own email address.

Step by step: [INSTALL.md → Cloudflare
Tunnel](INSTALL.md#cloudflare-tunnel-the-default). You can also skip this for
now: the installer carries on and lists it under "Still to do".

### 5. Run the installer

See what it will do first, then do it:

```bash
./install.sh --dry-run --prep-calendar Prep --host prep.your-domain.com
./install.sh --skip check --prep-calendar Prep --host prep.your-domain.com
```

`--skip check` is needed for now; see [Known issues](#known-issues). Run it as
your normal user, **not** with `sudo`: it asks for your password and uses
sudo only where needed.

During the run a browser window opens on Microsoft's sign-in page. **Sign
in, tick "Stay signed in", open Assignments and scroll to the bottom of the
list.** Then close the window.

The installer then checks your real accounts against today's data (Phase B
and C: nothing is written to iCloud yet), and starts the schedule **only if
those checks pass**.

### 6. Check the logic

Because step 5 skipped the built-in self-test, run it without the unit-test
suite:

```bash
cd ~/prep
.venv/bin/python -m prep.phases A --no-unit-tests
```

It should end with "Passed" (warnings are fine).

### 7. Open the dashboard

The installer prints your link at the end. It looks like
`https://prep.your-domain.com/?token=...`. Open it once in each browser you
use; a cookie remembers the token after that. **Treat that link like a
password.**

Then read the **Still to do** list the installer printed. Usually it says to
name your teachers (`python -m prep.setup --set-teachers`) and test your
alerts.

---

## Day to day

```bash
cd ~/prep
make refresh                              # a full run now
make recheck                              # the five-minute check now
systemctl list-timers 'prep-*'            # when each runs next
journalctl -u prep-scrape -n 100 --no-pager
```

When Teams asks you to sign in again (you'll get an alert), on the machine's
screen or over VNC:

```bash
cd ~/prep && .venv/bin/python -m prep.login --config config.yaml
```

Troubleshooting is in [INSTALL.md → When something
breaks](INSTALL.md#when-something-breaks).

---

## Known issues

- **About 40 unit tests fail.** Most test scheduling features that were
  added and then removed again. A few in `tests/test_recheck.py` depend on
  today's date. The installer's Phase A runs the whole suite and stops at
  `A.33 Unit tests`, which is why step 5 uses `--skip check` and step 6 runs
  Phase A with `--no-unit-tests`. Everything else in Phase A passes.
- **One plan at a time.** Planning holds a lock file
  (`prep.sqlite3.lock`). If you script against the code, don't call
  `pipeline.replan_all()` or `pipeline.reschedule()` inside
  `pipeline.planning_lock()`: they take the lock themselves, so the call
  waits on itself forever and every scheduled run behind it times out.
- **Teams changes its page often.** If a capture finds fewer assignments
  than you expect, run `python -m prep.diagnose --config config.yaml
  --expect N`.
- **Schools differ.** The school name, timetable feed, Teams tenant and
  teacher names are all yours to set; nothing about any particular school is
  built in.

---

## Layout

| Path | What it is |
|---|---|
| `prep/` | the application: capture, estimator, scheduler, dashboard |
| `tests/` | the pytest suite (`pytest -q`) |
| `install.sh` | one-shot installer for Ubuntu |
| `systemd/` | the dashboard service and the two timers |
| `deploy/` | nginx configs (Cloudflare Tunnel and direct HTTPS), credentials, remote access |
| `config.example.yaml` | every setting, with comments |
