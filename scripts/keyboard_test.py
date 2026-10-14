#!/usr/bin/env python3
"""Type on the target with your own keyboard, straight over the serial port.

The keyboard twin of `mouse_test.py`: every key you press here is sent to the
board as one USB keyboard report (`#B1@`, then an all-keys-up report) and the
board's reply is printed beside it, so you see at once whether the board takes
it and whether the key appears on the target.

Standard library only — runs with the system python3 on the InnoConnect box.

    python3 scripts/keyboard_test.py                  # /dev/ttyACM1
    python3 scripts/keyboard_test.py --remote         # switch the board to REMOTE first

Sent to the target
    letters, digits, symbols, space      (Shift is added for capitals and shifted symbols)
    Enter, Backspace, Tab, Esc
    arrows, Home, End, Page Up/Down, Insert, Delete, F1–F12

Not sent: Ctrl, Alt and Win combinations, so they never fight the terminal.

Quit with Ctrl+C or Ctrl+D (handled here, not sent).

Only one program may use the serial port: stop the adapter, ICR's eco-kvm
session, the GUI, cutecom and mouse_test.py first.
"""
from __future__ import annotations

import argparse
import os
import select
import sys
import termios
import time
import tty

from mouse_test import BAUDS, exchange, one_line, open_port

SHIFT = 0x02

# Character → (modifier, HID usage). US layout, as the board's own keymap.
CHARS: dict[str, tuple[int, int]] = {}
for i, ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
    CHARS[ch] = (0, 0x04 + i)
    CHARS[ch.upper()] = (SHIFT, 0x04 + i)
for ch, code in zip("1234567890", range(0x1E, 0x28)):
    CHARS[ch] = (0, code)
for ch, code in zip("!@#$%^&*()", range(0x1E, 0x28)):
    CHARS[ch] = (SHIFT, code)
for plain, shifted, code in (("-", "_", 0x2D), ("=", "+", 0x2E), ("[", "{", 0x2F), ("]", "}", 0x30),
                             ("\\", "|", 0x31), (";", ":", 0x33), ("'", '"', 0x34), ("`", "~", 0x35),
                             (",", "<", 0x36), (".", ">", 0x37), ("/", "?", 0x38)):
    CHARS[plain] = (0, code)
    CHARS[shifted] = (SHIFT, code)
CHARS[" "] = (0, 0x2C)

# Single control bytes a terminal sends for these keys.
SPECIAL_BYTES = {"\r": ("Enter", 0x28), "\n": ("Enter", 0x28), "\t": ("Tab", 0x2B),
                 "\x7f": ("Backspace", 0x2A), "\x08": ("Backspace", 0x2A)}

# Escape sequences (after ESC) → (name, HID usage). xterm / VT220 style.
ESCAPES = {
    "[A": ("Up", 0x52), "[B": ("Down", 0x51), "[C": ("Right", 0x4F), "[D": ("Left", 0x50),
    "OA": ("Up", 0x52), "OB": ("Down", 0x51), "OC": ("Right", 0x4F), "OD": ("Left", 0x50),
    "[H": ("Home", 0x4A), "[F": ("End", 0x4D), "OH": ("Home", 0x4A), "OF": ("End", 0x4D),
    "[1~": ("Home", 0x4A), "[4~": ("End", 0x4D),
    "[2~": ("Insert", 0x49), "[3~": ("Delete", 0x4C),
    "[5~": ("PageUp", 0x4B), "[6~": ("PageDown", 0x4E),
    "OP": ("F1", 0x3A), "OQ": ("F2", 0x3B), "OR": ("F3", 0x3C), "OS": ("F4", 0x3D),
    "[11~": ("F1", 0x3A), "[12~": ("F2", 0x3B), "[13~": ("F3", 0x3C), "[14~": ("F4", 0x3D),
    "[15~": ("F5", 0x3E), "[17~": ("F6", 0x3F), "[18~": ("F7", 0x40), "[19~": ("F8", 0x41),
    "[20~": ("F9", 0x42), "[21~": ("F10", 0x43), "[23~": ("F11", 0x44), "[24~": ("F12", 0x45),
}


def read_key(stdin_fd: int) -> tuple[str, int, int] | str | None:
    """One key press as (label, modifier, usage); "QUIT"; or None to ignore."""
    ch = os.read(stdin_fd, 1).decode(errors="replace")
    if ch == "\x04":                                            # Ctrl+D
        return "QUIT"
    if ch == "\x1b":
        seq = ""
        # Read the rest of the sequence: it ends with a letter or '~'.
        while select.select([stdin_fd], [], [], 0.03)[0]:
            seq += os.read(stdin_fd, 1).decode(errors="replace")
            if len(seq) >= 2 and (seq[-1].isalpha() or seq[-1] == "~"):
                break
            if len(seq) > 6:
                break
        if not seq:
            return ("Esc", 0, 0x29)
        hit = ESCAPES.get(seq)
        return (hit[0], 0, hit[1]) if hit else None             # Alt+key and unknowns: ignored
    if ch in SPECIAL_BYTES:
        name, usage = SPECIAL_BYTES[ch]
        return (name, 0, usage)
    if ch in CHARS:
        mod, usage = CHARS[ch]
        return (repr(ch) if ch == " " else ch, mod, usage)
    return None                                                 # other Ctrl+letters, non-ASCII


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/ttyACM1")
    ap.add_argument("--baud", type=int, default=115200, choices=sorted(BAUDS))
    ap.add_argument("--remote", action="store_true", help="switch the board to REMOTE before starting")
    ap.add_argument("--hold", type=float, default=0.03, help="seconds between key down and key up")
    args = ap.parse_args()

    try:
        fd = open_port(args.port, args.baud)
    except OSError as exc:
        print(f"Cannot open {args.port}: {exc}\n"
              f"Run with sudo, or add yourself to the dialout group. Check nothing else holds the "
              f"port: sudo fuser -v {args.port}")
        return 1

    print(f"Port {args.port} @ {args.baud}")
    print("Firmware :", one_line(exchange(fd, "#FF@FW!", 0.3)))
    if args.remote:
        print("REMOTE   :", one_line(exchange(fd, "#11@00!", 0.3)))
    state = exchange(fd, "#FF@STATE!", 0.4)
    print("State    :", one_line(state))
    if "Local" in state or ",LOCAL," in state:
        print("\n  The board is in LOCAL and will ignore remote keys. Quit and rerun with --remote.\n")
    if "keyboard" not in state.lower():
        print("Keyboard mode     :", one_line(exchange(fd, "#31@00!", 0.3)))
    print("\nClick on the target where the text should go, then type here. Ctrl+C or Ctrl+D quits.\n")

    def send(label: str, mod: int, usage: int) -> None:
        down = f"#B1@{mod:02X}00{usage:02X}0000000000!"
        reply = one_line(exchange(fd, down, 0.06))
        time.sleep(args.hold)
        up_reply = one_line(exchange(fd, "#B1@0000000000000000!", 0.04))
        extra = "" if up_reply == "(no reply)" or up_reply.startswith("#REPLY@KEYBOARD,RECEIVED") \
            else f"   [key up: {up_reply}]"
        shown = f"Shift+{label}" if mod & SHIFT and len(label) > 1 else label
        print(f"{shown:<10} {down}  ->  {reply}{extra}")

    stdin_fd = sys.stdin.fileno()
    saved = termios.tcgetattr(stdin_fd)
    try:
        tty.setcbreak(stdin_fd)                                 # keys one by one; Ctrl+C still quits
        while True:
            key = read_key(stdin_fd)
            if key == "QUIT":
                break
            if key is None:
                continue
            send(*key)
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved)
        try:
            exchange(fd, "#B1@0000000000000000!", 0.05)         # leave nothing held
        finally:
            os.close(fd)
    print("\nbye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
