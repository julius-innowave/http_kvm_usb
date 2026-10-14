#!/usr/bin/env bash
# Stop the HTTP KVM adapter started by ./run.sh.
#
#   ./stop.sh
#
# Asks it to stop (it releases held keys/buttons and closes the serial port),
# waits up to 10 s, and only then forces it.

set -uo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
PID_FILE="$DIR/logs/adapter.pid"

cwd_of() {   # working directory of a process (Linux /proc, else lsof)
    readlink "/proc/$1/cwd" 2>/dev/null || lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p'
}

pids=""
add() { case " $pids " in *" $1 "*) ;; *) pids="$pids $1" ;; esac; }

# 1. The one ./run.sh started.
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    add "$(cat "$PID_FILE")"
fi
# 2. Any other adapter from this folder (started by path, or by hand from inside it).
for p in $(pgrep -f "$DIR/app.py" || true); do add "$p"; done
for p in $(pgrep -f "[Pp]ython[0-9.]* app.py" || true); do    # macOS shows "Python"
    [ "$(cwd_of "$p")" = "$DIR" ] && add "$p"
done
pids=$(echo $pids)

if [ -z "$pids" ]; then
    echo "Not running."
    rm -f "$PID_FILE"
    exit 0
fi

echo "Stopping (pid $pids)…"
kill $pids 2>/dev/null
for _ in $(seq 1 20); do
    alive=""
    for p in $pids; do kill -0 "$p" 2>/dev/null && alive="$alive $p"; done
    [ -z "$alive" ] && break
    sleep 0.5
done
if [ -n "${alive:-}" ]; then
    echo "Still running after 10 s, forcing:$alive"
    kill -9 $alive 2>/dev/null
fi
rm -f "$PID_FILE"
[ -d "$DIR/logs" ] && echo "$(date '+%F %T') stopped" >> "$DIR/logs/adapter.out"
echo "Stopped."
