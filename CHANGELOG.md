# Sentinel Changelog

## Stage 17a — Probe History, BLE Names, Identity Feature Aggregation (2026-05-11)

### Context
- Foundation layer of the Stage 17 cross-modality identification engine. Stage 17a ingests data already in the database (probe_requests, bt_advertisements, observations, devices) and produces three new aggregate tables that Stages 17b-e (co-arrival, daily rhythm, candidate engine, payload decode) will build on.
- All compute is INCREMENTAL based on per-aggregator watermarks. Re-running the runner is idempotent.
- No new daemon, no new systemd unit. Runs inside the existing profiler tick (every 15 min by default).

### Added
- `sentinel/identification/` — new module (6 files): `__init__.py`, `_state.py` (watermark helpers), `probe_history.py`, `ble_names.py`, `aggregator.py` (identity_features rebuild), `runner.py` (top-level entrypoint).
- `schema.sql` — three new tables + watermark table:
  - `device_probe_history (mac, ssid, first_probed, last_probed, probe_count)` PK(mac,ssid), indexes on (ssid) and (mac).
  - `device_ble_names (mac, device_name, first_seen, last_seen, observation_count)` PK(mac,device_name), index on (device_name).
  - `device_identity_features (mac PK, first_seen, last_seen, total_observations, vendor, sources_seen, probe_ssid_count, ble_names, rssi_min, rssi_max, rssi_avg, hours_active, paired_mac_candidates, identity_id, last_updated)` — 15 columns, one row per unique MAC ever observed.
  - `identification_watermarks (aggregator PK, last_processed_ts, last_run_ts)`.
- `sentinel/db/schema.py: verify_schema` — added the four new tables to the expected list (count now 21).
- `sentinel/profiler/engine.py: run_profiler` — calls `run_incremental(db_path, identity_map)` at the end of each tick, AFTER all clustering passes. Wrapped in try/except so identification failures cannot break clustering output or future ticks.

### Decisions
- **Each aggregator owns its own connection with BEGIN IMMEDIATE.** Mirrors the existing `_persist_*_clusters(db_path, ...)` pattern in profiler/engine.py. Upserts + watermark update commit atomically per aggregator — a crash mid-run cannot double-count, and a failure in one aggregator does not rollback the others.
- **Tables go in schema.sql, not in `_apply_additive_migrations`.** For entirely-new tables, `CREATE TABLE IF NOT EXISTS` in schema.sql is sufficient: `executescript` runs first on every startup and creates them idempotently on both fresh and upgraded DBs. Migrations are reserved for things `CREATE TABLE IF NOT EXISTS` can't express (ALTER ADD COLUMN, indexes on altered columns).
- **`device_identity_features` is rebuild-per-MAC, not delta-merge.** Per-MAC recompute is cheap because the intermediate tables (`devices`, `observations`, `device_probe_history`, `device_ble_names`) are already aggregated. Delta-merging stats across all 15 columns would be more code and more bug surface for ~no perf win at this DB size.
- **Skip empty SSIDs and BLE names.** `probe_requests.ssid IS NULL OR ssid = ''` covers broadcast probes (modern iPhones, most of the time) — those carry no per-network identification signal. Same logic for empty BLE names. The two filters trim ~75% of probe rows and ~50% of BT rows from the aggregation.
- **`paired_mac_candidates` uses first-14-char MAC prefix match.** Catches the classic +1/-1 router pattern (e.g. `aa:bb:cc:dd:ee:01` vs `aa:bb:cc:dd:ee:02`). Stored as a JSON list. False positives possible for randomized MACs from the same OUI; treated as a hint, not a claim.
- **Identity dossier reused as-is.** `sentinel.identity.loader.load_identity_map` from Stage 15 is the source of truth for `identity_id`. No auto-naming, no dossier writes — that remains human-only across all of Stage 17.
- **`_last_features_rebuild` rate-limit is harmless for oneshot profiler.** The 5-minute guard in `runner.py` is a no-op for `Type=oneshot` services (each tick is a fresh process with `_last_features_rebuild=0`). Watermarks bound the work regardless. The guard is meaningful only if Stage 17b ever wires this up to a long-lived caller.

### Testable
- All imports succeed: `python -c 'from sentinel.identification import run_incremental; from sentinel.identification.probe_history import aggregate_probe_history; from sentinel.identification.ble_names import aggregate_ble_names; from sentinel.identification.aggregator import rebuild_identity_features'`.
- Schema verification: `python -m sentinel.db.schema --verify` shows all 21 tables present including the four new ones.
- First profiler tick logged: `Stage 17a aggregation: {'probe_history': 1142, 'ble_names': 958, 'identity_features': 3306}`. The 3306 identity_feature rows exactly matches `COUNT(DISTINCT mac) FROM observations` at that moment.
- Watermarks advanced from epoch to recent timestamps for all three aggregators.
- Idempotency: second profiler tick 2.5 min later produced `{'probe_history': 0, 'ble_names': 9, 'identity_features': 11}` — the 0 for probe_history is the clean idempotency proof; the 9 and 11 reflect live new BT advertisements and observations between runs, not reprocessed data (row counts grew from 958→965 and 3306→3315, not doubled).
- Device MACs all resolve: `aa:bb:cc:00:00:01→device-a`, `aa:bb:cc:00:00:07→device-b`, `aa:bb:cc:00:00:08→device-c`, `aa:bb:cc:00:00:09→device-c`, `aa:bb:cc:00:00:0a→device-d`. user MAC `aa:bb:cc:00:00:06` also tagged correctly with full feature row populated (sources=["wifi"], rssi -86..-26 avg -64.9, hours_active=56, total_observations=2630).
- Profiler perf: cold first-run 7m30s (full-history rebuild over 3306 MACs); subsequent runs 2m01s, matching pre-17a baseline of ~2 min. Watermark bounds subsequent ticks to only new activity.

### Not fixed
- **iPhones broadcast NULL SSID probes most of the time.** This is Apple's privacy design, not a Sentinel bug. `device_probe_history` will mostly capture older devices, IoT, and saved-network probes for hidden networks. Modern iPhones contribute via BLE names and manufacturer-data fingerprints, not probe history.
- **`_last_features_rebuild` rate-limit is a no-op for the current oneshot profiler.** Harmless but dead code until/unless 17b wires this to a long-lived caller.
- **No CLI, no view, no rendering.** Out of scope for 17a (deferred to 17b-e and later presentation work). Ad-hoc analysis queries the raw tables directly via `./sentinel.sh query`.

### Rollback
- `DROP TABLE IF EXISTS device_probe_history; DROP TABLE IF EXISTS device_ble_names; DROP TABLE IF EXISTS device_identity_features; DROP TABLE IF EXISTS identification_watermarks;`
- Remove the Stage 17a try/except block from `sentinel/profiler/engine.py: run_profiler`. The `sentinel/identification/` module can stay on disk harmlessly — it just won't be imported.
- No other tables touched. No existing functionality regressed.

## Stage 17a-prep — Enable rtl_433 `-M level` for RSSI Reporting (2026-05-11)

### Context
- Stage 17a will build `device_identity_features` rolling up rssi_min/max/avg across modalities (WiFi, BT, SDR). Currently every `sdr_tpms` row has NULL rssi because rtl_433 was spawned without `-M level`. Without this fix, the upcoming feature view would silently drop all SDR contributions to the RSSI aggregates.
- The fix is a one-line argv change; the ingest path already extracts `rssi` from rtl_433's per-event JSON via `_try_float(raw.get("rssi"))` in `_route_event`.

### Added
- `sentinel/capture/sdr_433.py: Sdr433CaptureD._capture` — added `"-M", "level"` to the rtl_433 subprocess argv. rtl_433 now emits `rssi`, `snr`, and `noise` fields in its JSON per decoded transmission.

### Decisions
- **AGC retained, no `-g` override.** Explicit gain tuning is a separate future project; for now AGC handles dynamic-range adequately and `-M level` is the minimum change to unblock Stage 17a.
- **No protocol filters (`-R`).** Broad decoding is intentional — `sdr_ism` is the catch-all and lossy filtering at the rtl_433 layer would hide future-useful traffic.
- **No schema changes.** The `sdr_tpms.rssi` column has always existed; the bug was that nothing was filling it. No migration, no rollback risk beyond reverting the one-line edit.

### Testable
- `python -c 'import sentinel.capture.sdr_433'` succeeds on the Pi venv.
- After restart, `ps aux | grep rtl_433` shows: `rtl_433 -F json -M level -f 315M`.
- `journalctl -u sentinel-sdr-433 --since '30 seconds ago'` shows one `Spawned rtl_433` line and zero `rc=` exits.
- Once a TPMS transmission lands post-deploy, the row's rssi column will be non-NULL. Observational — depends on a car driving by. Latest pre-deploy row (2026-05-11 22:26, Ford-CarRemote) still has NULL rssi as expected baseline.

### Not fixed
- Backfill of historical NULL rssi rows. Not possible — the data was never captured at the radio level.
- Per-capture LNA effectiveness measurement is now possible but no consumer exists yet (Stage 17a will be the first).

# Sentinel Changelog

## Stage 16b — Rate-Aware BLE Watchdog + Preventive bluetoothd Restart Timer (2026-05-06)

### Context
- bluez/bluez#904 documents that under heavy BLE device density (>20–30 devices in radio range), bluetoothd enters degraded states where `hciconfig` reset, rfkill cycle, and even full reboot do NOT recover. Only `systemctl restart bluetooth` reliably recovers the stack.
- bluez/bluez#1500 confirms Pi 5 specifically suffers from BLE discovery degradation in long-running scans. The Pi 5 BCM4345C0 internal radio does not support LE extended scanning, limiting userspace mitigation options.
- Stage 16's watchdog detects only full silence; in practice the failure mode is partial deafness — events keep flowing but at a small fraction of baseline rate. Stage 16's CRITICAL message also pointed to `rfkill block bluetooth` recovery, which we empirically confirmed does not work on this platform.

### Added
- `sentinel/capture/bluetooth.py: BleScanner._watchdog_loop` rewritten — now detects two failure modes:
  - **SILENT** (preserved from Stage 16): no events for ≥90s.
  - **DEGRADED** (new): rolling event-rate falls below 30% of learned baseline for ≥3 consecutive 1-minute buckets. Baseline is the median of the last 5 minutes' bucket counts; only fires once a baseline >5 events/min has been learned (so naturally quiet environments don't trip it).
- `BleScanner._rate_buckets` — `collections.deque(maxlen=5)` of `(start_ts, count)` per-minute buckets. Auto-evicts old buckets. Cleared after a recovery restart.
- `_detection_callback` heartbeat now does both `_last_event_ts` update and rate-bucket increment in one block.
- CRITICAL escalation message corrected: now points to `sudo systemctl restart bluetooth && sudo systemctl restart sentinel-bt` (per bluez#904) instead of the rfkill cycle (which doesn't work on Pi 5). Also references `sentinel-bt-recovery.timer` so the operator knows preventive recovery is already running.
- `/etc/systemd/system/sentinel-bt-recovery.service` — root-level oneshot. Runs `systemctl restart bluetooth.service` → 5s sleep → `systemctl restart sentinel-bt.service`. Crude but matches the documented-working recovery path from bluez#904.
- `/etc/systemd/system/sentinel-bt-recovery.timer` — fires the recovery service every 4 hours (`OnBootSec=4h`, `OnUnitActiveSec=4h`, `Persistent=true`).

### Decisions
- **Heartbeat-in-callback, not in queue:** the watchdog clock and rate buckets update inside `_detection_callback`, so they reflect actual BleakScanner activity. A queue-side measurement would conflate scanner health with downstream backpressure.
- **Median baseline, not mean:** one anomalous quiet minute (e.g., a brief radio interferer) shouldn't drag the baseline down enough to mask a subsequent degradation event.
- **5 events/min floor:** if the median baseline is ≤5/min, the environment is naturally quiet (no nearby BLE devices) and a "30% of baseline" threshold becomes degenerate. Skip the degraded check entirely in that regime.
- **One-shot recovery in-process; preventive restart out-of-process:** the watchdog's in-process `BleakScanner.stop() + start()` is for transient stuck states. Per bluez#904, the deeper bluetoothd-stuck state requires a full bluez restart, which can't be done from inside a Python process bound to the broken bluez session. That's why the system-level recovery timer exists.
- **4-hour preventive cadence:** chosen to keep capture downtime minimal (each recovery cycle is ~5–10s of bluetooth gap) while staying ahead of the typical degradation horizon. Adjust by editing `OnUnitActiveSec=` in the timer.
- **No `Requires=` in the timer's `[Unit]` section.** A `Requires=sentinel-bt-recovery.service` directive there creates a hard activation dependency: when the timer is started (boot, `enable --now`, manual restart), systemd pulls the service in immediately and runs it — not just when the schedule fires. We discovered this during deployment when `enable --now sentinel-bt-recovery.timer` triggered an immediate bluetooth restart cycle. The `Unit=sentinel-bt-recovery.service` directive in `[Timer]` is what actually binds periodic firing to the service; `Requires=` is redundant for that and harmful for activation behavior. The timer file has an inline comment to keep this from being added back.

### Testable
- `python -c 'import sentinel.capture.bluetooth'` succeeds.
- After daemon start, journal shows `BLE watchdog started (silent=90s, degraded=<30% of baseline x 3 min)`.
- Healthy operation: 5,309 BT advertisements observed in the minute after restart, zero watchdog warnings.
- `systemctl list-timers` shows `sentinel-bt-recovery.timer` with next trigger ~boot+4h (`Wed 2026-05-06 23:20:58 UTC`), `Active: active (waiting)`.
- Recovery service end-to-end path validated in production (incidentally — see Decisions note on `Requires=`): `bluetooth.service` restart → 5s sleep → `sentinel-bt.service` restart → watchdog re-arms → capture rate returns to baseline within ~5s.
- After removing `Requires=`, `systemctl restart sentinel-bt-recovery.timer` is silent: no recovery service fire, no daemon restart, next trigger unchanged.

### Not fixed
- The underlying Pi 5 + bluez 5.85 BLE degradation. Upstream bug. A USB BT adapter (per bluez#1500) would sidestep entirely but is out of scope.
- Degraded-rate detection is heuristic and only activates after 3 minutes of baseline learning. Cold-start (first ~3 min of capture) is silent-failure-only.

## Stage 16 — BLE Scanner Watchdog (2026-05-06)

### Added
- `sentinel/capture/bluetooth.py: BleScanner._watchdog_loop()` — async background task that monitors BLE event arrival and recovers from silent-failure mode (BleakScanner reports running, bluez reports `Discovering: yes`, but no callbacks ever fire).
- `BleScanner._last_event_ts` — heartbeat updated at the top of `_detection_callback`'s try block (before any processing), so even malformed advertisements count as proof the scanner is alive.
- `BleScanner._watchdog_task` / `_watchdog_running` — task lifecycle state. Watchdog launched at the end of `start()` and cancelled+awaited at the top of `stop()` to avoid a race where the watchdog tries to restart the scanner mid-shutdown.
- `import time` added (used for `time.monotonic()` heartbeat).

### Decisions
- **Thresholds:** 30s check interval, 90s idle threshold, 30s post-restart grace. Conservative to avoid false-positive restarts during legitimate quiet periods (BLE advertisements are bursty; sub-30s gaps are normal).
- **One-shot recovery:** the watchdog attempts a single `stop()` + 2s pause + new `BleakScanner` + `start()` cycle. If the post-restart grace also elapses without events, it logs `CRITICAL` with explicit operator instructions (`rfkill block bluetooth`/`unblock`/restart) and *stops trying* — the failure mode at that point is OS-level (bluez or kernel) and hammering it would only make things worse.
- **Heartbeat in callback, not queue:** the watchdog clock is updated when `_detection_callback` fires, not when an event reaches the queue. This catches the actual silent-failure mode (callback never fires) rather than secondary symptoms (queue full, ingest down).
- **No SIGHUP-tunable thresholds:** these are constants in the watchdog method. If 90s turns out to be too tight or too loose in field operation, change the constants and restart sentinel-bt.

### Testable
- `python -c 'import sentinel.capture.bluetooth'` succeeds.
- After daemon start, journal shows `BLE scanner started on hci0` and `BLE watchdog started (90s idle threshold)` within the same second.
- During healthy operation (5,244 BT advertisements over 2 min observed live), watchdog produces zero warning/critical lines.
- Silent-failure recovery path (idle >90s → warn + restart; idle still >120s → critical + back off) can only be verified against a real bluez stuck-state event; structure logged is what the operator should grep for.

## Stage 15 — Identity-Aware Ingest Tagging (2026-05-06)

### Added
- `sentinel/identity/loader.py` — new module. `load_identity_map()` walks the YAML dossier directory and returns a flat `identifier -> identity_id` dict. Permissive: malformed/unrecognized YAMLs log and skip, never raise. Supports three YAML shapes: top-level `devices[].macs.{wifi,bt}[].mac` (user.yaml), `device.mac`/`device.macs` (unknowns), and `device.{sensor_ids,device_id,station_id}` (SDR sources). `lookup_identity()` does case-insensitive lookup.
- `sentinel/identity/__init__.py` — package marker.
- `identity_id TEXT` column on 8 tables: `observations`, `wifi_frames`, `probe_requests`, `bt_advertisements`, `devices`, `sdr_tpms`, `sdr_weather`, `sdr_ism`. Both in `schema.sql` (fresh installs) and in `_apply_additive_migrations` (idempotent ALTER TABLE for upgraded DBs). 8 supporting indexes (only in migration, since `executescript` runs before migrations and an index on a not-yet-added column would fail on upgraded DBs — same pattern as Stage 14a/14d).
- `IngestDaemon._reload_identities()` — loads/reloads the identity map from `<db_dir>/identities/`. Called once at startup, again on SIGHUP. Reload failures keep the existing map intact.
- `IngestDaemon._on_sighup()` — combined SIGHUP handler that calls both `reload_thresholds()` (preserves existing detection-threshold hot-reload) and `_reload_identities()`. Wired via `loop.add_signal_handler(SIGHUP, ...)` since asyncio's signal handler replaces the synchronous one set by `install_sighup_handler()`.
- Identity tagging in `enrich_event()` for the mac-keyed path, plus an inline tag step in `_handle_event()` for `NON_MAC_SOURCES` (sdr_433) which bypasses `enrich_event` entirely. This ensures `sensor_id` / `station_id` / `device_id` matches actually populate `identity_id` on TPMS/weather/ISM rows.
- `identity_id` column added to all 8 INSERT statements in `EventBatcher.flush()` and to all corresponding buffer tuples (positional). Device upsert dict gets an `identity_id` field; conflict update uses `COALESCE(excluded.identity_id, devices.identity_id)` so a NULL in a later batch never wipes an existing tag.

### Decisions
- Indexes on the new column live in `schema.py` migrations, not `schema.sql`, following the Stage 14a/14d precedent. `CREATE INDEX IF NOT EXISTS` in `schema.sql` would crash on upgraded DBs because `executescript` runs before `_apply_additive_migrations`.
- NULL is the explicit "unknown" state — no breaking changes, all existing rows backfill as NULL.
- Identity tagging happens at ingest time (write-time), not at query time. Trades a tiny enrichment cost for trivial `WHERE identity_id = …` queries forever after.
- Last-loaded YAML wins on identifier collision. Loader walks `sorted(rglob("*.yaml"))`, so alphabetical order is deterministic but not necessarily what the dossier author intends — collisions are logged at WARN. (See note below.)
- SIGHUP reloads identities AND thresholds in the same handler. Avoids surprising users who already use SIGHUP for threshold hot-reload.

### Testable
- `python -c 'import sentinel.ingest.daemon; import sentinel.identity.loader'` succeeds.
- `python -m sentinel.db.schema` prints `Schema applied` + `All 17 tables verified.` on an existing DB.
- `pragma_table_info` confirms `identity_id` on all 8 target tables; `sqlite_master` confirms 8 `idx_*_identity` indexes.
- After restart with the live dossier (37 identifiers across 6 YAMLs), live observations within 2 min stamp `user` (49), `askey-1-aaaaaa` (14), `askey-2-bbbbbb` (10), and NULL (93,471 unknowns). `devices` table picks up `amazon-1-cccccc` immediately. `wifi_frames` shows tagging across all 4 known identities. BT-only identities (Tile, earbuds) populate as those devices advertise.
- `kill -HUP $(systemctl --user show sentinel-ingest -p MainPID --value)` produces a single `Reloaded 37 identifiers from identities dir` log line — works without daemon restart.

### Known data issue (operator action)
- 4 askey MACs (`aa:bb:cc:00:00:{0b,0c}` and `aa:bb:cc:00:00:{0d,0e}`) appear in BOTH `user.yaml` (under `household_context`, presumably) AND in the `askey-{1,2}` unknown YAMLs. Loader currently resolves to `askey-*` (last-loaded by alphabetical sort), and logs a `WARN: Identifier ... already maps to user, overwriting with askey-...` for each on every reload. Decide which YAML should own these and remove from the other to silence the warnings.

## Stage 14e + 14f — Service-UUID and Cross-Modality Copresence Clustering (2026-05-06)

### Added
- `sentinel/profiler/engine.py: compute_service_uuid_clusters()` / `_persist_service_uuid_clusters()` — Stage 14e. Clusters BT MACs by shared service UUID set within a 24-hour temporal window. Catches Tile (`0xfeed`), Apple Continuity, Google Fast Pair (`0xfef3`), Eddystone (`0xfeaa`), etc. Reuses `probe_clusters` table with `evidence_type='service_uuid'`.
- `sentinel/profiler/engine.py: compute_copresence_clusters()` / `_persist_copresence_clusters()` — Stage 14f. First cross-modality cluster type. Reads `device_profiles.companion_macs` and forms an undirected graph from bidirectional companion pairs (A→B AND B→A). Connected components become clusters. WiFi MAC + BT MAC can co-cluster — foundation for automatic identity discovery. `evidence_type='copresence'`.
- `import hashlib` for stable cluster_id derivation in both new paths.
- Two new evidence-type constants: `_EVIDENCE_SERVICE_UUID`, `_EVIDENCE_COPRESENCE`.
- Four new keys in `run_profiler` stats: `service_uuid_clusters_found`, `service_uuid_cluster_members`, `copresence_clusters_found`, `copresence_cluster_members`.

### Decisions
- Zero schema changes. Both stages reuse the existing `probe_clusters` / `probe_cluster_members` tables, distinguished only by `evidence_type`. Same wipe-and-rebuild atomic-transaction pattern as `_persist_ie_clusters` / `_persist_ble_clusters`.
- `ssid_set` is repurposed as a generic "defining set" column: normalized UUID JSON for service_uuid clusters, sorted member-MAC JSON for copresence clusters. Honest given the schema constraint (NOT NULL) and the column's role as cluster identity.
- Copresence requires `total_observations >= 50` to avoid spurious clusters from sparse profiles.
- Copresence edges require *bidirectional* companionship — A in B's list AND B in A's list — to filter out asymmetric proximity (e.g., a phone near a fixed AP).
- `devices.probe_cluster_id` is deliberately NOT touched by either path. Stage 14c will own multi-evidence fusion.

### Testable
- `python -c 'import sentinel.profiler.engine'` succeeds.
- Manual profiler run produces a single log line covering all 5 cluster types: ssid, ie, ble, service_uuid, copresence.
- `SELECT evidence_type, COUNT(*) FROM probe_clusters GROUP BY evidence_type` shows rows for `service_uuid` and `copresence` once data is present (verified live: 3 service_uuid clusters / 50 members, 3 copresence clusters / 104 members on the Pi DB).

## Stage 12 — Hardening and Polish (2026-04-19)

### Added
- Project documentation — quick reference, architecture, key files, development workflow, systemd units, DB schema, config, and code conventions.
- `sentinel/config.py: validate_config()` — startup validation for all config knobs: detection thresholds positive, Jaccard in (0,1], batch params positive, channel lists non-empty, dwell time >= 50ms, valid log level. Returns list of error strings.
- Ingest and detector daemons now validate config on startup and exit with clear errors if invalid.

### Fixed
- `sentinel/detector/engine.py` — moved `import time` to module level (was inside loop body).
- `sentinel/ingest/daemon.py` — added missing `import sys` for config validation exit.

### Testable
- `validate_config()` catches bad values (negative thresholds, out-of-range Jaccard, negative batch interval)
- All previous smoke tests pass after changes

## Stage 11 — systemd Units, Entrypoint, Installer (2026-04-19)

### Added
- `systemd/sentinel-wifi.service` — system unit for WiFi capture (root, raw sockets)
- `systemd/sentinel-bt.service` — system unit for Bluetooth capture (root, D-Bus system bus)
- `systemd/sentinel-ingest.service` — user unit for ingestion daemon
- `systemd/sentinel-detector.service` — user unit for detection daemon (After=ingest)
- `systemd/sentinel-profiler.service` — user oneshot for profiler
- `systemd/sentinel-profiler.timer` — user timer firing every 15 minutes
- `sentinel.sh` — single entrypoint: start/stop/restart/status/logs/selftest, falls through to CLI for other commands
- `install.sh` — full installer: apt deps, Python venv, pip install, data dirs, OUI download, schema apply, systemd unit install, lingering enable

### Testable
- `bash install.sh` on Pi 5 with Kali — creates venv, installs deps, downloads OUI, applies schema, installs all systemd units
- `./sentinel.sh start` — starts all daemons (system + user)
- `./sentinel.sh status` — shows systemd unit state + sentinel CLI status
- `./sentinel.sh selftest` — verifies full stack

### Decisions
- Capture daemons (wifi, bt) are system-level units under `/etc/systemd/system/` — they need root for raw sockets and D-Bus system bus access. Security hardened with ProtectSystem=strict, PrivateTmp, ReadWritePaths limited to data dir.
- Ingest, detector, profiler are user-level units under `~/.config/systemd/user/` — no elevated privileges needed.
- Profiler uses a systemd timer (OnUnitActiveSec=15min) rather than a long-running daemon with internal scheduling — cleaner resource usage, auto-restart on failure.
- `loginctl enable-linger` ensures user units survive SSH logout.
- OUI file re-downloaded if >30 days old, otherwise kept.

## Stage 10 — CLI and Query Layer (2026-04-19)

### Added
- `sentinel/query/api.py` — structured query API (library, no formatting):
  - `get_status()` — system summary (counts, last hour activity, schema version)
  - `list_devices()` — filtered device list (--since, --seen-in, --vendor, --new-since, --type)
  - `get_device()` — full device detail with profile, probe cluster, recent observations/alerts
  - `list_alerts()` — filtered alerts (--severity, --type, --since, --mac, --unacked)
  - `get_new_alerts()` — incremental poll by ID (for watch/live tail)
  - `execute_readonly_query()` — arbitrary SQL with auto-LIMIT
  - `export_table()` — table dump with validation against known table names
- `sentinel/cli/main.py` — click-based CLI (thin formatter on query API):
  - `sentinel status` — system overview
  - `sentinel devices` — table of devices with filters and `--json`
  - `sentinel device <mac>` — detailed view with profile histogram, probe cluster, alerts
  - `sentinel alerts` — alert table with filters
  - `sentinel watch` — live tail polling for new alerts
  - `sentinel query "<sql>"` — read-only SQL passthrough
  - `sentinel export <table>` — CSV or JSON export
  - `sentinel start/stop/restart` — wraps systemctl for system + user units
  - `sentinel selftest` — verifies DB, schema, config, interfaces, systemd units

### Testable
- Query API: 9 tests (status, devices with 4 filter combos, device detail with profile, alerts with severity filter, new_alerts incremental, raw query, export with validation)
- CLI: All subcommands verified against seeded DB — status, devices, device detail with histogram, alerts, query, export JSON, selftest

### Decisions
- Query layer returns plain dicts/lists — CLI is just formatting. Future web dashboard adds a different formatter, no logic changes needed.
- `query` command opens DB in read-only mode (enforced by `get_readonly_connection`). Auto-appends LIMIT if not present.
- `export` validates table name against whitelist — no SQL injection via table parameter.
- `selftest` reports interface/systemd failures as FAIL but doesn't exit early — shows full picture.

## Stage 9 — Detection Daemon (2026-04-19)

### Added
- `sentinel/detector/engine.py` — live detection daemon with all 7 anomaly types:
  - **new_device:** Unseen MAC (info during learning, low after)
  - **temporal:** Device at hour with zero historical observations, or z-score 3+ below active-hour mean
  - **location:** RSSI stronger than p95 + 2*stddev (device unusually close)
  - **behavioral:** Probing for SSIDs not in historical set, or probe rate 3x+ historical mean
  - **absence:** Device with 95%+ presence absent for 4+ hours (periodic sweep, not per-event)
  - **correlation:** Device appears without any of its usual companion devices
  - **probe_set_cluster:** Randomized MAC in known probe cluster, medium if at anomalous time
- **Learning mode:** Checks `installed_at` in sentinel_meta. During first 7 days, only new_device alerts fire (at info level). All other detectors suppressed.
- **DetectionDaemon:** Polls observations table for new rows, scores each against all detectors, writes alerts. Absence check runs every 5 minutes as a sweep.

### Testable
- 10 tests pass with seeded profiles: learning mode, new_device (known/unknown/learning), temporal (zero-hour/normal), location (strong/normal RSSI), behavioral (new SSID), absence (high-presence device gone), correlation (missing companion), probe_set_cluster, score_observation integration, learning suppression.

### Decisions
- Temporal detection uses two-tier approach: zero-count hours are always anomalous (if device has 3+ active hours), non-zero hours use z-score against active-hour distribution. This handles the common case where a device has a uniform schedule (all active hours equal count, stddev=0).
- Absence detection is a periodic sweep (every 5 min), not per-event — scanning for "missing" devices doesn't make sense per-observation.
- All alerts written to DB immediately via DatabaseWriter. Summary logged at INFO level.

## Stage 7 — Ingestion Daemon (2026-04-19)

### Added
- `sentinel/ingest/daemon.py` — full ingestion daemon:
  - **IngestDaemon:** Runs BusServer, receives events from capture daemons, processes through enrich -> dedup -> batch -> write pipeline. Clean SIGTERM shutdown with final flush.
  - **enrich_event():** OUI vendor lookup, GPS static fallback location, device type inference (wifi/ble/bt_classic), AP detection (beacons/probe-responses where src_mac == bssid).
  - **Deduplicator:** Time-windowed (mac, channel) dedup with periodic stale entry cleanup.
  - **EventBatcher:** Accumulates events, flushes on interval or count threshold. Writes to observations, wifi_frames, probe_requests (with IE BLOB), bt_advertisements, and devices (upsert with ON CONFLICT).

### Fixed
- `sentinel/db/writer.py` — removed debug print statement from error handler.
- `sentinel/ingest/daemon.py` — critical fix: `execute_many` was passed list references that were cleared before the writer thread processed them. Now passes `list()` copies.

### Testable
- Deduplicator: 8 assertions (duplicate detection, channel isolation, window expiry)
- Enrichment: WiFi/BT device type, AP detection, static GPS fallback
- EventBatcher: 8 event types -> 5 tables with full field verification (IE bytes, service UUIDs, AP flag, device types)
- E2E: synthetic generator -> bus -> ingest -> DB (58 events sent, 19 survived dedup, all tables populated)

### Decisions
- Event batches use list copies (`list(self._observations)`) not references when enqueueing to the writer, preventing a race between `clear()` and the writer thread consuming the queue.
- Device upsert uses `ON CONFLICT DO UPDATE` with `MAX(last_seen)`, `COALESCE(vendor)`, `MAX(is_ap)` — preserves earliest first_seen, latest data wins for mutable fields, AP flag is sticky (once detected as AP, always AP).

## Stage 8 — Profiler Engine (2026-04-19)

### Added
- `sentinel/profiler/engine.py` — full profiler engine:
  - **Per-device profiles:** time-of-day histogram (24 hourly bins), RSSI stats (mean/stddev/p95), channel set, probe SSID set, probe rate (probes/hour), companion devices (co-present within configurable window, >10% co-occurrence), 30-day presence percentage.
  - **Probe-set clustering (Level B):** Groups locally-administered (randomized) MACs whose probe-request SSID sets have Jaccard similarity above threshold (default 0.6). Uses union-find for transitive clustering. Writes cluster_id back to devices table.
  - **Statistical helpers:** mean, stddev, percentile (linear interpolation), Jaccard similarity.
  - **run_profiler():** Full cycle entry point — profiles all devices, runs clustering, writes to device_profiles/probe_clusters/probe_cluster_members.
  - CLI: `python -m sentinel.profiler.engine --config config.yaml`

### Testable
- 5 tests pass with seeded DB: statistical helpers, full device profile (time histogram, RSSI, channels, SSIDs, companions, presence), sparse device skipped, probe-set clustering (correct grouping + exclusion), full run with DB write verification.

### Decisions
- Companion detection samples up to 50 co-present MACs per observation to bound query cost. Companions kept if seen in >10% of target device's observations (min 3 times).
- Clustering only operates on locally-administered MACs (bit 1 of first octet set) — globally-unique MACs don't need clustering.
- Probe-set clustering uses union-find for transitive merging: if A clusters with B and B clusters with C, all three land in one cluster.

## Stage 5+6 — Bluetooth Capture + SDR/GPS Stubs (2026-04-19)

### Added
- `sentinel/capture/bluetooth.py` — full Bluetooth capture daemon:
  - **BleScanner:** Continuous BLE advertisement scanning via bleak with detection_callback. Extracts: mac, local_name, rssi, tx_power, manufacturer_data (hex-encoded), service_uuids.
  - **ClassicBtScanner:** Classic BT inquiry via dbus-next talking to BlueZ D-Bus API. Runs periodic StartDiscovery/StopDiscovery cycles. Handles InterfacesAdded and PropertiesChanged signals for real-time device detection. Extracts: mac, name, rssi, device_class (CoD), UUIDs, manufacturer_data.
  - **BluetoothCaptureD:** Extends BaseCaptureD. Runs BLE + classic scanners in parallel, merging events into a shared asyncio queue. Gracefully degrades if one scanner fails (e.g. no D-Bus = BLE only).
  - D-Bus Variant unpacking, stale device filtering (no RSSI = skip), queue overflow protection.
- `sentinel/capture/sdr.py` — RTL-SDR stub. Checks config flag, logs "not implemented", exits cleanly.
- `sentinel/capture/gps.py` — GPS/LoRa stub. Same pattern.

### Testable
- 8 BT tests pass without hardware: timestamp format, manufacturer data formatting, BLE event structure (full + empty fields), D-Bus Variant unpacking, classic BT event structure, stale device filtering
- Full daemon requires Pi 5 + bluetoothd: `python -m sentinel.capture.bluetooth`
- Stubs import cleanly and exit gracefully when disabled in config

### Decisions
- BLE and classic BT share a single asyncio.Queue(maxsize=5000) — simpler than two queues, and the ingest layer doesn't need to distinguish the source queue.
- Classic BT uses dbus-next (not subprocess bluetoothctl) per approved design decision. Signal-based device detection means we capture devices as they appear, not just at inquiry end.
- Both scanners are optional: if BLE fails but classic works (or vice versa), the daemon continues. Only fails if both fail.

## Stage 4 — WiFi Capture Daemon (2026-04-19)

### Added
- `sentinel/capture/wifi.py` — full WiFi capture daemon:
  - **Packet parser:** Extracts all spec fields from Dot11/RadioTap (src_mac, dst_mac, bssid, ssid, channel, rssi, frame_type, frame_subtype, sequence_num). Raw IE bytes hex-encoded for probe requests (Level C prep).
  - **ChannelHopper:** Background thread cycling through configured 2.4 + 5 GHz channels via `iw`, with configurable dwell time.
  - **MonitorMode:** Enable/disable monitor mode on the Alfa (tries `iw` first, falls back to `airmon-ng`). Handles interface rename (wlan1 -> wlan1mon).
  - **WifiCaptureD:** Extends BaseCaptureD. Uses scapy AsyncSniffer in background thread, bridges to asyncio via queue (10k buffer, drops on overflow).
  - Handles: empty SSID (broadcast probes), missing RadioTap fields (channel hopper fallback), control frames with no source MAC (skipped), zero-MAC filtering.

### Testable
- 7 parser tests pass without hardware: probe requests, data frames, beacons, broadcast probes, control frame filtering, channel hopper fallback, freq-to-channel conversion
- Full daemon requires Pi + Alfa in monitor mode: `python -m sentinel.capture.wifi --config config.yaml`

### Decisions
- scapy AsyncSniffer runs in background thread; events bridged to async loop via asyncio.Queue(maxsize=10000). If queue backs up, packets are dropped rather than blocking scapy's sniff thread — this trades occasional drops under extreme load for guaranteed responsiveness.
- Zero MAC (00:00:00:00:00:00) filtered out — scapy fills this for control frames missing addr2.
- `airmon-ng check kill` called before enabling monitor mode to prevent NetworkManager/wpa_supplicant interference.

## Stage 3 + 3.5 — IPC Bus, OUI Lookup, Smoke-Test Harness (2026-04-19)

### Added
- `sentinel/ingest/bus.py` — async Unix domain socket server (BusServer) and client (BusClient) for newline-delimited JSON events. Fire-and-forget protocol, client retry logic, clean shutdown.
- `sentinel/capture/base.py` — abstract base class for capture daemons. Handles lifecycle: config loading, bus connection, SIGTERM/SIGINT, logging. Subclasses implement `_setup()`, `_capture()`, `_teardown()`.
- `sentinel/common/oui.py` — OUI vendor lookup from IEEE oui.txt, MAC randomization detection (`is_locally_administered`), lazy-loaded dict cache.
- `sentinel/capture/synthetic.py` — (Stage 3.5) synthetic event generator emitting realistic WiFi, BT, and probe-request events at configurable rates. Includes device pools, randomized MAC clusters for testing probe-set clustering, and anomalous (new device) injection. Runs standalone: `python -m sentinel.capture.synthetic --rate 10 --duration 60`

### Testable
- Bus round-trip: start BusServer, connect BusClient, send 10 JSON events, verify all received
- Synthetic generator: generates WiFi data frames, probe requests, BLE ads, anomalous new-device events
- OUI: `is_locally_administered()` correctly identifies randomized MACs
- Integration: synthetic events flow through bus server with correct source tagging

### Decisions
- IPC is async (asyncio) not threaded — capture daemons and ingest are all async, avoids thread/async bridge complexity
- Logging deferred to runtime (not module import) so synthetic module can be imported without creating /home/user dirs on dev machine
- Socket permissions set to 0o600 (owner only) for security

## Stage 1 — Project Skeleton + Config + Schema (2026-04-19)

### Added
- Python package structure: `sentinel/` with subpackages for db, capture, ingest, profiler, detector, query, cli, common
- `pyproject.toml` with dependencies: scapy, bleak, dbus-next, pyyaml, click
- `config.yaml` — full configuration with all knobs: WiFi, Bluetooth, SDR (deferred), GPS/LoRa (deferred), ingestion, profiler, detection thresholds, logging
- `schema.sql` — idempotent schema defining all 13 tables:
  - Core: devices, observations, sessions, sentinel_meta
  - WiFi: wifi_frames, probe_requests (with ie_bytes BLOB for Level C prep)
  - Bluetooth: bt_advertisements
  - Profiling: device_profiles, probe_clusters, probe_cluster_members
  - Alerts: alerts
  - Deferred: gps_fixes, sdr_observations
- `sentinel/config.py` — YAML config loader with dataclass models, path resolution, hot-reloadable detection thresholds via SIGHUP
- `sentinel/db/schema.py` — idempotent schema applier with verify command (`python -m sentinel.db.schema`)
- `sentinel/db/writer.py` — single-writer thread with queue for all DB writes, plus read-only connection helper
- `sentinel/common/logging.py` — per-daemon rotating log files + stderr output for journald

### Testable
- `python -c "from sentinel.config import load_config; cfg = load_config('config.yaml'); print(cfg.wifi.interface)"` — config loads
- `python -m sentinel.db.schema` — creates DB with all 13 tables (needs install_dir override for local dev)

### Decisions
- sequence_num captured on all wifi_frames (not just probe_requests) — prep for Level C MAC de-randomization
- ie_bytes BLOB column on probe_requests — stores raw IEs for future Level C clustering
- sentinel_meta table added for schema versioning and install tracking
- DB writer uses PRAGMA synchronous=NORMAL (not FULL) for write throughput — WAL mode makes this safe
