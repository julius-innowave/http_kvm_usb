# HTTP KVM — USB

A self-contained HTTP service that lets ICR control a target computer through
the InnoKVM board over **USB**. It implements the InnoConnect **Remote Adapter
API**, so ICR's stock **HTTP KVM** (`http-kvm`) provider drives it with no
engine changes.

```
ICR engine ──HTTP :9150──▶ http_kvm_usb ──serial #XX@…!──▶ KM board ──USB HID──▶ target
                                │
target HDMI ──▶ capture card ───┘ /dev/video0  (screenshots)
```

Everything is in this folder. It needs no other folder, only Python 3.10+ and
the packages in `requirements.txt`.

```
http_kvm_usb/
├── run.sh               Start in the background (sudo ./run.sh also does the setup)
├── stop.sh              Stop
├── env.example          Settings template: cp env.example .env
├── requirements.txt
├── README.md
│
├── app.py               HTTP routes (FastAPI): the service itself
├── device.py            Board behind one lock: LOCAL/REMOTE, pixels → reports, errors, reconnect
├── board.py             Serial protocol: port discovery, state, both firmware families, ACK
├── keymap.py            USB HID key codes; text and key combos → #B1@ reports
├── video.py             Frames from the capture card (or a placeholder)
├── config.py            Settings from the environment or .env
│
├── scripts/             Hands-on tools
│   ├── mouse_test.py        Move the target's pointer with the arrow keys (direct serial)
│   ├── keyboard_test.py     Type on the target from your terminal (direct serial)
│   └── verify.sh            Check a running adapter, route by route
│
├── tests/               API tests against a simulated board, no hardware needed
│
└── (created when running, not in git)
    ├── .env                 Your settings
    ├── .venv/               Python packages
    └── logs/                adapter.out, adapter.pid, YYYY-MM-DD/debug.txt (serial traffic)
```

---

## 1. Start

```bash
cd ~/test/http_kvm_usb
sudo ./run.sh
```

That's all, the first time and every time. With `sudo`, `run.sh` also does the
setup:

| Step | What it does |
|---|---|
| Permissions | Adds your user to `dialout` (serial port) and `video` (capture card) if needed. Takes effect for the adapter immediately; no logout needed. |
| Free the port | Stops our own tools holding the board's port or the capture card: another `adapter/`, `adapter_usb/` or `http_kvm_usb` adapter, `mouse_test.py`, `keyboard_test.py`, cutecom, the LocalTest GUI. **Anything else (e.g. ICR's `eco-kvm` session) is reported, never killed**; delete that session in ICR. |
| Install | Creates `.venv` and installs `requirements.txt` if missing (needs internet the first time). |
| Settings | Creates `.env` from `env.example` if missing. Edit the API key and target resolution in it, then `./stop.sh && ./run.sh`. |
| Start | Starts the adapter **as your user, not root**, in the background, and prints its status. |

Without `sudo`, `./run.sh` skips the root-only steps and warns if your user is
not in `dialout`/`video` yet.

To use the two test scripts without `sudo` as well, log out and back in once
after the first `sudo ./run.sh`, so your own shell picks up the new groups.

### Check the board and the USB link (optional, before the adapter)

Stop the adapter first (`./stop.sh`): these scripts talk to the board directly.

```bash
python3 scripts/mouse_test.py --port /dev/ttyACM1 --width 1920 --height 1080
```

The pointer on the target should jump to the **centre**, then follow your arrow
keys. Press `r` if it says the board is in LOCAL, and `q` to quit. Then:

```bash
python3 scripts/keyboard_test.py --port /dev/ttyACM1
```

Click into a text field on the target first; what you type should appear there.
Ctrl+C quits.

| What you see | Meaning |
|---|---|
| Pointer and keys work | Board, cable and target are fine. Go to section 2. |
| Pointer goes to the **top-left** on start | Set `INNO_USB_ABS_RANGE=unsigned` in `.env` |
| `NOT_READY` in the replies | The target hasn't detected the board's USB device: check the USB cable from the box to the target, or try another USB port on the target |
| `RECEIVED` / `[USB_…]` echo, but nothing happens | The board sent it; the target ignored it. Check the target side. |
| `(no reply)` on every line | Wrong port, or another program has it open |

Not sure which port? This finds the board (`State: …` marks it):

```bash
for P in /dev/ttyACM* /dev/ttyUSB*; do
  [ -e "$P" ] || continue
  printf '%-16s ' "$P"
  stty -F $P 115200 raw -echo -hupcl 2>/dev/null \
    && { printf '#FF@STATE!\r\n' > $P; timeout 1 cat $P | tr -d '\r\n'; }
  echo
done
```

The adapter itself finds the port on its own.

---

## 2. Run and stop

```bash
cd ~/test/http_kvm_usb
sudo ./run.sh                                     # set up if needed, start in the background
bash scripts/verify.sh http://127.0.0.1:9150 test123
sudo ./stop.sh                                    # stop
```

`./run.sh` starts the adapter in the background (it keeps running after you
close SSH), waits until it answers, and prints its status:

```
Started as innoconnect (pid 28755), port 9150. Log: logs/adapter.out
Status: CONNECTED
Check it: bash scripts/verify.sh http://127.0.0.1:9150 test123
```

It refuses to start a second copy. `./stop.sh` asks the adapter to stop, so it
releases held keys and buttons and closes the serial port, and forces it only
after 10 s. It also finds a copy started by hand from this folder.

`verify.sh` runs six steps: status, screenshot, raw screenshot size, keyboard,
mouse, board state. They stop at the first failure so it names the layer.

| Task | Command |
|---|---|
| Start / stop | `sudo ./run.sh` / `sudo ./stop.sh` (`sudo` optional once set up) |
| Restart (e.g. after editing `.env`) | `sudo ./stop.sh && sudo ./run.sh` |
| Running? | `./run.sh` says "Already running", or `ps aux \| grep '[a]pp.py'` |
| Adapter output | `tail -f logs/adapter.out` |
| Serial traffic | `tail -f logs/$(date +%F)/debug.txt` |
| Live API docs | `http://<box>:9150/docs` |

## 3. Connect ICR

Delete the old `eco-kvm` session for this board first. Then create a new
session with provider **HTTP KVM**:

| Field | Value |
|---|---|
| Base URL | `http://172.17.0.1:9150` (the docker bridge gateway, not `127.0.0.1`) |
| Request headers | `X-API-Key` → your `INNO_API_KEY` |
| Verify TLS certificate | off |
| Request timeout (ms) | `2000` |
| Live view interval (ms) | `200` |

Check from inside the engine container first:

```bash
docker exec innoconnect-registry-engine curl -s -H 'X-API-Key: test123' http://172.17.0.1:9150/status
```

---

## 4. Configuration

Set in `.env` (copy `env.example`) or as environment variables. Environment
variables win.

| Variable | Default | Notes |
|---|---|---|
| `INNO_PORT` | `9150` | HTTP port |
| `INNO_HOST` | `0.0.0.0` | Listen address |
| `INNO_API_KEY` | *(blank)* | Blank disables the check; only sensible on `127.0.0.1` |
| `INNO_API_KEY_HEADER` | `X-API-Key` | |
| `INNO_SCREEN_WIDTH` / `_HEIGHT` | `1920` / `1080` | **The target's real resolution.** Absolute moves are scaled to it. |
| `INNO_VIDEO_DEVICE` | `/dev/video0` if present | `none` disables video |
| `INNO_CAPTURE_WIDTH` / `_HEIGHT` / `INNO_VIDEO_FPS` / `INNO_VIDEO_PIXEL` | screen size / 30 / MJPG | Capture card mode; frames are resized to the screen size |
| `INNO_RESPECT_LOCAL` | `true` | `true`: input while the board is in LOCAL gets `409`. `false`: take control automatically. |
| `INNO_USB_ABS_RANGE` | `signed` | `unsigned` if (0,0) lands in the middle of the screen |
| `INNO_SEQUENCE` | `auto` | Sequence numbers + ACK: on for legacy firmware, off for v2 |
| `INNO_USB_MOUSE_MODE` | `ABS` | `REL` only for legacy boards without the absolute interface |
| `INNO_SWITCH_BUS_AT_STARTUP` | `false` | Allow start-up to move a PS/2 board to USB |
| `INNO_BAUD` | `115200` | |
| `INNO_LOG_DIR` | `./logs` | Daily serial log; blank disables |
| `INNO_RECONNECT_EVERY_S` | `20` | How often a lost board is retried |

**About `INNO_RESPECT_LOCAL`.** The board starts in "Local control" after
power-up; in LOCAL it ignores remote input. ICR's HTTP KVM sessions have no
Local/Remote button, so with the default `true` someone must switch the board
to REMOTE once (`POST /_admin/communication {"mode":"REMOTE"}`, or `r` in
`scripts/mouse_test.py`). With `false`, the first ICR input switches it automatically,
even if someone is using the local keyboard and mouse at that moment.

---

## 5. How it talks to the board

Commands are ASCII `#XX@data!` + CR/LF at 115200 baud. The board's firmware
family is detected from its `#FF@STATE!` reply:

| | Legacy (`Firmware: 0.9`) | v2 (`INNOKVM Firmware: 18.03.2026`, e.g. TSMC-IC-01) |
|---|---|---|
| State reply | `State: USB,REMOTE,MOUSE,ABSOLUTE,16BIT` | `Protocol: USB` / `State: Remote mode` / `Input device: mouse` / `Mouse mode: absolute` / `Mouse mode: 16-bit` |
| Commands | `#11@` `#21@` `#31@` `#41@` `#51@` `#FF@…` | `#11@` remote/local, `#21@` USB/PS2, `#31@` kbd/mouse, `#FF@FW/DBG0/DBG1/RET/STATE` |
| State change reply | `#REPLY@STATE,LOCAL->REMOTE,OK!` | `[INFO][SYS] COMMUNICATION_CHANGE: REMOTE`, nothing if unchanged. Confirmed by reading the state back. |
| Mouse/keyboard reports | `#A4@SSSS,…!` → `#ACK@SSSS!`, resent until acknowledged | `#A4@…!` sent **once**, echoed as `[USB_ABSMS16] …`. No ACK, so no resend: a resend would repeat keystrokes. |

Reports this adapter sends:

| Report | Format | Example |
|---|---|---|
| Absolute mouse | `#A4@BBXXXXYYYYWW!`: buttons, X, Y (16-bit two's complement), wheel | `#A4@000011001E00!` ≈ centre |
| Relative mouse | `#A1@BBXXYYWW!`: buttons, dx, dy, wheel (8-bit) | `#A1@00643200!` = +100, +50 |
| Keyboard | `#B1@MM00K1K2K3K4K5K6!`: modifiers, reserved, 6 key codes | `#B1@02000B0000000000!` = `H` |
| Key release | `#B1@0000000000000000!` | sent after every key |

Buttons: left `01`, right `02`, middle `04`. Modifiers: Ctrl `01`, Shift `02`,
Alt `04`, Win `08` (right-hand versions `10`/`20`/`40`/`80`).

---

## 6. API

The spec routes (what ICR calls):

| Route | Body / result |
|---|---|
| `GET /status` | `status` CONNECTED / DISCONNECTED / ERROR, `width`, `height`, `capabilities`, `message`. Answers from cache; never blocks. |
| `GET /screenshot` | PNG of the target's screen (`X-Image-Placeholder: true` without video) |
| `GET /screenshot/raw` | RGB bytes, `width × height × 3` |
| `POST /mouse` | `{"type":"MOVE\|CLICK\|DOUBLE_CLICK\|PRESS\|RELEASE\|DRAG\|SCROLL","x":…,"y":…,"button":1\|2\|3,"fromX","fromY","scrollDelta"}` |
| `POST /keyboard` | `{"type":"TYPE_TEXT","text":"…"}` or `{"type":"KEY_PRESS\|HOTKEY","key":"enter","ctrl":true,"shift":false,"alt":false}`. `key` may also be `"ctrl+alt+delete"`. |

Maintenance routes (`/_admin`, same API key; ICR never calls them):

| Route | For |
|---|---|
| `GET /_admin/state` | Board state, port, firmware, family, last NOT_READY |
| `POST /_admin/communication` | `{"mode":"REMOTE"}` / `{"mode":"LOCAL"}` |
| `POST /_admin/usb-profile` | `{"profile":"ALL\|REL"}` (legacy firmware only) |
| `POST /_admin/device-command` | `{"action":"firmware\|debug_on\|debug_off"}` |
| `POST /_admin/raw` | `{"command":"#FF@FW!"}`: anything except `#21@` |
| `GET /_admin/log` | Recent serial lines |

Error answers are `{"errorMessage": "...", "code": "..."}`:

| HTTP | `code` | Meaning |
|---|---|---|
| 401 | `unauthorized` | Missing or wrong API key |
| 400 | `invalid_request` | Malformed body |
| 409 | `device_busy` | Board is in LOCAL |
| 422 | `unsupported` | Unknown key, KEY_DOWN/UP, `#21@` |
| 502 | `device_rejected` | Board refused it. `NOT_READY` = the target hasn't detected the board's USB device. |
| 503 | `device_unreachable` | No serial link; retried in the background |

---

## 7. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `scripts/verify.sh` step 1 DISCONNECTED, `Permission denied` | Start with `sudo ./run.sh` (section 1) |
| DISCONNECTED, "another program has this port open" | `sudo fuser -v /dev/ttyACM*`, stop that program |
| DISCONNECTED, "board is on the PS2 bus" | Move it to USB deliberately: `#11@01!` then `#21@00!` (the target loses PS/2 input until it reboots) |
| `409 device_busy` | Board in LOCAL: `curl -X POST -H 'X-API-Key: …' -H 'Content-Type: application/json' -d '{"mode":"REMOTE"}' http://127.0.0.1:9150/_admin/communication` |
| `502` with NOT_READY | USB cable / port between the box and the target |
| 200 but nothing happens on the target | Check the target side; compare with `scripts/mouse_test.py` |
| Clicks land off by a fixed fraction | `INNO_SCREEN_*` isn't the target's real resolution |
| Pointer starts in the middle when asked for (0,0) | `INNO_USB_ABS_RANGE=unsigned` |
| Screenshot is a placeholder | `/status` message says why: card busy (`sudo fuser -v /dev/video0`), not in the `video` group, or OpenCV missing |
| ICR "cannot connect" | Adapter alive? Base URL `http://172.17.0.1:9150`? Firewall: `sudo ufw allow from 172.17.0.0/16 to any port 9150` |

---

## 8. Tests (no hardware)

```bash
.venv/bin/python -m unittest discover -s tests -v
```

38 tests run the whole HTTP API against simulated boards of both firmware
families. They cover moves, clicks, scroll, text, key combos, NOT_READY,
LOCAL/REMOTE, raw commands, a PS/2 board refused at start-up, and a
permission-denied port. The simulated v2 board answers mouse reports with the
`[USB_ABSMS16]` echo seen on TSMC-IC-01.

**Not yet run as a whole against a real board.** `scripts/mouse_test.py` and
`scripts/keyboard_test.py` have been confirmed on TSMC-IC-01 (firmware 18.03.2026).
