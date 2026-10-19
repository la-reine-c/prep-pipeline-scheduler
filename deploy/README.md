# Serving the dashboard properly

Flask's built-in server is a development server. It is single-threaded, has no
request limits, and prints a warning saying so. Anything reachable beyond the
machine itself belongs behind nginx and gunicorn.

```
you  ──https──►  nginx  ──http──►  gunicorn  ──►  Flask app
                 :443            127.0.0.1:8080
```

gunicorn binds the loopback only. Nothing reaches it from the network without
going through nginx first — so if nginx is down the dashboard is unreachable,
rather than quietly served without TLS.

---

## 1. The certificate

This is the step that stalls people, so pick by how you reach the machine.
With a Cloudflare Tunnel (the installer's default) there is no certificate to
make here at all; Cloudflare's covers you.

### Cloudflare Tunnel — the default: reachable anywhere, certificate handled for you

`cloudflared` makes an outbound connection, so no inbound port is opened and
your home IP is never published. Cloudflare terminates TLS at their edge,
which means **they can see the traffic** — for a homework diary that is a
reasonable trade, but it is a real one and you should make it knowingly.
Put Cloudflare Access in front so the tunnel is not open to the world.

There is no certificate to manage on this machine: nginx uses
`nginx-prep-tunnel.conf`, which listens on `127.0.0.1:8088` in plain HTTP
(only `cloudflared` can reach it) and keeps the rate limits and the
token-free log. `install.sh` does all of this by default; the dashboard
steps (creating the tunnel, its public hostname, and Access) are in
`INSTALL.md` under "Cloudflare Tunnel (the default)".

### Self-signed — LAN only

Fine if you only ever use the dashboard at home, and it costs nothing:

```bash
sudo mkdir -p /etc/ssl/prep
sudo openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout /etc/ssl/prep/privkey.pem -out /etc/ssl/prep/fullchain.pem \
  -subj "/CN=prep.local" -addext "subjectAltName=DNS:prep.local,IP:192.168.1.50"
```

Your browser will warn once per device. Worth knowing why that warning is not
merely an annoyance: a self-signed certificate gives you encryption but not
identity, so clicking through it teaches you to click through the warning that
exists to catch an impostor. Prefer the Cloudflare Tunnel if you can.

---

## 2. Install

```bash
sudo apt install -y nginx
PREP_HOME=/home/$USER/prep
$PREP_HOME/.venv/bin/pip install gunicorn

sudo cp deploy/nginx-prep.conf /etc/nginx/sites-available/prep
sudo sed -i "s/PREP_HOST/your.host.name/g" /etc/nginx/sites-available/prep
sudo ln -sf /etc/nginx/sites-available/prep /etc/nginx/sites-enabled/prep
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

sed -e "s|PREP_USER|$USER|g" -e "s|PREP_HOME|$PREP_HOME|g" \
    systemd/prep-web.service | sudo tee /etc/systemd/system/prep-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now prep-web
```

In `config.yaml`, bind to the loopback so gunicorn is not separately exposed:

```yaml
web:
  host: 127.0.0.1
  port: 8080
  token: env:PREP_WEB_TOKEN
```

---

## 3. The token

The dashboard's only authentication is a bearer token, so its strength **is**
the security model. Generate a real one:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Put it in `.env` as `PREP_WEB_TOKEN`. Phase C checks this and complains if the
token is short or missing while bound publicly.

nginx rate-limits the state-changing endpoints to 12 requests a minute. That
is what stops a token being guessed at machine speed — without it, a long
token is only as good as the attacker's patience.

---

## What TLS does and does not do

Worth being precise, because it is easy to over-read.

**It does:** stop anyone between you and the machine reading the traffic or
your token, and (with a real certificate) prove you are talking to your
machine and not something impersonating it.

**It does not:** protect anything stored on the machine. TLS is transport
security. A file on that disk is exactly as readable after you add nginx as it
was before — by the app, by your user, by root, and by anything that gets
either. Adding HTTPS is worth doing; it just solves a different problem from
the one it is sometimes reached for.

**This dashboard never asks for your school username or password**, over TLS
or otherwise. Sign in on Microsoft's own page, on the machine's own screen —
see the screen-over-the-tunnel route in `deploy/remote-access.md`.
