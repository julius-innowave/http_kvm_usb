#!/usr/bin/env bash
# Walk the USB adapter's routes in the order a failure is cheapest to read:
# serial path, video, keyboard (no coordinates), mouse. The first FAIL names
# the layer rather than the symptom.
#
#   bash scripts/verify.sh [base-url] [api-key]
#   BASE=http://127.0.0.1:9150 KEY=… bash scripts/verify.sh

set -uo pipefail

BASE="${1:-${BASE:-http://127.0.0.1:9150}}"
KEY="${2:-${KEY:-}}"
BASE="${BASE%/}"
AUTH=(); [ -n "$KEY" ] && AUTH=(-H "X-API-Key: $KEY")
JSON=(-H "Content-Type: application/json")

pass=0; fail=0; skip=0
step() { printf '\n\033[1m%s\033[0m\n' "── $* ─────────────────────────────"; }
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$*"; pass=$((pass+1)); }
no()   { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; fail=$((fail+1)); }
note() { printf '        %s\n' "$*"; }
skipped() { printf '  \033[33mSKIP\033[0m  %s\n' "$*"; skip=$((skip+1)); }

field() { python3 -c "
import json,sys
try: print(json.load(sys.stdin).get('$1',''))
except Exception: print('')
"; }

step "1. /status — the serial path"
body=$(curl -s --max-time 10 "${AUTH[@]}" "$BASE/status")
if [ -z "$body" ]; then
    no "no answer from $BASE/status"
    note "Is the adapter running? Is the port reachable from here?"
    exit 1
fi
status=$(printf '%s' "$body" | field status)
case "$status" in
    CONNECTED)
        ok "status=CONNECTED  $(printf '%s' "$body" | field adapter) $(printf '%s' "$body" | field version)"
        note "screen $(printf '%s' "$body" | field width)x$(printf '%s' "$body" | field height)"
        msg=$(printf '%s' "$body" | field message)
        [ -n "$msg" ] && note "NOTE: $msg"
        ;;
    "") no "answer was not JSON: $(printf '%s' "$body" | head -c 200)"; exit 1 ;;
    *)  no "status=$status"
        printf '%s' "$body" | field message | fold -s -w 90 | sed 's/^/        /'
        note ""
        note "Nothing below this can pass. Usual causes:"
        note "  - another program holds the serial port: sudo fuser -v /dev/ttyACM*"
        note "    (ICR eco-kvm session, adapter/ or adapter_usb/, GUI, cutecom, arrow_mouse.py)"
        note "  - no permission for the port: sudo usermod -aG dialout,video \$USER, then log in again (README section 1)"
        note "  - the board is on PS2: see README section 7 (move it to USB deliberately)"
        note "  - just started: the adapter retries every 20 s; check tail -f adapter.out"
        exit 1 ;;
esac

step "2. /screenshot — video"
hdr=$(mktemp)
bytes=$(curl -s --max-time 10 "${AUTH[@]}" -D "$hdr" "$BASE/screenshot" | wc -c)
if grep -qi '^content-type: *image/png' "$hdr"; then
    ok "PNG, $bytes bytes"
    grep -qi '^x-image-placeholder: *true' "$hdr" && \
        note "PLACEHOLDER — not a picture of the screen. Check INNO_VIDEO_DEVICE / opencv."
else
    no "not an image/png answer"; sed -n '1,8p' "$hdr"
fi
rm -f "$hdr"

step "3. /screenshot/raw — exact byte count"
hdr=$(mktemp)
bytes=$(curl -s --max-time 10 "${AUTH[@]}" -D "$hdr" "$BASE/screenshot/raw" | wc -c)
w=$(grep -i '^x-image-width:' "$hdr" | tr -dc '0-9')
h=$(grep -i '^x-image-height:' "$hdr" | tr -dc '0-9')
if [ -n "$w" ] && [ -n "$h" ] && [ "$bytes" -eq $((w * h * 3)) ]; then
    ok "${w}x${h} RGB, $bytes bytes = w*h*3"
else
    no "body is $bytes bytes, headers say ${w:-?}x${h:-?}"
fi
rm -f "$hdr"

step "4. /keyboard — the USB input path, no coordinates needed"
out=$(curl -s -w '\n%{http_code}' --max-time 15 "${AUTH[@]}" "${JSON[@]}" \
      -d '{"type":"KEY_PRESS","key":"shift"}' "$BASE/keyboard")
code=$(printf '%s' "$out" | tail -n1)
case "$code" in
    200) ok "a bare Shift was accepted by the board (harmless on the target)" ;;
    409) no "409 device_busy — the board is latched to LOCAL"
         note "curl ${AUTH[*]} -H 'Content-Type: application/json' -d '{\"mode\":\"REMOTE\"}' $BASE/_admin/communication" ;;
    502) no "502 — the board refused it:"
         printf '%s' "$out" | head -n-1 | field errorMessage | fold -s -w 90 | sed 's/^/        /' ;;
    *)   no "HTTP $code"; printf '%s' "$out" | head -n-1 | sed 's/^/        /' ;;
esac

step "5. /mouse — corner, then centre"
if [ "$fail" -gt 0 ]; then
    skipped "earlier steps failed; fix those first"
else
    sw=$(printf '%s' "$body" | field width); sh=$(printf '%s' "$body" | field height)
    cx=$(( sw / 2 )); cy=$(( sh / 2 ))
    code=0
    for point in "$(( sw / 8 )),$(( sh / 8 ))" "$cx,$cy"; do
        out=$(curl -s -w '\n%{http_code}' --max-time 30 "${AUTH[@]}" "${JSON[@]}" \
              -d "{\"type\":\"MOVE\",\"x\":${point%,*},\"y\":${point#*,}}" "$BASE/mouse")
        code=$(printf '%s' "$out" | tail -n1)
        [ "$code" = 200 ] || break
        sleep 1
    done
    case "$code" in
        200) ok "MOVE to $(( sw / 8 )),$(( sh / 8 )) then ${cx},${cy} accepted"
             note "LOOK AT THE TARGET: upper-left eighth, then the middle of the screen."
             note "  lands off by a fixed fraction -> INNO_SCREEN_* is not the target's resolution"
             note "  did not move at all           -> board took it, target ignored it" ;;
        *)   no "HTTP $code"
             printf '%s' "$out" | head -n-1 | field errorMessage | fold -s -w 90 | sed 's/^/        /' ;;
    esac
fi

step "6. board state"
curl -s "${AUTH[@]}" "$BASE/_admin/state" | python3 -c "
import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    print('        (no JSON from /_admin/state)'); sys.exit()
for k in ('port','firmware','firmwareStyle','sequencedReports','state','mouseMode','absRange','lastNotReady','profileNote'):
    print('        %-18s %s' % (k, d.get(k)))
"

printf '\n\033[1m%d passed, %d failed, %d skipped\033[0m\n' "$pass" "$fail" "$skip"
[ "$fail" -eq 0 ]
