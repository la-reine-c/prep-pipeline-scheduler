# Encrypting the secrets this machine must hold

Some secrets genuinely have to live on the mini, because nothing is there to
type them at 3am:

- your iCloud app-specific password
- the timetable feed URL (it contains a personal key)
- the Telegram bot token, if you use one
- the dashboard token

Right now they sit in `.env` in plaintext, readable by every process running
as you, and one mistaken `cat` away from a screenshot. `systemd-creds` is
better, and it is already on the machine.

> **Your Teams password is not on that list and never will be.** These are
> secrets with no human-present alternative. Signing into Teams has one — you,
> on Microsoft's own page — which is why it is handled differently.

---

## What it actually protects against

Be clear about this, because it is easy to over-read.

`systemd-creds` encrypts each secret and decrypts it at service start into a
private tmpfs that **only that service** can read. With a TPM2 present the
key is sealed to the hardware, so the encrypted file is useless on another
machine or under another OS.

**It defends against:** the disk being read elsewhere, a backup leaking, a
stolen machine, another user on the box, and a stray `cat .env`.

**It does not defend against:** something that compromises the running
service. systemd's own design splits the key between the TPM and
`/var/lib/systemd/credential.secret`, and once the unit is running the
decrypted value is readable by that unit — which is the whole point of it
being decrypted at all.

So it is a real improvement in hygiene and blast radius, not a magic box. That
distinction is exactly why it is right for these secrets and still wrong for a
Teams password.

---

## Do you have a TPM2?

```bash
systemd-creds has-tpm2
ls /dev/tpm*
```

`yes` means secrets get sealed to the hardware. If it says `no` — which is
likely on a Mac mini, since neither the T2 nor Apple Silicon's Secure Enclave
is exposed to Linux as a TPM — `systemd-creds` still works, using a host key
at `/var/lib/systemd/credential.secret` (mode 0600, root-only). That is
weaker than TPM sealing but still better than `.env`: the secret is no longer
readable by your ordinary user processes, only by root and by the service it
was issued to.

---

## Encrypting a secret

```bash
# --name must match what the unit loads it as
sudo systemd-creds encrypt --name=ICLOUD_APP_PASSWORD - \
    /etc/prep/creds/icloud-app-password.cred
# type the password, then Ctrl-D
```

Repeat for each one:

| Credential name | What it is |
|---|---|
| `ICLOUD_USER` | your iCloud address |
| `ICLOUD_APP_PASSWORD` | the app-specific password from appleid.apple.com |
| `SOCS_FEED_URL` | the timetable feed, key and all |
| `PREP_WEB_TOKEN` | the dashboard token |
| `TELEGRAM_BOT_TOKEN` | optional |

```bash
sudo chmod 700 /etc/prep/creds
```

## Loading them in the unit

`./install.sh --encrypt-secrets --only secrets,systemd` does all of this,
including the unit lines and the config. By hand, add to
`prep-scrape.service`, `prep-recheck.service` and `prep-web.service` - one
line per credential you actually created, because a unit naming a missing
one will not start:

```ini
[Service]
LoadCredentialEncrypted=ICLOUD_USER:/etc/prep/creds/icloud-user.cred
LoadCredentialEncrypted=ICLOUD_APP_PASSWORD:/etc/prep/creds/icloud-app-password.cred
LoadCredentialEncrypted=SOCS_FEED_URL:/etc/prep/creds/socs-feed-url.cred
LoadCredentialEncrypted=PREP_WEB_TOKEN:/etc/prep/creds/prep-web-token.cred
```

## Pointing the config at them

In `config.yaml`, swap `env:` for `cred:`:

```yaml
calendar:
  caldav_url: https://caldav.icloud.com/
  username: cred:ICLOUD_USER
  password: cred:ICLOUD_APP_PASSWORD
  prep_calendar: Prep

feeds:
  socs: cred:SOCS_FEED_URL

web:
  token: cred:PREP_WEB_TOKEN
```

Then delete `.env`.

`cred:` only resolves when systemd started the process, so running by hand
gives you a clear error naming the unit setting rather than a mysterious
authentication failure. Keep `env:` for anything you run manually.

```bash
sudo systemctl daemon-reload
sudo systemctl restart prep-web
python3 -m prep.phases B --config config.yaml   # confirm it still signs in
```
