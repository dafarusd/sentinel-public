#!/usr/bin/env bash
# init_readsb_systemd.sh — Stage 18b one-shot setup for the distro-packaged
# readsb.service. Idempotent: safe to re-run after package updates or when
# tweaking the ENV flags. Expects readsb already installed via apt.
#
# What it does:
#   1. Verifies /usr/bin/readsb is installed; bails with an install hint if not.
#   2. Writes /etc/default/readsb with RECEIVER_OPTIONS / NET_OPTIONS / QUIET.
#   3. Reloads systemd so the updated ENV takes effect.
#   4. Enables + starts readsb.service (no-op if already enabled/running).
#
# What it does NOT do:
#   - Install readsb (apt-get install readsb is the operator's step)
#   - Write a custom unit file (distro unit at /lib/systemd/system/readsb.service
#     is authoritative; we only touch its ENV fragment)
#   - Configure ADS-B capture on the Sentinel side (that's the
#     sentinel-sdr-adsb.service user unit, installed by install.sh)
#
# Usage:
#   sudo bash scripts/init_readsb_systemd.sh

set -euo pipefail

READSB_BIN="/usr/bin/readsb"
READSB_DEFAULTS="/etc/default/readsb"
READSB_UNIT="readsb.service"

# Resolved later if present; intent Q7 locks in these flags for Stage 18b.
READSB_RECEIVER_OPTIONS='--device-type rtlsdr --gain -10 --ppm 0'
READSB_NET_OPTIONS='--net --net-sbs-port 30003 --net-beast-reduce-interval 1'

# --- 0. Privilege check ---
# Writing to /etc/default and managing system units requires root.
if [[ $EUID -ne 0 ]]; then
    echo "error: this script must be run as root (try: sudo bash $0)" >&2
    exit 1
fi

# --- 1. Is readsb installed? ---
if [[ ! -x "${READSB_BIN}" ]]; then
    cat >&2 <<EOF
error: readsb not found at ${READSB_BIN}

Install it first:
    sudo apt-get update && sudo apt-get install -y readsb

Then re-run this script.
EOF
    exit 1
fi

READSB_VERSION="$(${READSB_BIN} --version 2>&1 | head -1 || echo 'unknown')"
echo "Found readsb: ${READSB_VERSION}"

# --- 2. Write /etc/default/readsb ---
# Using install(1) gives us atomic replacement and sane permissions in one
# call; the file is world-readable but only root-writable, matching distro
# conventions.
TMP_DEFAULTS="$(mktemp)"
trap 'rm -f "${TMP_DEFAULTS}"' EXIT

cat > "${TMP_DEFAULTS}" <<EOF
# /etc/default/readsb — managed by scripts/init_readsb_systemd.sh (Stage 18b).
# Re-running the init script will overwrite this file. Hand-edit at your own
# risk; the authoritative source lives in the Sentinel repo.
RECEIVER_OPTIONS="${READSB_RECEIVER_OPTIONS}"
NET_OPTIONS="${READSB_NET_OPTIONS}"
QUIET=1
EOF

# Only rewrite if the content actually changed — avoids touching the mtime
# (and avoiding a pointless daemon-reload) on no-op re-runs.
if [[ -f "${READSB_DEFAULTS}" ]] && cmp -s "${TMP_DEFAULTS}" "${READSB_DEFAULTS}"; then
    echo "${READSB_DEFAULTS} already up to date, skipping rewrite"
else
    install -m 0644 "${TMP_DEFAULTS}" "${READSB_DEFAULTS}"
    echo "Wrote ${READSB_DEFAULTS}"
fi

# --- 3. Reload systemd so it picks up any drop-in / ENV changes ---
systemctl daemon-reload

# --- 4. Enable + start readsb ---
# enable --now is idempotent: enables if disabled, starts if stopped.
# We don't use --no-block so any immediate startup errors surface here
# rather than being discovered later via journalctl.
if systemctl enable --now "${READSB_UNIT}"; then
    echo "readsb.service enabled + started"
else
    echo "error: failed to enable/start ${READSB_UNIT}" >&2
    echo "Check: journalctl -u ${READSB_UNIT} -n 50 --no-pager" >&2
    exit 1
fi

# Final state report — useful for operators running this and watching the
# output rather than the journal.
echo ""
echo "Current status:"
systemctl is-enabled "${READSB_UNIT}" | sed 's/^/  enabled: /'
systemctl is-active "${READSB_UNIT}"  | sed 's/^/  active : /'
echo ""
echo "SBS-1 stream should be reachable at 127.0.0.1:30003 within a few seconds."
