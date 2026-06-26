# Sentinel Known Gaps

Tracking features that were scoped/schemed but never implemented.
This is separate from the roadmap (planned future work) and CHANGELOG
(shipped work history).

## Sessions table (no writer)

**Status:** schema exists, zero rows ever, no code writes to it
**Schema location:** `schema.sql`
**Date identified:** 2026-05-12

Table is defined:
```sql
sessions (mac, start_time, end_time, source, avg_rssi, observation_count)
```

But nothing in `sentinel/` issues `INSERT INTO sessions`. This is dead
schema from an early design phase. Either:
- Implement the session-tracking logic (would belong in the ingest
  daemon or a profiler aggregator with gap-detection)
- Drop the table to reduce confusion

A real session writer would tie into `device_identity_features`
(Stage 17a) — sessions are essentially time-bounded presence intervals
that the rhythm/co-arrival work (Stages 17b/c) needs anyway.

## Stage 14c — multi-evidence fusion

**Status:** never implemented
**Date identified:** 2026-05-12

Stages 14a (SSID Jaccard), 14b (IE fingerprint), 14d (BLE companion),
14e (service UUID), 14f (copresence) each emit independent cluster
evidence. There is no fusion layer that combines them into a single
per-(mac_a, mac_b) confidence score.

Effect: `devices.probe_cluster_id` is only written by the SSID-Jaccard
path. The other four evidence streams produce clusters but no unified
inference. The 149-MAC mega-cluster issue we saw in neighbor
identification work is partly a symptom of this gap.

Stage 14c should produce one row per device-pair with a fused score
based on weighted evidence from all five clustering modalities.

## Stages 17b-e

**Status:** roadmap entries, not started
**Date identified:** roadmap creation, 2026-05-06

- 17b: co-arrival / co-departure detection
- 17c: daily rhythm fingerprinting  
- 17d: identity candidate engine
- 17e: manufacturer-data deep decode (Apple Continuity, etc.)

17b-d benefit from accumulated data and depend on Stage 14c being
shipped first (they need fused confidence as a primitive).

17e is independent and can ship anytime — it's pure decode against
existing `bt_advertisements.manufacturer_data` bytes.

## Dependency order for future work

Suggested order:
1. Stage 14c (fusion) → unblocks 17b-d
2. 17b (co-arrival) → consumes 14c
3. 17c (rhythm) → consumes 14c, needs 7+ days of accumulated data
4. 17d (candidates) → consumes 14c, 17b, 17c
5. 17e (mfr-data) → independent, can ship any time
6. Sessions writer → can ship any time, would feed 17b/c
