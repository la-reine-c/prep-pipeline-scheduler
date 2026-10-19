# Installing on the mini (Intel Mac mini, Ubuntu 24.04)

One script does the whole install: `install.sh`. This page says what it
does, which options you might need, and what is still yours to do
afterwards. Every step is listed with the commands it runs, so nothing
happens that you could not have typed yourself.

Test on the Ubuntu desktop first (`../1-test-on-ubuntu/`). If Phase A fails
there, the code is wrong, and you don't want to find that out while also
fighting systemd.

---

## Quick start

Copy this folder to the mini, then **on the mini's own desktop** (not over
SSH, because signing into Teams opens a browser window):

```bash
cd 2-deploy-on-mini
./install.sh --dry-run --prep-calendar Prep     # read what it would do
./install.sh --prep-calendar Prep               # do it
```

Run it as **your normal user, not with sudo**. It asks for your password
once and uses sudo only for the steps that need root. Allow about 40
minutes, most of it unattended (apt, pip and the Chromium download).

It stops at the first thing that is wrong and says what to fix. Fix it and
run the same command again: every step is safe to repeat, and anything
already done is left alone.

---

## Before you run it

| You need | Why | Where it goes |
|---|---|---|
| `config.yaml` in this folder, filled in | the script installs it; it never invents your settings | already here; check the `REQUIRED` lines |
| An iCloud **app-specific** password | your normal Apple password does not work over CalDAV | `calendar.password` (appleid.apple.com → Sign-In and Security → App-Specific Passwords) |
| The timetable feed URL | without it prep gets booked through lessons, so the scheduler refuses to run | `feeds.socs` |
| A calendar for prep, and one called **Personal**, in iCloud | prep is booked into the first; the dashboard adds outside lessons to the second | create both in the Calendar app first |
| `teachers.json` from the desktop (optional) | saves naming every teacher again | `--teachers ~/teachers.json` |
| Tailscale on your phone (optional) | reaching the dashboard from outside the house | `--cert tailscale --install-tailscale` |

> **`config.yaml` currently books into `Prep-Pi-Test`.** That is the test
> calendar. Pass `--prep-calendar Prep` (or whatever your real one is
> called) when you are ready for real bookings. The script warns until you
> do.

---

## The options

All optional. With none at all it installs into `~/prep`, uses a
self-signed certificate and keeps your secrets where `config.yaml` has them.

| Option | Default | What it does |
|---|---|---|
| `--prep-calendar NAME` | whatever `config.yaml` says | sets `calendar.prep_calendar`. The calendar must already exist in iCloud |
| `--teachers FILE` | none | copies your `teachers.json` into place |
| `--prep-home DIR` | `~/prep` | where code, database, browser profile and `teachers.json` live |
| `--web-port PORT` | `8080` | the loopback port gunicorn listens on. Change it if the Wake-on-LAN service already holds 8080 |
| `--cert MODE` | `selfsigned` | HTTPS for the dashboard (see below) |
| `--host NAME` | Tailscale name, or this machine's hostname | the name on the certificate, and the address you type into the browser |
| `--lan-ip IP` | detected | the address put into a self-signed certificate |
| `--install-tailscale` | off | installs Tailscale and runs `tailscale up --ssh` (prints a sign-in link) |
| `--encrypt-secrets` | off | moves secrets out of `config.yaml` into systemd encrypted credentials (see below) |
| `--force-timers` | off | starts the schedule even if sign-in or the checks didn't pass. Not recommended |
| `--only STEP,STEP` | all | runs just these steps, e.g. `--only verify,systemd` |
| `--skip STEP,STEP` | none | runs everything except these |
| `--dry-run` | off | prints every command and changes nothing |
| `--yes` | off | doesn't stop to ask (the Teams sign-in still needs you there) |

### Picking `--cert`

| Mode | Use it when | What you get |
|---|---|---|
| `selfsigned` | you only use the dashboard at home | works immediately, but each browser warns once. Encryption without proof of identity |
| `tailscale` | you want it from your phone, anywhere | a real Let's Encrypt certificate for `your-mini.your-tailnet.ts.net`, renewed monthly by cron. First enable HTTPS in the Tailscale admin console (DNS → HTTPS Certificates). Sign in with a **personal** account, and never run Tailscale on the school network |
| `existing` | you already have a certificate | it uses `/etc/ssl/prep/fullchain.pem` and `privkey.pem` |
| `none` | you don't want nginx at all | the dashboard is reachable only from the mini itself |

`deploy/README.md` has the longer version, including Cloudflare Tunnel.

### `--encrypt-secrets`, and when to use it

Without it, your iCloud password, feed URL and dashboard token stay in
`config.yaml` (mode 600, so only you and root can read it). That works.

With it, after the checks pass, the script runs
`deploy/setup-credentials.sh`. That asks for each secret (hidden as you
type) and encrypts it into `/etc/prep/creds`. The script then rewrites those
lines of `config.yaml` to `cred:NAME`. systemd decrypts them into a private
area only the prep services can read. The old file is kept as
`config.yaml.before-encrypt` until you delete it.

The catch: `cred:` values exist only inside the services, so commands you
run by hand (`make phase-c`, `prep.login`, `prep.session`) can no longer read
them. Get everything working first, then encrypt:

```bash
./install.sh --encrypt-secrets --only secrets,systemd
```

`deploy/systemd-creds.md` covers what this does and doesn't protect against.

---

## What it does, step by step

Each step's name is what `--only` and `--skip` take.

| # | Step | What happens | Commands it runs |
|---|---|---|---|
| 1 | `preflight` | checks it isn't running as root, that this is the right folder, the OS, the CPU architecture, the systemd version, and that the dashboard port and 443 are free (or already ours). Asks for your sudo password | `sudo -v`, `ss -tlnp` |
| 2 | `packages` | installs the system packages. Chromium is deliberately left out: Ubuntu has no such apt package, and naming it makes apt refuse the whole line | `apt-get install python3-venv python3-pip util-linux rsync openssl curl nginx` |
| 3 | `copy` | copies the code into `--prep-home`. **Never deletes anything there and never overwrites an existing `config.yaml`**, so your database, browser profile and settings survive a re-install | `rsync -a` (excluding `config.yaml`, `.env`, `.venv`), `install -m 600 config.yaml` |
| 4 | `python` | creates `.venv` and installs `requirements.txt`. Rebuilds a venv left without pip | `python3 -m venv`, `pip install -r requirements.txt` |
| 5 | `browser` | installs Playwright's own Chromium and its system libraries | `python -m playwright install --with-deps chromium` |
| 6 | `config` | `chmod 600`; binds the dashboard to `127.0.0.1:PORT`; sets the prep calendar; generates a 43-character dashboard token if there is none; checks the required values are filled in; copies `teachers.json` | `deploy/configedit.py` (changes single lines and keeps every comment) |
| 7 | `check` | **Phase A**: the logic and the 499 offline tests on made-up data. No network, no passwords. If this fails it stops: the code is broken on this machine | `python -m prep.phases A` |
| 8 | `nosleep` | stops the mini sleeping. A machine asleep at 07:05 looks exactly like a week with no prep set | `systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target` |
| 9 | `signin` | if there is no persistent Teams session yet, opens Microsoft's sign-in page in a browser window. **Tick "Stay signed in", open Assignments, scroll to the bottom.** Then snapshots the profile. Over SSH there is no window, so it skips this and tells you how to do it later | `python -m prep.login`, `python -m prep.session --snapshot` |
| 10 | `verify` | **Phase B** (your real passwords and current prep) and **Phase C** (the whole chain against today's real diary). Phase C is a dry run: nothing goes to iCloud and the database is untouched | `python -m prep.phases B`, `python -m prep.phases C` |
| 11 | `secrets` | only with `--encrypt-secrets` (see above) | `sudo deploy/setup-credentials.sh` |
| 12 | `systemd` | installs five units with your user and paths filled in, plus one `LoadCredentialEncrypted=` line for each credential that actually exists (a unit naming a missing one refuses to start). Starts the dashboard. **Starts the timers only if sign-in and Phase B/C passed**; otherwise it tells you how to start them later | writes `/etc/systemd/system/prep-*.{service,timer}`, `systemctl enable --now` |
| 13 | `nginx` | gets the certificate, installs `deploy/nginx-prep.conf` with your host name and port, removes nginx's default site, tests the config and reloads it | `openssl req` or `tailscale cert`, `nginx -t`, `systemctl reload nginx` |
| 14 | `finish` | checks the dashboard answers directly and through nginx, lists the timers, prints your dashboard link, and lists what is still yours to do | `curl /healthz`, `systemctl list-timers` |

### What ends up running

| Unit | When | What it does |
|---|---|---|
| `prep-web.service` | always | the dashboard: gunicorn on `127.0.0.1:8080`, behind nginx, sandboxed to its own data |
| `prep-scrape.timer` → `prep-scrape.service` | hourly 07:05-22:05 (and on boot if a run was missed) | the full run: opens Teams, finds new or moved prep, estimates it, books it |
| `prep-recheck.timer` → `prep-recheck.service` | every 5 minutes | the recheck (below). Doesn't open Teams |
| `nginx` | always | HTTPS on 443, rate limits, and the token kept out of its logs |

---

## The five-minute recheck

Between full runs, `prep.recheck` checks whether the diary has gone stale,
and **re-plans only when one of these has happened**:

| Trigger | What counts | What it does |
|---|---|---|
| **Finished** | you logged how long a prep took | takes the rest of that prep's bookings out of the diary |
| **Missed** | a session ended over **24 hours** ago and you never ticked it, or you pressed **Missed** | stops counting that time as done and books it again before the deadline. Only that prep moves |
| **Timing** | a prep's estimate has moved by at least one 30-minute block since it was booked (you logged a time for that teacher, or corrected it by hand) | re-books that prep's future sessions to the new length |
| **Unplanned** | prep that was saved but never booked, because iCloud or the timetable was down on the run that found it | books it |

When nothing has happened, which is nearly every time, it reads the
database and stops: no network, and nothing moves. A booking clashing with
a lesson you added is also caught by the re-plan whenever one runs, and by
the full run regardless.

### The 24-hour backlog

The dashboard has a **Did you do these?** list: every session that has
ended and that you haven't confirmed. Each one has **Done** and **Missed**
buttons.

- For 24 hours after a session ends it **counts as done**, so you can be
  late confirming it and nothing is re-booked in the meantime.
- After 24 hours, an unconfirmed session counts as **missed**, and the next
  recheck books its time again.
- **Missed** books the time again straight away. **Done** just clears it
  from the list.
- Logging how long the whole prep took confirms every session of it at once.

Change the 24 hours with `scheduling.confirm_grace_hours` in `config.yaml`.

---

## After it finishes

The **Still to do** list at the end is the authoritative one. Typically:

1. **Sign into Teams**, if you ran it over SSH (step 9 was skipped). On the
   mini's screen, or over VNC from your phone (`deploy/remote-access.md`):
   ```bash
   cd ~/prep && .venv/bin/python -m prep.login --config config.yaml
   ./install.sh --only signin,verify,systemd     # checks it, then starts the timers
   ```
2. **Name your teachers**, if you didn't pass `--teachers`:
   ```bash
   cd ~/prep && .venv/bin/python -m prep.setup --config config.yaml --set-teachers
   ```
3. **Test your alerts**, so the first one you get isn't a real failure:
   ```bash
   cd ~/prep && .venv/bin/python -m prep.run --config config.yaml --test-alerts
   ```
   Setting up Telegram is in `LINUX.md`, section 3.
4. **Open the dashboard** from the printed link (it ends in `?token=...`)
   once in each browser. Treat that link like a password.
5. **Remote access** from your phone: `deploy/remote-access.md` (Tailscale
   SSH, plus noVNC for the one job that needs a screen).

---

## Running it

```bash
cd ~/prep
make refresh          # the full run now: capture, estimate, schedule
make recheck          # the five-minute check now
make phase-c          # check everything against today's real data, dry
make check-write      # the same, but actually writes
.venv/bin/python -m prep.session --config config.yaml   # how long the Teams session has left

sudo systemctl start prep-scrape          # a full run, as the timer does it
systemctl list-timers 'prep-*'            # when each runs next
journalctl -u prep-scrape -n 100 --no-pager
journalctl -u prep-recheck -n 50 --no-pager
journalctl -u prep-web -n 50 --no-pager
```

Dry runs (`--dry-run`, Phase C without `--write`) now work on a copy of the
database. Before this, a dry run recorded the prep it saw, so the first real
run found nothing new and booked nothing. Pretend `dry-run-*` sessions left
in an old database are cleared automatically the first time it is opened,
and the recheck then books that prep for real.

Upgrading an existing install needs nothing extra: run `./install.sh` again.
The database gains its new columns the first time it is opened, every
session already in the past counts as done (so your history isn't suddenly
"missed"), and your logged timings are kept.

## When something breaks

| Symptom | Cause | Fix |
|---|---|---|
| installer: `ensurepip is not available` | `python3-venv` missing | run the `packages` step, then re-run |
| installer: `port 8080 is already in use` | another service (Wake-on-LAN?) | `--web-port 8090` |
| installer: timers not started | sign-in or Phase B/C didn't pass | fix what it printed, then `./install.sh --only verify,systemd` |
| `prep-scrape` fails with status 243/CREDENTIALS | a unit names a credential file that isn't there | re-run `./install.sh --only systemd`: it lists only the ones that exist |
| "sign-in needed" alert | Teams session expired | `prep.login`, over VNC if you are out |
| "sign-in needed" a few days after every sign-in | third-party cookies blocked, so Teams cannot renew its access | `prep.session` says `third-party BLOCKED`; `prep.login` writes the setting, a *managed policy* needs changing by hand |
| `[FAIL] Calendar sign-in accepted` | iCloud answered and refused: ordinary Apple password, or whitespace/quotes around the value | `python3 -m prep.icloud --config config.yaml` |
| `[FAIL] Calendar reachable` | iCloud never answered — **not** a password problem | check the network; `prep.icloud` confirms which it is |
| Nothing scheduled, feed error | timetable feed unreachable | deliberate — it refuses to book prep through lessons. The recheck books it once the feed is back |
| Prep booked again that you did do | you didn't tick it within 24 hours | tick **Done** sooner, or raise `confirm_grace_hours` |
| Prep with no teacher | new class, or a teacher changed | `python3 -m prep.setup --config config.yaml --set-teachers` |
| Profile corrupted | bad shutdown, disk, Chromium upgrade | `prep.session --restore` |
| Opens on chat, not Assignments | normal — Teams has no URL for that view, so the capture clicks its way in | check the screenshot in `dumps/`; set `assignment_labels` if your school renamed the app |
| Fewer assignments than expected | the list was not scrolled to its end, the page had not finished, or a stale profile lock | `python3 -m prep.diagnose --config config.yaml --expect N` |
| Chromium opens but stays blank | GPU acceleration failing | `disable_gpu: true` under `teams:` — the default, so check it was not turned off |

Scrape job exit codes: **2** dead Teams session, **3** capture failed,
**4** calendar unreachable, **5** timetable feed failed so nothing was
scheduled on purpose.

A dead session doesn't mean an empty diary: prep is booked provisionally
from each teacher's usual pattern and corrected on the next real capture.

`LINUX.md` covers each part of the install in detail, `REFERENCE.md` is the
full technical document, and `PHASES.md` explains every check and failure
line.
