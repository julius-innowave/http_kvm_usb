#!/usr/bin/env python3
"""Move the target's mouse pointer with the arrow keys, straight over the serial port.

A hands-on test of the USB mouse path that needs neither the adapter nor ICR:
every key press sends one absolute `#A4@` report to the board and prints the
board's reply next to it, so you see at once whether the board takes it and
whether the target's pointer follows.

Standard library only — runs with the system python3 on the InnoConnect box.

    python3 scripts/mouse_test.py                    # /dev/ttyACM1, 1920x1080
    python3 scripts/mouse_test.py --port /dev/ttyACM1 --width 1920 --height 1080

Keys
    ← ↑ → ↓    move by STEP pixels
    + / -       double / halve STEP
    c           centre of the screen
    space       left click            (press, then release)
    t           corner tour: top-left, top-right, bottom-right, bottom-left, centre
    r / l       board to REMOTE / LOCAL
    s           print the board's state
    q           quit

Only one program may use the serial port: stop the adapter, ICR's eco-kvm
session, the GUI and cutecom first (`sudo fuser -v /dev/ttyACM1` prints nothing).
"""
from __future__ import annotations

import argparse
import os
import select
import sys
import termios
import time
import tty

BAUDS = {9600: termios.B9600, 19200: termios.B19200, 38400: termios.B38400,
         57600: termios.B57600, 115200: termios.B115200}


# ---- serial ---------------------------------------------------------------


def open_port(path: str, baud: int) -> int:
    """Open the board's port raw at `baud`, like `stty raw -echo -hupcl`."""
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    attrs = termios.tcgetattr(fd)
    attrs[0] = 0                                                  # iflag: no input processing
    attrs[1] = 0                                                  # oflag: no output processing
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL       # cflag: 8N1, no HUPCL
    attrs[3] = 0                                                  # lflag: no echo, no canonical
    attrs[4] = attrs[5] = BAUDS[baud]
    attrs[6][termios.VMIN], attrs[6][termios.VTIME] = 0, 0
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIOFLUSH)
    return fd


def read_for(fd: int, seconds: float) -> str:
    out, deadline = b"", time.monotonic() + seconds
    while (left := deadline - time.monotonic()) > 0:
        ready, _, _ = select.select([fd], [], [], left)
        if ready:
            try:
                out += os.read(fd, 4096)
            except BlockingIOError:
                pass
    return out.decode("utf-8", errors="replace")


def exchange(fd: int, command: str, wait: float = 0.15) -> str:
    """Send one command (CR+LF added) and return whatever comes back in `wait` s."""
    read_for(fd, 0)                                               # drop anything stale
    os.write(fd, (command + "\r\n").encode("ascii"))
    return read_for(fd, wait)


# ---- coordinates ------------------------------------------------------------


def abs_to_wire(x: int, y: int, width: int, height: int, signed: bool) -> tuple[int, int]:
    """Pixel → the two 16-bit words of an `#A4@` report (same as the adapter)."""
    if signed:
        x16 = -32767 + round(x * 65534 / (width - 1))
        y16 = -32767 + round(y * 65534 / (height - 1))
    else:
        x16 = round(x * 32767 / (width - 1))
        y16 = round(y * 32767 / (height - 1))
    return x16 & 0xFFFF, y16 & 0xFFFF


def one_line(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return " | ".join(lines) if lines else "(no reply)"


# ---- keyboard -------------------------------------------------------------


def read_key(stdin_fd: int) -> str:
    ch = os.read(stdin_fd, 1)
    if ch != b"\x1b":
        return ch.decode(errors="replace")
    seq = b""
    while select.select([stdin_fd], [], [], 0.03)[0]:
        seq += os.read(stdin_fd, 1)
        if len(seq) >= 2:
            break
    return {b"[A": "UP", b"[B": "DOWN", b"[C": "RIGHT", b"[D": "LEFT",
            b"OA": "UP", b"OB": "DOWN", b"OC": "RIGHT", b"OD": "LEFT"}.get(seq, "ESC")


# ---- main -----------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/ttyACM1")
    ap.add_argument("--baud", type=int, default=115200, choices=sorted(BAUDS))
    ap.add_argument("--width", type=int, default=1920, help="target screen width in pixels")
    ap.add_argument("--height", type=int, default=1080, help="target screen height in pixels")
    ap.add_argument("--step", type=int, default=40, help="pixels per arrow press")
    ap.add_argument("--range", choices=("signed", "unsigned"), default="signed",
                    help="absolute coordinate range: signed -32767..32767 (default) or 0..32767")
    args = ap.parse_args()

    try:
        fd = open_port(args.port, args.baud)
    except OSError as exc:
        print(f"Cannot open {args.port}: {exc}\n"
              f"Run with sudo, or add yourself to the dialout group. Check nothing else holds the "
              f"port: sudo fuser -v {args.port}")
        return 1

    signed = args.range == "signed"
    w, h = args.width, args.height
    x, y, step = w // 2, h // 2, max(1, args.step)

    print(f"Port {args.port} @ {args.baud}   screen {w}x{h}   range {args.range}")
    print("Firmware :", one_line(exchange(fd, "#FF@FW!", 0.3)))
    state = exchange(fd, "#FF@STATE!", 0.4)
    print("State    :", one_line(state))
    if "Local" in state or ",LOCAL," in state:
        print("\n  The board is in LOCAL and will ignore remote reports. Press r to switch it to REMOTE.")
    if "Input device: keyboard" in state or ",KEYBOARD" in state:
        print("Switching the board to mouse mode:", one_line(exchange(fd, "#31@01!", 0.3)))
    print(__doc__.split("Keys", 1)[1].split("Only one", 1)[0].rstrip())
    print()

    def send(px: int, py: int, buttons: int = 0, label: str = "") -> None:
        hx, hy = abs_to_wire(px, py, w, h, signed)
        cmd = f"#A4@{buttons:02X}{hx:04X}{hy:04X}00!"
        reply = one_line(exchange(fd, cmd, 0.08))
        print(f"{label:<7} x={px:5d} y={py:5d}  {cmd}  ->  {reply}")

    stdin_fd = sys.stdin.fileno()
    saved = termios.tcgetattr(stdin_fd)
    try:
        tty.setcbreak(stdin_fd)
        send(x, y, label="centre")
        while True:
            key = read_key(stdin_fd)
            if key in ("q", "Q"):
                break
            if key in ("UP", "DOWN", "LEFT", "RIGHT"):
                dx = {"LEFT": -step, "RIGHT": step}.get(key, 0)
                dy = {"UP": -step, "DOWN": step}.get(key, 0)
                x = max(0, min(w - 1, x + dx))
                y = max(0, min(h - 1, y + dy))
                send(x, y, label=key.lower())
            elif key == "+":
                step = min(1000, step * 2)
                print(f"step = {step} px")
            elif key == "-":
                step = max(1, step // 2)
                print(f"step = {step} px")
            elif key in ("c", "C"):
                x, y = w // 2, h // 2
                send(x, y, label="centre")
            elif key == " ":
                send(x, y, buttons=1, label="press")
                time.sleep(0.05)
                send(x, y, buttons=0, label="release")
            elif key in ("t", "T"):
                for (px, py), name in (((0, 0), "TL"), ((w - 1, 0), "TR"), ((w - 1, h - 1), "BR"),
                                       ((0, h - 1), "BL"), ((w // 2, h // 2), "centre")):
                    x, y = px, py
                    send(x, y, label=name)
                    time.sleep(1.0)
            elif key in ("r", "R"):
                print("REMOTE :", one_line(exchange(fd, "#11@00!", 0.3)))
                print("mouse  :", one_line(exchange(fd, "#31@01!", 0.3)))
            elif key in ("l", "L"):
                print("LOCAL  :", one_line(exchange(fd, "#11@01!", 0.3)))
            elif key in ("s", "S"):
                print("State  :", one_line(exchange(fd, "#FF@STATE!", 0.4)))
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved)
        os.close(fd)
    print("bye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
