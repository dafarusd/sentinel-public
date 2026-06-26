-- Sentinel database schema
-- Idempotent: safe to run multiple times (CREATE TABLE IF NOT EXISTS).
-- All tables defined up front, including deferred sensors (sdr_observations, gps_fixes).

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================
-- Core tables
-- ============================================================

CREATE TABLE IF NOT EXISTS devices (
    mac             TEXT PRIMARY KEY,
    first_seen      TEXT NOT NULL,           -- ISO 8601 UTC
    last_seen       TEXT NOT NULL,           -- ISO 8601 UTC
    vendor          TEXT,                    -- OUI lookup result
    device_name     TEXT,                    -- advertised name (BT or mDNS)
    device_type     TEXT,                    -- 'wifi', 'bt_classic', 'ble', 'sdr', 'unknown'
    is_ap           INTEGER NOT NULL DEFAULT 0,
    probe_cluster_id TEXT,                   -- synthetic ID linking randomized MACs
    identity_id     TEXT,                    -- Stage 15: tagged by ingest from YAML dossier
    notes           TEXT                     -- user-supplied annotation
);

CREATE TABLE IF NOT EXISTS observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,           -- ISO 8601 UTC
    mac             TEXT NOT NULL,
    source          TEXT NOT NULL,           -- 'wifi', 'bt', 'sdr', 'gps'
    rssi            INTEGER,                -- dBm
    channel         INTEGER,
    latitude        REAL,
    longitude       REAL,
    identity_id     TEXT,                    -- Stage 15: tagged by ingest
    extra_json      TEXT                     -- source-specific fields as JSON
);

CREATE INDEX IF NOT EXISTS idx_observations_mac_ts ON observations (mac, timestamp);
CREATE INDEX IF NOT EXISTS idx_observations_ts ON observations (timestamp);

-- ============================================================
-- WiFi-specific
-- ============================================================

CREATE TABLE IF NOT EXISTS wifi_frames (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    src_mac         TEXT NOT NULL,
    dst_mac         TEXT,
    bssid           TEXT,
    ssid            TEXT,
    channel         INTEGER,
    rssi            INTEGER,
    frame_type      INTEGER NOT NULL,        -- 0=mgmt, 1=ctrl, 2=data
    frame_subtype   INTEGER NOT NULL,
    sequence_num    INTEGER,                 -- 802.11 sequence number (for Level C prep)
    identity_id     TEXT,                    -- Stage 15
    extra_json      TEXT
);

CREATE INDEX IF NOT EXISTS idx_wifi_frames_src_ts ON wifi_frames (src_mac, timestamp);
CREATE INDEX IF NOT EXISTS idx_wifi_frames_ts ON wifi_frames (timestamp);

CREATE TABLE IF NOT EXISTS probe_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    mac             TEXT NOT NULL,
    ssid            TEXT,                    -- NULL for broadcast probes
    rssi            INTEGER,
    channel         INTEGER,
    sequence_num    INTEGER,
    ie_bytes        BLOB,                   -- raw Information Element bytes (Level C prep)
    ie_fingerprint_hash TEXT,                -- SHA-256 of canonical IE fingerprint (Stage 14a)
    identity_id     TEXT,                    -- Stage 15
    extra_json      TEXT
);

CREATE INDEX IF NOT EXISTS idx_probe_mac_ts ON probe_requests (mac, timestamp);
CREATE INDEX IF NOT EXISTS idx_probe_ssid ON probe_requests (ssid);
-- idx_probe_ie_fp is created by _apply_additive_migrations (Stage 14a)
-- so it runs after the column is guaranteed to exist on upgraded DBs.

-- ============================================================
-- Bluetooth-specific
-- ============================================================

CREATE TABLE IF NOT EXISTS bt_advertisements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    mac             TEXT NOT NULL,
    device_name     TEXT,
    rssi            INTEGER,
    tx_power        INTEGER,
    manufacturer_data_hex TEXT,
    service_uuids   TEXT,                    -- JSON array of UUID strings
    device_class    INTEGER,                 -- classic BT class of device
    is_classic      INTEGER NOT NULL DEFAULT 0,
    mfr_fingerprint_hash TEXT,               -- SHA-256 of canonical mfr-data fingerprint (Stage 14d)
    identity_id     TEXT,                    -- Stage 15
    extra_json      TEXT
);

CREATE INDEX IF NOT EXISTS idx_bt_mac_ts ON bt_advertisements (mac, timestamp);
CREATE INDEX IF NOT EXISTS idx_bt_ts ON bt_advertisements (timestamp);
-- idx_bt_mfr_fp is created by _apply_additive_migrations (Stage 14d)
-- so it runs after the column is guaranteed to exist on upgraded DBs.

-- ============================================================
-- Profiling
-- ============================================================

CREATE TABLE IF NOT EXISTS device_profiles (
    mac             TEXT PRIMARY KEY,
    updated_at      TEXT NOT NULL,
    time_histogram  TEXT NOT NULL,            -- JSON: 24-element array (hourly presence counts)
    rssi_mean       REAL,
    rssi_stddev     REAL,
    rssi_p95        REAL,
    channel_set     TEXT,                    -- JSON array of observed channels
    probe_ssid_set  TEXT,                    -- JSON array of probed SSIDs
    probe_rate_mean REAL,                    -- probes per hour
    companion_macs  TEXT,                    -- JSON array of co-present MACs
    presence_pct_30d REAL,                   -- % of hours present in last 30 days
    total_observations INTEGER NOT NULL DEFAULT 0,
    extra_json      TEXT
);

CREATE TABLE IF NOT EXISTS probe_clusters (
    cluster_id      TEXT PRIMARY KEY,        -- synthetic UUID
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    ssid_set        TEXT NOT NULL,            -- JSON array of SSIDs defining this cluster
    device_count    INTEGER NOT NULL DEFAULT 0,
    evidence_type   TEXT NOT NULL DEFAULT 'ssid_jaccard',  -- 'ssid_jaccard' | 'ie_fingerprint' (Stage 14b)
    extra_json      TEXT
);

CREATE TABLE IF NOT EXISTS probe_cluster_members (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_id      TEXT NOT NULL REFERENCES probe_clusters(cluster_id),
    mac             TEXT NOT NULL,
    joined_at       TEXT NOT NULL,
    jaccard_score   REAL NOT NULL,
    UNIQUE(cluster_id, mac)
);

CREATE INDEX IF NOT EXISTS idx_pcm_cluster ON probe_cluster_members (cluster_id);
CREATE INDEX IF NOT EXISTS idx_pcm_mac ON probe_cluster_members (mac);

-- ============================================================
-- Alerts
-- ============================================================

CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    alert_type      TEXT NOT NULL,            -- new_device, temporal, location, behavioral, absence, correlation, probe_set_cluster
    severity        TEXT NOT NULL,            -- info, low, medium, high
    mac             TEXT,
    summary         TEXT NOT NULL,
    details_json    TEXT,                     -- structured alert context
    acknowledged    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts (timestamp);
CREATE INDEX IF NOT EXISTS idx_alerts_type ON alerts (alert_type);
CREATE INDEX IF NOT EXISTS idx_alerts_severity ON alerts (severity);
CREATE INDEX IF NOT EXISTS idx_alerts_mac ON alerts (mac);

-- ============================================================
-- Sessions (device presence windows)
-- ============================================================

CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mac             TEXT NOT NULL,
    start_time      TEXT NOT NULL,
    end_time        TEXT,                    -- NULL if session still active
    source          TEXT NOT NULL,
    avg_rssi        REAL,
    observation_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sessions_mac_ts ON sessions (mac, start_time);
CREATE INDEX IF NOT EXISTS idx_sessions_ts ON sessions (start_time);

-- ============================================================
-- Deferred sensor tables (schema defined now, populated later)
-- ============================================================

CREATE TABLE IF NOT EXISTS gps_fixes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    latitude        REAL NOT NULL,
    longitude       REAL NOT NULL,
    altitude        REAL,
    speed           REAL,
    satellites      INTEGER,
    fix_quality     INTEGER                  -- 0=invalid, 1=GPS, 2=DGPS
);

CREATE INDEX IF NOT EXISTS idx_gps_ts ON gps_fixes (timestamp);

CREATE TABLE IF NOT EXISTS sdr_observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    center_freq     INTEGER NOT NULL,        -- Hz
    bandwidth       INTEGER,
    peak_power_dbm  REAL,
    signal_type     TEXT,                    -- classification label if any
    duration_ms     REAL,
    extra_json      TEXT
);

CREATE INDEX IF NOT EXISTS idx_sdr_ts ON sdr_observations (timestamp);
CREATE INDEX IF NOT EXISTS idx_sdr_freq ON sdr_observations (center_freq);

-- Stage 18b: ADS-B aircraft messages from readsb (SBS-1 stream).
-- One row per decoded MSG record. No foreign key to devices — aircraft
-- live in their own namespace identified by icao_hex.
CREATE TABLE IF NOT EXISTS sdr_adsb (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp         TEXT NOT NULL,            -- ISO 8601 UTC
    icao_hex          TEXT NOT NULL,            -- 6-char ICAO24, lowercase
    callsign          TEXT,                     -- flight ID (MSG,1)
    altitude_ft       INTEGER,                  -- barometric altitude
    ground_speed_kt   INTEGER,
    track_deg         REAL,
    latitude          REAL,
    longitude         REAL,
    vertical_rate_fpm INTEGER,
    squawk            TEXT,                     -- 4-digit octal
    rssi_dbfs         REAL,                     -- signal strength (NULL on SBS-1)
    message_type      TEXT,                     -- SBS-1 transmission type 1-8
    extra_json        TEXT                      -- alert/emergency/spi/is_on_ground/session_id
);

CREATE INDEX IF NOT EXISTS idx_sdr_adsb_ts ON sdr_adsb (timestamp);
CREATE INDEX IF NOT EXISTS idx_sdr_adsb_icao_ts ON sdr_adsb (icao_hex, timestamp);

-- Stage D: 433 MHz capture via rtl_433. Three tables split by event class.
-- Like sdr_adsb, none have a foreign key to devices — these emitters live
-- in their own ID namespace (sensor_id / station_id / device_id) and don't
-- participate in the MAC-keyed devices/observations pipeline.

-- TPMS sensors (one row per beacon). sensor_id is the per-tire fixed ID
-- broadcast by the sensor — stable across reboots, the cross-source
-- correlation hook for vehicle re-identification.
CREATE TABLE IF NOT EXISTS sdr_tpms (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,           -- ISO 8601 UTC
    sensor_id       TEXT NOT NULL,           -- per-sensor fixed ID
    protocol        TEXT,                    -- rtl_433 model field (e.g. "Schrader-EG53MA4")
    pressure_kpa    REAL,
    temperature_c   REAL,
    battery_low     INTEGER,                 -- 0/1, NULL if not reported
    rssi            REAL,                    -- dB (rtl_433 emits "rssi" as float)
    flags           TEXT,
    identity_id     TEXT,                    -- Stage 15: tagged by sensor_id match
    extra_json      TEXT                     -- full raw rtl_433 event
);

CREATE INDEX IF NOT EXISTS idx_sdr_tpms_ts ON sdr_tpms (timestamp);
CREATE INDEX IF NOT EXISTS idx_sdr_tpms_sensor_id ON sdr_tpms (sensor_id);

-- Weather stations (Acurite, LaCrosse, Bresser, etc). station_id is the
-- per-station ID; many cheap stations randomize on battery change so
-- correlation utility is lower than TPMS.
CREATE TABLE IF NOT EXISTS sdr_weather (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    station_id      TEXT NOT NULL,
    protocol        TEXT,
    temperature_c   REAL,
    humidity        REAL,
    wind_kph        REAL,
    rain_mm         REAL,
    battery_low     INTEGER,
    rssi            REAL,
    identity_id     TEXT,                    -- Stage 15: tagged by station_id match
    extra_json      TEXT
);

CREATE INDEX IF NOT EXISTS idx_sdr_weather_ts ON sdr_weather (timestamp);
CREATE INDEX IF NOT EXISTS idx_sdr_weather_station_id ON sdr_weather (station_id);

-- Catch-all for non-TPMS, non-weather 433 MHz traffic (garage doors,
-- doorbells, alarms, pet trackers, plus anything the protocol map didn't
-- recognize — those land here with category='unknown' and full raw event
-- in extra_json so we can refine the map without re-capturing).
CREATE TABLE IF NOT EXISTS sdr_ism (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    device_id       TEXT,                    -- nullable; some protocols don't carry an ID
    protocol        TEXT,                    -- rtl_433 model field
    category        TEXT,                    -- 'garage'|'doorbell'|'alarm'|'pet'|'unknown'|...
    rssi            REAL,
    identity_id     TEXT,                    -- Stage 15: tagged by device_id match
    extra_json      TEXT
);

CREATE INDEX IF NOT EXISTS idx_sdr_ism_ts ON sdr_ism (timestamp);
CREATE INDEX IF NOT EXISTS idx_sdr_ism_protocol ON sdr_ism (protocol);

-- ============================================================
-- Metadata
-- ============================================================

CREATE TABLE IF NOT EXISTS sentinel_meta (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL
);

-- Track schema version and install timestamp
INSERT OR IGNORE INTO sentinel_meta (key, value) VALUES ('schema_version', '1');
INSERT OR IGNORE INTO sentinel_meta (key, value) VALUES ('installed_at', datetime('now'));

-- ============================================================
-- Stage 17a: Cross-modality identification aggregates
-- ============================================================

-- Per-MAC SSID set, append-only with first/last/count. SSID index
-- supports "who else probes for this network" queries.
CREATE TABLE IF NOT EXISTS device_probe_history (
    mac          TEXT NOT NULL,
    ssid         TEXT NOT NULL,
    first_probed TEXT NOT NULL,
    last_probed  TEXT NOT NULL,
    probe_count  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (mac, ssid)
);
CREATE INDEX IF NOT EXISTS idx_dph_ssid ON device_probe_history (ssid);
CREATE INDEX IF NOT EXISTS idx_dph_mac  ON device_probe_history (mac);

-- Every BLE device_name ever broadcast per MAC. Composite PK because
-- a single MAC can broadcast multiple names over time (firmware updates,
-- user renames).
CREATE TABLE IF NOT EXISTS device_ble_names (
    mac               TEXT NOT NULL,
    device_name       TEXT NOT NULL,
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL,
    observation_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (mac, device_name)
);
CREATE INDEX IF NOT EXISTS idx_dbn_name ON device_ble_names (device_name);

-- Unified per-MAC rollup. Foundation for Stage 17b-e. No identity_id
-- index at this stage; the table is small (one row per unique MAC
-- ever observed). Add if Stage 17b's joins demand it.
CREATE TABLE IF NOT EXISTS device_identity_features (
    mac                    TEXT PRIMARY KEY,
    first_seen             TEXT NOT NULL,
    last_seen              TEXT NOT NULL,
    total_observations     INTEGER NOT NULL DEFAULT 0,
    vendor                 TEXT,
    sources_seen           TEXT NOT NULL DEFAULT '[]',
    probe_ssid_count       INTEGER NOT NULL DEFAULT 0,
    ble_names              TEXT,
    rssi_min               INTEGER,
    rssi_max               INTEGER,
    rssi_avg               REAL,
    hours_active           INTEGER NOT NULL DEFAULT 0,
    paired_mac_candidates  TEXT,
    identity_id            TEXT,
    last_updated           TEXT NOT NULL
);

-- Incremental-aggregation watermarks. One row per aggregator
-- (probe_history, ble_names, identity_features).
CREATE TABLE IF NOT EXISTS identification_watermarks (
    aggregator        TEXT PRIMARY KEY,
    last_processed_ts TEXT NOT NULL,
    last_run_ts       TEXT NOT NULL
);
