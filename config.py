"""Everything an operator may change, read from the environment.

Nothing is read from outside the project. Optionally, the project's `.env`
file is loaded first (KEY=value per line; start from `env.example`). Real
environment variables win over it.
"""
from __future__ import annotations

import os
from pathlib import Path

#: The project folder (the one holding run.sh, .env and logs/).
ROOT = Path(__file__).resolve().parent


def _load_env_file() -> None:
    path = Path(os.getenv("INNO_ENV_FILE", ROOT / ".env"))
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file()


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ---- identity / HTTP ----------------------------------------------------------

ADAPTER_NAME = os.getenv("INNO_ADAPTER_NAME", "http-kvm-usb")
ADAPTER_VERSION = "1.0.0"
HOST = os.getenv("INNO_HOST", "0.0.0.0")
PORT = int(os.getenv("INNO_PORT", "9150"))

#: Blank disables the check — only reasonable when HOST is 127.0.0.1.
API_KEY = os.getenv("INNO_API_KEY", "")
API_KEY_HEADER = os.getenv("INNO_API_KEY_HEADER", "X-API-Key")

# ---- serial -------------------------------------------------------------------

BAUD = int(os.getenv("INNO_BAUD", "115200"))

#: Sequence numbers + ACK on HID reports. `auto`: on for legacy firmware
#: (`Firmware: 0.9`), off for v2 (`18.03.2026`), which has no ACK — resending
#: there would repeat the keystroke.
SEQUENCE = os.getenv("INNO_SEQUENCE", "auto").strip().lower()
if SEQUENCE not in {"auto", "on", "off"}:
    raise SystemExit(f"INNO_SEQUENCE must be auto, on or off, not {SEQUENCE!r}")

PACKET_INTERVAL_S = float(os.getenv("INNO_PACKET_INTERVAL_S", "0.002"))

#: Allow start-up to move a board that is on PS/2 over to USB. Off by default:
#: a PS/2 target loses its PS/2 keyboard and mouse until it reboots.
SWITCH_BUS_AT_STARTUP = _bool("INNO_SWITCH_BUS_AT_STARTUP", False)

#: Refuse input (409) while the board is in LOCAL instead of taking control.
#: Set false if the board falls back to LOCAL on its own (e.g. after a power
#: cycle) and the HTTP client should always be able to take control.
RESPECT_LOCAL = _bool("INNO_RESPECT_LOCAL", True)

LOCAL_CHECK_S = float(os.getenv("INNO_LOCAL_CHECK_S", "2.0"))
STATUS_CACHE_S = float(os.getenv("INNO_STATUS_CACHE_S", "3.0"))
RECONNECT_EVERY_S = float(os.getenv("INNO_RECONNECT_EVERY_S", "20"))

# ---- mouse --------------------------------------------------------------------

#: `ABS`: `#A4@` absolute 16-bit, a pixel maps straight to a position.
#: `REL`: `#A1@` relative 8-bit (legacy boards with the REL USB profile only).
MOUSE_MODE = os.getenv("INNO_USB_MOUSE_MODE", "ABS").strip().upper()
if MOUSE_MODE not in {"ABS", "REL"}:
    raise SystemExit(f"INNO_USB_MOUSE_MODE must be ABS or REL, not {MOUSE_MODE!r}")

#: `signed` −32767…32767 (default) or `unsigned` 0…32767. If (0,0) lands in
#: the middle of the screen, the firmware wants `unsigned`.
ABS_RANGE = os.getenv("INNO_USB_ABS_RANGE", "signed").strip().lower()
if ABS_RANGE not in {"signed", "unsigned"}:
    raise SystemExit(f"INNO_USB_ABS_RANGE must be signed or unsigned, not {ABS_RANGE!r}")

#: Legacy only: restore the USB profile with the absolute interface at start-up.
SET_PROFILE_AT_STARTUP = _bool("INNO_USB_SET_PROFILE", True)

REL_MAX_DELTA = 127
HOME_STEPS = int(os.getenv("INNO_HOME_STEPS", "24"))
SCALE_X = float(os.getenv("INNO_SCALE_X", "1.0"))
SCALE_Y = float(os.getenv("INNO_SCALE_Y", "1.0"))

CLICK_HOLD_S = float(os.getenv("INNO_CLICK_HOLD_S", "0.03"))
CLICK_GAP_S = float(os.getenv("INNO_CLICK_GAP_S", "0.03"))
KEY_HOLD_S = float(os.getenv("INNO_KEY_HOLD_S", "0.02"))

# ---- screen and video ---------------------------------------------------------
# SCREEN_* is the TARGET's real resolution: the /mouse coordinate space and the
# size /screenshot is returned at. CAPTURE_* is the mode asked of the card.

SCREEN_WIDTH = int(os.getenv("INNO_SCREEN_WIDTH", "1920"))
SCREEN_HEIGHT = int(os.getenv("INNO_SCREEN_HEIGHT", "1080"))
CAPTURE_WIDTH = int(os.getenv("INNO_CAPTURE_WIDTH", str(SCREEN_WIDTH)))
CAPTURE_HEIGHT = int(os.getenv("INNO_CAPTURE_HEIGHT", str(SCREEN_HEIGHT)))

#: `/dev/video0` when it exists, else none (placeholder frames). `none` disables.
_video_default = "/dev/video0" if Path("/dev/video0").exists() else ""
VIDEO_DEVICE = os.getenv("INNO_VIDEO_DEVICE", _video_default).strip()
if VIDEO_DEVICE.lower() == "none":
    VIDEO_DEVICE = ""
VIDEO_FPS = float(os.getenv("INNO_VIDEO_FPS", "30"))
VIDEO_PIXEL = os.getenv("INNO_VIDEO_PIXEL", "MJPG").strip().upper()

# ---- logging ------------------------------------------------------------------

#: Daily serial log, `<dir>/YYYY-MM-DD/debug.txt`. Blank disables it.
#: run.sh also keeps adapter.out and adapter.pid in the project's logs/.
LOG_DIR = os.getenv("INNO_LOG_DIR", str(ROOT / "logs"))


def abs_to_wire(x: int, y: int) -> tuple[int, int]:
    """A pixel on the target's screen → the two 16-bit words of `#A4@` (two's complement)."""
    w, h = max(2, SCREEN_WIDTH), max(2, SCREEN_HEIGHT)
    if ABS_RANGE == "signed":
        x16 = -32767 + round(x * 65534 / (w - 1))
        y16 = -32767 + round(y * 65534 / (h - 1))
    else:
        x16 = round(x * 32767 / (w - 1))
        y16 = round(y * 32767 / (h - 1))
    return x16 & 0xFFFF, y16 & 0xFFFF


def capabilities() -> list[str]:
    """No KEY_HOLD: the board releases a USB key on its own after each report."""
    caps = ["MOUSE_CONTROL", "KEYBOARD_CONTROL", "SCROLL"]
    if VIDEO_DEVICE:
        caps.insert(0, "SCREEN_CAPTURE")
    return caps
