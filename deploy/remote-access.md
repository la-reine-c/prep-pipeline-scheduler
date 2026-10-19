# Reaching the mini from your phone

The reason signing in feels painful is not the signing in — it is getting to
the machine. Fix that and re-authenticating becomes a thing you do from the
bus.

Two pieces. Set them up once.

---

## 1. Tailscale — a private network your phone is on

```bash
# on the mini
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh
```

Then install Tailscale on your phone and sign in **with a personal account,
not your school one** — the AUP forbids school usernames on third-party
sites. Your phone and the mini can now reach each other from anywhere, with
no port forwarding and nothing exposed to the internet.

**Never run Tailscale on the school network.** That is the VPN clause. On
your home machine and your phone's mobile data it is fine.

### `--ssh` is the part that matters

Tailscale SSH means **no keys to generate, copy or lose**. Authentication is
your Tailscale identity, access is controlled by ACLs you can change from the
admin console, and it is revocable centrally — if your phone is lost, you
remove that device and its access is gone immediately.

From your phone, any SSH client (Termius, Blink, Prompt) connects with just:

```
ssh user@prep-mini
```

There is also a browser-based console in the Tailscale admin UI, so you can
get a shell with no app installed at all.

---

## 2. A screen, for the one job that needs one

SSH is enough for everything except signing into Teams, which needs a real
browser window because you type into **Microsoft's own page**. For that you
need to see the mini's screen.

**Ubuntu first: turn Wayland off.** Ubuntu 24.04's desktop runs on Wayland,
and x11vnc can only see an X11 session — on Wayland it shows a black screen
or refuses outright, which you would discover from your phone at exactly the
moment you needed it. Switch the login manager to X11 once:

```bash
sudo sed -i 's/^#\?\s*WaylandEnable=.*/WaylandEnable=false/' /etc/gdm3/custom.conf
grep WaylandEnable /etc/gdm3/custom.conf     # should say WaylandEnable=false
sudo reboot
```

Enable automatic login for your account too (Settings → System → Users), so
after a power cut there is a desktop to connect to rather than a login screen.

Then, logged into that desktop (or over SSH as the same user):

```bash
sudo apt install -y x11vnc novnc websockify
x11vnc -storepasswd                  # the second lock - see below
x11vnc -find -auth guess -usepw -localhost -rfbport 5900 -forever -shared -bg
websockify --web=/usr/share/novnc 6080 localhost:5900 &
```

`-find` rather than `-display :0`: on Ubuntu the login screen owns `:0` and
your own session is usually `:1`, so `:0` would show you the login screen.
`-auth guess` finds the session's X authority, which is what lets this work
when started over SSH.

Both bind the loopback only, so they are reachable *just* through nginx, which
already terminates TLS. Add to your nginx server block:

```nginx
    # The mini's screen, for signing into Teams. Same token as the dashboard.
    location /screen/ {
        limit_req zone=prep_login burst=5 nodelay;
        proxy_pass http://127.0.0.1:6080/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host       $host;
        proxy_read_timeout 3600s;
    }
```

The VNC password set above (`-usepw` makes it required) matters — the nginx
token is not enough on its own, because a websocket upgrade is not a page load and you want a second
lock on something that grants full control of the machine.

Then, from your phone: open `https://prep-mini.your-tailnet.ts.net/screen/`,
see the mini's desktop, run `python3 -m prep.login --config config.yaml` in a
terminal on it, and sign in on Microsoft's real page.

---

## What this gets you

Re-authenticating stops being "wait until I'm physically at the mini" and
becomes "tap a link, type my password into Microsoft, done" — from anywhere,
in about ninety seconds.

And note what never happens in that flow: your password is never typed into
anything of ours, never stored, never transmitted anywhere except to
Microsoft over their own TLS. The convenience problem and the credential
problem turn out to be separable, and only one of them needed solving.

---

## How often will you actually need this?

Probably far less than you think. Microsoft's refresh tokens **replace
themselves with a fresh 90-day expiry every time they are used**, and the
pipeline uses the session several times a day. The 90 days is an *inactivity*
limit, not a countdown from sign-in.

What genuinely ends a session: changing your school password, an admin
revoking tokens, or a Conditional Access sign-in-frequency policy your school
may set.

So measure before optimising:

```bash
python3 -m prep.session --config config.yaml
```

If that number keeps resetting into the future, you may go a whole term
without touching it.
