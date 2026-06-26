# SENTINEL OPERATOR'S MANUAL

**Version:** 1.0 (April 2026)
**Platform:** Raspberry Pi 5 + Kali Linux (primary host) | Framework 16 + Ubuntu (remote ops)
**Database:** `/home/user/sentinel/data/sentinel.db` (SQLite, WAL mode)
**Audience:** You. The operator.

This is a field manual, not a tutorial. Every block is copy-paste-ready. Sections are independent — jump to what you need.

This manual lives in TWO places:
- On the **Framework**: `~/projects/sentinel/OPERATOR_MANUAL.md` (source of truth)
- On the **Pi**: `/home/user/sentinel/OPERATOR_MANUAL.md` (for when you're SSH'd in alone)

Keep both in sync with `rsync` (see Section 19).

---

## TABLE OF CONTENTS

```
PART I — FOUNDATIONS
  1. Architecture and Mental Model
  2. The Full Data Model (every table explained)
  3. Installation and Deployment
  4. Daily Lifecycle Commands

PART II — THE CLI
  5. Built-in sentinel.sh Commands
  6. The 12-Query Cheatsheet

PART III — THE QUERY LIBRARY (Ferrari Gear)
  7. Identity and Recognition
  8. Rhythm and Pattern
  9. Proximity and Distance
  10. Network Topology Reconstruction
  11. Probe Cluster Analysis (MAC Randomization Defeat)
  12. BLE Intelligence
  13. Alert Intelligence
  14. SSID and Network-Name Intelligence
  15. Session Reconstruction
  16. Statistical and Diagnostic Queries

PART IV — INVESTIGATION PLAYBOOKS
  17. 14 Scenarios with Step-by-Step Query Sequences

PART V — OPERATIONS
  18. Live Monitoring — Watch Windows
  19. Database Maintenance
  20. Remote Access Patterns
  21. Watchlists and Automated Monitoring
  22. Export and Offline Analysis

PART VI — SURVIVAL
  23. Troubleshooting Tree
  24. Dictionary (802.11 frame types, BLE mfr IDs, RSSI, OUI, channels)
  25. Emergency Commands
  26. Quick-Reference Card
```

---

# PART I — FOUNDATIONS

## 1. Architecture and Mental Model

### 1.1 What Sentinel Is

Sentinel is a **time machine for the RF neighborhood around your antenna.**

Every row in the database is a moment where something was detected. You can rewind to any moment and ask:

- **WHO** was there (MACs, vendors, fingerprints, clusters)
- **WHEN** they were there (microsecond timestamps)
- **WHERE** they were (RSSI = distance proxy; GPS once the hat arrives)
- **WHAT** they were doing (frame type, traffic pattern, associations)

Every useful question is some combination of those axes. Once you think in WHO/WHEN/WHERE/WHAT, every query writes itself.

### 1.2 The Five-Layer Pipeline

```
     ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
     │ WiFi capture │  │ BT capture   │  │ SDR/GPS stubs│
     │  (wlan1)     │  │  (hci0)      │  │              │
     └──────┬───────┘  └──────┬───────┘  └──────┬───────┘
            │                 │                 │
            └─────────────────┼─────────────────┘
                              ▼
                    ┌─────────────────┐
                    │   Unix-socket   │
                    │      BUS        │
                    └────────┬────────┘
                             ▼
                    ┌─────────────────┐
                    │    INGEST       │  (OUI lookup, enrichment,
                    │    daemon       │   batched writes to DB)
                    └────────┬────────┘
                             ▼
              ┌──────────────────────────────┐
              │      SQLite DB (WAL)         │
              │  /home/user/sentinel/data/   │
              └──┬───────────────────────┬───┘
                 │                       │
                 ▼                       ▼
         ┌──────────────┐        ┌──────────────┐
         │  PROFILER    │        │  DETECTOR    │
         │ (every 15m)  │        │ (continuous) │
         └──────┬───────┘        └──────┬───────┘
                │                       │
                └────── profiles ───────┤
                        clusters        │
                                        ▼
                                  ┌──────────────┐
                                  │   ALERTS     │
                                  └──────────────┘
```

- **Capture daemons** emit normalized events (WiFi frames, BLE advertisements)
- **Bus** is a Unix domain socket at `/run/sentinel/bus.sock` — IPC between capture and ingest
- **Ingest** batches writes, does OUI lookup, maintains `devices` and `observations` tables
- **Profiler** builds per-device statistical profiles and probe-SSID clusters every 15 min
- **Detector** scores incoming events against profiles and writes anomaly alerts

### 1.3 Learning Mode

First **7 days** after install, only `new_device` alerts fire at "info" severity. The profiler needs baseline data before its anomaly scoring is meaningful. After day 7, full anomaly detection kicks in: temporal, location, behavioral, absence, correlation, probe-set-cluster alerts at info/low/medium/high severity.

### 1.4 The One File That Is Everything

`/home/user/sentinel/data/sentinel.db` + `sentinel.db-wal` + `sentinel.db-shm` IS your entire knowledge base. Backup, move, copy those three together. Nothing else matters.

Deleting those three and restarting = fresh start. Zero history.

### 1.5 What Sentinel Does NOT Do

Critical for ethical clarity and expectation-setting:

- **No packet content.** Data frame payloads are encrypted (WPA2/WPA3) and Sentinel doesn't decrypt them. We capture headers only.
- **No password cracking.** No deauth attacks. No WPA handshake capture for offline brute force. No evil-twin attacks.
- **No active transmission.** The Alfa is listen-only in monitor mode. The Pi Bluetooth is scan-only, not advertising. Zero RF emissions caused by Sentinel.
- **No PII beyond what devices leak themselves.** MACs, vendor lookups, BLE-advertised device names. If a device names itself "John's iPhone," you see that. If it doesn't, you don't.
- **No cloud. No telemetry. No upload.** All data stays on the Pi.
- **No GPS of captured devices.** Sentinel logs the Pi's location (when GPS hat present). It cannot localize a target phone beyond "within RF range of this antenna."

---

## 2. The Full Data Model

13 tables. Know them cold — most queries are `SELECT ... FROM <one of these> JOIN devices ...`.

### 2.1 Primary Tables

**`devices`** — one row per unique MAC ever observed.
```
mac TEXT PRIMARY KEY
first_seen TEXT              -- ISO timestamp with TZ
last_seen TEXT
vendor TEXT                  -- from OUI lookup
device_name TEXT             -- from BLE ads (nullable)
device_type TEXT             -- 'wifi' | 'ble' | 'bt_classic'
is_ap INTEGER                -- 0 | 1 (flagged when acts like AP)
probe_cluster_id TEXT        -- FK to probe_clusters (nullable)
notes TEXT                   -- operator annotations (nullable)
```

**`observations`** — every RF detection event, denormalized.
```
id INTEGER PRIMARY KEY
mac TEXT
timestamp TEXT
source TEXT                  -- 'wifi' | 'ble' | 'bt' | 'sdr' | 'synthetic'
rssi INTEGER                 -- negative, dBm
channel INTEGER              -- WiFi channel or BLE advertising channel
lat REAL                     -- nullable, from GPS or config fallback
lon REAL                     -- nullable
session_id TEXT              -- FK to sessions
```

**`wifi_frames`** — every 802.11 frame captured.
```
id INTEGER PRIMARY KEY
timestamp TEXT
src_mac TEXT
dst_mac TEXT
bssid TEXT
ssid TEXT                    -- nullable
frame_type INTEGER           -- 0=mgmt, 1=ctrl, 2=data
frame_subtype INTEGER
rssi INTEGER
channel INTEGER
sequence_number INTEGER
ie_bytes BLOB                -- raw Information Elements (Level C future)
```

**`probe_requests`** — extracted subset of probe-request frames for fingerprinting.
```
id INTEGER PRIMARY KEY
timestamp TEXT
mac TEXT
ssid TEXT
rssi INTEGER
channel INTEGER
sequence_number INTEGER
ie_bytes BLOB
```

**`bt_advertisements`** — every BLE advertisement captured.
```
id INTEGER PRIMARY KEY
timestamp TEXT
mac TEXT
device_name TEXT             -- often NULL (modern phones randomize)
rssi INTEGER
manufacturer_data_hex TEXT   -- raw mfr data, hex-encoded
service_uuids TEXT           -- comma-separated 16-bit or 128-bit UUIDs
tx_power INTEGER
device_class INTEGER         -- classic BT device class
```

### 2.2 Derived Tables (built by profiler)

**`device_profiles`** — one row per device with enough data.
```
mac TEXT PRIMARY KEY
updated_at TEXT
time_histogram TEXT          -- JSON: 24-entry int array (per-hour count)
rssi_mean REAL
rssi_stddev REAL
rssi_p95 REAL
channel_set TEXT             -- JSON array
probe_ssid_set TEXT          -- JSON array
probe_rate_mean REAL         -- probes/min
companion_macs TEXT          -- JSON array of MACs seen within ±60s
presence_pct_30d REAL        -- % of 5-min windows device was present (last 30d)
total_observations INTEGER
extra_json TEXT              -- future-use extensions
```

**`probe_clusters`** — groups of randomized MACs that share probe SSID sets.
```
cluster_id TEXT PRIMARY KEY
first_seen TEXT
last_seen TEXT
representative_ssid_set TEXT -- JSON
```

**`probe_cluster_members`** — which MACs belong to which cluster.
```
cluster_id TEXT
mac TEXT
first_seen_in_cluster TEXT
PRIMARY KEY (cluster_id, mac)
```

### 2.3 Supporting Tables

**`alerts`** — anomalies written by detector.
```
id INTEGER PRIMARY KEY
timestamp TEXT
mac TEXT
alert_type TEXT              -- new_device|temporal|location|behavioral|absence|correlation|probe_set_cluster
severity TEXT                -- info|low|medium|high
description TEXT             -- human-readable
detail_json TEXT             -- structured context
acknowledged INTEGER         -- 0|1
```

**`sessions`** — when Sentinel itself was running (distinguishes "device absent" from "sensor down").
```
session_id TEXT PRIMARY KEY
started_at TEXT
ended_at TEXT                -- NULL if still running
capture_sources TEXT         -- JSON array
notes TEXT
```

**`gps_fixes`** — GPS location history (empty until hat arrives).
```
id INTEGER PRIMARY KEY
timestamp TEXT
lat REAL
lon REAL
accuracy_m REAL
```

**`sdr_observations`** — RTL-SDR spectrum data (empty until dongle arrives).
```
id INTEGER PRIMARY KEY
timestamp TEXT
frequency_mhz REAL
signal_strength REAL
modulation TEXT
raw_data BLOB
```

**`oui_vendors`** — IEEE OUI lookup table (populated on install, 39k+ rows).
```
oui TEXT PRIMARY KEY         -- first 3 octets normalized
vendor TEXT
```

**`schema_version`** — internal versioning.

### 2.4 Knowing the Schema Live

```bash
sqlite3 ~/sentinel/data/sentinel.db ".schema"
sqlite3 ~/sentinel/data/sentinel.db ".tables"
sqlite3 ~/sentinel/data/sentinel.db ".indices"
```

For one table:

```bash
sqlite3 ~/sentinel/data/sentinel.db ".schema devices"
```

---

## 3. Installation and Deployment

### 3.1 Initial Install (Pi, first time)

Assumes:
- Kali on Pi 5 (aarch64, kernel ≥ 6.12)
- Alfa AWUS036ACM (MT7612U) plugged in → appears as `wlan1`
- Pi onboard WiFi on `wlan0`, Bluetooth on `hci0`
- Ethernet connected for initial management

```bash
# SSH in
ssh user@192.168.1.100

# Transfer project from Framework (run on Framework first)
# Skip this if already done:
# rsync -avz --exclude '.venv' --exclude '__pycache__' --exclude 'data' \
#   ~/projects/sentinel/ user@192.168.1.100:/home/user/sentinel/

# Install
cd ~/sentinel
bash install.sh
```

Install runs 7 stages:
1. apt system packages (bluez, aircrack-ng, iw, libglib2-dev, etc.)
2. Python venv at `~/sentinel/.venv`
3. pip install editable project + deps (scapy, bleak, dbus-next, pyyaml, click)
4. Create data directories (`data/`, `data/logs/`)
5. Download IEEE OUI database
6. Apply `schema.sql` (13 tables, ~20 indices)
7. Install systemd units (2 system-level, 3 user + 1 timer)

### 3.2 Verify Installation

```bash
cd ~/sentinel && ./sentinel.sh selftest
```

Expected before start: `5 passed, 5 failed` (the 5 "failed" are just "systemd units inactive" — they'll flip to OK after start).

### 3.3 Start

```bash
./sentinel.sh start
./sentinel.sh selftest
```

Should now show `10 passed, 0 failed`.

### 3.4 Known Side Effect: NetworkManager Dies

The `sentinel-wifi` service runs `airmon-ng start wlan1`, which calls `airmon-ng check kill` by default on Kali. This **kills NetworkManager**, which means:

- `wlan0` (Pi onboard WiFi) drops its connection
- The Pi desktop WiFi indicator shows "off"
- Eth0 is your only remaining network path

This is by design (NM aggressively fights monitor mode). Workaround: keep the Pi on ethernet while Sentinel runs. To bring wlan0 back temporarily:

```bash
sudo systemctl start NetworkManager
sudo nmcli device connect wlan0
```

Future fix (Stage 13): configure airmon to not kill NM globally, only exclude wlan1 from NM management.

### 3.5 Updating Code on Pi

From Framework:

```bash
rsync -avz --exclude '.venv' --exclude '__pycache__' --exclude 'data' --exclude '.git' \
  ~/projects/sentinel/ user@192.168.1.100:/home/user/sentinel/
```

On Pi:

```bash
cd ~/sentinel
./sentinel.sh stop
.venv/bin/pip install -e .   # only if dependencies changed
bash install.sh              # only if schema or systemd units changed
./sentinel.sh start
```

### 3.6 Fresh Reinstall (Keep Data)

```bash
cd ~/sentinel
./sentinel.sh stop
rm -rf .venv
bash install.sh
./sentinel.sh start
```

### 3.7 Nuclear Reinstall (Wipe Data)

```bash
cd ~/sentinel
./sentinel.sh stop
rm -rf .venv data
sudo rm -f /etc/systemd/system/sentinel-wifi.service /etc/systemd/system/sentinel-bt.service
rm -f ~/.config/systemd/user/sentinel-*.service ~/.config/systemd/user/sentinel-*.timer
sudo systemctl daemon-reload
systemctl --user daemon-reload
bash install.sh
./sentinel.sh start
```

---

## 4. Daily Lifecycle Commands

All from `~/sentinel` on the Pi.

### Start / Stop / Restart

```bash
./sentinel.sh start
./sentinel.sh stop
./sentinel.sh restart
```

### Status

```bash
./sentinel.sh status
```

Prints: systemd unit states, device count, observation count, alerts (total + unacked), last-hour activity.

### Selftest

```bash
./sentinel.sh selftest
```

10 checks. All OK = healthy.

### Logs

**All daemons, followed:**

```bash
./sentinel.sh logs
```

**Specific daemon:**

```bash
sudo journalctl -u sentinel-wifi -f
sudo journalctl -u sentinel-bt -f
journalctl --user -u sentinel-ingest -f
journalctl --user -u sentinel-detector -f
journalctl --user -u sentinel-profiler -f
```

**Show last N lines without following:**

```bash
sudo journalctl -u sentinel-wifi -n 100 --no-pager
```

### Force Profiler to Run Now

```bash
systemctl --user start sentinel-profiler.service
```

Useful after data seeding, testing, or when you want clusters to refresh immediately.

### Verify Auto-Start on Boot

```bash
systemctl is-enabled sentinel-wifi sentinel-bt
systemctl --user is-enabled sentinel-ingest sentinel-detector sentinel-profiler.timer
```

All should say `enabled`.
---

# PART II — THE CLI

## 5. Built-in sentinel.sh Commands

Every command here runs as `./sentinel.sh <verb>` from `~/sentinel`.

### 5.1 `status`

Complete system snapshot.

```bash
./sentinel.sh status
```

### 5.2 `devices` — list devices with filters

Every flag:

```bash
./sentinel.sh devices                         # all, most-recent first
./sentinel.sh devices --limit 100             # increase from default 50
./sentinel.sh devices --seen-in 24            # active in last 24 hours
./sentinel.sh devices --since "2026-04-20"    # first_seen or last_seen after date
./sentinel.sh devices --new-since "2026-04-19" # first_seen after date
./sentinel.sh devices --vendor Apple          # substring match on vendor
./sentinel.sh devices --type wifi             # wifi | ble | bt_classic
./sentinel.sh devices --is-ap                 # only access points
./sentinel.sh devices --not-ap                # exclude access points
./sentinel.sh devices --json                  # machine-readable
./sentinel.sh devices --sort first_seen       # first_seen | last_seen | obs_count
```

Combine flags freely:

```bash
./sentinel.sh devices --vendor Apple --seen-in 1 --not-ap --sort last_seen
```

### 5.3 `device <mac>` — full profile of one device

```bash
./sentinel.sh device aa:bb:cc:00:00:01
./sentinel.sh device aa:bb:cc:00:00:01 --json
./sentinel.sh device aa:bb:cc:00:00:01 --obs 50   # increase obs limit from 10
```

Output sections: device record, recent observations, SSID probe set, cluster membership, recent alerts, profile.

### 5.4 `alerts` — recent anomaly events

```bash
./sentinel.sh alerts
./sentinel.sh alerts --severity high
./sentinel.sh alerts --severity medium,high
./sentinel.sh alerts --type temporal
./sentinel.sh alerts --since "2026-04-20"
./sentinel.sh alerts --mac aa:bb:cc:00:00:01
./sentinel.sh alerts --unacked
./sentinel.sh alerts --json
./sentinel.sh alerts --ack 142                # mark alert id 142 acknowledged
./sentinel.sh alerts --ack-all-before "2026-04-15"
```

### 5.5 `watch` — live tail

```bash
./sentinel.sh watch                           # all events + alerts
./sentinel.sh watch --alerts-only             # only alerts
./sentinel.sh watch --severity medium,high    # high-severity only
./sentinel.sh watch --mac aa:bb:cc:00:00:01   # focus one device
```

Press `q` to quit.

### 5.6 `query "<sql>"` — arbitrary read-only SQL

```bash
./sentinel.sh query "SELECT COUNT(*) FROM observations"
./sentinel.sh query "SELECT * FROM alerts WHERE severity='high' LIMIT 10"
./sentinel.sh query "SELECT vendor, COUNT(*) FROM devices GROUP BY vendor" --json
```

DB is opened read-only. Safe to experiment.

### 5.7 `export <table>` — CSV or JSON dumps

```bash
./sentinel.sh export devices --format csv > devices.csv
./sentinel.sh export alerts --format json > alerts.json
./sentinel.sh export observations --format csv --since "2026-04-20" > today.csv
./sentinel.sh export wifi_frames --format csv --since "2026-04-20 02:00" --until "2026-04-20 04:00" > session.csv
./sentinel.sh export probe_requests --format csv > probes.csv
./sentinel.sh export bt_advertisements --format csv > ble.csv
```

### 5.8 `annotate <mac> <note>` — operator notes on a device

```bash
./sentinel.sh annotate aa:bb:cc:00:00:01 "My iPhone 15 Pro — confirmed mine, do not alert"
./sentinel.sh annotate aa:bb:cc:00:00:02 "Home router 2.4GHz — ISP gateway"
./sentinel.sh annotate aa:bb:cc:00:00:03 "Framework Laptop 16 — primary dev machine"
```

Stored in `devices.notes` column. Surfaces in `device <mac>` output.

### 5.9 `whitelist <mac>` — suppress alerts for a device

```bash
./sentinel.sh whitelist aa:bb:cc:00:00:01
./sentinel.sh whitelist-remove aa:bb:cc:00:00:01
./sentinel.sh whitelist-list
```

### 5.10 `selftest`, `start`, `stop`, `restart`, `logs`

See Section 4.

---

## 6. The 12-Query Cheatsheet

Save this as `~/sentinel-queries.sh` on the Pi. This is your front-door — 90% of daily use is covered here. Every query below is safe and read-only.

### 6.1 Install the Cheatsheet (copy-paste block)

```bash
cat > ~/sentinel-queries.sh <<'QSCRIPT_EOF'
#!/bin/bash
# Sentinel query cheatsheet — 12 canonical questions.
# Usage: ./sentinel-queries.sh <query-number|name> [arg]

DB=~/sentinel/data/sentinel.db
Q="sqlite3 -column -header $DB"

case "$1" in
    1|now)
        echo "=== Devices seen in last 5 minutes ==="
        $Q "SELECT mac, vendor, device_type, COUNT(*) as obs
            FROM observations o LEFT JOIN devices d USING(mac)
            WHERE timestamp > datetime('now','-5 minutes')
            GROUP BY mac ORDER BY obs DESC LIMIT 30"
        ;;
    2|hour)
        echo "=== Devices typically here at hour $(date +%H):00 (last 7 days) ==="
        $Q "SELECT mac, vendor, COUNT(*) as times_seen_this_hour
            FROM observations o LEFT JOIN devices d USING(mac)
            WHERE strftime('%H', timestamp) = strftime('%H', 'now')
              AND timestamp > datetime('now','-7 days')
            GROUP BY mac ORDER BY times_seen_this_hour DESC LIMIT 30"
        ;;
    3|known)
        [ -z "$2" ] && { echo "Usage: $0 known <mac>"; exit 1; }
        echo "=== History of $2 ==="
        $Q "SELECT first_seen, last_seen, vendor, device_type, is_ap FROM devices WHERE mac='$2'"
        $Q "SELECT COUNT(*) as total_obs, MIN(timestamp) as first, MAX(timestamp) as last
            FROM observations WHERE mac='$2'"
        ;;
    4|whois)
        [ -z "$2" ] && { echo "Usage: $0 whois <mac>"; exit 1; }
        echo "=== Full profile of $2 ==="
        echo "--- Device ---"
        $Q "SELECT * FROM devices WHERE mac='$2'"
        echo "--- Recent observations ---"
        $Q "SELECT timestamp, source, rssi, channel FROM observations WHERE mac='$2' ORDER BY timestamp DESC LIMIT 10"
        echo "--- SSIDs this device probes for ---"
        $Q "SELECT DISTINCT ssid FROM probe_requests WHERE mac='$2' AND ssid != ''"
        echo "--- Profile ---"
        $Q "SELECT * FROM device_profiles WHERE mac='$2'"
        ;;
    5|rhythm)
        echo "=== Hourly observation rhythm, last 7 days ==="
        $Q "SELECT strftime('%H', timestamp) as hour, COUNT(*) as events,
                   COUNT(DISTINCT mac) as unique_devices
            FROM observations
            WHERE timestamp > datetime('now','-7 days')
            GROUP BY hour ORDER BY hour"
        ;;
    6|regulars)
        echo "=== Regulars (seen on 3+ different days) ==="
        $Q "SELECT mac, vendor, COUNT(DISTINCT date(timestamp)) as days_seen, COUNT(*) as obs
            FROM observations o LEFT JOIN devices d USING(mac)
            GROUP BY mac HAVING days_seen >= 3
            ORDER BY days_seen DESC, obs DESC LIMIT 30"
        ;;
    7|new)
        echo "=== New devices in last 24 hours ==="
        $Q "SELECT mac, vendor, device_type, first_seen, is_ap
            FROM devices WHERE first_seen > datetime('now','-1 day')
            ORDER BY first_seen DESC LIMIT 30"
        ;;
    8|close)
        echo "=== Closest devices right now (last 5 min, strongest RSSI) ==="
        $Q "SELECT mac, vendor, MAX(rssi) as strongest_rssi, COUNT(*) as obs
            FROM observations o LEFT JOIN devices d USING(mac)
            WHERE timestamp > datetime('now','-5 minutes')
            GROUP BY mac ORDER BY strongest_rssi DESC LIMIT 20"
        ;;
    9|close-at)
        [ -z "$2" ] && { echo "Usage: $0 close-at 'YYYY-MM-DD HH:MM'"; exit 1; }
        echo "=== Close devices at time $2 (within 10 min window) ==="
        $Q "SELECT mac, vendor, MAX(rssi) as strongest, COUNT(*) as obs
            FROM observations o LEFT JOIN devices d USING(mac)
            WHERE timestamp BETWEEN datetime('$2','-5 minutes') AND datetime('$2','+5 minutes')
            GROUP BY mac ORDER BY strongest DESC LIMIT 20"
        ;;
    10|rssi)
        [ -z "$2" ] && { echo "Usage: $0 rssi <mac>"; exit 1; }
        echo "=== RSSI distribution for $2 ==="
        $Q "SELECT MIN(rssi) as min, MAX(rssi) as max, ROUND(AVG(rssi),1) as avg, COUNT(*) as n
            FROM observations WHERE mac='$2'"
        echo "--- By hour ---"
        $Q "SELECT strftime('%H', timestamp) as hour, ROUND(AVG(rssi),1) as avg_rssi, COUNT(*) as n
            FROM observations WHERE mac='$2' GROUP BY hour ORDER BY hour"
        ;;
    11|ssids)
        [ -z "$2" ] && { echo "Usage: $0 ssids <mac>"; exit 1; }
        echo "=== SSIDs $2 has probed for ==="
        $Q "SELECT DISTINCT ssid, COUNT(*) as probe_count
            FROM probe_requests WHERE mac='$2' AND ssid != ''
            GROUP BY ssid ORDER BY probe_count DESC"
        ;;
    12|who-probes)
        [ -z "$2" ] && { echo "Usage: $0 who-probes <ssid>"; exit 1; }
        echo "=== Devices that have probed for '$2' ==="
        $Q "SELECT mac, vendor, COUNT(*) as probes, MIN(timestamp) as first, MAX(timestamp) as last
            FROM probe_requests p LEFT JOIN devices d USING(mac)
            WHERE ssid='$2'
            GROUP BY mac ORDER BY probes DESC"
        ;;
    *)
        cat <<HELP
Sentinel Query Cheatsheet — 12 canonical queries

  1  now                    Devices here in last 5 min
  2  hour                   Devices typical at this hour (7-day avg)
  3  known <mac>            Summary of one MAC
  4  whois <mac>            Full profile of one MAC
  5  rhythm                 Hourly event pattern, last 7 days
  6  regulars               Devices seen on 3+ different days
  7  new                    New devices in last 24 hours
  8  close                  Closest devices right now
  9  close-at 'TIME'        Who was close at a specific time
  10 rssi <mac>             RSSI distribution for one device
  11 ssids <mac>            SSIDs this device has probed for
  12 who-probes <ssid>      Who has probed for this SSID

Examples:
  ~/sentinel-queries.sh now
  ~/sentinel-queries.sh whois aa:bb:cc:00:00:01
  ~/sentinel-queries.sh who-probes "HomeNetwork-2G"
  ~/sentinel-queries.sh close-at "2026-04-20 03:00"
HELP
        ;;
esac
QSCRIPT_EOF
chmod +x ~/sentinel-queries.sh
```

### 6.2 Verify Install

```bash
ls -lh ~/sentinel-queries.sh
~/sentinel-queries.sh              # no args -> help
```
---

# PART III — THE QUERY LIBRARY (FERRARI GEAR)

These are the queries that unlock Sentinel's real power. The 12-question cheatsheet covers the 80%; this library covers the advanced 20%. Every block is copy-paste-ready.

**Note:** in examples, replace placeholder MACs and SSIDs with your actual values.

## 7. Identity and Recognition

### 7.1 Full vendor distribution (all devices ever seen)

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT vendor, device_type, COUNT(*) as device_count
FROM devices
GROUP BY vendor, device_type
ORDER BY device_count DESC LIMIT 40"
```

### 7.2 Devices with human-readable BLE names (the low-privacy devices)

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT DISTINCT mac, device_name, COUNT(*) as broadcasts
FROM bt_advertisements
WHERE device_name IS NOT NULL AND device_name != ''
GROUP BY mac, device_name
ORDER BY broadcasts DESC LIMIT 50"
```

### 7.3 Every known access point with SSID resolution

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT 
  d.mac as bssid,
  d.vendor,
  COALESCE(
    (SELECT ssid FROM wifi_frames wf 
     WHERE wf.src_mac=d.mac AND wf.frame_type=0 AND wf.frame_subtype=8 
       AND wf.ssid IS NOT NULL AND wf.ssid != ''
     LIMIT 1),
    'hidden/unseen'
  ) as ssid,
  (SELECT COUNT(*) FROM observations o WHERE o.mac=d.mac) as obs,
  d.last_seen
FROM devices d
WHERE d.is_ap=1
ORDER BY obs DESC LIMIT 40"
```

### 7.4 Randomized (locally-administered) vs factory MACs

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT 
  CASE 
    WHEN CAST(('0x' || SUBSTR(mac, 2, 1)) AS INTEGER) & 2 = 2 THEN 'randomized'
    ELSE 'factory'
  END as mac_type,
  device_type,
  COUNT(*) as count
FROM devices
GROUP BY mac_type, device_type
ORDER BY count DESC"
```

### 7.5 Count of devices by each byte of first octet (OUI frequency)

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT SUBSTR(mac, 1, 8) as oui, vendor, COUNT(*) as devices_with_this_oui
FROM devices
GROUP BY oui
ORDER BY devices_with_this_oui DESC LIMIT 20"
```

### 7.6 Find devices matching vendor substring

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT mac, vendor, device_type, first_seen, last_seen
FROM devices
WHERE LOWER(vendor) LIKE LOWER('%apple%')
ORDER BY last_seen DESC LIMIT 50"
```

Replace `apple` with: `samsung`, `amazon`, `wyze`, `espressif`, `sagemcom`, `sercomm`, etc.

---

## 8. Rhythm and Pattern

### 8.1 24-hour presence heatmap for last 7 days

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT 
  strftime('%H', timestamp) as hour,
  COUNT(*) as events,
  COUNT(DISTINCT mac) as unique_devices,
  ROUND(AVG(rssi), 1) as avg_rssi
FROM observations
WHERE timestamp > datetime('now','-7 days')
GROUP BY hour ORDER BY hour"
```

### 8.2 Day-of-week rhythm

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT 
  CASE strftime('%w', timestamp)
    WHEN '0' THEN 'Sun' WHEN '1' THEN 'Mon' WHEN '2' THEN 'Tue'
    WHEN '3' THEN 'Wed' WHEN '4' THEN 'Thu' WHEN '5' THEN 'Fri'
    WHEN '6' THEN 'Sat'
  END as dow,
  COUNT(*) as events,
  COUNT(DISTINCT mac) as unique_devices
FROM observations
WHERE timestamp > datetime('now','-30 days')
GROUP BY strftime('%w', timestamp)
ORDER BY strftime('%w', timestamp)"
```

### 8.3 Night owls — devices active 00:00–04:59

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT d.mac, d.vendor, d.device_type,
       COUNT(*) as night_obs,
       COUNT(DISTINCT date(o.timestamp)) as nights
FROM observations o
JOIN devices d USING(mac)
WHERE CAST(strftime('%H', o.timestamp) AS INTEGER) BETWEEN 0 AND 4
  AND o.timestamp > datetime('now','-14 days')
GROUP BY d.mac
HAVING nights >= 2
ORDER BY night_obs DESC LIMIT 40"
```

### 8.4 Daytime devices (09:00–17:00)

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT d.mac, d.vendor, COUNT(*) as day_obs,
       COUNT(DISTINCT date(o.timestamp)) as days
FROM observations o
JOIN devices d USING(mac)
WHERE CAST(strftime('%H', o.timestamp) AS INTEGER) BETWEEN 9 AND 17
  AND o.timestamp > datetime('now','-14 days')
GROUP BY d.mac
HAVING days >= 3
ORDER BY day_obs DESC LIMIT 40"
```

### 8.5 First appearance chronology (last week)

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT first_seen, mac, vendor, device_type, is_ap
FROM devices
WHERE first_seen > datetime('now','-7 days')
ORDER BY first_seen ASC"
```

### 8.6 Devices that have disappeared (absent 3+ days but previously regular)

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT d.mac, d.vendor, d.last_seen,
       CAST((julianday('now') - julianday(d.last_seen)) AS INTEGER) as days_absent,
       (SELECT COUNT(DISTINCT date(o.timestamp)) FROM observations o WHERE o.mac=d.mac) as prior_days
FROM devices d
WHERE julianday('now') - julianday(d.last_seen) >= 3
  AND (SELECT COUNT(DISTINCT date(o.timestamp)) FROM observations o WHERE o.mac=d.mac) >= 5
ORDER BY prior_days DESC LIMIT 30"
```

### 8.7 Arrival / departure pattern for one device

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
WITH obs_with_gap AS (
  SELECT timestamp, 
         LAG(timestamp) OVER (ORDER BY timestamp) as prev_ts
  FROM observations
  WHERE mac='aa:bb:cc:00:00:01'
),
sessions AS (
  SELECT timestamp,
         CASE WHEN prev_ts IS NULL 
                OR (julianday(timestamp) - julianday(prev_ts)) * 1440 > 30 
              THEN 1 ELSE 0 END as new_session
  FROM obs_with_gap
)
SELECT timestamp as arrival_or_departure, new_session
FROM sessions
WHERE new_session = 1
ORDER BY timestamp DESC LIMIT 30"
```

Identifies sessions separated by 30+ min gaps = arrivals. Replace MAC.

---

## 9. Proximity and Distance

### 9.1 Closest approaches ever (strongest RSSI per device)

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT d.mac, d.vendor, MAX(o.rssi) as closest_ever,
       COUNT(*) as total_obs,
       datetime(MIN(CASE WHEN o.rssi = (SELECT MAX(rssi) FROM observations WHERE mac=d.mac) THEN o.timestamp END)) as closest_time
FROM observations o
JOIN devices d USING(mac)
GROUP BY d.mac
HAVING closest_ever > -40
ORDER BY closest_ever DESC LIMIT 30"
```

### 9.2 Unusually close devices RIGHT NOW

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT mac, d.vendor, MAX(rssi) as rssi_now, COUNT(*) as obs
FROM observations o
LEFT JOIN devices d USING(mac)
WHERE timestamp > datetime('now','-2 minutes')
  AND rssi > -50
GROUP BY mac
ORDER BY rssi_now DESC LIMIT 30"
```

### 9.3 Devices getting closer over time (trending stronger RSSI)

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
WITH daily_strongest AS (
  SELECT mac, date(timestamp) as day, MAX(rssi) as daily_max
  FROM observations
  WHERE timestamp > datetime('now','-14 days')
  GROUP BY mac, day
)
SELECT mac, d.vendor,
       MIN(daily_max) as earliest_max,
       MAX(daily_max) as recent_max,
       (MAX(daily_max) - MIN(daily_max)) as dbm_increase,
       COUNT(DISTINCT day) as days_tracked
FROM daily_strongest
LEFT JOIN devices d USING(mac)
GROUP BY mac
HAVING days_tracked >= 3
   AND dbm_increase > 10
ORDER BY dbm_increase DESC LIMIT 20"
```

### 9.4 RSSI histogram for one device

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT 
  CASE 
    WHEN rssi > -30 THEN '-30+ (inches)'
    WHEN rssi > -45 THEN '-45 to -30 (same room)'
    WHEN rssi > -60 THEN '-60 to -45 (same house)'
    WHEN rssi > -75 THEN '-75 to -60 (next door)'
    WHEN rssi > -85 THEN '-85 to -75 (down the block)'
    ELSE 'below -85 (fringe)'
  END as distance_bucket,
  COUNT(*) as observations
FROM observations
WHERE mac='aa:bb:cc:00:00:01'
GROUP BY distance_bucket
ORDER BY MIN(rssi) DESC"
```

### 9.5 All devices within conversational range right now (RSSI > -40)

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT mac, d.vendor, MAX(rssi) as rssi, MAX(timestamp) as seen_at
FROM observations o
LEFT JOIN devices d USING(mac)
WHERE timestamp > datetime('now','-5 minutes')
  AND rssi > -40
GROUP BY mac
ORDER BY rssi DESC"
```

### 9.6 Motion detection (RSSI delta over short windows — device moving)

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
WITH windows AS (
  SELECT mac,
         strftime('%Y-%m-%d %H:%M', timestamp) as minute,
         MIN(rssi) as min_rssi,
         MAX(rssi) as max_rssi
  FROM observations
  WHERE timestamp > datetime('now','-1 hour')
  GROUP BY mac, minute
  HAVING COUNT(*) >= 3
)
SELECT mac, d.vendor, minute, (max_rssi - min_rssi) as rssi_swing
FROM windows
LEFT JOIN devices d USING(mac)
WHERE rssi_swing > 15
ORDER BY minute DESC, rssi_swing DESC LIMIT 30"
```

A device whose RSSI swings >15 dB within a minute is physically moving (or the RF environment shifted — person walked between it and Pi).

---

## 10. Network Topology Reconstruction

### 10.1 Every client of a specific access point

Replace BSSID:

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT DISTINCT 
  CASE WHEN src_mac='aa:bb:cc:00:00:02' THEN dst_mac ELSE src_mac END as client_mac,
  d.vendor,
  d.device_type,
  COUNT(*) as frames
FROM wifi_frames wf
LEFT JOIN devices d ON d.mac = CASE WHEN src_mac='aa:bb:cc:00:00:02' THEN dst_mac ELSE src_mac END
WHERE (src_mac='aa:bb:cc:00:00:02' OR dst_mac='aa:bb:cc:00:00:02')
  AND frame_type=2
  AND src_mac NOT LIKE 'ff:%' AND dst_mac NOT LIKE 'ff:%'
  AND src_mac NOT LIKE '01:%' AND dst_mac NOT LIKE '01:%'
GROUP BY client_mac ORDER BY frames DESC LIMIT 30"
```

### 10.2 Every AP a specific device has talked to

Replace device MAC:

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT 
  wf.bssid,
  COALESCE((SELECT ssid FROM wifi_frames b
            WHERE b.src_mac=wf.bssid AND b.frame_type=0 AND b.frame_subtype=8
              AND b.ssid IS NOT NULL AND b.ssid != '' LIMIT 1), 'unknown') as ssid,
  COUNT(*) as frames,
  MIN(wf.timestamp) as first_contact,
  MAX(wf.timestamp) as last_contact
FROM wifi_frames wf
WHERE (wf.src_mac='aa:bb:cc:00:00:01' OR wf.dst_mac='aa:bb:cc:00:00:01')
  AND wf.bssid IS NOT NULL AND wf.bssid != ''
  AND wf.bssid NOT LIKE 'ff:%' AND wf.bssid NOT LIKE '01:%'
GROUP BY wf.bssid
ORDER BY frames DESC"
```

### 10.3 Paired AP radios (2.4/5 GHz on the same hardware)

Devices whose last 3 octets match but last byte differs by 1:

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT d1.mac as radio_1, d2.mac as radio_2, d1.vendor
FROM devices d1
JOIN devices d2 ON d1.vendor = d2.vendor
  AND SUBSTR(d1.mac,1,15) = SUBSTR(d2.mac,1,15)
  AND d1.mac < d2.mac
  AND d1.is_ap=1 AND d2.is_ap=1
ORDER BY d1.vendor"
```

### 10.4 Find hidden SSIDs (BSSID referenced but never self-beaconed)

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT DISTINCT bssid, COUNT(*) as frames_addressed
FROM wifi_frames
WHERE bssid IS NOT NULL AND bssid != ''
  AND bssid NOT LIKE 'ff:%' AND bssid NOT LIKE '01:%'
  AND bssid NOT IN (
    SELECT DISTINCT src_mac FROM wifi_frames 
    WHERE frame_type=0 AND frame_subtype=8
  )
GROUP BY bssid
ORDER BY frames_addressed DESC LIMIT 20"
```

### 10.5 Channel usage heatmap

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT channel,
       CASE 
         WHEN channel BETWEEN 1 AND 14 THEN '2.4 GHz'
         WHEN channel BETWEEN 36 AND 64 THEN '5 GHz UNII-1/2A'
         WHEN channel BETWEEN 100 AND 144 THEN '5 GHz UNII-2C'
         WHEN channel BETWEEN 149 AND 165 THEN '5 GHz UNII-3'
         ELSE 'other'
       END as band,
       COUNT(*) as frames,
       COUNT(DISTINCT src_mac) as distinct_sources
FROM wifi_frames
GROUP BY channel
ORDER BY frames DESC"
```

### 10.6 Clients that have roamed between multiple APs

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT src_mac as client, COUNT(DISTINCT bssid) as ap_count, 
       GROUP_CONCAT(DISTINCT bssid) as aps_used
FROM wifi_frames
WHERE frame_type=2
  AND bssid IS NOT NULL AND bssid != ''
  AND bssid NOT LIKE 'ff:%' AND bssid NOT LIKE '01:%'
  AND src_mac NOT IN (SELECT mac FROM devices WHERE is_ap=1)
GROUP BY src_mac
HAVING ap_count >= 2
ORDER BY ap_count DESC LIMIT 20"
```

---

## 11. Probe Cluster Analysis (MAC Randomization Defeat)

### 11.1 All clusters summary

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT 
  pc.cluster_id,
  pc.first_seen,
  pc.last_seen,
  SUBSTR(pc.representative_ssid_set, 1, 80) as ssid_preview,
  (SELECT COUNT(*) FROM probe_cluster_members WHERE cluster_id=pc.cluster_id) as member_count
FROM probe_clusters pc
ORDER BY member_count DESC"
```

### 11.2 Members of one cluster (same physical device, different MACs)

Replace cluster_id:

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT pcm.mac, d.vendor, pcm.first_seen_in_cluster,
       d.last_seen
FROM probe_cluster_members pcm
LEFT JOIN devices d USING(mac)
WHERE cluster_id='cluster_1745123456'
ORDER BY first_seen_in_cluster"
```

### 11.3 Find which cluster a specific MAC belongs to

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT pc.cluster_id, pc.representative_ssid_set,
       (SELECT COUNT(*) FROM probe_cluster_members WHERE cluster_id=pc.cluster_id) as members
FROM probe_cluster_members pcm
JOIN probe_clusters pc USING(cluster_id)
WHERE pcm.mac='xx:xx:xx:xx:xx:xx'"
```

### 11.4 Biggest clusters (most MAC rotations tracked)

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT cluster_id, COUNT(mac) as mac_rotations,
       MIN(first_seen_in_cluster) as tracking_since
FROM probe_cluster_members
GROUP BY cluster_id
ORDER BY mac_rotations DESC LIMIT 20"
```

### 11.5 SSID intersection — shared networks between two clusters

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT pc1.cluster_id as cluster_a, pc2.cluster_id as cluster_b,
       pc1.representative_ssid_set as ssids_a,
       pc2.representative_ssid_set as ssids_b
FROM probe_clusters pc1, probe_clusters pc2
WHERE pc1.cluster_id < pc2.cluster_id
LIMIT 20"
```

Manual inspection to find overlap — SQLite doesn't have great JSON intersection operators.

---

## 12. BLE Intelligence

### 12.1 Manufacturer ID distribution decoded

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
WITH mfr_counts AS (
  SELECT SUBSTR(manufacturer_data_hex, 1, 4) as mfr_id_hex, COUNT(*) as cnt
  FROM bt_advertisements
  WHERE manufacturer_data_hex IS NOT NULL AND manufacturer_data_hex != ''
  GROUP BY mfr_id_hex
)
SELECT 
  mfr_id_hex,
  CASE mfr_id_hex
    WHEN '4c00' THEN 'Apple'
    WHEN '7500' THEN 'Samsung'
    WHEN '0600' THEN 'Microsoft'
    WHEN 'e000' THEN 'Google'
    WHEN '8700' THEN 'Garmin'
    WHEN 'da03' THEN 'Amazon'
    WHEN '0500' THEN 'Nordic Semiconductor'
    WHEN 'ff13' THEN 'Xiaomi'
    WHEN '5701' THEN 'Nintendo'
    WHEN '4d00' THEN 'Fitbit'
    WHEN '8c00' THEN 'Logitech'
    WHEN '5b00' THEN 'Tile'
    WHEN 'ff00' THEN 'Intel'
    WHEN '4700' THEN 'Sony'
    WHEN '5c00' THEN 'Harman International'
    ELSE 'unknown'
  END as vendor,
  cnt as advertisements
FROM mfr_counts
ORDER BY cnt DESC LIMIT 20"
```

### 12.2 Apple device count estimation (before/after rotation bias)

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT 
  COUNT(DISTINCT mac) as unique_apple_macs_1h,
  COUNT(*) as total_apple_ads_1h
FROM bt_advertisements
WHERE manufacturer_data_hex LIKE '4c00%'
  AND timestamp > datetime('now','-1 hour')"
```

The unique-MAC count is inflated by randomization. Real Apple device count is ~ `unique_macs / 4` (MACs rotate every 15 min, hour = 4 rotations).

### 12.3 Devices advertising specific service UUIDs

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT DISTINCT mac, d.vendor, service_uuids, COUNT(*) as ads
FROM bt_advertisements bta
LEFT JOIN devices d USING(mac)
WHERE service_uuids LIKE '%180d%'   -- 0x180D = Heart Rate service
GROUP BY mac, service_uuids
ORDER BY ads DESC"
```

Useful UUIDs to filter on:
- `180d` = heart rate
- `180f` = battery
- `1812` = HID (keyboards/mice)
- `110b` = audio sink
- `fe9f` = Google Fast Pair
- `fd5a` = Apple Find My Network

### 12.4 Classic BT devices by device class

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT mac, device_name, device_class,
       CASE (device_class >> 8) & 0x1f
         WHEN 1 THEN 'Computer'
         WHEN 2 THEN 'Phone'
         WHEN 3 THEN 'LAN/Network'
         WHEN 4 THEN 'Audio/Video'
         WHEN 5 THEN 'Peripheral'
         WHEN 6 THEN 'Imaging'
         WHEN 7 THEN 'Wearable'
         WHEN 8 THEN 'Toy'
         WHEN 9 THEN 'Health'
         ELSE 'Misc'
       END as major_class,
       COUNT(*) as ads
FROM bt_advertisements
WHERE device_class IS NOT NULL AND device_class > 0
GROUP BY mac
ORDER BY ads DESC LIMIT 30"
```

### 12.5 BLE advertisement timing patterns (device fingerprint)

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
WITH intervals AS (
  SELECT mac,
         timestamp,
         (julianday(timestamp) - julianday(LAG(timestamp) OVER (PARTITION BY mac ORDER BY timestamp))) * 86400 as seconds_since_last
  FROM bt_advertisements
  WHERE timestamp > datetime('now','-1 hour')
)
SELECT mac,
       ROUND(AVG(seconds_since_last), 2) as avg_interval_sec,
       ROUND(MIN(seconds_since_last), 2) as min_interval,
       COUNT(*) as ads
FROM intervals
WHERE seconds_since_last IS NOT NULL AND seconds_since_last < 60
GROUP BY mac
HAVING ads >= 20
ORDER BY ads DESC LIMIT 20"
```

iOS devices advertise at ~1 sec intervals. Watches ~0.5 sec. Fitbits ~0.1 sec. Timing is a fingerprint.

---

## 13. Alert Intelligence

### 13.1 Alert rate per hour

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT strftime('%Y-%m-%d %H', timestamp) as hour,
       severity,
       COUNT(*) as alerts
FROM alerts
GROUP BY hour, severity
ORDER BY hour DESC, severity LIMIT 48"
```

### 13.2 Top alerting devices

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT a.mac, d.vendor, a.alert_type, a.severity, COUNT(*) as firings,
       MIN(a.timestamp) as first_fire,
       MAX(a.timestamp) as last_fire
FROM alerts a
LEFT JOIN devices d USING(mac)
GROUP BY a.mac, a.alert_type, a.severity
ORDER BY firings DESC LIMIT 30"
```

### 13.3 Alert type distribution

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT alert_type, severity, COUNT(*) as count
FROM alerts
WHERE timestamp > datetime('now','-7 days')
GROUP BY alert_type, severity
ORDER BY count DESC"
```

### 13.4 Unacknowledged alerts worth looking at

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT id, timestamp, mac, alert_type, severity, description
FROM alerts
WHERE acknowledged = 0
  AND severity IN ('medium', 'high')
ORDER BY timestamp DESC LIMIT 30"
```

### 13.5 Parse alert detail JSON (for temporal anomalies)

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT id, timestamp, mac, description,
       json_extract(detail_json, '\$.unusual_hour') as hour,
       json_extract(detail_json, '\$.expected_probability') as expected_p
FROM alerts
WHERE alert_type='temporal'
ORDER BY timestamp DESC LIMIT 20"
```

---

## 14. SSID and Network-Name Intelligence

### 14.1 Every unique SSID ever captured

```bash
sqlite3 ~/sentinel/data/sentinel.db "
SELECT DISTINCT ssid FROM (
  SELECT ssid FROM probe_requests WHERE ssid != '' 
  UNION 
  SELECT ssid FROM wifi_frames WHERE ssid IS NOT NULL AND ssid != ''
) ORDER BY ssid"
```

### 14.2 SSIDs probed for but never beaconed (external networks remembered by passing phones)

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT p.ssid, COUNT(DISTINCT p.mac) as devices_remembering, COUNT(*) as probe_events
FROM probe_requests p
WHERE p.ssid != ''
  AND p.ssid NOT IN (
    SELECT DISTINCT ssid FROM wifi_frames 
    WHERE frame_type=0 AND frame_subtype=8 AND ssid IS NOT NULL AND ssid != ''
  )
GROUP BY p.ssid
ORDER BY devices_remembering DESC LIMIT 30"
```

### 14.3 SSIDs with beacons in range (actual APs nearby)

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT ssid, COUNT(DISTINCT src_mac) as unique_bssids,
       COUNT(*) as total_beacons
FROM wifi_frames
WHERE frame_type=0 AND frame_subtype=8
  AND ssid IS NOT NULL AND ssid != ''
GROUP BY ssid
ORDER BY total_beacons DESC"
```

### 14.4 Every SSID a specific device has ever probed

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT ssid, COUNT(*) as probes,
       MIN(timestamp) as first_probe,
       MAX(timestamp) as last_probe
FROM probe_requests
WHERE mac='aa:bb:cc:00:00:01' AND ssid != ''
GROUP BY ssid ORDER BY probes DESC"
```

### 14.5 Find social connections — shared networks between devices

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT p1.mac as device_a, p2.mac as device_b,
       COUNT(DISTINCT p1.ssid) as shared_networks,
       GROUP_CONCAT(DISTINCT p1.ssid) as networks
FROM probe_requests p1
JOIN probe_requests p2 ON p1.ssid = p2.ssid AND p1.mac < p2.mac
WHERE p1.ssid != ''
GROUP BY p1.mac, p2.mac
HAVING shared_networks >= 2
ORDER BY shared_networks DESC LIMIT 20"
```

### 14.6 Suspicious / interesting SSID name patterns

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT DISTINCT ssid FROM probe_requests
WHERE ssid != '' 
  AND (
    LOWER(ssid) LIKE '%airport%'
    OR LOWER(ssid) LIKE '%hotel%'
    OR LOWER(ssid) LIKE '%starbucks%'
    OR LOWER(ssid) LIKE '%guest%'
    OR LOWER(ssid) LIKE '%wifi%'
    OR LOWER(ssid) LIKE '%.local%'
  )"
```

Customize patterns for travel, work, chains, etc.

---

## 15. Session Reconstruction

### 15.1 Complete frame timeline for one MAC in a time window

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT 
  datetime(timestamp) as time,
  frame_type,
  frame_subtype,
  rssi,
  channel,
  COALESCE(ssid, '') as ssid,
  bssid
FROM wifi_frames
WHERE src_mac='aa:bb:cc:00:00:01'
  AND timestamp BETWEEN '2026-04-20 02:00' AND '2026-04-20 04:00'
ORDER BY timestamp"
```

### 15.2 Minute-by-minute activity count for a device

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT strftime('%Y-%m-%d %H:%M', timestamp) as minute,
       COUNT(*) as events,
       MIN(rssi) as weakest,
       MAX(rssi) as strongest,
       GROUP_CONCAT(DISTINCT channel) as channels
FROM observations
WHERE mac='aa:bb:cc:00:00:01'
  AND timestamp > datetime('now','-2 hours')
GROUP BY minute
ORDER BY minute DESC LIMIT 60"
```

### 15.3 Detect association/disassociation events

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT datetime(timestamp) as time, src_mac, bssid, ssid, rssi, channel,
       CASE frame_subtype
         WHEN 0 THEN 'Association Request'
         WHEN 1 THEN 'Association Response'
         WHEN 2 THEN 'Reassociation Request'
         WHEN 3 THEN 'Reassociation Response'
         WHEN 10 THEN 'Disassociation'
         WHEN 11 THEN 'Authentication'
         WHEN 12 THEN 'Deauthentication'
       END as event_type
FROM wifi_frames
WHERE src_mac='aa:bb:cc:00:00:01'
  AND frame_type=0
  AND frame_subtype IN (0, 1, 2, 3, 10, 11, 12)
ORDER BY timestamp DESC LIMIT 20"
```

### 15.4 Data frame burst detection (moments of heavy activity)

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT strftime('%Y-%m-%d %H:%M', timestamp) as minute,
       src_mac,
       COUNT(*) as data_frames
FROM wifi_frames
WHERE frame_type=2 AND frame_subtype=8
  AND src_mac NOT IN (SELECT mac FROM devices WHERE is_ap=1)
GROUP BY minute, src_mac
HAVING data_frames > 20
ORDER BY minute DESC LIMIT 30"
```

### 15.5 Companion device timing for one MAC

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
WITH target AS (
  SELECT timestamp FROM observations WHERE mac='aa:bb:cc:00:00:01'
)
SELECT o.mac as companion, d.vendor, COUNT(*) as cooccurrences
FROM observations o
LEFT JOIN devices d USING(mac)
JOIN target t ON ABS(strftime('%s', o.timestamp) - strftime('%s', t.timestamp)) <= 60
WHERE o.mac != 'aa:bb:cc:00:00:01'
GROUP BY o.mac
ORDER BY cooccurrences DESC LIMIT 20"
```

---

## 16. Statistical and Diagnostic Queries

### 16.1 DB health snapshot

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT 'devices' as table_name, COUNT(*) as rows FROM devices
UNION ALL SELECT 'observations', COUNT(*) FROM observations
UNION ALL SELECT 'wifi_frames', COUNT(*) FROM wifi_frames
UNION ALL SELECT 'probe_requests', COUNT(*) FROM probe_requests
UNION ALL SELECT 'bt_advertisements', COUNT(*) FROM bt_advertisements
UNION ALL SELECT 'device_profiles', COUNT(*) FROM device_profiles
UNION ALL SELECT 'probe_clusters', COUNT(*) FROM probe_clusters
UNION ALL SELECT 'probe_cluster_members', COUNT(*) FROM probe_cluster_members
UNION ALL SELECT 'alerts', COUNT(*) FROM alerts
UNION ALL SELECT 'sessions', COUNT(*) FROM sessions
UNION ALL SELECT 'gps_fixes', COUNT(*) FROM gps_fixes
UNION ALL SELECT 'sdr_observations', COUNT(*) FROM sdr_observations
UNION ALL SELECT 'oui_vendors', COUNT(*) FROM oui_vendors"
```

### 16.2 Capture rate (events per minute, last hour)

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT strftime('%Y-%m-%d %H:%M', timestamp) as minute,
       COUNT(*) as events
FROM observations
WHERE timestamp > datetime('now','-1 hour')
GROUP BY minute
ORDER BY minute"
```

### 16.3 Observation source split

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT source, COUNT(*) as events, COUNT(DISTINCT mac) as unique_devices
FROM observations
WHERE timestamp > datetime('now','-1 day')
GROUP BY source"
```

### 16.4 Pace to DB growth estimation

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT 
  (SELECT COUNT(*) FROM observations WHERE timestamp > datetime('now','-1 hour')) as events_1h,
  (SELECT COUNT(*) FROM observations WHERE timestamp > datetime('now','-1 hour')) * 24 as projected_per_day,
  (SELECT COUNT(*) FROM observations WHERE timestamp > datetime('now','-1 hour')) * 24 * 365 as projected_per_year"
```

### 16.5 Top talkative devices (by observation count)

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT d.mac, d.vendor, d.device_type, COUNT(*) as total,
       COUNT(DISTINCT date(o.timestamp)) as days
FROM observations o
LEFT JOIN devices d USING(mac)
GROUP BY d.mac
ORDER BY total DESC LIMIT 30"
```

### 16.6 Sensor session history

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT session_id, started_at, ended_at, capture_sources,
       CASE WHEN ended_at IS NULL THEN 'RUNNING' ELSE 
         CAST((julianday(ended_at) - julianday(started_at)) * 1440 AS INTEGER) || ' min' 
       END as duration
FROM sessions
ORDER BY started_at DESC LIMIT 20"
```
---

# PART IV — INVESTIGATION PLAYBOOKS

14 scenarios, each a concrete question you might actually ask, and the exact sequence of queries to answer it. Copy-paste each block in order.

## Playbook A: "Was anything unusual at X o'clock last night?"

```bash
# Step 1: Who was close during that window
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT mac, d.vendor, MAX(rssi) as strongest, COUNT(*) as obs,
       MIN(timestamp) as first, MAX(timestamp) as last
FROM observations o LEFT JOIN devices d USING(mac)
WHERE timestamp BETWEEN '2026-04-20 02:30' AND '2026-04-20 03:30'
GROUP BY mac ORDER BY strongest DESC LIMIT 30"

# Step 2: New devices that first appeared then
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT mac, vendor, first_seen, is_ap
FROM devices
WHERE first_seen BETWEEN '2026-04-20 02:30' AND '2026-04-20 03:30'
ORDER BY first_seen"

# Step 3: Alerts fired in that window
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT timestamp, mac, alert_type, severity, description
FROM alerts
WHERE timestamp BETWEEN '2026-04-20 02:30' AND '2026-04-20 03:30'
ORDER BY timestamp"

# Step 4: Unusually strong signal (close approach)
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT timestamp, mac, d.vendor, rssi, channel
FROM observations o LEFT JOIN devices d USING(mac)
WHERE timestamp BETWEEN '2026-04-20 02:30' AND '2026-04-20 03:30'
  AND rssi > -45
ORDER BY rssi DESC"
```

---

## Playbook B: "Who visits my house but isn't on my WiFi?"

Devices that appear regularly but never probe for your home SSID. Potential neighbors' phones, guests, random passers-by.

```bash
# Edit the SSID to match yours
HOME_SSID="HomeNetwork-2G"

sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT d.mac, d.vendor, d.device_type,
       COUNT(DISTINCT date(o.timestamp)) as days_seen,
       COUNT(*) as total_obs,
       MAX(o.rssi) as closest_ever
FROM devices d
JOIN observations o USING(mac)
WHERE d.mac NOT IN (
  SELECT mac FROM probe_requests WHERE ssid='HomeNetwork-2G'
)
  AND d.is_ap = 0
GROUP BY d.mac
HAVING days_seen >= 3
ORDER BY days_seen DESC LIMIT 30"
```

---

## Playbook C: "Is device X here right now? And when was it last here?"

```bash
MAC="aa:bb:cc:00:00:01"

sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT mac, vendor, 
       last_seen,
       CAST((julianday('now') - julianday(last_seen)) * 1440 AS INTEGER) as minutes_since_seen,
       CASE 
         WHEN julianday('now') - julianday(last_seen) < (5.0/1440) THEN 'HERE NOW'
         WHEN julianday('now') - julianday(last_seen) < (60.0/1440) THEN 'RECENT (last hour)'
         WHEN julianday('now') - julianday(last_seen) < 1 THEN 'TODAY'
         ELSE 'ABSENT'
       END as status
FROM devices WHERE mac='aa:bb:cc:00:00:01'"
```

---

## Playbook D: "Fingerprint a stranger"

Target a randomized or unknown MAC. Build every piece of evidence.

```bash
MAC="aa:bb:cc:00:00:05"

echo "=== 1. Basic device record ==="
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT * FROM devices WHERE mac='$MAC'"

echo "=== 2. Observation statistics ==="
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT source, COUNT(*) as n, MIN(rssi) as min_rssi, MAX(rssi) as max_rssi,
       MIN(timestamp) as first, MAX(timestamp) as last,
       COUNT(DISTINCT date(timestamp)) as days
FROM observations WHERE mac='$MAC' GROUP BY source"

echo "=== 3. SSID probe set (Level B fingerprint) ==="
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT ssid, COUNT(*) as probes
FROM probe_requests WHERE mac='$MAC' AND ssid != ''
GROUP BY ssid ORDER BY probes DESC"

echo "=== 4. BLE advertisements ==="
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT COUNT(*) as ads, 
       GROUP_CONCAT(DISTINCT SUBSTR(manufacturer_data_hex,1,4)) as mfr_ids,
       GROUP_CONCAT(DISTINCT service_uuids) as service_uuids,
       MAX(device_name) as any_name_broadcast
FROM bt_advertisements WHERE mac='$MAC'"

echo "=== 5. Cluster membership ==="
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT pc.cluster_id, pc.representative_ssid_set
FROM probe_cluster_members pcm
JOIN probe_clusters pc USING(cluster_id)
WHERE pcm.mac='$MAC'"

echo "=== 6. Other MACs in same cluster (same physical device?) ==="
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT pcm2.mac, d.vendor
FROM probe_cluster_members pcm1
JOIN probe_cluster_members pcm2 USING(cluster_id)
LEFT JOIN devices d ON d.mac = pcm2.mac
WHERE pcm1.mac='$MAC' AND pcm2.mac != '$MAC'"

echo "=== 7. Companion MACs (seen within ±60s) ==="
sqlite3 -column -header ~/sentinel/data/sentinel.db "
WITH target AS (SELECT timestamp FROM observations WHERE mac='$MAC')
SELECT o.mac as companion, d.vendor, COUNT(*) as coocurrence
FROM observations o
LEFT JOIN devices d USING(mac)
JOIN target t ON ABS(strftime('%s', o.timestamp) - strftime('%s', t.timestamp)) <= 60
WHERE o.mac != '$MAC'
GROUP BY o.mac ORDER BY coocurrence DESC LIMIT 10"
```

---

## Playbook E: "Audit my own devices — what am I leaking?"

```bash
# Define YOUR known MACs
YOUR_MACS="('aa:bb:cc:00:00:01','aa:bb:cc:00:00:03','aa:bb:cc:00:00:04')"

sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT d.mac, d.vendor,
       (SELECT COUNT(DISTINCT ssid) FROM probe_requests WHERE mac=d.mac AND ssid != '') as ssids_leaked,
       (SELECT GROUP_CONCAT(DISTINCT ssid) FROM probe_requests WHERE mac=d.mac AND ssid != '') as networks,
       (SELECT COUNT(*) FROM observations WHERE mac=d.mac) as total_obs
FROM devices d
WHERE d.mac IN $YOUR_MACS"
```

---

## Playbook F: "Track a device's arrival and departure pattern"

```bash
MAC="aa:bb:cc:00:00:01"

# Treat gaps > 30 min as session boundaries
sqlite3 -column -header ~/sentinel/data/sentinel.db "
WITH ordered AS (
  SELECT timestamp,
         LAG(timestamp) OVER (ORDER BY timestamp) as prev_ts
  FROM observations
  WHERE mac='$MAC'
    AND timestamp > datetime('now','-30 days')
),
sessions AS (
  SELECT timestamp,
         CASE WHEN prev_ts IS NULL 
                OR (julianday(timestamp) - julianday(prev_ts)) * 1440 > 30
              THEN 'ARRIVAL' END as event
  FROM ordered
)
SELECT timestamp as arrival_time
FROM sessions WHERE event='ARRIVAL'
ORDER BY timestamp DESC LIMIT 30"
```

---

## Playbook G: "Who was close to my home at a specific moment?"

```bash
# Specify the timestamp of interest
AT="2026-04-20 03:45"

~/sentinel-queries.sh close-at "$AT"

# Expanded: what were they doing
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT datetime(timestamp) as time, mac, d.vendor, source, rssi, channel, 
       COALESCE(p.ssid, '') as probed_ssid
FROM observations o
LEFT JOIN devices d USING(mac)
LEFT JOIN probe_requests p ON p.mac = o.mac AND p.timestamp = o.timestamp
WHERE timestamp BETWEEN datetime('$AT','-5 minutes') AND datetime('$AT','+5 minutes')
  AND rssi > -60
ORDER BY rssi DESC"
```

---

## Playbook H: "Find the Wyze cam / IoT device that just got a signal boost"

Spot IoT devices whose RSSI jumped abnormally (someone moved the router, moved the device, or a new one appeared).

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
WITH yesterday AS (
  SELECT mac, AVG(rssi) as avg_rssi_yesterday
  FROM observations
  WHERE timestamp BETWEEN datetime('now','-2 days') AND datetime('now','-1 day')
  GROUP BY mac
),
today AS (
  SELECT mac, AVG(rssi) as avg_rssi_today
  FROM observations
  WHERE timestamp > datetime('now','-1 day')
  GROUP BY mac
)
SELECT t.mac, d.vendor,
       ROUND(y.avg_rssi_yesterday, 1) as yesterday_avg,
       ROUND(t.avg_rssi_today, 1) as today_avg,
       ROUND(t.avg_rssi_today - y.avg_rssi_yesterday, 1) as change
FROM today t
JOIN yesterday y USING(mac)
LEFT JOIN devices d ON d.mac = t.mac
WHERE ABS(t.avg_rssi_today - y.avg_rssi_yesterday) > 10
ORDER BY ABS(change) DESC LIMIT 20"
```

---

## Playbook I: "What network has my device been connecting to when I'm not home?"

Find times when your device was seen but you weren't managing your network actively. Requires knowing when you were home vs. away.

```bash
MAC="aa:bb:cc:00:00:01"

# Show BSSIDs it communicated with, along with when
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT date(wf.timestamp) as day,
       strftime('%H', wf.timestamp) as hour,
       wf.bssid,
       COUNT(*) as data_frames
FROM wifi_frames wf
WHERE (wf.src_mac='$MAC' OR wf.dst_mac='$MAC')
  AND wf.frame_type=2 AND wf.frame_subtype=8
  AND wf.bssid NOT LIKE 'ff:%' AND wf.bssid NOT LIKE '01:%'
GROUP BY day, hour, wf.bssid
ORDER BY day DESC, hour DESC LIMIT 40"
```

---

## Playbook J: "Find all probe-request SSIDs mentioned exactly once (one-hit wonders)"

Good for spotting one-off visitors who came through once with an unusual network memory.

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT p.ssid, p.mac, d.vendor, p.timestamp
FROM probe_requests p
LEFT JOIN devices d USING(mac)
WHERE p.ssid != ''
  AND p.ssid IN (
    SELECT ssid FROM probe_requests WHERE ssid != '' 
    GROUP BY ssid HAVING COUNT(*) = 1
  )
ORDER BY p.timestamp DESC LIMIT 30"
```

---

## Playbook K: "Device X arrived — trigger on detection"

Set up a 5-minute cron watchlist.

```bash
# Create the watchlist script
cat > ~/sentinel-watchlist.sh <<'WATCH_EOF'
#!/bin/bash
# Watchlist: alert to ~/sentinel-alerts.log when target MACs appear
DB=~/sentinel/data/sentinel.db
WATCHLIST="aa:bb:cc:00:00:01 aa:bb:cc:00:00:05"
LOGFILE=~/sentinel-alerts.log

for MAC in $WATCHLIST; do
  COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM observations 
    WHERE mac='$MAC' AND timestamp > datetime('now','-5 minutes')")
  if [ "$COUNT" -gt 0 ]; then
    RSSI=$(sqlite3 "$DB" "SELECT MAX(rssi) FROM observations 
      WHERE mac='$MAC' AND timestamp > datetime('now','-5 minutes')")
    echo "$(date '+%Y-%m-%d %H:%M') WATCHLIST $MAC detected, $COUNT obs, RSSI $RSSI" >> "$LOGFILE"
  fi
done
WATCH_EOF
chmod +x ~/sentinel-watchlist.sh

# Install cron: run every 5 min
(crontab -l 2>/dev/null; echo "*/5 * * * * ~/sentinel-watchlist.sh") | crontab -

# Verify
crontab -l
tail -f ~/sentinel-alerts.log
```

Remove the cron with: `crontab -e` (delete the line).

---

## Playbook L: "Identify my router's guest/IoT network"

```bash
# Replace with your main 2.4 GHz BSSID
MAIN_BSSID="aa:bb:cc:00:00:02"
PREFIX="d8:a7:56:81:8e:"

sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT DISTINCT bssid,
       (SELECT ssid FROM wifi_frames b
        WHERE b.src_mac=bssid AND b.frame_type=0 AND b.frame_subtype=8
          AND b.ssid IS NOT NULL AND b.ssid != ''
        LIMIT 1) as ssid,
       COUNT(*) as frames_addressed_to
FROM wifi_frames
WHERE bssid LIKE '$PREFIX%'
  AND bssid NOT LIKE 'ff:%' AND bssid NOT LIKE '01:%'
GROUP BY bssid
ORDER BY frames_addressed_to DESC"
```

Each row is one virtual AP on the same physical radio (Multi-BSSID). Their SSIDs reveal the full network config.

---

## Playbook M: "Find surveilling device / tracker in your environment"

Look for devices that:
- Appear in multiple locations (high channel variety)
- Have consistent RSSI (not moving much)
- Broadcast BLE at unusual intervals
- Are NOT a router, NOT a known-yours device

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT d.mac, d.vendor, d.device_type,
       COUNT(DISTINCT o.channel) as channels,
       ROUND(AVG(o.rssi), 1) as avg_rssi,
       ROUND(MAX(o.rssi) - MIN(o.rssi), 1) as rssi_range,
       COUNT(*) as obs,
       COUNT(DISTINCT date(o.timestamp)) as days
FROM devices d
JOIN observations o USING(mac)
WHERE d.is_ap = 0
  AND d.mac NOT IN (
    -- exclude your known MACs
    'aa:bb:cc:00:00:01','aa:bb:cc:00:00:03','aa:bb:cc:00:00:04'
  )
GROUP BY d.mac
HAVING obs > 100
   AND rssi_range < 10
   AND days >= 2
ORDER BY obs DESC LIMIT 20"
```

---

## Playbook N: "Discover Apple device belonging to a specific person (they're using Find My)"

Apple devices broadcasting Find My (`0x004C` manufacturer data, service UUID `fd5a`):

```bash
sqlite3 -column -header ~/sentinel/data/sentinel.db "
SELECT DISTINCT bta.mac, d.vendor, 
       MAX(bta.rssi) as closest,
       COUNT(*) as broadcasts,
       MIN(bta.timestamp) as first,
       MAX(bta.timestamp) as last
FROM bt_advertisements bta
LEFT JOIN devices d USING(mac)
WHERE (bta.manufacturer_data_hex LIKE '4c0012%' OR bta.service_uuids LIKE '%fd5a%')
GROUP BY bta.mac
ORDER BY broadcasts DESC LIMIT 30"
```

This captures AirTags and devices in "Find My" network relay mode.

---

# PART V — OPERATIONS

## 17. Live Monitoring — Watch Windows

Open multiple SSH sessions. Each window gets a different role.

### 17.1 Window 1 — Sentinel's built-in tailer

```bash
./sentinel.sh watch
```

### 17.2 Window 2 — Raw probe requests

```bash
watch -n 2 "sqlite3 -column -header ~/sentinel/data/sentinel.db \"SELECT datetime(timestamp) as time, mac, ssid, rssi FROM probe_requests WHERE ssid != '' ORDER BY id DESC LIMIT 15\""
```

### 17.3 Window 3 — Table row growth

```bash
watch -n 5 "sqlite3 -column -header ~/sentinel/data/sentinel.db \"SELECT 'devices' as t, COUNT(*) as n FROM devices UNION ALL SELECT 'obs', COUNT(*) FROM observations UNION ALL SELECT 'wifi_frames', COUNT(*) FROM wifi_frames UNION ALL SELECT 'probes', COUNT(*) FROM probe_requests UNION ALL SELECT 'bt_ads', COUNT(*) FROM bt_advertisements UNION ALL SELECT 'alerts', COUNT(*) FROM alerts\""
```

### 17.4 Window 4 — Proximity alarm (anyone closer than -50 dBm)

```bash
watch -n 5 "sqlite3 -column -header ~/sentinel/data/sentinel.db \"SELECT mac, vendor, MAX(rssi) as rssi, COUNT(*) as obs FROM observations o LEFT JOIN devices d USING(mac) WHERE timestamp > datetime('now','-2 minutes') AND rssi > -50 GROUP BY mac ORDER BY rssi DESC LIMIT 15\""
```

### 17.5 Window 5 — System resources

```bash
watch -n 5 "free -h; echo; df -h ~/sentinel/data; echo; ls -lh ~/sentinel/data/sentinel.db*"
```

### 17.6 Window 6 — Live daemon logs

```bash
journalctl --user -u sentinel-ingest -u sentinel-detector -f
```

### 17.7 Window 7 — New MAC detection

```bash
watch -n 10 "sqlite3 -column -header ~/sentinel/data/sentinel.db \"SELECT first_seen, mac, vendor, device_type FROM devices WHERE first_seen > datetime('now','-10 minutes') ORDER BY first_seen DESC\""
```

### 17.8 Window 8 — Alert-only tail

```bash
./sentinel.sh watch --alerts-only
```

---

## 18. Database Maintenance

### 18.1 Size checks

```bash
ls -lh ~/sentinel/data/sentinel.db*
du -sh ~/sentinel/data/
df -h ~/sentinel/data/
```

### 18.2 Integrity check

```bash
sqlite3 ~/sentinel/data/sentinel.db "PRAGMA integrity_check;"
```

Should return `ok`. Anything else = corruption; restore from backup.

### 18.3 Vacuum (reclaim space, compact)

```bash
./sentinel.sh stop
sqlite3 ~/sentinel/data/sentinel.db "VACUUM"
./sentinel.sh start
```

### 18.4 Checkpoint WAL (merge write-ahead log into main DB)

Usually automatic, but you can force it:

```bash
sqlite3 ~/sentinel/data/sentinel.db "PRAGMA wal_checkpoint(TRUNCATE);"
```

### 18.5 Safe online backup (while running)

```bash
sqlite3 ~/sentinel/data/sentinel.db ".backup ~/sentinel-backup-$(date +%Y%m%d-%H%M).db"
ls -lh ~/sentinel-backup-*.db
```

### 18.6 Full cold backup (stopped daemons)

```bash
./sentinel.sh stop
tar czf ~/sentinel-full-backup-$(date +%Y%m%d).tar.gz -C ~/sentinel data/
./sentinel.sh start
ls -lh ~/sentinel-full-backup-*.tar.gz
```

### 18.7 Export as portable SQL dump

```bash
sqlite3 ~/sentinel/data/sentinel.db .dump > ~/sentinel-dump-$(date +%Y%m%d).sql
gzip ~/sentinel-dump-*.sql
```

### 18.8 Restore from SQL dump

```bash
./sentinel.sh stop
mv ~/sentinel/data/sentinel.db ~/sentinel/data/sentinel.db.old
sqlite3 ~/sentinel/data/sentinel.db < ~/sentinel-dump-20260420.sql
./sentinel.sh start
```

### 18.9 Prune old data

```bash
# Delete data older than N days
./sentinel.sh stop
sqlite3 ~/sentinel/data/sentinel.db "
  DELETE FROM observations WHERE timestamp < datetime('now','-30 days');
  DELETE FROM wifi_frames WHERE timestamp < datetime('now','-30 days');
  DELETE FROM probe_requests WHERE timestamp < datetime('now','-30 days');
  DELETE FROM bt_advertisements WHERE timestamp < datetime('now','-30 days');
  DELETE FROM alerts WHERE timestamp < datetime('now','-30 days');
  VACUUM;
"
./sentinel.sh start
```

### 18.10 Rebuild profiles and clusters

If profiles seem stale or you want them from scratch:

```bash
sqlite3 ~/sentinel/data/sentinel.db "
  DELETE FROM device_profiles;
  DELETE FROM probe_clusters;
  DELETE FROM probe_cluster_members;
"
systemctl --user start sentinel-profiler.service
```

### 18.11 Backup rotation (daily, keep 14)

```bash
cat > ~/sentinel-backup-rotate.sh <<'ROT_EOF'
#!/bin/bash
# Daily backup, keep last 14
BACKUP_DIR=~/sentinel-backups
mkdir -p "$BACKUP_DIR"
DATE=$(date +%Y%m%d)
sqlite3 ~/sentinel/data/sentinel.db ".backup $BACKUP_DIR/sentinel-$DATE.db"
# Keep last 14
ls -t "$BACKUP_DIR"/sentinel-*.db | tail -n +15 | xargs -r rm
ROT_EOF
chmod +x ~/sentinel-backup-rotate.sh

# Install daily at 04:00
(crontab -l 2>/dev/null; echo "0 4 * * * ~/sentinel-backup-rotate.sh") | crontab -
```

---

## 19. Remote Access Patterns

### 19.1 Standard SSH

```bash
# From Framework over LAN ethernet to Pi eth0
ssh user@192.168.1.100

# From Framework over LAN WiFi to Pi wlan0 (if wlan0 is up — not during Sentinel)
ssh user@192.168.1.101

# From anywhere via Tailscale
ssh user@192.0.2.10
```

### 19.2 Run one-off query from Framework without a shell session

```bash
ssh user@192.168.1.100 "sqlite3 ~/sentinel/data/sentinel.db 'SELECT COUNT(*) FROM devices'"
ssh user@192.168.1.100 "~/sentinel-queries.sh close"
```

### 19.3 Multi-line script execution over SSH

```bash
ssh user@192.168.1.100 "bash -s" <<'REMOTE_EOF'
~/sentinel-queries.sh close
echo "---"
~/sentinel-queries.sh new
echo "---"
./sentinel.sh status
REMOTE_EOF
```

### 19.4 Pull DB snapshot to Framework for offline analysis

```bash
# Hot copy
ssh user@192.168.1.100 "sqlite3 ~/sentinel/data/sentinel.db '.backup /tmp/sentinel-snapshot.db'"
scp user@192.168.1.100:/tmp/sentinel-snapshot.db ~/sentinel-snapshots/$(date +%Y%m%d-%H%M).db
ssh user@192.168.1.100 "rm /tmp/sentinel-snapshot.db"

# Query it from Framework
sqlite3 ~/sentinel-snapshots/20260420-0400.db
```

### 19.5 Tail Pi logs from Framework

```bash
ssh user@192.168.1.100 "sudo journalctl -u sentinel-wifi -f"
ssh user@192.168.1.100 "journalctl --user -u sentinel-ingest -f"
```

### 19.6 Push this manual TO the Pi

```bash
# From Framework (run on Framework)
rsync -avz ~/projects/sentinel/OPERATOR_MANUAL.md user@192.168.1.100:/home/user/sentinel/OPERATOR_MANUAL.md
```

### 19.7 Read the manual from the Pi

```bash
# SSH in, then:
less ~/sentinel/OPERATOR_MANUAL.md
# Or jump to a section:
grep -n "^## " ~/sentinel/OPERATOR_MANUAL.md
less +'/Playbook D' ~/sentinel/OPERATOR_MANUAL.md
```

---

## 20. Watchlists and Automated Monitoring

### 20.1 Inline cron-based watchlist (see also Playbook K)

```bash
cat > ~/sentinel-watchlist.sh <<'WL_EOF'
#!/bin/bash
DB=~/sentinel/data/sentinel.db
LOGFILE=~/sentinel-alerts.log

# Watch for specific MACs
WATCH_MACS="aa:bb:cc:00:00:05 xx:xx:xx:xx:xx:xx"

# Watch for close devices
CLOSE_THRESHOLD=-40

# Watch for devices probing specific SSIDs
WATCH_SSIDS="HomeNetwork-2G"

for MAC in $WATCH_MACS; do
  COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM observations WHERE mac='$MAC' AND timestamp > datetime('now','-5 minutes')")
  if [ "$COUNT" -gt 0 ]; then
    RSSI=$(sqlite3 "$DB" "SELECT MAX(rssi) FROM observations WHERE mac='$MAC' AND timestamp > datetime('now','-5 minutes')")
    echo "$(date '+%Y-%m-%d %H:%M') WATCH-MAC $MAC RSSI $RSSI ($COUNT obs)" >> "$LOGFILE"
  fi
done

CLOSE_COUNT=$(sqlite3 "$DB" "SELECT COUNT(DISTINCT mac) FROM observations WHERE timestamp > datetime('now','-5 minutes') AND rssi > $CLOSE_THRESHOLD")
if [ "$CLOSE_COUNT" -gt 0 ]; then
  echo "$(date '+%Y-%m-%d %H:%M') CLOSE-DEVICES count=$CLOSE_COUNT" >> "$LOGFILE"
fi

for SSID in $WATCH_SSIDS; do
  PROBERS=$(sqlite3 "$DB" "SELECT COUNT(DISTINCT mac) FROM probe_requests WHERE ssid='$SSID' AND timestamp > datetime('now','-5 minutes')")
  if [ "$PROBERS" -gt 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M') SSID-PROBED '$SSID' by $PROBERS devices" >> "$LOGFILE"
  fi
done
WL_EOF
chmod +x ~/sentinel-watchlist.sh

(crontab -l 2>/dev/null; echo "*/5 * * * * ~/sentinel-watchlist.sh") | crontab -
tail -f ~/sentinel-alerts.log
```

### 20.2 Review cron config

```bash
crontab -l
```

### 20.3 Remove cron entries

```bash
crontab -e
# delete lines manually and save
```

---

## 21. Export and Offline Analysis

### 21.1 CSV exports of tables

```bash
./sentinel.sh export devices --format csv > ~/sentinel-exports/devices-$(date +%Y%m%d).csv
./sentinel.sh export alerts --format csv > ~/sentinel-exports/alerts-$(date +%Y%m%d).csv
./sentinel.sh export observations --format csv --since "$(date -d 'yesterday' +%Y-%m-%d)" > ~/sentinel-exports/obs-yesterday.csv
```

### 21.2 JSON exports

```bash
./sentinel.sh export devices --format json > ~/sentinel-exports/devices.json
./sentinel.sh export alerts --format json > ~/sentinel-exports/alerts.json
```

### 21.3 Open a snapshot in pandas on Framework

```bash
# Pull snapshot
ssh user@192.168.1.100 "sqlite3 ~/sentinel/data/sentinel.db '.backup /tmp/snap.db'"
scp user@192.168.1.100:/tmp/snap.db ~/snap.db

# Analyze in Python
python3 <<'PY_EOF'
import sqlite3, pandas as pd
c = sqlite3.connect('/home/user/snap.db')
df = pd.read_sql("SELECT * FROM observations WHERE timestamp > datetime('now','-1 day')", c)
print(df.describe())
print(df.groupby('mac').size().sort_values(ascending=False).head(20))
PY_EOF
```
---

# PART VI — SURVIVAL

## 22. Troubleshooting Tree

### 22.1 Nothing is capturing

**Symptom:** `./sentinel.sh status` shows all daemons active, but `observations` count stays flat.

Diagnose:

```bash
# 1. Is Alfa plugged in and recognized?
lsusb | grep -iE "mediatek|realtek|alfa"
ip link show wlan1

# 2. Is wlan1 in monitor mode?
iwconfig wlan1
# Expected: "Mode:Monitor"

# 3. Does the interface actually receive frames?
sudo tcpdump -i wlan1 -c 20

# 4. Is the bus socket alive?
ls -la /run/sentinel/bus.sock
```

Fixes:

```bash
# If wlan1 not in monitor mode:
sudo ip link set wlan1 down
sudo iw dev wlan1 set type monitor
sudo ip link set wlan1 up
./sentinel.sh restart

# If no frames received:
sudo airmon-ng check kill
sudo airmon-ng start wlan1
./sentinel.sh restart
```

### 22.2 Daemon in crash loop

**Symptom:** `systemctl status` shows "activating (auto-restart)" repeatedly.

```bash
# Find the error
sudo journalctl -u sentinel-wifi -n 100 --no-pager
journalctl --user -u sentinel-ingest -n 100 --no-pager
```

Common causes and fixes:

| Error message | Cause | Fix |
|---|---|---|
| `OSError: [Errno 19] No such device` | Interface gone | Plug Alfa in, `./sentinel.sh restart` |
| `PermissionError` | Running as non-root | Check systemd unit, should be `User=root` for capture daemons |
| `Database is locked` | Stale lock from crash | `./sentinel.sh stop`, wait 10s, `./sentinel.sh start` |
| `No module named 'scapy'` | Venv broken | `cd ~/sentinel && .venv/bin/pip install -e .` |
| `Address already in use` | Stale bus socket | `rm -f /run/sentinel/bus.sock && ./sentinel.sh restart` |

### 22.3 DB corruption

```bash
sqlite3 ~/sentinel/data/sentinel.db "PRAGMA integrity_check;"
```

If not `ok`:

```bash
./sentinel.sh stop
mv ~/sentinel/data/sentinel.db ~/sentinel/data/sentinel.db.corrupt
cp ~/sentinel-backups/sentinel-LATEST.db ~/sentinel/data/sentinel.db
./sentinel.sh start
```

### 22.4 wlan0 lost WiFi after Sentinel started

Expected — `airmon-ng check kill` stops NetworkManager. Restore for the current boot:

```bash
sudo systemctl start NetworkManager
sudo nmcli device connect wlan0
```

Permanent fix: Stage 13 hardening (future work).

### 22.5 Selftest all-FAIL after reboot

```bash
# Are units enabled?
systemctl is-enabled sentinel-wifi sentinel-bt
systemctl --user is-enabled sentinel-ingest sentinel-detector sentinel-profiler.timer

# If not:
sudo systemctl enable sentinel-wifi sentinel-bt
systemctl --user enable sentinel-ingest sentinel-detector sentinel-profiler.timer

# Then:
./sentinel.sh start
./sentinel.sh selftest
```

### 22.6 "install.sh failed at pip step"

Most common cause: `pyproject.toml` has a broken build-backend string.

Check:

```bash
head -5 ~/sentinel/pyproject.toml
```

Should be:
```
[build-system]
requires = ["setuptools>=68.0", "wheel"]
build-backend = "setuptools.build_meta"
```

If you see `setuptools.backends._legacy:_Backend` (an invalid backend that can creep into the file), fix:

```bash
sed -i 's|build-backend = "setuptools.backends._legacy:_Backend"|build-backend = "setuptools.build_meta"|' ~/sentinel/pyproject.toml
bash ~/sentinel/install.sh
```

### 22.7 Pi ran out of disk space

```bash
df -h ~/sentinel/data
du -sh ~/sentinel/data/*
```

If DB is enormous, prune (see 18.9) or migrate to external storage.

### 22.8 High CPU usage

```bash
top -p $(pgrep -d',' -f sentinel)
```

Normal: ingest daemon ~5-15% CPU, WiFi capture ~10-20%, BLE capture ~5%. Spikes above 50% sustained indicate something is wrong — usually write pressure on the DB. Check disk I/O:

```bash
iostat -x 2
```

### 22.9 Alerts never firing even after 7 days

```bash
# Have profiles actually been built?
sqlite3 ~/sentinel/data/sentinel.db "SELECT COUNT(*) FROM device_profiles"

# When was profiler last run?
journalctl --user -u sentinel-profiler -n 20

# Force profiler run
systemctl --user start sentinel-profiler.service

# Is detector running?
systemctl --user status sentinel-detector
```

If profiles exist and detector is active, alerts should fire for real anomalies. Check detector config in `config.yaml` — anomaly thresholds might be too strict.

---

## 23. Dictionary

### 23.1 802.11 Frame Type/Subtype Decoder

| type | subtype | Name | Meaning |
|---|---|---|---|
| 0 | 0 | Association Request | Client asking to join this AP |
| 0 | 1 | Association Response | AP's answer |
| 0 | 2 | Reassociation Request | Client re-joining after roam |
| 0 | 3 | Reassociation Response | |
| 0 | 4 | **Probe Request** | Client looking for a network |
| 0 | 5 | Probe Response | AP answering a probe |
| 0 | 8 | **Beacon** | AP announcing itself (~10/sec per AP) |
| 0 | 9 | ATIM | Power-save notification |
| 0 | 10 | Disassociation | Normal leave |
| 0 | 11 | Authentication | Pre-association handshake |
| 0 | 12 | Deauthentication | Forced disconnect |
| 0 | 13 | Action | Management action (channel switch, etc.) |
| 1 | 8 | Block Ack Request | QoS control |
| 1 | 9 | Block Ack | QoS control |
| 1 | 11 | RTS | Request to send |
| 1 | 12 | CTS | Clear to send |
| 1 | 13 | ACK | Frame acknowledgment |
| 1 | 14 | CF-End | Contention-free period end |
| 2 | 0 | Data | Plain payload |
| 2 | 4 | **QoS Null** | Power-save keepalive (device is sleeping) |
| 2 | 5 | QoS CF-ACK | |
| 2 | 8 | **QoS Data** | Payload with QoS tag |

### 23.2 BLE Manufacturer ID Decoder

`manufacturer_data_hex` starts with a 2-byte little-endian vendor ID. First 4 hex chars = vendor.

| hex (4 chars) | Vendor |
|---|---|
| `4c00` | Apple |
| `7500` | Samsung |
| `0600` | Microsoft |
| `e000` | Google |
| `8700` | Garmin |
| `da03` | Amazon |
| `0500` | Nordic Semiconductor |
| `ff13` | Xiaomi |
| `5701` | Nintendo |
| `4d00` | Fitbit |
| `8c00` | Logitech |
| `5b00` | Tile |
| `ff00` | Intel |
| `4700` | Sony |
| `5c00` | Harman International |
| `0f00` | Broadcom |
| `0100` | Ericsson |
| `0200` | Nokia |
| `0300` | IBM |
| `3600` | Silicon Labs |

### 23.3 BLE Service UUID Quick Reference

| UUID | Service |
|---|---|
| `1800` | Generic Access |
| `1801` | Generic Attribute |
| `180a` | Device Information |
| `180f` | Battery Service |
| `180d` | Heart Rate |
| `1812` | HID (keyboards, mice) |
| `110b` | Audio Sink |
| `110a` | Audio Source |
| `fe9f` | Google Fast Pair |
| `fd5a` | Apple Find My Network |
| `fdd2` | Bose headphones |
| `feaa` | Eddystone beacons |
| `fee7` | Tencent |
| `ffff` | (often custom/proprietary) |

### 23.4 RSSI → Approximate Distance (Indoor Residential)

| RSSI (dBm) | Distance | Inference |
|---|---|---|
| -10 to -30 | Inches to ~10 ft | Same room, device likely on/near Pi |
| -30 to -50 | 10-30 ft | Same house, probably a nearby room |
| -50 to -70 | 30-100 ft | Next door, through multiple walls |
| -70 to -85 | 100-300 ft | Down the block |
| -85 and weaker | Fringe | Detection limit |

### 23.5 MAC Address Reading

First octet, second hex digit, bit 1 (the "locally administered" bit):

| Second hex digit ends in | Bit 1 | Meaning |
|---|---|---|
| `0`, `4`, `8`, `c` | 0 | Factory-assigned (real) |
| `2`, `6`, `a`, `e` | 1 | Locally administered (randomized) |

Examples:
- `88:1e:...` → `88` = `10001000` → bit 1 = 0 → factory MAC
- `d6:28:...` → `d6` = `11010110` → bit 1 = 1 → randomized
- `f4:28:...` → `f4` = `11110100` → bit 1 = 0 → factory
- `1a:e3:...` → `1a` = `00011010` → bit 1 = 1 → randomized

First 3 octets (OUI) = vendor assignment, look up in `oui_vendors` table.

### 23.6 WiFi Channel → Frequency

| Channel | Frequency | Band |
|---|---|---|
| 1 | 2412 MHz | 2.4 GHz |
| 6 | 2437 MHz | 2.4 GHz |
| 11 | 2462 MHz | 2.4 GHz |
| 36 | 5180 MHz | 5 GHz UNII-1 |
| 40 | 5200 MHz | 5 GHz UNII-1 |
| 44 | 5220 MHz | 5 GHz UNII-1 |
| 48 | 5240 MHz | 5 GHz UNII-1 |
| 100-144 | 5500-5720 MHz | 5 GHz UNII-2C (DFS) |
| 149-165 | 5745-5825 MHz | 5 GHz UNII-3 |

### 23.7 Alert Types

| Type | Meaning |
|---|---|
| `new_device` | MAC seen for the first time |
| `temporal` | Device active at unusual hour (vs. its time_histogram) |
| `location` | Device at unusually strong/weak RSSI |
| `behavioral` | Unusual probe rate or channel set |
| `absence` | Regular device suddenly missing |
| `correlation` | Device seen without usual companion |
| `probe_set_cluster` | New cluster of randomized MACs identified |

### 23.8 Config File Reference

`~/sentinel/config.yaml` key options:

```yaml
capture:
  wifi:
    interface: wlan1
    channels: [1, 6, 11, 36, 40, 44, 48, 149, 153, 157, 161]
    hop_interval_ms: 250
  bluetooth:
    adapter: hci0
    scan_interval_ms: 10000
    scan_duration_ms: 8000
  sdr:
    enabled: false
  gps:
    enabled: false
    fallback_lat: 28.5383
    fallback_lon: -81.3792

detection:
  learning_mode_days: 7
  thresholds:
    temporal_zscore: 2.5
    location_rssi_delta: 15
    absence_days: 3
  whitelist: []

storage:
  db_path: /home/user/sentinel/data/sentinel.db
  retention_days: 90
```

---

## 24. Emergency Commands

### 24.1 Panic stop

```bash
sudo systemctl stop sentinel-wifi sentinel-bt
systemctl --user stop sentinel-ingest sentinel-detector sentinel-profiler.timer sentinel-profiler.service
sudo pkill -9 -f sentinel
```

### 24.2 Restore Pi's normal WiFi

```bash
sudo systemctl stop sentinel-wifi sentinel-bt
sudo ip link set wlan1 down
sudo iw dev wlan1 set type managed
sudo ip link set wlan1 up
sudo systemctl start NetworkManager
sudo nmcli device connect wlan0
```

### 24.3 Nuke everything and reinstall

```bash
cd ~/sentinel
./sentinel.sh stop
rm -rf ~/sentinel/.venv ~/sentinel/data
sudo rm -f /etc/systemd/system/sentinel-wifi.service /etc/systemd/system/sentinel-bt.service
rm -f ~/.config/systemd/user/sentinel-*.service ~/.config/systemd/user/sentinel-*.timer
sudo systemctl daemon-reload
systemctl --user daemon-reload
bash install.sh
./sentinel.sh start
./sentinel.sh selftest
```

### 24.4 Find what's locking the DB

```bash
sudo fuser ~/sentinel/data/sentinel.db
sudo lsof ~/sentinel/data/sentinel.db*
```

### 24.5 Crash-loop detection

```bash
sudo systemctl status sentinel-wifi sentinel-bt
systemctl --user status sentinel-ingest sentinel-detector
```

Look at the "Active:" line. Cycling `activating → failed → activating` = crash loop. Stop, check logs, fix config.

### 24.6 Wipe DB, keep installation

```bash
./sentinel.sh stop
rm ~/sentinel/data/sentinel.db*
python3 -c "
import sqlite3
conn = sqlite3.connect('/home/user/sentinel/data/sentinel.db')
conn.execute('PRAGMA journal_mode=WAL')
with open('/home/user/sentinel/schema.sql') as f:
    conn.executescript(f.read())
conn.commit()
conn.close()
"
./sentinel.sh start
```

### 24.7 Hard reboot the Pi (if SSH is frozen)

```bash
# From Framework via Tailscale fallback
ssh user@192.0.2.10 "sudo reboot"

# If nothing responds, physically power cycle
```

### 24.8 Run sentinel in foreground for live debugging

```bash
./sentinel.sh stop
cd ~/sentinel
.venv/bin/python -m sentinel.capture.wifi --config config.yaml --verbose
# Ctrl+C to exit
```

Same pattern for bt, ingest, detector, profiler modules.

---

## 25. Quick-Reference Card

**20 commands you will actually use 90% of the time.**

```bash
# Lifecycle
./sentinel.sh start
./sentinel.sh stop
./sentinel.sh restart
./sentinel.sh status
./sentinel.sh selftest

# Daily queries
~/sentinel-queries.sh now                    # Who's here right now
~/sentinel-queries.sh close                  # Who's close right now
~/sentinel-queries.sh new                    # New devices last 24h
~/sentinel-queries.sh rhythm                 # Hourly pattern
~/sentinel-queries.sh regulars               # Devices seen 3+ days
~/sentinel-queries.sh whois <mac>            # Deep dive one device
~/sentinel-queries.sh ssids <mac>            # Networks a phone remembers
~/sentinel-queries.sh who-probes "<ssid>"    # Who's visited this network
~/sentinel-queries.sh close-at "TIME"        # Who was close at T

# Watching
./sentinel.sh watch                          # Live tail

# Alerts
./sentinel.sh alerts --severity medium
./sentinel.sh alerts --unacked

# Schema
sqlite3 ~/sentinel/data/sentinel.db ".tables"

# DB size
ls -lh ~/sentinel/data/sentinel.db*

# Logs
sudo journalctl -u sentinel-wifi -n 50
```

---

## 26. Skill Building — Week 1 Exercise

The fastest path from "I see rows" to fluency. Three passes over one week.

### Pass 1 — First 4 hours after install

```bash
~/sentinel-queries.sh close
~/sentinel-queries.sh rhythm
~/sentinel-queries.sh new
```

**Goal:** Identify 5 devices you're sure are yours. Write their MACs down. Annotate them:

```bash
./sentinel.sh annotate aa:bb:cc:00:00:01 "My iPhone"
./sentinel.sh annotate aa:bb:cc:00:00:03 "My Framework laptop"
./sentinel.sh annotate aa:bb:cc:00:00:04 "Some ESP32 device in my house"
```

### Pass 2 — Day 2 or 3

```bash
~/sentinel-queries.sh whois <your-phone-mac>
~/sentinel-queries.sh ssids <your-phone-mac>
~/sentinel-queries.sh regulars
```

**Goal:** Read your own phone's probe SSID list. Recognize every network on it. That's your digital travel history. Then pick one unrecognized "regular" device and investigate it using Playbook D.

### Pass 3 — Day 5 or 6

```bash
~/sentinel-queries.sh rhythm
./sentinel.sh alerts
sqlite3 ~/sentinel/data/sentinel.db "SELECT COUNT(*) FROM probe_clusters"
```

**Goal:** Start noticing patterns without queries. You'll recognize the rhythm. You'll see when "the mailman showed up" reflects in the data. That's fluency.

---

## APPENDIX — Changelog for This Manual

Track versions here so you know what's been added.

- **1.0 (2026-04-20)** — Initial full manual. 13 sections, 14 playbooks, 60+ queries.

---

**End of Manual.**

Keep this file in both locations. When Sentinel evolves (Stage 13 NM coexistence, RTL-SDR, GPS/LoRa), append the new commands here. The manual IS the operator's knowledge — code in the repo, operator intuition here.
