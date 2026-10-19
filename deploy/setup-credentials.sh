#!/usr/bin/env bash
#
# Move the secrets this machine must hold out of plaintext .env and into
# systemd encrypted credentials.
#
#   sudo ./deploy/setup-credentials.sh
#
# Each one is typed in here, encrypted immediately, and never written to a
# file in the clear. The script does not read your existing .env: retyping
# four values is a small price for not having a script that opens the very
# file you are trying to stop relying on.
#
# Your Teams password is deliberately NOT on the list. Encryption at rest
# does not protect a secret from whatever compromises the running service,
# and unlike these, signing into Teams has a human-present alternative:
# python3 -m prep.login, on Microsoft's own page.

set -euo pipefail

CRED_DIR="${CRED_DIR:-/etc/prep/creds}"

if [[ $EUID -ne 0 ]]; then
    echo "Run this with sudo - it writes to ${CRED_DIR} and reads the host key."
    exit 1
fi

if ! command -v systemd-creds >/dev/null; then
    echo "systemd-creds is not available. You need systemd 250 or newer."
    exit 1
fi

echo "Encrypting credentials into ${CRED_DIR}"
echo

# Read the verdict, not the exit status: has-tpm2 exits non-zero for "partial"
# as well as "no", and partial is both common and materially different from
# no support at all.
TPM_VERDICT="$(systemd-creds has-tpm2 2>/dev/null | head -1 || true)"
case "${TPM_VERDICT}" in
    yes)
        echo "  TPM2: yes - secrets are sealed to this hardware, so the"
        echo "        encrypted files are useless on any other machine."
        ;;
    partial)
        echo "  TPM2: partial - a TPM exists but is not fully usable (this is"
        echo "        what you get without Secure Boot). Sealing may still"
        echo "        apply; treat it as somewhere between yes and no."
        ;;
    *)
        echo "  TPM2: no - falling back to the host key at"
        echo "        /var/lib/systemd/credential.secret (root-only, 0600)."
        echo "        Expected on a Mac mini: neither the T2 nor Apple Silicon's"
        echo "        Secure Enclave is exposed to Linux as a TPM."
        ;;
esac
echo
echo "  This protects against other users on this box, and against a leaked"
echo "  backup. It does NOT protect against whatever compromises the running"
echo "  service - that is not what it is for."
echo

# Without a TPM the host key is an ordinary file. If the disk is not
# encrypted, someone holding the disk holds the key - and systemd says so
# itself when you encrypt. Worth knowing before trusting this with the
# iCloud password.
if [[ "${TPM_VERDICT}" != "yes" ]] && ! lsblk -o TYPE 2>/dev/null | grep -q crypt; then
    echo "  NOTE: no TPM2 and no encrypted volume found. The host key sits on"
    echo "        unencrypted disk, so this stops other users but not someone"
    echo "        holding the drive. Full-disk encryption (LUKS) is what makes"
    echo "        the at-rest protection real. Still worth doing either way."
    echo
fi

mkdir -p "${CRED_DIR}"
chmod 700 "${CRED_DIR}"

# name|filename|prompt|required
SECRETS=(
  "ICLOUD_USER|icloud-user.cred|Your iCloud address|yes"
  "ICLOUD_APP_PASSWORD|icloud-app-password.cred|iCloud APP-SPECIFIC password (appleid.apple.com, not your Apple password)|yes"
  "SOCS_FEED_URL|socs-feed-url.cred|Timetable feed URL (it contains a personal key, so treat it as one)|yes"
  "PREP_WEB_TOKEN|prep-web-token.cred|Dashboard token (blank to generate one)|no"
  "TELEGRAM_BOT_TOKEN|telegram-bot-token.cred|Telegram bot token (blank to skip)|no"
  "TELEGRAM_CHAT_ID|telegram-chat-id.cred|Telegram chat id (blank to skip)|no"
  "ALERT_EMAIL_USER|alert-email-user.cred|Alert sender address (blank to skip)|no"
  "ALERT_EMAIL_APP_PW|alert-email-app-pw.cred|Gmail app password for alerts (blank to skip)|no"
  "ALERT_EMAIL_TO|alert-email-to.cred|Where alerts go (blank to skip)|no"
)

written=()

for entry in "${SECRETS[@]}"; do
    IFS='|' read -r name file prompt required <<< "${entry}"

    if [[ -f "${CRED_DIR}/${file}" ]]; then
        read -r -p "${name} already exists. Replace it? [y/N] " replace
        [[ "${replace}" =~ ^[Yy]$ ]] || { written+=("${name}:${file}"); continue; }
    fi

    # -s so it never appears on screen or in scrollback.
    read -r -s -p "${prompt}: " value
    echo

    if [[ -z "${value}" && "${name}" == "PREP_WEB_TOKEN" ]]; then
        value="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
        echo "  generated a 43-character token"
    fi

    if [[ -z "${value}" ]]; then
        if [[ "${required}" == "yes" ]]; then
            echo "  ${name} is required - rerun when you have it."
            exit 1
        fi
        echo "  skipped"
        continue
    fi

    printf '%s' "${value}" | systemd-creds encrypt --name="${name}" - \
        "${CRED_DIR}/${file}"
    chmod 600 "${CRED_DIR}/${file}"
    unset value
    echo "  encrypted -> ${CRED_DIR}/${file}"
    written+=("${name}:${file}")
done

echo
echo "================================================================"
echo "install.sh --only systemd adds these lines for you. By hand, add them to"
echo "prep-scrape.service, prep-recheck.service and prep-web.service, under [Service]:"
echo
for pair in "${written[@]}"; do
    IFS=':' read -r name file <<< "${pair}"
    echo "LoadCredentialEncrypted=${name}:${CRED_DIR}/${file}"
done
echo
echo "Then in config.yaml, replace each  env:NAME  with  cred:NAME  and"
echo "delete .env. See deploy/systemd-creds.md."
echo
echo "Check it worked:   sudo systemctl restart prep-web"
echo "                   python3 -m prep.phases B --config config.yaml"
echo "================================================================"
