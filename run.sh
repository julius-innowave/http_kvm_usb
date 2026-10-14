#!/usr/bin/env bash
# Start the HTTP KVM adapter in the background.
#
#   sudo ./run.sh     first time / any time: also does the setup (see below)
#   ./run.sh          plain start, no setup that needs root
#
# With sudo it also:
#   1. adds your user to the dialout (serial port) and video (capture card) groups
#   2. stops our own tools holding the board's port or the capture card
#      (another adapter, mouse_test.py, keyboard_test.py, cutecom, the GUI);
#      anything else holding them is reported, never killed
#   3. creates .venv and installs requirements.txt if missing
#   4. creates .env from env.example if missing
# The adapter itself always runs as YOUR user, not root.
#
# Output goes to logs/adapter.out, the process id to logs/adapter.pid. Stop with ./stop.sh.

set -uo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
PY="$DIR/.venv/bin/python"
LOGS="$DIR/logs"
PID_FILE="$LOGS/adapter.pid"
OUT="$LOGS/adapter.out"
# What a running adapter from this folder looks like in the process list.
SERVICE="$DIR/app.py"

# ---- who runs the adapter ---------------------------------------------------

IS_ROOT=false
RUN_USER="$(id -un)"
if [ "$(id -u)" = 0 ]; then
    IS_ROOT=true
    RUN_USER="${SUDO_USER:-root}"
    if [ "$RUN_USER" = root ]; then
        echo "Note: run this with sudo from your own account (sudo ./run.sh), not as root."
        echo "      Continuing; the adapter will run as root."
    fi
fi

# Run a command as RUN_USER. As root this starts a fresh login of the user, which
# picks up group changes made a moment ago without logging out.
as_user() {
    if $IS_ROOT && [ "$RUN_USER" != root ]; then
        if command -v runuser >/dev/null; then
            runuser -u "$RUN_USER" -- "$@"
        else
            sudo -u "$RUN_USER" -- "$@"
        fi
    else
        "$@"
    fi
}

# ---- settings (same as the adapter: environment, then .env) -------------------

env_value() {
    local name="$1" default="$2" value="${!1:-}"
    if [ -z "$value" ] && [ -f "$DIR/.env" ]; then
        value=$(grep -E "^[[:space:]]*$name=" "$DIR/.env" | tail -n1 | cut -d= -f2- | tr -d "\"' \r")
    fi
    echo "${value:-$default}"
}

# ---- already running? ---------------------------------------------------------

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "Already running (pid $(cat "$PID_FILE")). Use ./stop.sh first."
    exit 0
fi
rm -f "$PID_FILE"

# ---- 1. groups (root only) ------------------------------------------------------

if $IS_ROOT && [ "$RUN_USER" != root ]; then
    for group in dialout video; do
        getent group "$group" >/dev/null || continue
        if ! id -nG "$RUN_USER" | tr ' ' '\n' | grep -qx "$group"; then
            usermod -aG "$group" "$RUN_USER" && echo "[setup] added $RUN_USER to the $group group"
        fi
    done
elif ! $IS_ROOT; then
    for group in dialout video; do
        getent group "$group" >/dev/null 2>&1 || continue
        id -nG | tr ' ' '\n' | grep -qx "$group" \
            || echo "Warning: you are not in the $group group yet. Run once: sudo ./run.sh"
    done
fi

# ---- 2. free the serial port and the capture card -----------------------------

# Ours: the test tools by name, and an adapter only when it runs from one of our
# folders. Anything else (ICR's engine in Docker, say) is never touched.
OUR_SCRIPTS='mouse_test\.py|arrow_mouse\.py|keyboard_test\.py|cutecom|Quick_Test\.py|usb_api_windows\.py'
OUR_FOLDERS='(^|/)(adapter|adapter_usb|http_kvm_usb|LocalTest_Tool)(/|$)'
is_our_tool() {   # pid, command line
    echo "$2" | grep -Eq "$OUR_SCRIPTS" && return 0
    echo "$2" | grep -Eq 'app\.py' || return 1
    local cwd
    cwd=$(readlink "/proc/$1/cwd" 2>/dev/null || lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')
    echo "$2 $cwd" | grep -Eq "$OUR_FOLDERS"
}

devices=$(ls /dev/ttyACM* /dev/ttyUSB* /dev/video0 2>/dev/null || true)
if [ -n "$devices" ] && command -v fuser >/dev/null; then
    stopped=""
    for dev in $devices; do
        for pid in $(fuser "$dev" 2>/dev/null | tr -cs '0-9' ' '); do
            [ "$pid" = "$$" ] && continue
            cmd=$(ps -o args= -p "$pid" 2>/dev/null || true)
            [ -z "$cmd" ] && continue
            if is_our_tool "$pid" "$cmd"; then
                case " $stopped " in *" $pid "*) continue ;; esac
                if kill "$pid" 2>/dev/null; then
                    echo "[setup] stopped pid $pid holding $dev: $cmd"
                    stopped="$stopped $pid"
                else
                    echo "Warning: $dev is held by pid $pid ($cmd) and could not be stopped. Try: sudo ./run.sh"
                fi
            else
                echo "Warning: $dev is in use by pid $pid: $cmd"
                echo "         Not stopped automatically. If that is ICR's eco-kvm session, delete the session."
            fi
        done
    done
    # Give stopped tools time to release keys and close the port.
    for pid in $stopped; do
        for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
    done
fi

# ---- 3. .venv -------------------------------------------------------------------

if [ ! -x "$PY" ]; then
    echo "[setup] creating .venv and installing requirements (needs internet)…"
    if ! as_user python3 -m venv "$DIR/.venv"; then
        echo "Could not create .venv. On Ubuntu: sudo apt install -y python3-venv"
        exit 1
    fi
    if ! as_user "$PY" -m pip install -q -r "$DIR/requirements.txt"; then
        echo "pip install failed. Check the internet connection, then run this again."
        exit 1
    fi
fi

# ---- 4. .env ----------------------------------------------------------------------

if [ ! -f "$DIR/.env" ]; then
    as_user cp "$DIR/env.example" "$DIR/.env"
    echo "[setup] created .env from env.example — edit INNO_API_KEY and the target resolution,"
    echo "        then restart: ./stop.sh && ./run.sh"
fi

PORT=$(env_value INNO_PORT 9150)
KEY=$(env_value INNO_API_KEY "")

# ---- start --------------------------------------------------------------------------

other=$(pgrep -f "$SERVICE" || true)
if [ -n "$other" ]; then
    echo "An adapter from this folder is already running without a pid file (pid $other)."
    echo "Stop it with ./stop.sh, then run this again."
    exit 1
fi

# Started by RUN_USER's own shell, so logs/ and its files belong to that user.
as_user mkdir -p "$LOGS"
as_user bash -c 'cd "$1" && echo "=== $(date "+%F %T") starting" >> "$2"
                 nohup "$0" "$1/app.py" >> "$2" 2>&1 &
                 echo $! > "$3"' "$PY" "$DIR" "$OUT" "$PID_FILE"
if [ ! -s "$PID_FILE" ]; then
    echo "Could not start the adapter. See logs/adapter.out."
    exit 1
fi
echo "Started as $RUN_USER (pid $(cat "$PID_FILE")), port $PORT. Log: logs/adapter.out"

# Wait until it answers /status, then say what it reports.
AUTH=(); [ -n "$KEY" ] && AUTH=(-H "X-API-Key: $KEY")
field() { printf '%s' "$body" | python3 -c "
import json, sys
try: print(json.load(sys.stdin).get('$1', ''))
except Exception: print('')"; }
for _ in $(seq 1 30); do
    sleep 1
    if ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "It stopped right away. Last lines of logs/adapter.out:"
        tail -n 15 "$OUT"
        rm -f "$PID_FILE"
        exit 1
    fi
    body=$(curl -s --max-time 2 "${AUTH[@]}" "http://127.0.0.1:$PORT/status" || true)
    if [ -n "$body" ]; then
        status=$(field status)
        message=$(field message)
        echo "Status: ${status:-?}"
        [ -n "$message" ] && echo "        $message"
        if [ "$status" = "CONNECTED" ]; then
            echo "Check it: bash scripts/verify.sh http://127.0.0.1:$PORT ${KEY:-<api-key>}"
        else
            echo "It keeps retrying the board every 20 s. Watch: tail -f logs/adapter.out"
        fi
        exit 0
    fi
done
echo "Running, but /status did not answer within 30 s. Watch: tail -f logs/adapter.out"
