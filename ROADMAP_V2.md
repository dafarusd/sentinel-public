# Sentinel Roadmap V2 — Sophisticated Sentinel

**Status:** Planning document for Stages 14–20.
**Baseline:** Stage 13 (commit `6855a17`) — chunked retention, operator manual, boot cheatsheet.
**Written:** 2026-04-22

---

## Why this doc exists

Stages 1–13 built a working passive RF surveillance platform: capture, storage, profiling, basic clustering, systemd-managed daemons, chunked retention, operational tooling. It works — 3,262 devices and 168k observations in ~48 hours of operation proved the pipeline is solid.

But the data being collected is **far richer** than what Sentinel currently does with it. The existing `probe_clusters` table has been sitting empty through 40+ profiler runs because the SSID-Jaccard clustering approach can't catch modern MAC-randomized devices — they don't leak named SSIDs anymore. Meanwhile, raw 802.11 Information Element bytes (`probe_requests.ie_bytes`) and 802.11 sequence numbers (`wifi_frames.sequence_num`) are **already being captured** but have no downstream consumer.

This roadmap transforms Sentinel from "logs and simple profiles" into a multi-evidence identity inference platform with multi-spectrum SDR integration, real anomaly detection, and a serious query surface.

## Out of scope (explicit)

- **Hailo AI HAT+ integration** — opted out. All inference CPU-based or future GPU.
- **LLM integration** — reserved for Agent Ultra project, not Sentinel.
- **Face recognition / license plate lookups / social media scraping** — these would tie anonymous RF identities to named individuals. Hard boundary: Sentinel does not cross from "ambient observation" into "targeted identification of specific named humans via external data sources."
- **Active RF operations** — no signal injection, deauth, jamming, or anything that goes beyond passive reception. Sentinel listens; it does not transmit.
- **Decryption of encrypted content** — headers and metadata are in the air passively. Payloads behind encryption remain unread.

## Design decisions (locked in)

1. **Schema migrations are additive.** ALTER TABLE ADD COLUMN is fine; no destructive changes to existing tables. Backward compatible.
2. **Clustering confidence policy: start conservative, tune down.** Default merge threshold at 0.85; observe real data; relax toward 0.70 as validated.
3. **Identity inference runs in the existing 15-min profiler cycle** (`run_profiler()`), not a separate daemon. Reuses the read connection and writer already open.

## Vocabulary

- **Observation** — a single radio packet captured (existing).
- **Device** — a MAC address we've seen (existing).
- **Identity** / **Cluster** — inferred physical device, potentially spanning multiple MACs (extending).
- **Evidence type** — the signal used to link MACs into an identity: `ssid_jaccard`, `ie_fingerprint`, `sequence_num`, `rssi_pattern`, `ble_manuf`, `continuity`, `multi_evidence`.
- **Fingerprint hash** — stable hash of canonical IE structure from a probe request.

---

# Stage 14 — Cross-Observation Fingerprinting

**Goal:** Collapse the 3,262 fragmented MAC addresses into stable identities that survive MAC rotation, using 802.11 Information Element fingerprinting as the primary evidence source.

**Success metric:** 3,262 devices → ~400–800 identities in a backfill pass. "Real device count" reflects physical reality, not MAC address churn.

---

## Stage 14a — IE fingerprint extraction + backfill

### Intent
Compute a canonical fingerprint hash from the raw IE bytes already being captured. Store on `probe_requests`. Backfill all existing observations.

### Files affected
- `schema.sql` — add column
- `sentinel/db/schema.py` — reflect the added column
- `sentinel/profiler/fingerprint.py` — **NEW** — the hashing module
- `sentinel/capture/wifi.py` — compute hash inline during capture
- `sentinel/profiler/backfill_fingerprints.py` — **NEW** — one-shot backfill

### Schema migration
```sql
ALTER TABLE probe_requests ADD COLUMN ie_fingerprint_hash TEXT;
CREATE INDEX IF NOT EXISTS idx_probe_ie_fp ON probe_requests (ie_fingerprint_hash);
```

Idempotent-safe ALTER pattern:
```python
def _add_column_if_missing(conn, table, column, type_spec):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_spec}")
```

### Canonical fingerprint composition

Include in the hash (stable across MAC rotation):
- HT Capabilities (IE ID 45) — raw bytes
- VHT Capabilities (IE ID 191) — raw bytes
- HE Capabilities (IE ID 255, ext 35) — raw bytes
- Extended Capabilities (IE ID 127) — raw bytes
- Supported Rates (IE ID 1) — sorted bytes
- Extended Supported Rates (IE ID 50) — sorted bytes
- Vendor-specific IEs (IE ID 221) — filtered: keep OUI prefixes only, drop noisy content

Exclude from the hash:
- SSID (IE ID 0) — this is what we're querying BY, not fingerprinting FROM
- DS Parameter Set (IE ID 3) — channel varies with environment
- RSN (IE ID 48) — varies with network security context
- Any IE with timestamps, nonces, or dynamic state

### Hash function
SHA-256 over the sorted, length-prefixed canonical representation. 64-char hex output. Collisions within a sensible human-scale dataset are negligible.

### Backfill script behavior
- Read `probe_requests WHERE ie_bytes IS NOT NULL AND ie_fingerprint_hash IS NULL`
- Batch in chunks of 5000 rows
- Compute hash, UPDATE row
- Progress logged every 10k rows
- Safe to re-run (idempotent on the NULL check)

### Acceptance
After running:
- `SELECT COUNT(*) FROM probe_requests WHERE ie_fingerprint_hash IS NOT NULL` ≈ total non-NULL `ie_bytes` count
- `SELECT COUNT(DISTINCT ie_fingerprint_hash) FROM probe_requests` produces a count that's far smaller than the MAC count (proof that multiple MACs share fingerprints)

---

## Stage 14b — IE-fingerprint clustering

### Intent
Extend the profiler to cluster MACs by shared IE fingerprint + temporal co-occurrence.

### Files affected
- `sentinel/profiler/engine.py` — add `compute_ie_clusters()` alongside existing `compute_probe_clusters()`
- `schema.sql` — add `evidence_type` column to `probe_clusters`
- `sentinel/db/schema.py` — reflect

### Schema migration
```sql
ALTER TABLE probe_clusters ADD COLUMN evidence_type TEXT NOT NULL DEFAULT 'ssid_jaccard';
```

(All existing clusters, if any, are tagged 'ssid_jaccard' by default.)

### Clustering algorithm
Parallel to existing `compute_probe_clusters()` but keyed on `ie_fingerprint_hash`:

1. Query `probe_requests` grouped by `ie_fingerprint_hash`, exclude NULL.
2. For each fingerprint hash shared by 2+ distinct MACs:
   - Check pairwise time-window overlap: were the MACs observed within N hours of each other? (Default N=24, configurable.)
   - If yes, union-find merge into a cluster.
3. Emit clusters with `evidence_type='ie_fingerprint'`.
4. Write to `probe_clusters` / `probe_cluster_members` / update `devices.probe_cluster_id`.

### Temporal overlap rule
Two MACs cluster under IE fingerprint evidence only if their first_seen/last_seen windows overlap OR abut within the configured N-hour gap. Prevents collapsing identical-model devices across months into one cluster.

### Scoring
For IE-fingerprint clusters, the `jaccard_score` column is repurposed: store the **temporal overlap ratio** (overlap hours / total span hours) as a quick proxy for confidence. Full scoring (Stage 14c) comes next.

### Acceptance
- `SELECT COUNT(*) FROM probe_clusters WHERE evidence_type='ie_fingerprint'` > 0
- Visual inspection: clusters contain MACs that plausibly belong to the same physical device (compare first/last_seen timestamps and RSSI distributions)

---

## Stage 14c — Multi-evidence scoring

### Intent
Combine multiple evidence signals into a unified confidence score. Merge compatible clusters. Promote strong-evidence identities above single-signal clusters.

### Evidence types and weights

| Evidence | Source | Weight | Rationale |
|---|---|---|---|
| IE fingerprint exact match | `probe_requests.ie_fingerprint_hash` | 0.50 | Strong structural signal, survives MAC rotation |
| SSID Jaccard ≥0.6 | existing computation | 0.25 | Strong when available (older/leaky devices) |
| Sequence number continuity | `wifi_frames.sequence_num` | 0.15 | Device-level counter leak across MAC boundary |
| RSSI distribution match | computed | 0.05 | Weak individually, useful as tiebreaker |
| Temporal co-occurrence | timestamps | 0.05 | Required baseline, not additive on its own |

Sum to 1.0. Threshold for merge: 0.85 (conservative default per Stage 13 decision).

### Sequence number evidence
For MACs that appear sequentially (one disappears, another appears within seconds), if the 802.11 sequence numbers form a continuous or near-continuous range, mark as same-identity evidence.

### RSSI distribution evidence
For each MAC, compute the per-channel RSSI mean and stddev. Two MACs with overlapping 95% RSSI confidence intervals (per channel) in the same time window → weak same-identity evidence.

### Merge algorithm
1. For each pair of existing clusters, compute summed evidence score across all member MACs.
2. If score ≥ 0.85, merge clusters into `multi_evidence` supercluster.
3. Retain lower-evidence clusters as subclusters inside the supercluster (don't lose data, just aggregate).

### Acceptance
- Identities count drops further from Stage 14b baseline.
- Manual spot-check: your own 20 identified devices each appear as single identities (not multiple clusters).

---

## Stage 14d — BLE manufacturer data fingerprinting

### Intent
Extend identity inference to Bluetooth. Apple Continuity packets, Google Fast Pair, Microsoft CDP all contain fingerprintable structure in `bt_advertisements.manufacturer_data_hex`.

### Files affected
- `sentinel/profiler/ble_fingerprint.py` — **NEW** — parse Apple Continuity, Google FP, MS CDP
- `sentinel/profiler/engine.py` — wire BLE clustering into `run_profiler()`
- `schema.sql` — add `ble_fingerprint_hash` column to `bt_advertisements`

### Apple Continuity decoders
Apple publishes the Continuity packet structure informally. Known sub-message types include: Handoff, Nearby Action, AirDrop, AirPods, Find My, Nearby Info. Each contains partial device identity.

Parse the manufacturer data, extract stable fields (device model hint, OS hint, handoff state), hash.

### Google Fast Pair / Microsoft CDP
Similar — parse manufacturer_data_hex for stable structural fields.

### Cross-modal linking
A MAC seen via BLE and a MAC seen via WiFi, with temporal co-occurrence, can be linked via `multi_evidence`. This is how a phone's WiFi identity and BLE identity become a single inferred identity.

### Acceptance
- BLE-only identities form for devices that have no WiFi probe activity (headphones, watches, trackers).
- Phones that emit both WiFi and BLE get unified into single identities.

---

## Stage 14e — CLI visibility for identities

### Intent
Make identities queryable from `sentinel.sh`.

### New commands
```
./sentinel.sh identities                 # list all identities/clusters, summary view
./sentinel.sh identity <cluster_id>      # detail view: members, evidence, first/last seen
./sentinel.sh identity --mac <mac>       # find cluster containing this MAC
./sentinel.sh identity --top 20          # top N most-observed identities
```

### `./sentinel.sh status` extension
Add an "Identities" line alongside "Devices":
```
  Devices:      3262  (raw MACs)
  Identities:   612   (inferred, -81% from MAC count)
  Profiles:     571
```

### Files affected
- `sentinel/cli/main.py` — new subcommand group
- `sentinel/query/api.py` — identity query functions
- `sentinel.sh` — wire identities subcommand

---

## Stage 14f — Backfill + validation

### Intent
Run all stages 14a–d against existing observations to collapse historical MACs into identities.

### Steps
1. Backfill IE fingerprints (14a script).
2. Run profiler cycle manually to populate IE clusters (14b).
3. Run multi-evidence scoring over all clusters (14c).
4. Run BLE fingerprinting backfill (14d).
5. Spot-check against the 20 known self-devices — each should appear as single identity.
6. Spot-check against visible patterns in the data (e.g., devices known to be one phone appearing as 5+ MACs before should now be one identity).

### Acceptance
- Total identity count is substantially less than device count.
- Your own 20 devices are correctly unified.
- No obviously wrong merges (two clearly-different devices collapsed incorrectly).

---

# Stage 15 — Temporal Pattern Modeling

**Goal:** Build per-identity routines. Not "last hour activity" — real time-of-day, day-of-week, session-duration, transition-pattern models that deviations can be compared against.

## Stage 15a — Per-identity time-of-day + day-of-week baselines

Extend `device_profiles` with:
- `dow_histogram` — 7-element array (day-of-week presence counts)
- `session_duration_stats` — mean, stddev, p50, p95 of session lengths
- `inter_session_gap_stats` — how long between sessions typically

Compute over rolling 30-day window. Update on each profiler cycle.

## Stage 15b — Presence rhythm

For each identity, detect the dominant temporal rhythm: daily, weekly, weekdays-only, weekends-only, irregular. Store as `rhythm_class` with confidence.

## Stage 15c — Stability scoring

Some identities are stable residents (present most days, same hours). Others are transient (visitors, delivery drivers, neighbors passing). Compute `stability_score` 0–1 per identity.

## Stage 15d — Expected-presence windows

For each stable identity, compute expected-presence intervals (e.g., "home 18:00–08:00 weekdays with 85% confidence"). These become inputs to Stage 17 anomaly detection.

---

# Stage 16 — Co-occurrence and Social Graph

**Goal:** Which identities consistently appear together? Which follow each other? Build a co-occurrence graph from the existing `companion_macs` data, lifted to identities.

## Stage 16a — Identity-level co-occurrence

Existing `device_profiles.companion_macs` is MAC-level. Re-compute at identity level: which identities co-occur in the same window.

New table:
```sql
CREATE TABLE identity_cooccurrence (
    identity_a TEXT NOT NULL,
    identity_b TEXT NOT NULL,
    cooccur_count INTEGER NOT NULL,
    first_observed TEXT NOT NULL,
    last_observed TEXT NOT NULL,
    window_size_s INTEGER NOT NULL,
    PRIMARY KEY (identity_a, identity_b)
);
```

## Stage 16b — Follow detection

Detect directional following: identity A appears, identity B appears within seconds/minutes, consistently. Different from simple co-occurrence.

## Stage 16c — Group inference

Identities that co-occur in groups of 3+ with high frequency are "groups" (households, co-workers, friend groups passing together).

## Stage 16d — Relationship strength scoring

Co-occurrence count × consistency × duration = relationship strength. A spouse is stronger than a once-a-week meetup.

**Explicit scope limit:** This stage infers relational structure without attaching names. We never cross-reference to external identification databases.

---

# Stage 17 — Real Anomaly Detection

**Goal:** Replace "new device detected" alerts with statistically grounded anomaly detection on identities.

## Stage 17a — Temporal deviation detector

For each stable identity (Stage 15), alert when they appear outside expected windows. Severity scales with:
- How far outside expected
- Identity's stability score
- Deviation persistence (single observation vs sustained)

## Stage 17b — Absence detector

For each stable identity, alert when they fail to appear during their expected window. "Person who is home every weeknight is absent tonight" is a real signal.

## Stage 17c — New identity with high-engagement pattern

Alert when a new identity appears and immediately shows high engagement (long sessions, high co-occurrence with residents, returning within hours). Differentiates "package delivery bounce" from "someone hanging around."

## Stage 17d — Group/cohort anomalies

A usual co-occurring pair/group appearing without one member. Or a group's typical composition changing.

## Stage 17e — Alert tuning

Implement user feedback loop: mark alerts as true/false positive, retrain thresholds. Avoid drowning you in noise.

---

# Stage 18 — SDR Integration

**Hardware on hand (arriving Apr 22):**
- NooElec NESDR Smart XTR SDR (E4000 tuner, 0.5 PPM TCXO, SMA female, ~65 MHz–2.3 GHz with gap ~1.1–1.25 GHz)
- Nooelec RaTLSnake M6 v2 antenna bundle (helical, DVB-T2, telescopic; magnetic base + RG-58 6' cable)

**Connection:** Pi USB port, NOT USB 3.0 (causes RFI around 1 GHz that drowns ADS-B). Use USB 2.0 port on Pi 5.

## Stage 18a — Driver install + baseline validation

```
sudo apt install rtl-sdr librtlsdr-dev rtl-433 dump1090-fa multimon-ng
```

Blacklist kernel DVB driver (standard RTL-SDR setup). udev rules for non-root access.

First-boot validation: `rtl_test -t` detects SDR. Tune to local FM station (known-strong signal) and confirm audio decode.

## Stage 18b — ADS-B daemon (1090 MHz)

Goal: Every aircraft within ~150 miles logged to a new `sdr_adsb` table.

- `dump1090-fa` running on port 30003 (SBS-1 format)
- New daemon `sentinel-sdr-adsb` consuming that stream
- Ingest into structured table

```sql
CREATE TABLE sdr_adsb (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    icao24 TEXT NOT NULL,
    callsign TEXT,
    altitude INTEGER,
    latitude REAL,
    longitude REAL,
    velocity REAL,
    heading REAL,
    vertical_rate INTEGER,
    squawk TEXT,
    signal_db REAL
);
CREATE INDEX idx_adsb_icao_ts ON sdr_adsb(icao24, timestamp);
CREATE INDEX idx_adsb_ts ON sdr_adsb(timestamp);
```

Aircraft tracking is primarily a **validation** target: it proves the full chain works end-to-end on day one. Thousands of observations immediately.

Antenna for this: **helical** from the RaTLSnake kit.

## Stage 18c — rtl_433 ISM band (433 MHz primary)

The real Sentinel-value target. `rtl_433 -F json` produces structured events for hundreds of device protocols: weather stations, TPMS, doorbells, smoke detectors, garage door openers, pool sensors, energy meters, car remotes.

New daemon `sentinel-sdr-rtl433` consuming the JSON stream.

Schema design: ONE wide table with protocol column + structured JSON, OR per-protocol tables. Decision below.

### Schema proposal (per-protocol tables for highest-value protocols, JSON blob for rest)

```sql
CREATE TABLE sdr_tpms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    tpms_id TEXT NOT NULL,
    model TEXT,                -- TPMS manufacturer protocol (Schrader, Continental, etc.)
    pressure_kpa REAL,
    temperature_c REAL,
    battery_ok INTEGER,
    signal_db REAL
);
CREATE INDEX idx_tpms_id_ts ON sdr_tpms(tpms_id, timestamp);

CREATE TABLE sdr_weather (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    device_id TEXT NOT NULL,
    model TEXT,
    temperature_c REAL,
    humidity_pct REAL,
    wind_kph REAL,
    rain_mm REAL,
    signal_db REAL
);

CREATE TABLE sdr_rtl433_generic (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    protocol TEXT NOT NULL,
    device_id TEXT,
    payload_json TEXT NOT NULL,
    signal_db REAL
);
```

TPMS and weather stations get their own tables because they're high-volume and high-value. Everything else goes in the generic table for flexibility.

Antenna for this: **DVB-T2** from the kit.

## Stage 18d — TPMS as vehicle identity layer

Every car has 4 TPMS sensors, each with a unique ID, broadcasting whenever the car moves. Aggregate groups of 4 TPMS IDs seen together within a few seconds into implied vehicle identities.

New table:
```sql
CREATE TABLE vehicle_identities (
    id TEXT PRIMARY KEY,
    tpms_ids_json TEXT NOT NULL,  -- JSON array of 2-4 TPMS IDs
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    observation_count INTEGER NOT NULL DEFAULT 0,
    notes TEXT
);
```

Cross-correlate vehicle appearances with WiFi/BLE identity arrivals — driver phone + vehicle = driver↔vehicle association.

## Stage 18e — SDR time-sharing architecture

Single SDR can only tune to one band at a time. With a single Smart XTR:
- Option 1: Rotate (ADS-B 10 min, rtl_433 10 min, cycle). Loses continuous coverage.
- Option 2: Pick one band. For Sentinel-value, **rtl_433 wins**.
- Option 3: Buy second $25 RTL-SDR V3 for dedicated second band. Likely purchase after Stage 18d proves value.

Default: start single-SDR on rtl_433 (433 MHz + 315 MHz TPMS). Validate ADS-B once briefly to prove chain, then commit to rtl_433 full-time.

## Stage 18f — Cross-modal fusion into unified timeline

At this point `observations`, `bt_advertisements`, `probe_requests`, `wifi_frames`, `sdr_tpms`, `sdr_rtl433_generic`, `sdr_adsb` all live in the same DB. Build a unified query layer that presents them as one timeline.

```
./sentinel.sh timeline --since "2 hours ago"
```

Emits a time-sorted stream of ALL event types with consistent formatting. First step toward the serious query surface in Stage 20.

## Deferred (not this stage)
- POCSAG pagers (~929 MHz with multimon-ng)
- NOAA weather satellites (137 MHz)
- AIS marine (not relevant to apartment context)
- LoRa spectrum monitoring (overlaps Meshtastic; later)
- Ham-It-Up converter for HF bands (hardware purchase)

---

# Stage 19 — Cross-Modal Fusion

**Goal:** Identities are currently per-modality (WiFi, BLE, SDR). Fuse them into single "physical thing" identities.

## Stage 19a — Identity fusion

A WiFi identity and a BLE identity with temporal co-occurrence + consistent RSSI pattern = one physical device. Merge into unified identity.

A vehicle identity (TPMS cluster) + a WiFi/BLE identity always arriving together = driver↔vehicle binding.

## Stage 19b — Environmental timeline

Present the data as a unified environmental record. Every signal ever received, time-sorted, with source attribution and inferred identity linkage.

## Stage 19c — Signature library

Per-identity signature: typical WiFi probe patterns + BLE advertisement pattern + TPMS IDs (if vehicle-associated) + session characteristics. Stored as a JSON blob per identity.

---

# Stage 20 — Query Surface

**Goal:** Make the dataset actually usable. Right now it's SQL or CLI. Build a real query API.

## Stage 20a — Structured query language

A small DSL for common queries:
```
./sentinel.sh query "identities present between 02:00 and 04:00 last week"
./sentinel.sh query "who was near identity X in the last month"
./sentinel.sh query "anomalies this week severity >= medium"
./sentinel.sh query "vehicles arriving after 22:00"
```

Parses to SQL, runs, formats results.

## Stage 20b — Saved queries / views

Save query expressions as named views. Re-run on demand.

## Stage 20c — Export surface

JSON / CSV export for external analysis (pandas, Jupyter, whatever).

## Stage 20d — Web UI (optional, later)

Local-only web UI on Pi for the non-CLI-comfortable view. Low priority — CLI is enough for now.

---

# Execution order

**Priority sequence:**
1. Stage 14a-b (IE fingerprinting + clustering) — biggest immediate win, uses existing data
2. Stage 18a-c (SDR driver + ADS-B validation + rtl_433) — unlocks TPMS when hardware arrives
3. Stage 14c-f (full multi-evidence scoring) — deepens identity quality
4. Stage 15 (temporal modeling) — required before real anomaly detection
5. Stage 18d (TPMS vehicle identities) — bridges SDR into identity system
6. Stage 16 (co-occurrence graph) — builds on identities
7. Stage 17 (real anomaly detection) — culmination of 14-16
8. Stage 19 (cross-modal fusion) — unifies everything
9. Stage 20 (query surface) — makes it usable

## Dependencies
- Stage 14 → 15 → 16 → 17 (identity → temporal → relational → anomaly)
- Stage 18 can run parallel to 14–17 (SDR is independent subsystem)
- Stage 19 requires 14, 15, 18d (needs identities + temporal + vehicle)
- Stage 20 requires 19 (query over unified data)

## Checkpoints
Every stage commits to git with tag `stage-NN-description`. Every major sub-stage (14a, 14b, etc.) commits individually. Clean history for rollback.

---

# Operational principles

1. **Idempotency always.** Every script, migration, and backfill must be safe to re-run.
2. **Additive migrations only.** ALTER TABLE ADD COLUMN, never DROP. Backward compatible at every stage.
3. **Read-only where possible.** Clustering and profiling use read-only connections. Only the writer daemon mutates.
4. **Testable in isolation.** Each sub-stage should have at minimum one integration test that exercises the new code path against a fixture DB.
5. **Configurable, not hardcoded.** Thresholds, window sizes, decay rates all live in `config.yaml`.
6. **Observable by default.** Every new component logs to the existing logging setup. Stats go into the profiler stats dict.

---

# What this document is NOT

- A commitment to execute all stages. Priorities can shift based on what proves valuable.
- A timeline. No dates. Each stage takes however long it takes.
- Immutable. Amendments are fine; just record them here with a dated note.

---

## Amendment log

_(empty — add entries here when plan changes)_
