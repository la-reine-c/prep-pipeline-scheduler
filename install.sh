#!/usr/bin/env bash
#
# Install the prep pipeline on the mini (Ubuntu 24.04), end to end.
#
#   ./install.sh                          # everything, with the defaults
#   ./install.sh --dry-run                # print every command, change nothing
#   ./install.sh --help                   # all the options
#
# Run it from this folder, as your normal user - NOT with sudo. It asks for
# your password once and uses sudo only for the steps that need root.
#
# README.md explains each step, every option, and what to do when one stops.
# Every step is safe to run again: re-running the script after fixing
# whatever stopped it carries on from where it got to.

set -euo pipefail

# ---------------------------------------------------------------- defaults

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREP_HOME="${HOME}/prep"
HOST=""
CERT="cloudflare"
LAN_IP=""
WEB_PORT="8080"
TUNNEL_PORT="8088"
PREP_CALENDAR=""
TEACHERS=""
ENCRYPT=0
FORCE_TIMERS=0
DRY_RUN=0
ASSUME_YES=0
ONLY=""
SKIP=""

STEPS=(preflight packages copy python browser config check nosleep signin
       verify secrets systemd nginx finish)

CRED_DIR="/etc/prep/creds"
CERT_DIR="/etc/ssl/prep"

# Which config key each encrypted credential replaces. The names match
# deploy/setup-credentials.sh; the file is the name, lower-case, _ -> -.
CRED_KEYS=(
  "ICLOUD_USER=calendar.username"
  "ICLOUD_APP_PASSWORD=calendar.password"
  "SOCS_FEED_URL=feeds.socs"
  "PREP_WEB_TOKEN=web.token"
  "TELEGRAM_BOT_TOKEN=notify.telegram.bot_token"
  "TELEGRAM_CHAT_ID=notify.telegram.chat_id"
  "ALERT_EMAIL_USER=notify.email.username"
  "ALERT_EMAIL_APP_PW=notify.email.password"
  "ALERT_EMAIL_TO=notify.email.to"
)

usage() {
    cat <<EOF
Install the prep pipeline on this machine.

Usage: ./install.sh [options]

Where things go
  --prep-home DIR        install into DIR (default: ~/prep). The data - database,
                         browser profile, teachers.json - lives here too.
  --web-port PORT        port gunicorn listens on, loopback only (default: 8080)

Your config
  --prep-calendar NAME   iCloud calendar prep is booked into (sets
                         calendar.prep_calendar). It must already exist.
  --teachers FILE        teachers.json to copy in, e.g. from the desktop

The dashboard over HTTPS
  --host NAME            name on the certificate, and what you type in the
                         browser (default: this machine's hostname). With
                         --cert cloudflare: the tunnel's public hostname
  --cert MODE            cloudflare (default) | selfsigned | existing | none
                           cloudflare  Cloudflare Tunnel: reachable anywhere, no
                                       open ports, Cloudflare's certificate. Reads
                                       CLOUDFLARE_TUNNEL_TOKEN from .env or asks
                           selfsigned  make one in ${CERT_DIR}; browsers warn
                           existing    use ${CERT_DIR}/{fullchain,privkey}.pem
                           none        no nginx; dashboard on the loopback only
  --lan-ip IP            address to put in a self-signed certificate
                         (default: detected)
  --tunnel-port PORT     loopback port nginx serves the tunnel on (default: 8088)

Secrets
  --encrypt-secrets      after the checks pass, move secrets out of config.yaml
                         into systemd encrypted credentials (asks for each one)

Control
  --force-timers         start the scheduled runs even if sign-in or the
                         checks did not pass (not recommended)
  --only STEP[,STEP]     run just these steps
  --skip STEP[,STEP]     run everything except these
  --dry-run              print every command instead of running it
  --yes                  do not stop to ask before carrying on
  -h, --help             this text

Steps, in order:
  ${STEPS[*]}
EOF
}

# ---------------------------------------------------------------- arguments

while [[ $# -gt 0 ]]; do
    case "$1" in
        --prep-home)        PREP_HOME="$2"; shift 2 ;;
        --web-port)         WEB_PORT="$2"; shift 2 ;;
        --prep-calendar)    PREP_CALENDAR="$2"; shift 2 ;;
        --teachers)         TEACHERS="$2"; shift 2 ;;
        --host)             HOST="$2"; shift 2 ;;
        --cert)             CERT="$2"; shift 2 ;;
        --lan-ip)           LAN_IP="$2"; shift 2 ;;
        --tunnel-port)      TUNNEL_PORT="$2"; shift 2 ;;
        --encrypt-secrets)  ENCRYPT=1; shift ;;
        --force-timers)     FORCE_TIMERS=1; shift ;;
        --only)             ONLY="$2"; shift 2 ;;
        --skip)             SKIP="$2"; shift 2 ;;
        --dry-run)          DRY_RUN=1; shift ;;
        --yes|-y)           ASSUME_YES=1; shift ;;
        -h|--help)          usage; exit 0 ;;
        *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
    esac
done

case "${CERT}" in
    cloudflare|selfsigned|existing|none) ;;
    *) echo "--cert must be cloudflare, selfsigned, existing or none" >&2; exit 2 ;;
esac
[[ "${WEB_PORT}" =~ ^[0-9]+$ ]] || { echo "--web-port must be a number" >&2; exit 2; }
[[ "${TUNNEL_PORT}" =~ ^[0-9]+$ ]] || { echo "--tunnel-port must be a number" >&2; exit 2; }
for name in ${ONLY//,/ } ${SKIP//,/ }; do
    [[ " ${STEPS[*]} " == *" ${name} "* ]] \
        || { echo "unknown step '${name}' - steps are: ${STEPS[*]}" >&2; exit 2; }
done
PREP_HOME="$(realpath -m "${PREP_HOME/#\~/$HOME}")"
VENV_PY="${PREP_HOME}/.venv/bin/python"
CONFIG="${PREP_HOME}/config.yaml"

# ---------------------------------------------------------------- output

if [[ -t 1 ]]; then
    BOLD=$'\e[1m'; DIM=$'\e[2m'; RED=$'\e[31m'; GREEN=$'\e[32m'
    YELLOW=$'\e[33m'; RESET=$'\e[0m'
else
    BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; RESET=""
fi

STEP_NO=0
LATER=()          # things you still have to do, printed at the end

step()  { STEP_NO=$((STEP_NO + 1)); echo; echo "${BOLD}== ${STEP_NO}. $*${RESET}"; }
info()  { echo "   $*"; }
ok()    { echo "   ${GREEN}ok${RESET}    $*"; }
warn()  { echo "   ${YELLOW}warn${RESET}  $*"; }
fail()  { echo "   ${RED}FAIL${RESET}  $*" >&2; exit 1; }
later() { LATER+=("$*"); }

# Every command that changes anything goes through run, so --dry-run shows
# exactly what a real run would do.
run() {
    echo "   ${DIM}\$ $*${RESET}"
    if [[ ${DRY_RUN} -eq 0 ]]; then
        "$@"
    fi
}

# For a command with a pipe or redirect in it.
run_sh() {
    echo "   ${DIM}\$ $1${RESET}"
    if [[ ${DRY_RUN} -eq 0 ]]; then
        bash -c "$1"
    fi
}

confirm() {
    [[ ${ASSUME_YES} -eq 1 || ${DRY_RUN} -eq 1 ]] && return 0
    local reply
    read -r -p "   $* [Y/n] " reply
    [[ -z "${reply}" || "${reply}" =~ ^[Yy] ]]
}

wanted() {
    local name="$1"
    if [[ -n "${ONLY}" ]]; then
        [[ ",${ONLY}," == *",${name},"* ]] || return 1
    fi
    [[ ",${SKIP}," == *",${name},"* ]] && return 1
    return 0
}

cfg_get() {
    # Before the venv exists (a dry run), the system python will do.
    local py="${VENV_PY}" file="${CONFIG}"
    [[ -x "${py}" ]] || py=python3
    # A dry run has not copied the config yet; read the one it would copy.
    [[ -f "${file}" || ${DRY_RUN} -eq 0 ]] || file="${SRC}/config.yaml"
    "${py}" "${SRC}/deploy/configedit.py" "${file}" get "$1" 2>/dev/null || true
}

cfg_set() {
    run "${VENV_PY}" "${SRC}/deploy/configedit.py" "${CONFIG}" set "$1" "$2"
}

uses_cred() { grep -q '^[^#]*cred:' "${CONFIG}" 2>/dev/null; }

# A dry run of a fresh install changes nothing, so later steps cannot see the
# files earlier ones would have made. Commands are shown, not checked.

# Run python from PREP_HOME, with .env exported the way systemd would.
prep_py() {
    (cd "${PREP_HOME}" && set -a && { [[ -f .env ]] && . ./.env || true; } \
        && set +a && "${VENV_PY}" "$@")
}

SIGNED_IN=0
VERIFIED=0

# ================================================================= steps

do_preflight() {
    step "Preflight"

    [[ ${EUID} -ne 0 ]] || fail "run this as your normal user, not with sudo -
          the services run as whoever runs this"
    for file in requirements.txt prep/run.py prep/recheck.py systemd/prep-web.service \
                deploy/nginx-prep.conf deploy/configedit.py; do
        [[ -e "${SRC}/${file}" ]] || fail "${SRC}/${file} is missing - run this from the 2-deploy-on-mini folder"
    done
    ok "source folder ${SRC}"

    if [[ -r /etc/os-release ]]; then
        . /etc/os-release
        if [[ "${ID:-}" == "ubuntu" ]]; then
            ok "${PRETTY_NAME}"
        else
            warn "${PRETTY_NAME:-unknown OS} - written for Ubuntu 24.04; carrying on"
        fi
    fi

    local arch; arch="$(uname -m)"
    if [[ "${arch}" == "x86_64" ]]; then
        ok "x86_64 - Playwright's own Chromium will be used"
    else
        warn "${arch}: if 'playwright install' has no build for it, use the snap"
        warn "(sudo snap install chromium; chromium_path: /snap/bin/chromium)"
    fi

    command -v python3 >/dev/null || fail "python3 is not installed"
    ok "$(python3 --version)"

    local sd; sd="$(systemctl --version | awk 'NR==1 {print $2}')"
    if [[ ${ENCRYPT} -eq 1 && "${sd}" -lt 250 ]]; then
        fail "systemd ${sd} is too old for encrypted credentials (needs 250+)"
    fi
    ok "systemd ${sd}"

    if [[ ${DRY_RUN} -eq 0 ]]; then
        info "sudo is needed for packages, systemd and nginx:"
        sudo -v || fail "sudo is required"
    fi

    # Something else - the Wake-on-LAN service, say - already on the ports?
    # Root is needed to see WHICH process holds a port, so a re-run can tell
    # our own gunicorn and nginx from a stranger.
    local ss_cmd=(ss -tlnH)
    [[ ${DRY_RUN} -eq 0 ]] && ss_cmd=(sudo ss -tlnpH)
    if command -v ss >/dev/null; then
        local holder
        holder="$("${ss_cmd[@]}" "sport = :${WEB_PORT}" 2>/dev/null || true)"
        if [[ -n "${holder}" && "${holder}" != *gunicorn* ]]; then
            warn "port ${WEB_PORT} is already in use:"
            info "${holder}"
            [[ ${DRY_RUN} -eq 1 ]] || fail "pick a free one with --web-port"
        else
            ok "port ${WEB_PORT} is free (or already ours)"
        fi
        if [[ "${CERT}" == "cloudflare" ]]; then
            holder="$("${ss_cmd[@]}" "sport = :${TUNNEL_PORT}" 2>/dev/null || true)"
            if [[ -n "${holder}" && "${holder}" != *nginx* ]]; then
                warn "port ${TUNNEL_PORT} is already in use:"
                info "${holder}"
                [[ ${DRY_RUN} -eq 1 ]] || fail "pick a free one with --tunnel-port"
            fi
        elif [[ "${CERT}" != "none" ]]; then
            holder="$("${ss_cmd[@]}" 'sport = :443' 2>/dev/null || true)"
            if [[ -n "${holder}" && "${holder}" != *nginx* ]]; then
                warn "port 443 is held by something other than nginx:"
                info "${holder}"
                warn "see LINUX.md section 1 (url_prefix) before going on"
                confirm "Carry on anyway?" || exit 1
            fi
        fi
    fi
}

do_packages() {
    step "System packages"
    run sudo apt-get update
    # No chromium here on purpose: Ubuntu has no such apt package, and apt
    # then refuses the whole line - nginx included.
    run sudo apt-get install -y python3-venv python3-pip util-linux rsync \
        openssl curl nginx
    if [[ "${CERT}" == "cloudflare" ]]; then
        if command -v cloudflared >/dev/null; then
            ok "cloudflared already installed"
        else
            # Cloudflare's own repository: Ubuntu does not package cloudflared.
            run sudo mkdir -p --mode=0755 /usr/share/keyrings
            run_sh "curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null"
            run_sh "echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' | sudo tee /etc/apt/sources.list.d/cloudflared.list >/dev/null"
            run sudo apt-get update
            run sudo apt-get install -y cloudflared
        fi
    fi
}

do_copy() {
    step "Code into ${PREP_HOME}"
    if [[ "$(realpath "${SRC}")" == "${PREP_HOME}" ]]; then
        ok "already running from ${PREP_HOME}; nothing to copy"
        return
    fi
    run mkdir -p "${PREP_HOME}"
    # No --delete: the database, browser profile and teachers.json live here
    # too, and the live config.yaml is never overwritten.
    run rsync -a --exclude '.venv/' --exclude '__pycache__/' \
        --exclude '.pytest_cache/' --exclude 'config.yaml' --exclude '.env' \
        "${SRC}/" "${PREP_HOME}/"
    ok "code copied"

    if [[ -f "${PREP_HOME}/config.yaml" ]]; then
        if [[ -f "${SRC}/config.yaml" ]] && ! cmp -s "${SRC}/config.yaml" "${PREP_HOME}/config.yaml"; then
            warn "${PREP_HOME}/config.yaml already exists and differs from this folder's."
            warn "Kept the installed one (it may hold settings this script made)."
            info "To use this folder's instead: cp ${SRC}/config.yaml ${PREP_HOME}/ and re-run."
        else
            ok "config.yaml already in place"
        fi
    elif [[ -f "${SRC}/config.yaml" ]]; then
        run install -m 600 "${SRC}/config.yaml" "${PREP_HOME}/config.yaml"
        ok "config.yaml copied (mode 600)"
    else
        run install -m 600 "${SRC}/config.example.yaml" "${PREP_HOME}/config.yaml"
        warn "no config.yaml here, so the example was copied."
        fail "fill in ${PREP_HOME}/config.yaml (see README.md), then re-run"
    fi

    if [[ -f "${SRC}/.env" && ! -f "${PREP_HOME}/.env" ]]; then
        run install -m 600 "${SRC}/.env" "${PREP_HOME}/.env"
        ok ".env copied (mode 600)"
    fi
}

do_python() {
    step "Python environment"
    # A venv made while python3-venv was missing has no pip, and every run
    # after that fails at the pip line below. Rebuild it rather than reuse it.
    if [[ -x "${VENV_PY}" ]] && ! "${VENV_PY}" -m pip --version >/dev/null 2>&1; then
        warn "the existing venv has no pip (python3-venv was missing?) - rebuilding it"
        run rm -rf "${PREP_HOME}/.venv"
    fi
    if [[ ! -x "${VENV_PY}" || ${DRY_RUN} -eq 1 && ! -d "${PREP_HOME}/.venv" ]]; then
        run python3 -m venv "${PREP_HOME}/.venv" \
            || fail "could not create the venv - run the packages step (it installs python3-venv)"
    else
        ok "venv exists"
    fi
    run "${VENV_PY}" -m pip install --quiet --upgrade pip
    run "${VENV_PY}" -m pip install --quiet -r "${PREP_HOME}/requirements.txt"
    ok "requirements installed"
}

do_browser() {
    step "Chromium for Playwright"
    # --with-deps installs the system libraries too, through sudo.
    run "${VENV_PY}" -m playwright install --with-deps chromium
    ok "chromium installed"
}

do_config() {
    step "Config"
    [[ -f "${CONFIG}" || ${DRY_RUN} -eq 1 ]] || fail "${CONFIG} is missing - run the copy step first"
    run chmod 600 "${CONFIG}"
    [[ -f "${PREP_HOME}/.env" ]] && run chmod 600 "${PREP_HOME}/.env"

    # gunicorn listens on the loopback and nginx is the only way in; the
    # config says the same, so the token check and the dev server agree.
    [[ "$(cfg_get web.host)" == "127.0.0.1" ]] || cfg_set web.host 127.0.0.1
    [[ "$(cfg_get web.port)" == "${WEB_PORT}" ]] || cfg_set web.port "${WEB_PORT}"

    if [[ -n "${PREP_CALENDAR}" ]]; then
        cfg_set calendar.prep_calendar "${PREP_CALENDAR}"
    fi
    local calendar="${PREP_CALENDAR}"
    [[ -n "${calendar}" ]] || calendar="$(cfg_get calendar.prep_calendar)"
    if [[ "${calendar,,}" == *test* ]]; then
        warn "prep is booked into \"${calendar}\" - a test calendar."
        warn "For real use: --prep-calendar Prep (it must exist in iCloud)."
        later "Prep is still going to the test calendar \"${calendar}\". Re-run with --prep-calendar <name> --only config,systemd when you are ready."
    else
        ok "prep is booked into \"${calendar}\""
    fi
    later "Make sure the iCloud calendars \"${calendar}\" and \"$(cfg_get calendar.personal_calendar)\" exist (Calendar app). The dashboard writes outside lessons to the second."

    # A dashboard token, if there is none. The token is the dashboard's only
    # lock, so it is long and random.
    local token; token="$(cfg_get web.token)"
    if [[ -z "${token}" ]]; then
        cfg_set web.token "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
        ok "generated a 43-character dashboard token"
    elif [[ "${token}" == env:* || "${token}" == cred:* ]]; then
        ok "dashboard token comes from ${token}"
    elif [[ ${#token} -lt 24 ]]; then
        warn "the dashboard token is only ${#token} characters; use 24 or more"
    else
        ok "dashboard token set"
    fi

    if uses_cred; then
        ok "config uses cred: - it can only be read under systemd, so it is checked there"
    elif [[ ${DRY_RUN} -eq 0 ]]; then
        prep_py - <<'PY' || fail "config.yaml is not complete - fix the lines above and re-run"
import sys
from prep import config
try:
    cfg = config.load("config.yaml")
except Exception as exc:
    print(f"   cannot load config.yaml: {exc}")
    sys.exit(1)
missing = [name for name, value in (
    ("calendar.username (your iCloud address)", cfg.calendar.username),
    ("calendar.password (app-specific password)", cfg.calendar.password),
    ("feeds.socs (timetable feed URL)", (cfg.feeds or {}).get("socs", "")),
) if not str(value or "").strip()]
for name in missing:
    print(f"   missing: {name}")
if not cfg.scheduling.windows:
    print("   missing: scheduling.windows")
    missing.append("windows")
sys.exit(1 if missing else 0)
PY
        ok "config loads, and the required values are filled in"
    fi

    # teachers.json: who set what. Answered once, copied between machines.
    local learned
    learned="$( (cd "${PREP_HOME}" && "${VENV_PY}" -c \
        'from prep import config; print(config.load("config.yaml").learned_map_path)') 2>/dev/null \
        || echo "${PREP_HOME}/teachers.json")"
    if [[ -n "${TEACHERS}" ]]; then
        [[ -f "${TEACHERS}" ]] || fail "--teachers ${TEACHERS}: no such file"
        run install -m 600 "${TEACHERS}" "${learned}"
        ok "teachers.json copied to ${learned}"
    elif [[ -f "${learned}" ]]; then
        ok "teachers.json in place"
    else
        warn "no teachers.json yet"
        later "Name your teachers: copy teachers.json from the desktop (re-run with --teachers FILE --only config) or run: cd ${PREP_HOME} && .venv/bin/python -m prep.setup --config config.yaml --set-teachers"
    fi
}

do_check() {
    step "Phase A - the logic, offline (no network, no passwords)"
    if [[ ${DRY_RUN} -eq 1 ]]; then
        run "${VENV_PY}" -m prep.phases A
        return
    fi
    if prep_py -m prep.phases A; then
        ok "Phase A passed"
    else
        fail "Phase A failed - the code itself is wrong on this machine. Do not go further."
    fi
}

do_nosleep() {
    step "Stop the machine sleeping"
    # A machine asleep at 07:05 produces the same output as a week with no
    # prep set.
    run sudo systemctl mask sleep.target suspend.target hibernate.target \
        hybrid-sleep.target
    ok "sleep masked"
}

do_signin() {
    step "Teams sign-in"
    if [[ ${DRY_RUN} -eq 0 ]] && prep_py -m prep.session --config config.yaml >/dev/null 2>&1; then
        ok "a saved Teams session is in place and persistent"
        SIGNED_IN=1
        return
    fi
    if [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
        warn "no desktop here (over SSH?), so the sign-in window cannot open."
        later "Sign into Teams on the mini's own screen (or over VNC, deploy/remote-access.md): cd ${PREP_HOME} && .venv/bin/python -m prep.login --config config.yaml - tick 'Stay signed in', open Assignments, scroll to the bottom. Then re-run: ./install.sh --only signin,verify,systemd"
        return
    fi
    info "A browser window opens on Microsoft's own sign-in page."
    info "Tick 'Stay signed in', open Assignments and scroll to the bottom."
    if ! confirm "Open it now?"; then
        later "Sign into Teams: cd ${PREP_HOME} && .venv/bin/python -m prep.login --config config.yaml, then ./install.sh --only signin,verify,systemd"
        return
    fi
    if [[ ${DRY_RUN} -eq 1 ]]; then
        run "${VENV_PY}" -m prep.login --config config.yaml
        run "${VENV_PY}" -m prep.session --config config.yaml --snapshot
        return
    fi
    if prep_py -m prep.login --config config.yaml; then
        prep_py -m prep.session --config config.yaml --snapshot || true
        ok "signed in, and the profile snapshotted"
        SIGNED_IN=1
    else
        warn "sign-in did not complete"
        later "Sign into Teams: cd ${PREP_HOME} && .venv/bin/python -m prep.login --config config.yaml, then ./install.sh --only signin,verify,systemd"
    fi
}

do_verify() {
    step "Phase B and C - real passwords, real data, dry run"
    if uses_cred; then
        warn "config uses cred:, which only resolves under systemd - skipped."
        info "Check it with: sudo systemctl start prep-scrape; journalctl -u prep-scrape -n 50"
        VERIFIED=1
        return
    fi
    if [[ ${SIGNED_IN} -eq 0 && ${DRY_RUN} -eq 0 ]]; then
        if prep_py -m prep.session --config config.yaml >/dev/null 2>&1; then
            SIGNED_IN=1
        else
            warn "not signed into Teams yet - skipped"
            return
        fi
    fi
    if [[ ${DRY_RUN} -eq 1 ]]; then
        run "${VENV_PY}" -m prep.phases B --config config.yaml
        run "${VENV_PY}" -m prep.phases C --config config.yaml
        return
    fi
    if ! prep_py -m prep.phases B --config config.yaml; then
        warn "Phase B failed - read the [FAIL] lines above (PHASES.md explains each)"
        later "Fix Phase B, then re-run: ./install.sh --only verify,systemd"
        return
    fi
    # Phase C is a dry run: it reads iCloud and the timetable, writes nothing
    # to either, and works on a copy of the database.
    if ! prep_py -m prep.phases C --config config.yaml; then
        warn "Phase C failed - read the [FAIL] lines above"
        later "Fix Phase C, then re-run: ./install.sh --only verify,systemd"
        return
    fi
    ok "Phase B and C passed"
    VERIFIED=1
}

do_secrets() {
    [[ ${ENCRYPT} -eq 1 ]] || return 0
    step "Encrypt secrets (systemd-creds)"
    if [[ ${VERIFIED} -eq 0 && ${DRY_RUN} -eq 0 ]]; then
        warn "the checks have not passed yet. Encrypt after they do, so a"
        warn "wrong password is caught by Phase B rather than at 07:05."
        later "Encrypt secrets once the checks pass: ./install.sh --encrypt-secrets --only secrets,systemd"
        return
    fi
    info "It asks for each secret in turn; typing is hidden. For the dashboard"
    info "token, leave it blank to generate a new one - the new link is printed"
    info "at the end. This script adds the LoadCredentialEncrypted= lines for you."
    run sudo "${SRC}/deploy/setup-credentials.sh"

    local backup="${CONFIG}.before-encrypt"
    run install -m 600 "${CONFIG}" "${backup}"
    local entry name key file
    for entry in "${CRED_KEYS[@]}"; do
        name="${entry%%=*}"; key="${entry#*=}"
        file="${CRED_DIR}/$(echo "${name,,}" | tr _ -).cred"
        if [[ ${DRY_RUN} -eq 1 ]] || sudo test -f "${file}"; then
            cfg_set "${key}" "cred:${name}"
        fi
    done
    ok "config.yaml now reads its secrets from ${CRED_DIR}"
    later "The old config with plaintext secrets is at ${backup}. Once the dashboard and a scheduled run both work, delete it: shred -u ${backup}"
}

# The LoadCredentialEncrypted= lines for whatever credentials exist. A unit
# naming a credential file that is not there fails to start at all, so the
# list is built from what is actually on disk, never from a fixed list.
credential_lines() {
    local file name
    sudo test -d "${CRED_DIR}" || return 0
    while IFS= read -r file; do
        name="$(basename "${file}" .cred | tr a-z- A-Z_)"
        echo "LoadCredentialEncrypted=${name}:${file}"
    done < <(sudo find "${CRED_DIR}" -maxdepth 1 -name '*.cred' | sort)
}

render_unit() {
    local unit="$1" creds="$2"
    awk -v creds="${creds}" '
        /^LoadCredentialEncrypted=/ {
            if (!done && creds != "") print creds
            done = 1
            next
        }
        { print }
    ' "${SRC}/systemd/${unit}" \
    | sed -e "s|PREP_USER|${USER}|g" -e "s|PREP_HOME|${PREP_HOME}|g" \
          -e "s|127.0.0.1:8080|127.0.0.1:${WEB_PORT}|g"
}

do_systemd() {
    step "systemd services and timers"
    local creds=""
    [[ ${DRY_RUN} -eq 0 ]] && creds="$(credential_lines)"
    if [[ -n "${creds}" ]]; then
        ok "$(echo "${creds}" | wc -l) encrypted credential(s) passed to the services"
    else
        info "no encrypted credentials - secrets come from config.yaml or .env"
    fi
    if uses_cred && [[ -z "${creds}" && ${DRY_RUN} -eq 0 ]]; then
        fail "config.yaml asks for cred: values but ${CRED_DIR} holds none - run with --encrypt-secrets"
    fi

    local unit
    for unit in prep-web.service prep-scrape.service prep-scrape.timer \
                prep-recheck.service prep-recheck.timer; do
        echo "   ${DIM}\$ render systemd/${unit} > /etc/systemd/system/${unit}${RESET}"
        if [[ ${DRY_RUN} -eq 0 ]]; then
            render_unit "${unit}" "${creds}" | sudo tee "/etc/systemd/system/${unit}" >/dev/null
        fi
    done
    run sudo systemctl daemon-reload
    run sudo systemctl enable prep-web.service
    run sudo systemctl restart prep-web.service
    ok "dashboard service running on 127.0.0.1:${WEB_PORT}"

    if [[ ${VERIFIED} -eq 1 || ${FORCE_TIMERS} -eq 1 || ${DRY_RUN} -eq 1 ]]; then
        run sudo systemctl enable --now prep-scrape.timer prep-recheck.timer
        ok "scheduled: full run hourly 07:05-22:05, recheck every 5 minutes"
    else
        warn "timers NOT started: sign-in and Phase B/C have not both passed."
        info "That is on purpose - a timer on a broken setup alerts you every hour."
        later "Start the schedule once the checks pass: ./install.sh --only verify,systemd (or sudo systemctl enable --now prep-scrape.timer prep-recheck.timer)"
    fi
}

detect_host() {
    if [[ -n "${HOST}" ]]; then return; fi
    [[ -n "${HOST}" ]] || HOST="$(hostname -f 2>/dev/null || hostname)"
}

do_nginx() {
    if [[ "${CERT}" == "none" ]]; then
        step "nginx"
        info "--cert none: no nginx. The dashboard is only on this machine:"
        info "http://127.0.0.1:${WEB_PORT}/"
        return
    fi
    if [[ "${CERT}" == "cloudflare" ]]; then
        do_tunnel
        return
    fi
    detect_host
    step "HTTPS: certificate and nginx for ${HOST}"
    run sudo mkdir -p "${CERT_DIR}"
    run sudo chmod 755 "${CERT_DIR}"

    case "${CERT}" in
        selfsigned)
            if [[ ${DRY_RUN} -eq 0 ]] && sudo test -f "${CERT_DIR}/fullchain.pem"; then
                ok "certificate already in ${CERT_DIR}"
            else
                [[ -n "${LAN_IP}" ]] || LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
                local san="DNS:${HOST},DNS:localhost,IP:127.0.0.1"
                [[ -n "${LAN_IP}" ]] && san="${san},IP:${LAN_IP}"
                run sudo openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
                    -keyout "${CERT_DIR}/privkey.pem" -out "${CERT_DIR}/fullchain.pem" \
                    -subj "/CN=${HOST}" -addext "subjectAltName=${san}"
                ok "self-signed certificate for ${san}"
                info "Browsers warn once per device - expected for self-signed."
            fi
            ;;
        existing)
            if [[ ${DRY_RUN} -eq 0 ]]; then
                sudo test -f "${CERT_DIR}/fullchain.pem" && sudo test -f "${CERT_DIR}/privkey.pem" \
                    || fail "--cert existing: put fullchain.pem and privkey.pem in ${CERT_DIR} first"
            fi
            ok "using the certificate in ${CERT_DIR}"
            ;;
    esac
    run sudo chmod 600 "${CERT_DIR}/privkey.pem"

    echo "   ${DIM}\$ render deploy/nginx-prep.conf > /etc/nginx/sites-available/prep${RESET}"
    if [[ ${DRY_RUN} -eq 0 ]]; then
        sed -e "s/PREP_HOST/${HOST}/g" -e "s|127.0.0.1:8080|127.0.0.1:${WEB_PORT}|g" \
            "${SRC}/deploy/nginx-prep.conf" | sudo tee /etc/nginx/sites-available/prep >/dev/null
    fi
    run sudo ln -sf /etc/nginx/sites-available/prep /etc/nginx/sites-enabled/prep
    run sudo rm -f /etc/nginx/sites-enabled/default
    run sudo nginx -t
    run sudo systemctl enable nginx
    run sudo systemctl reload-or-restart nginx
    ok "nginx serving https://${HOST}/"
}

# The token that ties this machine to a tunnel made in the Cloudflare
# dashboard. Never on the command line or in the printed commands: anyone
# holding it can serve their own site under your hostname.
tunnel_token() {
    if [[ -n "${CLOUDFLARE_TUNNEL_TOKEN:-}" ]]; then
        printf '%s' "${CLOUDFLARE_TUNNEL_TOKEN}"
        return
    fi
    local env_file value
    for env_file in "${PREP_HOME}/.env" "${SRC}/.env"; do
        [[ -f "${env_file}" ]] || continue
        value="$(set -a; . "${env_file}"; printf '%s' "${CLOUDFLARE_TUNNEL_TOKEN:-}")"
        if [[ -n "${value}" ]]; then
            printf '%s' "${value}"
            return
        fi
    done
}

do_tunnel() {
    step "Cloudflare Tunnel: nginx on 127.0.0.1:${TUNNEL_PORT}, cloudflared to Cloudflare"
    echo "   ${DIM}\$ render deploy/nginx-prep-tunnel.conf > /etc/nginx/sites-available/prep${RESET}"
    if [[ ${DRY_RUN} -eq 0 ]]; then
        sed -e "s/TUNNEL_PORT/${TUNNEL_PORT}/g" -e "s|127.0.0.1:8080|127.0.0.1:${WEB_PORT}|g" \
            "${SRC}/deploy/nginx-prep-tunnel.conf" | sudo tee /etc/nginx/sites-available/prep >/dev/null
    fi
    run sudo ln -sf /etc/nginx/sites-available/prep /etc/nginx/sites-enabled/prep
    run sudo rm -f /etc/nginx/sites-enabled/default
    run sudo nginx -t
    run sudo systemctl enable nginx
    run sudo systemctl reload-or-restart nginx
    ok "nginx serving the tunnel origin on http://127.0.0.1:${TUNNEL_PORT}/ (loopback only)"

    if [[ ${DRY_RUN} -eq 0 ]] && systemctl list-unit-files cloudflared.service >/dev/null 2>&1 \
            && systemctl is-enabled --quiet cloudflared 2>/dev/null; then
        ok "cloudflared is already installed as a service"
        return
    fi
    local token; token="$(tunnel_token || true)"
    if [[ -z "${token}" && ${DRY_RUN} -eq 0 && ${ASSUME_YES} -eq 0 && -t 0 ]]; then
        info "Paste the tunnel token from the Cloudflare dashboard (Zero Trust ->"
        info "Networks -> Tunnels -> your tunnel), or press Enter to do it later."
        read -r -s -p "   token: " token
        echo
    fi
    if [[ -z "${token}" ]]; then
        if [[ ${DRY_RUN} -eq 1 ]]; then
            echo "   ${DIM}\$ sudo cloudflared service install <tunnel token>${RESET}"
        else
            warn "no tunnel token: the dashboard is reachable from this machine only, for now"
            later "Connect the Cloudflare Tunnel: put CLOUDFLARE_TUNNEL_TOKEN=... in ${PREP_HOME}/.env and run ./install.sh --only nginx (README.md, 'Cloudflare Tunnel')"
        fi
        return
    fi
    echo "   ${DIM}\$ sudo cloudflared service install <tunnel token>${RESET}"
    if [[ ${DRY_RUN} -eq 0 ]]; then
        sudo cloudflared service install "${token}" || fail "cloudflared service install failed - check the token"
    fi
    ok "cloudflared is running as a service and connects out to Cloudflare"
    info "In the Cloudflare dashboard, the tunnel's public hostname must point at"
    info "http://localhost:${TUNNEL_PORT}. Put Cloudflare Access in front of it."
}

do_finish() {
    step "Checking it all works"
    if [[ ${DRY_RUN} -eq 1 ]]; then
        info "(dry run - nothing was changed)"
    else
        if systemctl is-active --quiet prep-web; then
            ok "prep-web is running"
        else
            warn "prep-web is not running: journalctl -u prep-web -n 50 --no-pager"
        fi
        local health
        if health="$(curl -fsS "http://127.0.0.1:${WEB_PORT}/healthz" 2>/dev/null)"; then
            ok "dashboard answers on the loopback: ${health}"
        else
            warn "nothing on http://127.0.0.1:${WEB_PORT}/healthz yet (it can take a few seconds)"
        fi
        if [[ "${CERT}" == "cloudflare" ]]; then
            if curl -fsS "http://127.0.0.1:${TUNNEL_PORT}/healthz" >/dev/null 2>&1; then
                ok "and through nginx on the tunnel port"
            else
                warn "http://127.0.0.1:${TUNNEL_PORT}/healthz did not answer through nginx"
            fi
            if systemctl is-active --quiet cloudflared 2>/dev/null; then
                ok "cloudflared is connected"
            else
                warn "cloudflared is not running (no token yet, or: journalctl -u cloudflared -n 50)"
            fi
        elif [[ "${CERT}" != "none" ]]; then
            detect_host
            if curl -fsSk --resolve "${HOST}:443:127.0.0.1" "https://${HOST}/healthz" >/dev/null 2>&1; then
                ok "and through nginx over HTTPS"
            else
                warn "https://${HOST}/healthz did not answer through nginx"
            fi
        fi
        systemctl list-timers 'prep-*' --no-pager 2>/dev/null | sed 's/^/   /' || true
    fi

    local token; token="$(cfg_get web.token)"
    if [[ ${DRY_RUN} -eq 1 ]]; then
        token="<your token>"
    elif [[ "${token}" == cred:* ]]; then
        token="$(sudo systemd-creds decrypt --name=PREP_WEB_TOKEN \
            "${CRED_DIR}/prep-web-token.cred" - 2>/dev/null || true)"
    elif [[ "${token}" == env:* ]]; then
        token="$(set -a; [[ -f "${PREP_HOME}/.env" ]] && . "${PREP_HOME}/.env"; \
                 printenv "${token#env:}" || true)"
    fi
    echo
    echo "${BOLD}Dashboard${RESET}"
    if [[ "${CERT}" == "none" ]]; then
        info "http://127.0.0.1:${WEB_PORT}/?token=${token:-<your token>}"
    elif [[ "${CERT}" == "cloudflare" ]]; then
        info "https://${HOST:-<your tunnel hostname>}/?token=${token:-<your token>}"
        info "(on this machine: http://127.0.0.1:${TUNNEL_PORT}/?token=...)"
    else
        detect_host
        info "https://${HOST}/?token=${token:-<your token>}"
    fi
    info "Open it once per browser with the ?token= part; a cookie remembers it."
    info "Treat that link like a password."

    if [[ ${#LATER[@]} -gt 0 ]]; then
        echo
        echo "${BOLD}Still to do${RESET}"
        local n=1 item
        for item in "${LATER[@]}"; do
            echo "   ${n}. ${item}" | fold -s -w 76 | sed '2,$s/^/      /'
            n=$((n + 1))
        done
    else
        echo
        ok "Nothing left to do."
    fi
}

# ================================================================= main

echo "${BOLD}prep pipeline installer${RESET}"
info "from:  ${SRC}"
info "into:  ${PREP_HOME}"
info "https: ${CERT}${HOST:+ (${HOST})}, dashboard on 127.0.0.1:${WEB_PORT}"
[[ "${CERT}" == "cloudflare" ]] && info "tunnel origin: nginx on 127.0.0.1:${TUNNEL_PORT}"
[[ ${DRY_RUN} -eq 1 ]] && info "${YELLOW}dry run: commands are printed, not run${RESET}"

for name in "${STEPS[@]}"; do
    if wanted "${name}"; then
        "do_${name}"
    fi
done
