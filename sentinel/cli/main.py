"""Sentinel CLI — command-line interface for querying and managing Sentinel.

All data access goes through sentinel.query.api (structured data).
This module is just formatting and argument parsing.

Usage:
    sentinel status
    sentinel devices --seen-in 1 --vendor Apple
    sentinel device aa:bb:cc:dd:ee:ff
    sentinel alerts --severity medium --since 2026-04-19
    sentinel watch
    sentinel query "SELECT * FROM devices WHERE is_ap = 1"
    sentinel export observations --since 2026-04-19 --format csv
    sentinel start|stop|restart
    sentinel selftest
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import Any

import click

from sentinel.config import get_config, load_config
from sentinel.db.writer import get_readonly_connection
from sentinel.query import api


def _get_conn() -> Any:
    """Get a read-only DB connection using current config."""
    cfg = get_config()
    return get_readonly_connection(cfg.resolved_db_path)


def _format_table(columns: list[str], rows: list[tuple | dict], max_col_width: int = 40) -> str:
    """Format data as an aligned text table."""
    if not rows:
        return "(no results)"

    # Normalize rows to list of lists
    if rows and isinstance(rows[0], dict):
        data = [[str(r.get(c, ""))[:max_col_width] for c in columns] for r in rows]
    else:
        data = [[str(v)[:max_col_width] if v is not None else "" for v in r] for r in rows]

    # Compute column widths
    widths = [len(c) for c in columns]
    for row in data:
        for i, val in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(val))

    # Build output
    header = "  ".join(c.ljust(widths[i]) for i, c in enumerate(columns))
    separator = "  ".join("-" * w for w in widths)
    lines = [header, separator]
    for row in data:
        lines.append("  ".join(
            (row[i] if i < len(row) else "").ljust(widths[i])
            for i in range(len(columns))
        ))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------

@click.group()
@click.option("--config", "-c", default=None, help="Path to config.yaml")
def cli(config: str | None) -> None:
    """Sentinel — passive RF surveillance platform."""
    if config:
        load_config(config)
    else:
        # Try default locations
        for candidate in ["config.yaml", "/home/user/sentinel/config.yaml"]:
            if Path(candidate).exists():
                load_config(candidate)
                break


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@cli.command()
def status() -> None:
    """Show system status summary."""
    cfg = get_config()
    conn = _get_conn()
    try:
        s = api.get_status(conn)
        adsb = api.get_adsb_summary(conn, enabled=cfg.sdr.adsb_enabled)
    finally:
        conn.close()

    click.echo(f"Sentinel Status")
    click.echo(f"  DB:              {s['db_path']}")
    click.echo(f"  Schema:          v{s['schema_version']}")
    click.echo(f"  Installed:       {s['installed_at']}")
    click.echo(f"  Devices:         {s['device_count']}")
    click.echo(f"  Observations:    {s['observation_count']}")
    click.echo(f"  Profiles:        {s['profile_count']}")
    click.echo(f"  Clusters:        {s['cluster_count']}")
    click.echo(f"  Alerts:          {s['alert_count']} ({s['unacked_alerts']} unacknowledged)")
    click.echo(f"  Last hour:")
    click.echo(f"    Observations:  {s['last_hour']['observations']}")
    click.echo(f"    Active devices:{s['last_hour']['active_devices']}")
    click.echo(f"    Alerts:        {s['last_hour']['alerts']}")

    # SDR / ADS-B — Stage 18b. Three display modes:
    #   disabled               → single line
    #   enabled, no data yet   → single line
    #   enabled with data      → full block
    if not adsb["enabled"]:
        click.echo(f"  SDR / ADS-B:     disabled")
    elif not adsb["has_data"]:
        click.echo(f"  SDR / ADS-B:     enabled, no data yet")
    else:
        latest_ts = (adsb["latest_observation"] or "")[:19]
        latest_icao = adsb["latest_icao"] or ""
        click.echo(f"  SDR / ADS-B:")
        click.echo(f"    Aircraft (last hour):   {adsb['last_hour_aircraft']}")
        click.echo(f"    Messages (last hour):   {adsb['last_hour_messages']}")
        click.echo(f"    Latest aircraft:        {latest_ts} (icao {latest_icao})")


# ---------------------------------------------------------------------------
# devices
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--since", default=None, help="Last seen after (ISO timestamp)")
@click.option("--seen-in", type=float, default=None, help="Seen in last N hours")
@click.option("--vendor", default=None, help="Filter by vendor (substring)")
@click.option("--new-since", default=None, help="First seen after (ISO timestamp)")
@click.option("--type", "device_type", default=None, help="Device type (wifi, ble, bt_classic)")
@click.option("--limit", default=50, help="Max results")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def devices(since: str | None, seen_in: float | None, vendor: str | None,
            new_since: str | None, device_type: str | None, limit: int,
            as_json: bool) -> None:
    """List known devices."""
    conn = _get_conn()
    try:
        results = api.list_devices(conn, since=since, seen_in=seen_in, vendor=vendor,
                                    new_since=new_since, device_type=device_type, limit=limit)
    finally:
        conn.close()

    if as_json:
        click.echo(json.dumps(results, indent=2))
        return

    if not results:
        click.echo("No devices found.")
        return

    columns = ["mac", "vendor", "device_type", "is_ap", "last_seen", "obs_count"]
    rows = []
    for d in results:
        rows.append((
            d["mac"],
            (d.get("vendor") or "")[:25],
            d.get("device_type", ""),
            "AP" if d.get("is_ap") else "",
            (d.get("last_seen") or "")[:19],
            str(d.get("obs_count", 0)),
        ))
    click.echo(_format_table(columns, rows))
    click.echo(f"\n{len(results)} device(s)")


# ---------------------------------------------------------------------------
# device <mac>
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("mac")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def device(mac: str, as_json: bool) -> None:
    """Show detailed info for a single device."""
    conn = _get_conn()
    try:
        result = api.get_device(conn, mac.lower())
    finally:
        conn.close()

    if result is None:
        click.echo(f"Device {mac} not found.")
        sys.exit(1)

    if as_json:
        click.echo(json.dumps(result, indent=2, default=str))
        return

    click.echo(f"Device: {result['mac']}")
    click.echo(f"  Vendor:       {result.get('vendor') or 'unknown'}")
    click.echo(f"  Name:         {result.get('device_name') or 'none'}")
    click.echo(f"  Type:         {result.get('device_type', 'unknown')}")
    click.echo(f"  AP:           {'yes' if result.get('is_ap') else 'no'}")
    click.echo(f"  First seen:   {result.get('first_seen', '')[:19]}")
    click.echo(f"  Last seen:    {result.get('last_seen', '')[:19]}")
    click.echo(f"  Observations: {result.get('observation_count', 0)}")

    if result.get("profile"):
        p = result["profile"]
        click.echo(f"\n  Profile (updated {p.get('updated_at', '')[:19]}):")
        click.echo(f"    RSSI:         mean={p.get('rssi_mean')}, stddev={p.get('rssi_stddev')}, p95={p.get('rssi_p95')}")
        click.echo(f"    Channels:     {p.get('channel_set', [])}")
        click.echo(f"    Probe SSIDs:  {p.get('probe_ssid_set', [])}")
        click.echo(f"    Probe rate:   {p.get('probe_rate_mean', 0):.2f}/hr")
        click.echo(f"    Companions:   {p.get('companion_macs', [])}")
        click.echo(f"    Presence 30d: {p.get('presence_pct_30d', 0):.1f}%")

        # Time histogram as a simple bar chart
        hist = p.get("time_histogram", [])
        if hist and max(hist) > 0:
            click.echo(f"    Time histogram:")
            scale = 40.0 / max(hist)
            for h in range(24):
                bar = "#" * int(hist[h] * scale)
                click.echo(f"      {h:02d}:00 {bar}")

    if result.get("probe_cluster"):
        pc = result["probe_cluster"]
        click.echo(f"\n  Probe Cluster: {pc['cluster_id'][:16]}...")
        click.echo(f"    Jaccard:    {pc.get('jaccard_score', 0):.4f}")
        click.echo(f"    SSIDs:      {pc.get('ssid_set', [])}")
        click.echo(f"    Members:    {pc.get('device_count', 0)}")

    if result.get("recent_alerts"):
        click.echo(f"\n  Recent Alerts:")
        for a in result["recent_alerts"][:5]:
            click.echo(f"    [{a['severity'].upper():6s}] {a['alert_type']:20s} {a['summary']}")


# ---------------------------------------------------------------------------
# alerts
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--severity", default=None, help="Filter by severity (info/low/medium/high)")
@click.option("--type", "alert_type", default=None, help="Filter by alert type")
@click.option("--since", default=None, help="Alerts after (ISO timestamp)")
@click.option("--mac", default=None, help="Filter by MAC address")
@click.option("--unacked", is_flag=True, help="Only unacknowledged alerts")
@click.option("--limit", default=50, help="Max results")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def alerts(severity: str | None, alert_type: str | None, since: str | None,
           mac: str | None, unacked: bool, limit: int, as_json: bool) -> None:
    """List alerts."""
    conn = _get_conn()
    try:
        results = api.list_alerts(conn, severity=severity, alert_type=alert_type,
                                   since=since, mac=mac, unacked_only=unacked, limit=limit)
    finally:
        conn.close()

    if as_json:
        click.echo(json.dumps(results, indent=2))
        return

    if not results:
        click.echo("No alerts found.")
        return

    columns = ["id", "timestamp", "severity", "alert_type", "mac", "summary"]
    rows = []
    for a in results:
        rows.append((
            str(a["id"]),
            (a.get("timestamp") or "")[:19],
            a.get("severity", ""),
            a.get("alert_type", ""),
            a.get("mac") or "",
            (a.get("summary") or "")[:60],
        ))
    click.echo(_format_table(columns, rows))
    click.echo(f"\n{len(results)} alert(s)")


# ---------------------------------------------------------------------------
# watch (live tail)
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--interval", default=1.0, help="Poll interval in seconds")
def watch(interval: float) -> None:
    """Live tail of alerts (Ctrl+C to stop)."""
    click.echo("Watching for new alerts (Ctrl+C to stop)...")
    last_id = 0

    # Get current max ID to start from
    conn = _get_conn()
    try:
        row = conn.execute("SELECT MAX(id) FROM alerts").fetchone()
        if row and row[0]:
            last_id = row[0]
    finally:
        conn.close()

    try:
        while True:
            conn = _get_conn()
            try:
                new_alerts = api.get_new_alerts(conn, after_id=last_id)
            finally:
                conn.close()

            for a in new_alerts:
                ts = (a.get("timestamp") or "")[:19]
                sev = a.get("severity", "?").upper()
                atype = a.get("alert_type", "?")
                mac = a.get("mac") or "?"
                summary = a.get("summary", "")
                click.echo(f"[{ts}] [{sev:6s}] {atype:20s} {mac}  {summary}")
                last_id = max(last_id, a.get("id", 0))

            time.sleep(interval)
    except KeyboardInterrupt:
        click.echo("\nStopped.")


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------

@cli.command("query")
@click.argument("sql")
@click.option("--limit", default=100, help="Max results")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def query_cmd(sql: str, limit: int, as_json: bool) -> None:
    """Execute a read-only SQL query."""
    conn = _get_conn()
    try:
        columns, rows = api.execute_readonly_query(conn, sql, limit=limit)
    except Exception as e:
        click.echo(f"Query error: {e}", err=True)
        sys.exit(1)
    finally:
        conn.close()

    if as_json:
        result = [dict(zip(columns, r)) for r in rows]
        click.echo(json.dumps(result, indent=2, default=str))
        return

    click.echo(_format_table(columns, rows))
    click.echo(f"\n{len(rows)} row(s)")


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("table")
@click.option("--since", default=None, help="Export rows after (ISO timestamp)")
@click.option("--limit", default=10000, help="Max rows")
@click.option("--format", "fmt", type=click.Choice(["csv", "json"]), default="csv", help="Output format")
@click.option("--output", "-o", default=None, help="Output file (default: stdout)")
def export(table: str, since: str | None, limit: int, fmt: str, output: str | None) -> None:
    """Export table data as CSV or JSON."""
    conn = _get_conn()
    try:
        columns, rows = api.export_table(conn, table, since=since, limit=limit)
    except ValueError as e:
        click.echo(str(e), err=True)
        sys.exit(1)
    finally:
        conn.close()

    if not rows:
        click.echo(f"No data in {table}.")
        return

    if fmt == "json":
        text = json.dumps(rows, indent=2, default=str)
    else:
        buf = StringIO()
        writer = csv.DictWriter(buf, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
        text = buf.getvalue()

    if output:
        Path(output).write_text(text)
        click.echo(f"Exported {len(rows)} rows to {output}")
    else:
        click.echo(text)


# ---------------------------------------------------------------------------
# start / stop / restart
# ---------------------------------------------------------------------------

_SYSTEM_UNITS = ["sentinel-wifi", "sentinel-bt"]
_USER_UNITS = ["sentinel-ingest", "sentinel-detector", "sentinel-sdr-adsb"]
_USER_TIMERS = ["sentinel-profiler.timer"]


def _systemctl(args: list[str], user: bool = False) -> subprocess.CompletedProcess[str]:
    cmd = ["systemctl"]
    if user:
        cmd.append("--user")
    cmd.extend(args)
    return subprocess.run(cmd, capture_output=True, text=True)


@cli.command()
def start() -> None:
    """Start all Sentinel daemons."""
    click.echo("Starting Sentinel...")
    for unit in _SYSTEM_UNITS:
        r = _systemctl(["start", unit])
        status = "ok" if r.returncode == 0 else f"FAILED: {r.stderr.strip()}"
        click.echo(f"  {unit}: {status}")
    for unit in _USER_UNITS + _USER_TIMERS:
        r = _systemctl(["start", unit], user=True)
        status = "ok" if r.returncode == 0 else f"FAILED: {r.stderr.strip()}"
        click.echo(f"  {unit}: {status}")


@cli.command()
def stop() -> None:
    """Stop all Sentinel daemons."""
    click.echo("Stopping Sentinel...")
    for unit in _USER_TIMERS + _USER_UNITS:
        r = _systemctl(["stop", unit], user=True)
        status = "ok" if r.returncode == 0 else f"FAILED: {r.stderr.strip()}"
        click.echo(f"  {unit}: {status}")
    for unit in _SYSTEM_UNITS:
        r = _systemctl(["stop", unit])
        status = "ok" if r.returncode == 0 else f"FAILED: {r.stderr.strip()}"
        click.echo(f"  {unit}: {status}")


@cli.command()
def restart() -> None:
    """Restart all Sentinel daemons."""
    click.echo("Restarting Sentinel...")
    for unit in _SYSTEM_UNITS:
        r = _systemctl(["restart", unit])
        status = "ok" if r.returncode == 0 else f"FAILED: {r.stderr.strip()}"
        click.echo(f"  {unit}: {status}")
    for unit in _USER_UNITS + _USER_TIMERS:
        r = _systemctl(["restart", unit], user=True)
        status = "ok" if r.returncode == 0 else f"FAILED: {r.stderr.strip()}"
        click.echo(f"  {unit}: {status}")


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------

@cli.command()
def selftest() -> None:
    """Verify system health: DB, systemd units, interfaces."""
    ok_count = 0
    fail_count = 0

    def check(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok_count, fail_count
        if passed:
            ok_count += 1
            click.echo(f"  [OK]   {name}" + (f" ({detail})" if detail else ""))
        else:
            fail_count += 1
            click.echo(f"  [FAIL] {name}" + (f" ({detail})" if detail else ""))

    click.echo("Sentinel Self-Test")
    cfg = get_config()

    # DB reachable
    try:
        conn = _get_conn()
        conn.execute("SELECT 1")
        check("Database reachable", True, str(cfg.resolved_db_path))
        conn.close()
    except Exception as e:
        check("Database reachable", False, str(e))

    # Schema complete
    try:
        from sentinel.db.schema import verify_schema
        results = verify_schema(cfg.resolved_db_path)
        missing = [t for t, ok in results.items() if not ok]
        check("Schema complete", not missing,
              f"{len(results)} tables" if not missing else f"missing: {missing}")
    except Exception as e:
        check("Schema complete", False, str(e))

    # Config loaded
    check("Config loaded", True, str(cfg.install_dir))

    # WiFi interface
    if cfg.wifi.enabled:
        iface = cfg.wifi.interface
        iface_exists = Path(f"/sys/class/net/{iface}").exists()
        check(f"WiFi interface ({iface})", iface_exists,
              "present" if iface_exists else "not found")
    else:
        check("WiFi interface", True, "disabled in config")

    # Bluetooth adapter
    if cfg.bluetooth.enabled:
        adapter = cfg.bluetooth.adapter
        adapter_path = Path(f"/sys/class/bluetooth/{adapter}")
        check(f"BT adapter ({adapter})", adapter_path.exists(),
              "present" if adapter_path.exists() else "not found")
    else:
        check("BT adapter", True, "disabled in config")

    # Systemd units loaded
    for unit in _SYSTEM_UNITS:
        r = _systemctl(["is-active", unit])
        active = r.stdout.strip()
        check(f"systemd {unit}", active == "active", active)

    for unit in _USER_UNITS + _USER_TIMERS:
        r = _systemctl(["is-active", unit], user=True)
        active = r.stdout.strip()
        check(f"systemd {unit} (user)", active in ("active", "activating"), active)

    click.echo(f"\n{ok_count} passed, {fail_count} failed")
    sys.exit(0 if fail_count == 0 else 1)


if __name__ == "__main__":
    cli()
