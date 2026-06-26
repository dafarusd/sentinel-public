#!/usr/bin/env bash
# Sentinel Installer
#
# Sets up the Sentinel RF surveillance platform on a Raspberry Pi 5
# running Kali Linux. Designed to be run as user kali (uses sudo where needed).
#
# What it does:
#   1. Installs system packages (apt)
#   2. Creates Python virtual environment and installs deps
#   3. Creates data directories
#   4. Downloads IEEE OUI database
#   5. Applies database schema
#   6. Installs systemd units
#   7. Enables lingering for user units
#
# Usage:
#   cd /home/user/sentinel
#   bash install.sh

set -euo pipefail

INSTALL_DIR="${SENTINEL_INSTALL_DIR:-$(pwd)}"
VENV="${INSTALL_DIR}/.venv"
PYTHON="${VENV}/bin/python"
DATA_DIR="${INSTALL_DIR}/data"
LOG_DIR="${DATA_DIR}/logs"
CONFIG="${INSTALL_DIR}/config.yaml"

# OUI database URL (IEEE MA-L assignments)
OUI_URL="https://standards-oui.ieee.org/oui/oui.txt"

echo "========================================"
echo "  Sentinel Installer"
echo "  Install dir: ${INSTALL_DIR}"
echo "========================================"
echo ""

# --- 1. System packages ---
echo "[1/7] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3 python3-venv python3-pip python3-dev \
    iw aircrack-ng \
    bluetooth bluez \
    libdbus-1-dev libglib2.0-dev \
    sqlite3 \
    wireless-tools \
    2>/dev/null
echo "  Done."

# --- 2. Python virtual environment ---
echo "[2/7] Setting up Python virtual environment..."
if [[ ! -d "${VENV}" ]]; then
    python3 -m venv "${VENV}"
    echo "  Created venv at ${VENV}"
else
    echo "  Venv already exists at ${VENV}"
fi

"${VENV}/bin/pip" install --quiet --upgrade pip
"${VENV}/bin/pip" install --quiet -e "${INSTALL_DIR}"
echo "  Dependencies installed."

# --- 3. Data directories ---
echo "[3/7] Creating data directories..."
mkdir -p "${DATA_DIR}"
mkdir -p "${LOG_DIR}"
echo "  ${DATA_DIR}"
echo "  ${LOG_DIR}"

# --- 4. OUI database ---
echo "[4/7] Downloading IEEE OUI database..."
OUI_PATH="${DATA_DIR}/oui.txt"
if [[ -f "${OUI_PATH}" ]]; then
    # Re-download if older than 30 days
    if [[ $(find "${OUI_PATH}" -mtime +30 -print 2>/dev/null) ]]; then
        echo "  OUI file is >30 days old, re-downloading..."
        curl -sS -o "${OUI_PATH}" "${OUI_URL}" || echo "  WARNING: Download failed, keeping existing file"
    else
        echo "  OUI file is recent, skipping download."
    fi
else
    if curl -sS -o "${OUI_PATH}" "${OUI_URL}"; then
        echo "  Downloaded to ${OUI_PATH}"
    else
        echo "  WARNING: OUI download failed. Vendor lookups will be disabled."
        echo "  You can manually download from: ${OUI_URL}"
    fi
fi

if [[ -f "${OUI_PATH}" ]]; then
    OUI_COUNT=$(grep -c "(hex)" "${OUI_PATH}" 2>/dev/null || echo "0")
    echo "  OUI entries: ${OUI_COUNT}"
fi

# --- 5. Database schema ---
echo "[5/7] Applying database schema..."
"${PYTHON}" -m sentinel.db.schema --config "${CONFIG}"

# --- 6. Systemd units ---
echo "[6/7] Installing systemd units..."
SYSTEMD_DIR="${INSTALL_DIR}/systemd"

# System-level units (capture daemons that need root)
for unit in sentinel-wifi.service sentinel-bt.service sentinel-sdr-433.service; do
    sudo cp "${SYSTEMD_DIR}/${unit}" /etc/systemd/system/
    sudo systemctl daemon-reload
    # Stage B: manual-start mode. Units install but do not auto-start at
    # boot. Bring them up via ./sentinel.sh start (the only blessed path).
    sudo systemctl disable "${unit}" 2>/dev/null || true
    echo "  Installed system unit: ${unit}"
done

# User-level units
USER_SYSTEMD_DIR="${HOME}/.config/systemd/user"
mkdir -p "${USER_SYSTEMD_DIR}"
for unit in sentinel-ingest.service sentinel-detector.service sentinel-profiler.service sentinel-profiler.timer sentinel-sdr-adsb.service; do
    cp "${SYSTEMD_DIR}/${unit}" "${USER_SYSTEMD_DIR}/"
    echo "  Installed user unit: ${unit}"
done
systemctl --user daemon-reload
# Stage B: manual-start mode. Units install but do not auto-start at boot.
# Bring them up via ./sentinel.sh start (the only blessed path).
for unit in sentinel-ingest.service sentinel-detector.service sentinel-profiler.timer sentinel-sdr-adsb.service; do
    systemctl --user disable "${unit}" 2>/dev/null || true
done

# --- 7. Enable lingering (so user units survive logout) ---
echo "[7/7] Enabling systemd lingering..."
sudo loginctl enable-linger "$(whoami)" 2>/dev/null || echo "  WARNING: Could not enable lingering"

# --- Make entrypoint executable ---
chmod +x "${INSTALL_DIR}/sentinel.sh"

echo ""
echo "========================================"
echo "  Installation complete!"
echo ""
echo "  Start:    ./sentinel.sh start"
echo "  Status:   ./sentinel.sh status"
echo "  Selftest: ./sentinel.sh selftest"
echo "  CLI:      ./sentinel.sh devices"
echo "  Logs:     ./sentinel.sh logs"
echo "========================================"
