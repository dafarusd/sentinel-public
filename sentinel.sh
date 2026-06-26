#!/usr/bin/env bash
# Sentinel — single entrypoint for the RF surveillance platform.
#
# Usage:
#   ./sentinel.sh start       Start all daemons (manual-start mode)
#   ./sentinel.sh stop        Stop all daemons
#   ./sentinel.sh restart     Restart all daemons
#   ./sentinel.sh status      Show system status
#   ./sentinel.sh logs [name] Tail logs (all or specific daemon)
#   ./sentinel.sh selftest    Run self-test
#   ./sentinel.sh <cmd> ...   Pass through to sentinel CLI
#
# Designed for: Raspberry Pi 5 + Kali Linux, user kali.
#
# Lifecycle notes:
#   - start/stop block on is-active/is-inactive transitions; no
#     race-prone fire-and-forget calls.
#   - Units install as disabled (Stage B) — nothing boots automatically.
#     This script is the only blessed path to bring the system up.
#   - stop ceiling is 120s per unit (matches systemd's default
#     TimeoutStopSec). On expiry we warn and continue rather than block
#     the whole shutdown; systemd's SIGKILL will still land.

set -euo pipefail

INSTALL_DIR="${SENTINEL_INSTALL_DIR:-/home/user/sentinel}"
VENV="${INSTALL_DIR}/.venv"
PYTHON="${VENV}/bin/python"
CONFIG="${INSTALL_DIR}/config.yaml"
LOG_DIR="${INSTALL_DIR}/data/logs"

# System-level units (run as root for raw sockets / package-provided users)
SYSTEM_UNITS=("sentinel-wifi" "sentinel-bt" "sentinel-sdr-433")
SYSTEM_TIMERS=()

# User-level units (run as kali)
USER_UNITS=("sentinel-ingest" "sentinel-detector" "sentinel-sdr-adsb")
USER_TIMERS=("sentinel-profiler.timer")

# Ingest socket path — matches config.yaml's socket_path resolved against
# install_dir. Used to gate tier-2 startup on tier-1 readiness.
INGEST_SOCKET="${INSTALL_DIR}/data/sentinel.sock"

# Poll interval + lifecycle timeouts. Stop timeout mirrors systemd's
# default TimeoutStopSec; we warn and continue past it, we never kill.
_POLL_INTERVAL=0.5
_START_TIMEOUT_S=30
_STOP_TIMEOUT_S=120
_SOCKET_TIMEOUT_S=15

# --- ANSI color helpers ------------------------------------------------------

_is_tty() {
    [[ -t 1 ]]
}

_color() {
    # Usage: _color <sgr-code> <text>
    # Falls back to plain text when stdout isn't a terminal so log captures
    # don't accumulate escape garbage.
    if _is_tty; then
        printf '\033[%sm%s\033[0m' "$1" "$2"
    else
        printf '%s' "$2"
    fi
}

_colorize_state() {
    # systemctl is-active states: active / inactive / activating /
    # deactivating / failed / reloading / unknown.
    case "$1" in
        active)                       _color '32;1' "$1" ;;
        failed)                       _color '31;1' "$1" ;;
        activating|deactivating|reloading)
                                      _color '33;1' "$1" ;;
        *)                            _color '37'   "$1" ;;
    esac
}

_label() {
    # Right-pad to a fixed width so [OK]/[FAILED] markers align.
    printf '%-36s' "$1"
}

# --- Time helpers ------------------------------------------------------------

_now_s() {
    date +%s.%N
}

_elapsed() {
    # awk is preferred over bc because awk is universally available;
    # bc is not guaranteed on minimal Kali installs.
    awk "BEGIN {printf \"%.1f\", $2 - $1}"
}

_gt() {
    # Returns true if float $1 > float $2. Again: awk, not bc.
    awk "BEGIN {exit !($1 > $2)}"
}

# --- Privilege + dispatch ---------------------------------------------------

_preflight_sudo() {
    # Fail fast before polluting output with half-started units.
    if ! sudo -n true 2>/dev/null; then
        echo "error: passwordless sudo required for Sentinel lifecycle commands" >&2
        echo "       configure with: sudo visudo -f /etc/sudoers.d/sentinel" >&2
        echo "       example line  : ${USER:-kali} ALL=(ALL) NOPASSWD: /bin/systemctl" >&2
        exit 1
    fi
}

_systemctl() {
    # Usage: _systemctl <scope> <args...>   where scope = 'system' | 'user'
    local scope="$1"; shift
    if [[ "${scope}" == "user" ]]; then
        systemctl --user "$@"
    else
        sudo systemctl "$@"
    fi
}

# --- State polling ----------------------------------------------------------

_wait_active() {
    # Poll systemctl is-active for <unit> in <scope> until it reports
    # 'active' or 'failed', or <deadline> seconds elapse. Returns 0 on
    # active, 1 on failed or timeout. Emits the terminal marker on the
    # same line the caller already printed (no newline from us).
    local unit="$1" scope="$2" deadline="$3"
    local start_t; start_t=$(_now_s)
    local state elapsed
    while :; do
        state=$(_systemctl "${scope}" is-active "${unit}" 2>/dev/null || true)
        elapsed=$(_elapsed "${start_t}" "$(_now_s)")
        case "${state}" in
            active)
                printf ' %s (%ss)\n' "$(_color '32;1' '[OK]')" "${elapsed}"
                return 0
                ;;
            failed)
                printf ' %s (%ss)\n' "$(_color '31;1' '[FAILED]')" "${elapsed}"
                return 1
                ;;
        esac
        if _gt "${elapsed}" "${deadline}"; then
            printf ' %s (%ss, state=%s)\n' \
                "$(_color '33;1' '[TIMEOUT]')" "${elapsed}" "${state}"
            return 1
        fi
        sleep "${_POLL_INTERVAL}"
    done
}

_wait_inactive() {
    # Poll systemctl is-active for <unit> in <scope> until it reports
    # 'inactive' or 'failed' (both terminal for stop purposes), or
    # <deadline> seconds elapse. On timeout print KILL-PENDING with the
    # current state so the operator knows systemd's SIGKILL is in flight.
    local unit="$1" scope="$2" deadline="$3"
    local start_t; start_t=$(_now_s)
    local state elapsed elapsed_int last_wait=0
    while :; do
        state=$(_systemctl "${scope}" is-active "${unit}" 2>/dev/null || true)
        elapsed=$(_elapsed "${start_t}" "$(_now_s)")
        case "${state}" in
            inactive|failed)
                printf ' %s (%ss)\n' "$(_color '32;1' '[OK]')" "${elapsed}"
                return 0
                ;;
        esac
        # Emit a progress marker every 10s so a long stop doesn't look frozen.
        elapsed_int=$(awk "BEGIN {printf \"%d\", ${elapsed}}")
        if (( elapsed_int >= last_wait + 10 )); then
            printf ' [WAIT %ss]' "${elapsed_int}"
            last_wait=${elapsed_int}
        fi
        if _gt "${elapsed}" "${deadline}"; then
            printf ' %s (%ss, state=%s)\n' \
                "$(_color '33;1' '[KILL-PENDING]')" "${elapsed}" "${state}"
            printf '           %s  systemd SIGKILL in progress; continuing. Verify with ./sentinel.sh status\n' \
                "$(_color '33;1' 'WARN')"
            return 1
        fi
        sleep "${_POLL_INTERVAL}"
    done
}

_wait_socket() {
    # Poll for a Unix-domain socket file to appear, up to _SOCKET_TIMEOUT_S.
    # The ingest daemon creates its socket only after schema init + bus
    # startup — waiting on it is the only reliable "tier 1 ready" signal.
    local path="$1"
    printf '  [wait]  %s' "$(_label "ingest socket")"
    local start_t; start_t=$(_now_s)
    local elapsed
    while :; do
        if [[ -S "${path}" ]]; then
            elapsed=$(_elapsed "${start_t}" "$(_now_s)")
            printf ' %s (%ss)\n' "$(_color '32;1' '[OK]')" "${elapsed}"
            return 0
        fi
        elapsed=$(_elapsed "${start_t}" "$(_now_s)")
        if _gt "${elapsed}" "${_SOCKET_TIMEOUT_S}"; then
            printf ' %s (%ss, path=%s)\n' \
                "$(_color '33;1' '[TIMEOUT]')" "${elapsed}" "${path}"
            return 1
        fi
        sleep "${_POLL_INTERVAL}"
    done
}

# --- Unit lifecycle ---------------------------------------------------------

_start_unit() {
    # Issue start and block on is-active transition. start itself is
    # allowed to fail (|| true); truth about success comes from the poll.
    local unit="$1" scope="$2"
    printf '  [start] %s' "$(_label "${unit}")"
    _systemctl "${scope}" start "${unit}" 2>/dev/null || true
    _wait_active "${unit}" "${scope}" "${_START_TIMEOUT_S}"
}

_stop_unit() {
    # Issue stop and block on is-inactive transition. Timeout prints
    # KILL-PENDING and returns non-zero; caller ignores (we continue).
    local unit="$1" scope="$2"
    printf '  [stop ] %s' "$(_label "${unit}")"
    _systemctl "${scope}" stop "${unit}" 2>/dev/null || true
    _wait_inactive "${unit}" "${scope}" "${_STOP_TIMEOUT_S}"
}

# --- Commands ---------------------------------------------------------------

_sentinel_cli() {
    "${PYTHON}" -m sentinel.cli.main --config "${CONFIG}" "$@"
}

cmd_start() {
    local start_ts end_ts total
    start_ts=$(_now_s)

    echo "Starting Sentinel (manual-start mode)..."
    _preflight_sudo

    # Tier 1: ingest daemon + its socket. Everything downstream connects
    # to this socket, so a capture daemon racing ahead of it lands in
    # the "ran for 3h without a bus connection" failure mode we saw
    # during Stage 18b deploy.
    _start_unit sentinel-ingest user || true
    _wait_socket "${INGEST_SOCKET}" || true

    # Tier 2: captures + detector. Order within this tier isn't strict
    # because each only depends on the bus socket that tier 1 provided.
    _start_unit sentinel-wifi       system || true
    _start_unit sentinel-bt         system || true
    _start_unit sentinel-detector   user   || true
    _start_unit sentinel-sdr-adsb   user   || true
    _start_unit sentinel-sdr-433    system || true

    # Tier 3: timers that drive periodic work (profiler cycle, DB rotate).
    # No downstream dependencies on them; safe to start last.
    _start_unit sentinel-profiler.timer user   || true

    end_ts=$(_now_s)
    total=$(_elapsed "${start_ts}" "${end_ts}")
    echo "Done (${total}s)."
}

cmd_stop() {
    local start_ts end_ts total
    start_ts=$(_now_s)

    echo "Stopping Sentinel..."
    _preflight_sudo

    # Reverse-tier order: kill producers before the consumer they feed.

    # Tier 3: timers stop scheduling new work first.
    _stop_unit sentinel-profiler.timer user   || true

    # Tier 2: captures + detector — they write to the ingest bus and the
    # DB respectively. Stop them before ingest so in-flight events drain
    # upstream rather than getting cut mid-flush.
    _stop_unit sentinel-sdr-433  system || true
    _stop_unit sentinel-sdr-adsb user   || true
    _stop_unit sentinel-detector user   || true
    _stop_unit sentinel-wifi     system || true
    _stop_unit sentinel-bt       system || true

    # Short drain before ingest teardown — batcher flushes on SIGTERM
    # but this gives the socket readers a moment to finish their
    # last read() and close cleanly.
    # Drain window matches config.yaml ingest.batch_interval_s (default 2.0s).
    # If that config value changes, this sleep should change with it.
    sleep 2

    # Tier 1: ingest last. Known latent bug: its SIGTERM handler can
    # hang on writer-thread join; 120s ceiling in _stop_unit contains it.
    _stop_unit sentinel-ingest user || true

    end_ts=$(_now_s)
    total=$(_elapsed "${start_ts}" "${end_ts}")
    echo "Done (${total}s)."
}

cmd_restart() {
    # cmd_stop already blocks on is-inactive transitions, so there is no
    # race-prone middle state — we don't need a sleep between calls.
    cmd_stop
    cmd_start
}

cmd_status() {
    local state
    echo "=== Systemd Units ==="
    for unit in "${SYSTEM_UNITS[@]}" "${SYSTEM_TIMERS[@]}"; do
        state=$(sudo systemctl is-active "${unit}" 2>/dev/null || true)
        printf "  %-30s %s\n" "${unit}" "$(_colorize_state "${state}")"
    done
    for unit in "${USER_UNITS[@]}" "${USER_TIMERS[@]}"; do
        state=$(systemctl --user is-active "${unit}" 2>/dev/null || true)
        printf "  %-30s %s\n" "${unit} (user)" "$(_colorize_state "${state}")"
    done

    echo ""
    echo "=== Sentinel Status ==="
    _sentinel_cli status
}

cmd_logs() {
    local name="${1:-}"
    if [[ -n "${name}" ]]; then
        if [[ -f "${LOG_DIR}/${name}.log" ]]; then
            tail -f "${LOG_DIR}/${name}.log"
        else
            echo "Log file not found: ${LOG_DIR}/${name}.log"
            echo "Available: $(ls "${LOG_DIR}"/*.log 2>/dev/null | xargs -I{} basename {} .log | tr '\n' ' ')"
            exit 1
        fi
    else
        tail -f "${LOG_DIR}"/*.log 2>/dev/null || echo "No log files found in ${LOG_DIR}"
    fi
}

cmd_selftest() {
    _sentinel_cli selftest
}

# --- Main -------------------------------------------------------------------

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 {start|stop|restart|status|logs|selftest|<cli-command>}"
    exit 1
fi

case "$1" in
    start)    cmd_start ;;
    stop)     cmd_stop ;;
    restart)  cmd_restart ;;
    status)   cmd_status ;;
    logs)     shift; cmd_logs "$@" ;;
    selftest) cmd_selftest ;;
    *)        _sentinel_cli "$@" ;;
esac
