# Running on the Mac mini (Ubuntu)

`install.sh` does all of this for you, and `README.md` says what it does and
which options to pass; this is the detail behind each step, for doing one by
hand or understanding what went wrong. Both assume Ubuntu (24.04 tested) and
the code in `~/prep`, the same folder the data defaults to.

Replaces MACOS.md, which assumed macOS. On Linux you get systemd rather than
launchd, and iMessage is not an option — Telegram replaces it.

**Check your architecture first**, because it decides where Chromium comes
from:

```bash
uname -m
```

- `x86_64` — an Intel mini, which is what this one is. Playwright's own
  Chromium, installed below.
- anything else — if `playwright install` has no build for it, use Ubuntu's
  snap (`sudo snap install chromium`, `chromium_path: /snap/bin/chromium`)
  with the profile in a plain folder in your home directory.

Ubuntu has no Chromium in apt at all: `apt install chromium` has no
candidate, and `chromium-browser` only installs the snap.

---

## 1. Sit alongside Wake-on-LAN

The dashboard is served by gunicorn on `127.0.0.1:8080`, with nginx in front
of it on 443 (`deploy/README.md`). Check neither is already taken by the WoL
service:

```bash
sudo ss -tlnp | grep -E ':(443|8080)\b'
```

If 8080 is taken, change `--bind` in `systemd/prep-web.service` and
`proxy_pass` in `deploy/nginx-prep.conf` to the same free port. If something
else must own 443, give the dashboard a sub-path with `url_prefix: "/prep"`:
the app then serves only under `/prep` and 404s elsewhere, so it cannot shadow
the WoL routes. In `config.yaml`:

```yaml
web:
  host: 127.0.0.1
  port: 8080
  token: env:PREP_WEB_TOKEN
```

---

## 2. Install

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip util-linux

mkdir -p ~/prep
cp -r prep-pipeline/2-deploy-on-mini/. ~/prep/     # this folder's contents
cd ~/prep
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m playwright install --with-deps chromium
```

Leave `chromium_path: ""` in the config.

### Secrets

Two ways. Use the first.

**systemd encrypted credentials (preferred):**

```bash
sudo ./deploy/setup-credentials.sh
```

It prompts for each secret, encrypts it immediately, and prints the
`LoadCredentialEncrypted=` lines to paste into the units. Then use `cred:NAME`
in `config.yaml` instead of `env:NAME` and delete `.env`. Full walkthrough in
`deploy/systemd-creds.md`.

Your Teams password is not among these and never will be: these are secrets
with no human-present alternative, and signing into Teams has one.

**Or `.env` (`chmod 600`), if you want to get running first:**

```
ICLOUD_USER=you@icloud.com
ICLOUD_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
SOCS_FEED_URL=https://calendar.example-school.net/...
PREP_WEB_TOKEN=some-long-random-string
ALERT_EMAIL_USER=you@gmail.com
ALERT_EMAIL_APP_PW=abcd efgh ijkl mnop
ALERT_EMAIL_TO=you@gmail.com
TELEGRAM_BOT_TOKEN=123456:AA...
TELEGRAM_CHAT_ID=987654321
```

---

## 3. Alerts

**Email** — Gmail app password from myaccount.google.com → Security → 2-Step
Verification → App passwords. Not your account password.

**Telegram, instead of SMS.** Free, instant, and it reaches your phone. UK
carriers have mostly shut down email-to-SMS gateways, and a paid SMS service
is not worth it for a handful of alerts a term.

1. Message **@BotFather** on Telegram, send `/newbot`, follow the prompts.
   It gives you a token like `123456:AA...`
2. Send your new bot any message (it cannot message you first).
3. Get your chat id:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['result'][0]['message']['chat']['id'])"
```

Then prove the whole thing works before relying on it:

```bash
./.venv/bin/python -m prep.run --config config.yaml --test-alerts
```

It prints which channels are configured and sends a real alert on each.

---

## 4. Stop it sleeping

```bash
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
systemctl status sleep.target        # should show "masked"
```

If it is a laptop-style lid setup, also set `HandleLidSwitch=ignore` in
`/etc/systemd/logind.conf`.

A machine asleep at a scheduled time produces exactly the same output as a
week with no prep set, which is why the timer uses `Persistent=true` — a
missed run fires on the next boot.

---

## 5. Sign into Teams once

Needs a desktop session. If the mini is headless, use VNC or X forwarding.

```bash
./.venv/bin/python -m prep.login --config config.yaml
```

Not a hand-written one-liner: this uses the profile, browser and flags from
your config, and sets up the profile before launching — in particular it
**allows third-party cookies**, without which Teams cannot renew its own
access and this sign-in quietly stops working a few days from now.

Tick "Stay signed in", open Assignments, and scroll to the bottom of the list.
The address bar does not change when you reach Assignments; that is expected,
and the command watches the requests rather than the address.

If the window opens blank, `prep.login` works down its rendering ladder by
itself (`software`, `swiftshader`, `x11`) and prints the config lines that
make whichever one drew permanent.

---

## 6. systemd

`./install.sh --only systemd` does this step. By hand: the unit files contain
`PREP_USER` and `PREP_HOME` placeholders, substituted for your account.

**Delete any `LoadCredentialEncrypted=` line whose file is not in
`/etc/prep/creds`.** A unit naming a credential that is not there refuses to
start at all (status 243/CREDENTIALS). The installer writes only the lines
for credentials that exist.

```bash
cd ~/prep
for unit in systemd/prep-*.service systemd/prep-*.timer; do
  sed -e "s|PREP_USER|$(whoami)|g" -e "s|PREP_HOME|$HOME/prep|g" \
      "$unit" | sudo tee "/etc/systemd/system/$(basename "$unit")" >/dev/null
done

sudo systemctl daemon-reload
sudo systemctl enable --now prep-scrape.timer prep-recheck.timer prep-web.service
systemctl list-timers 'prep-*'
```

| Unit | Runs |
|---|---|
| `prep-scrape.timer` | the full run (`prep.run`) hourly from 07:05 to 22:05 |
| `prep-recheck.timer` | `prep.recheck` every five minutes: re-plans only finished, missed (24 h unconfirmed), re-estimated or unplanned prep, and does nothing at all otherwise |
| `prep-web.service` | the dashboard, gunicorn on the loopback |

Run it immediately rather than waiting:

```bash
sudo systemctl start prep-scrape.service
journalctl -u prep-scrape -n 50 --no-pager
```

---

## 7. Reaching it from school

Do not port-forward. `install.sh` connects a **Cloudflare Tunnel** by
default: an outbound connection from this machine, so nothing is opened on
your router. Put Cloudflare Access in front of it so only your own email
address gets through.

The dashboard is then `https://prep.your-domain.com/?token=...`. The setup
steps in the Cloudflare dashboard are in `INSTALL.md` under "Cloudflare
Tunnel (the default)".

---

## When something breaks

```bash
systemctl list-timers 'prep-*'              # when does each next run
systemctl status prep-scrape.service        # how did the last run end
journalctl -u prep-scrape -n 100 --no-pager
journalctl -u prep-recheck -n 50 --no-pager # what the 5-minute check moved
journalctl -u prep-web -n 50 --no-pager
sudo ss -tlnp | grep 8080                   # is gunicorn listening
curl -s 127.0.0.1:8080/healthz
cat ~/prep/alerts.json                      # problems alerted and not yet fixed
```

Exit codes from the scrape job: **2** a dead Teams session (redo step 5),
**3** capture failed, **4** calendar unreachable, **5** the timetable feed
failed so nothing was scheduled on purpose.
