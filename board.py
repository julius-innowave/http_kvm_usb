"""The KM board's serial protocol: finding it, reading its state, sending reports.

Every command is ASCII `#XX@data!` followed by CR+LF. Two firmware families
exist and both are handled:

"legacy" (`Firmware: 0.9`)
    `#FF@STATE!` → `State: USB,REMOTE,MOUSE,ABSOLUTE,16BIT` (one line)
    State change → `#REPLY@STATE,LOCAL->REMOTE,OK!`
    HID reports carry a sequence number and are acknowledged: `#A4@SSSS,…!` → `#ACK@SSSS!`
    Latches: #11@ remote/local, #21@ USB/PS2, #31@ kbd/mouse, #41@ abs/rel, #51@ 8/16-bit,
    plus `#51@ALL!` / `#51@REL!` USB descriptor profile.

"v2" (`INNOKVM Firmware: 18.03.2026`)
    `#FF@STATE!` → several lines: `Protocol: USB` / `State: Remote mode` /
    `Input device: mouse` / `Mouse mode: absolute` / `Mouse mode: 16-bit`
    (LOCAL prints only the first two, as `State: Local control`).
    State change → `[INFO][SYS] COMMUNICATION_CHANGE: REMOTE`, or nothing when unchanged.
    HID reports are not acknowledged: sent once, answered with a debug echo such
    as `[USB_ABSMS16] 06,00,0011,001E,00`.
    Latches: only #11@, #21@, #31@.

One `Board` owns one open port. It is not thread-safe on its own; the caller
(`device.Device`) serialises every call behind its lock.
"""
from __future__ import annotations

import re
import time
from typing import Callable

import serial
from serial.tools import list_ports

LINE_END = "\r\n"
STATE_QUERY = "#FF@STATE!"

# Replaceable for tests.
serial_factory: Callable[..., serial.Serial] = serial.Serial
list_candidate_ports: Callable[[], list] = list_ports.comports


# ---- errors ---------------------------------------------------------------


class BoardError(Exception):
    """Anything the board or the link did wrong."""


class NotReady(BoardError):
    """The target has not enumerated the board's USB device (`NOT_READY`)."""


class Refused(BoardError):
    """The board rejected a command (`[ERROR]…`, FORMAT, BUSY after retries, …)."""


class LinkLost(BoardError):
    """The serial link itself failed: write error, no answer, port gone."""


# ---- state parsing ----------------------------------------------------------

_LEGACY = re.compile(r"(?:^|\n)\s*State:\s*(USB|PS2),(LOCAL|REMOTE),(KEYBOARD|MOUSE)"
                     r"(?:,(ABSOLUTE|RELATIVE)(?:,(8BIT|16BIT))?)?[ \t]*\r?\n")
_V2_PROTOCOL = re.compile(r"Protocol:\s*(USB|PS/?2)", re.I)
_V2_STATE = re.compile(r"State:\s*(Local|Remote)\b", re.I)
_V2_DEV = re.compile(r"Input device:\s*(mouse|keyboard)", re.I)
_V2_MODE = re.compile(r"Mouse mode:\s*(absolute|relative)", re.I)
_V2_RES = re.compile(r"Mouse mode:\s*(8|16)-?bit", re.I)


def parse_state(text: str) -> tuple[dict, str] | None:
    """`#FF@STATE!` reply → (state, style). None when it is not a state reply.

    state keys: PROTOCOL (USB|PS2), COMM (REMOTE|LOCAL), and when reported
    DEV (KB|MS), MS_MODE (ABS|REL), MS_RES (8BIT|16BIT).
    """
    match = _LEGACY.search(text)
    if match:
        protocol, comm, dev, mode, width = match.groups()
        state = {"PROTOCOL": protocol, "COMM": comm, "DEV": "KB" if dev == "KEYBOARD" else "MS"}
        if mode:
            state["MS_MODE"] = "ABS" if mode == "ABSOLUTE" else "REL"
        if width:
            state["MS_RES"] = width
        return state, "legacy"
    protocol, comm = _V2_PROTOCOL.search(text), _V2_STATE.search(text)
    if not protocol or not comm:
        return None
    state = {"PROTOCOL": "USB" if protocol[1].upper() == "USB" else "PS2",
             "COMM": "REMOTE" if comm[1].lower() == "remote" else "LOCAL"}
    if dev := _V2_DEV.search(text):
        state["DEV"] = "MS" if dev[1].lower() == "mouse" else "KB"
    if mode := _V2_MODE.search(text):
        state["MS_MODE"] = "ABS" if mode[1].lower() == "absolute" else "REL"
    if res := _V2_RES.search(text):
        state["MS_RES"] = f"{res[1]}BIT"
    return state, "v2"


_NOT_READY = re.compile(r"#REPLY@(?:MOUSE|KEYBOARD),NOT_READY[^\r\n]*|#NACK@[0-9A-F]{4},NOT_READY!")
_REFUSED = re.compile(r"\[ERROR\][^\r\n]*|#REPLY@(?:MOUSE|KEYBOARD),(?:FORMAT|BUSY|BLOCKED)[^\r\n]*"
                      r"|#NACK@[^\r\n]*")


# ---- the board --------------------------------------------------------------

#: Commands per latch value, per firmware family.
_LEGACY_COMMANDS = {
    "COMM": {"REMOTE": "#11@00!", "LOCAL": "#11@01!"},
    "PROTOCOL": {"USB": "#21@00!", "PS2": "#21@01!"},
    "DEV": {"KB": "#31@00!", "MS": "#31@01!"},
    "MS_MODE": {"ABS": "#41@00!", "REL": "#41@01!"},
    "MS_RES": {"8BIT": "#51@00!", "16BIT": "#51@01!"},
}
_V2_COMMANDS = {key: _LEGACY_COMMANDS[key] for key in ("COMM", "PROTOCOL", "DEV")}
_LEGACY_REPLY_NAMES = {"MS": "MOUSE", "KB": "KEYBOARD", "ABS": "ABSOLUTE", "REL": "RELATIVE"}


class Board:
    """One open serial port to the board, with its protocol."""

    ACK_TIMEOUT_S = 0.25
    MAX_ATTEMPTS = 8
    PLAIN_WINDOW_S = 0.05
    #: An 8/16-bit or profile change re-enumerates the USB device on the target.
    REENUMERATE_S = 1.0

    def __init__(self, ser, style: str, state: dict, *, sequenced: bool,
                 log: Callable[[str], None] = lambda line: None,
                 packet_interval_s: float = 0.002) -> None:
        self.ser = ser
        self.style = style
        self.state = dict(state)            # last known latches; cleared when in doubt
        self.sequenced = sequenced
        self.log = log
        self.packet_interval_s = packet_interval_s
        self.usb_profile: str | None = None
        self._sequence = 0

    @property
    def port(self) -> str:
        return getattr(self.ser, "port", "?")

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:                                # noqa: BLE001 — closing is best effort
            pass

    # ---- raw I/O -----------------------------------------------------------

    def _write(self, command: str) -> None:
        wire = (command.rstrip("\r\n") + LINE_END).encode("ascii")
        try:
            written = self.ser.write(wire)
            self.ser.flush()
        except (OSError, serial.SerialException) as exc:
            raise LinkLost(f"write to {self.port} failed: {exc}") from exc
        self.log(f"[UART][TX] {wire!r}")
        if written is not None and written != len(wire):
            raise LinkLost(f"incomplete write to {self.port}: {written}/{len(wire)} bytes")

    def _read_for(self, seconds: float, until: Callable[[str], bool] | None = None) -> str:
        """Collect what arrives for `seconds`, or until `until(text)` is true."""
        deadline = time.monotonic() + seconds
        data = ""
        while time.monotonic() < deadline:
            try:
                waiting = self.ser.in_waiting
                chunk = self.ser.read(waiting) if waiting else b""
            except (OSError, serial.SerialException) as exc:
                raise LinkLost(f"read from {self.port} failed: {exc}") from exc
            if chunk:
                self.log(f"[UART][RX] {chunk!r}")
                data += chunk.decode("utf-8", errors="replace")
                if len(data) > 65536:
                    raise Refused("The board is printing too much; turn its debug output off (#FF@DBG0!).")
                if until and until(data):
                    break
            else:
                time.sleep(0.002)
        return data

    def _drain(self) -> None:
        """Drop late replies to earlier commands so they are not read as this one's."""
        try:
            waiting = self.ser.in_waiting
            if waiting:
                self.log(f"[UART][RX][stale] {self.ser.read(waiting)!r}")
        except (OSError, serial.SerialException) as exc:
            raise LinkLost(f"read from {self.port} failed: {exc}") from exc

    def exchange(self, command: str, wait: float = 0.3,
                 until: Callable[[str], bool] | None = None) -> str:
        """Send one command and return the board's reply text (may be empty)."""
        self._drain()
        self._write(command)
        return self._read_for(wait, until)

    # ---- state -------------------------------------------------------------

    def read_state(self) -> dict | None:
        """Ask the board for its latches. Updates `self.state`; None if it did not answer."""
        parsed = parse_state(self.exchange(STATE_QUERY, 0.5))
        if parsed is None:
            return None
        self.state = dict(parsed[0])
        return dict(self.state)

    def set_state(self, desired: dict) -> None:
        """Move the latches to `desired` ({key: value}, in order). Confirmed or raises Refused."""
        for key, value in desired.items():
            if self.state.get(key) == value:
                continue
            if self.style == "legacy":
                self._set_legacy(key, value)
            else:
                self._set_v2(key, value)
            self.log(f"[STATE][PASS] {key}={value}")

    def _set_legacy(self, key: str, value: str) -> None:
        command = _LEGACY_COMMANDS[key][value]
        target = _LEGACY_REPLY_NAMES.get(value, value)
        ok = re.compile(rf"#REPLY@STATE,[^,!]+->{target},OK(?:,UNCHANGED)?!")
        reply = self.exchange(command, 0.5, until=lambda text: bool(ok.search(text)))
        if not ok.search(reply):
            self.state.clear()
            raise Refused(f"{key} → {value} ({command}) was not confirmed: {reply.strip() or '(no reply)'}")
        self.state[key] = value
        if key == "MS_RES":
            time.sleep(self.REENUMERATE_S)

    def _set_v2(self, key: str, value: str) -> None:
        command = _V2_COMMANDS.get(key, {}).get(value)
        if command:
            self.exchange(command, 0.3)
        actual = self.read_state()
        if actual is None or actual.get(key) != value:
            self.state.clear()
            seen = actual.get(key, "not reported") if actual else "no state reply"
            if command:
                raise Refused(f"{key} → {value} was not confirmed: after {command} the board "
                              f"reports {key}={seen}.")
            raise Refused(f"This firmware has no command to set {key}. The board reports "
                          f"{key}={seen}; {value} is needed.")

    def set_usb_profile(self, profile: str) -> None:
        """Legacy only: `ALL` (with the absolute mouse interface) or `REL` (without)."""
        if self.style != "legacy":
            raise Refused("This firmware has no USB profile command (#51@ALL / #51@REL).")
        expected = f"#REPLY@USB,PROFILE,{profile},OK!"
        reply = self.exchange(f"#51@{profile}!", 0.5, until=lambda text: expected in text)
        if expected not in reply:
            raise Refused(f"USB profile {profile} was not confirmed: {reply.strip() or '(no reply)'}")
        self.usb_profile = profile
        time.sleep(self.REENUMERATE_S)
        if profile == "REL":
            self.state["MS_MODE"] = "REL"        # the firmware selects relative with the profile
            self.state.pop("MS_RES", None)

    # ---- HID reports -------------------------------------------------------

    def send_report(self, command: str) -> str:
        """Send one mouse/keyboard report. Raises NotReady, Refused or LinkLost."""
        reply = self._send_sequenced(command) if self.sequenced else self._send_plain(command)
        if self.packet_interval_s:
            time.sleep(self.packet_interval_s)
        return reply

    def _send_plain(self, command: str) -> str:
        """Sent once, no sequence number: on firmware without ACK a resend repeats the key."""
        reply = self.exchange(command, self.PLAIN_WINDOW_S)
        if match := _NOT_READY.search(reply):
            raise NotReady(match.group(0))
        if match := _REFUSED.search(reply):
            raise Refused(match.group(0))
        return reply

    def _send_sequenced(self, command: str) -> str:
        """`#A4@SSSS,payload!` until `#ACK@SSSS!`. The board drops duplicate sequences."""
        sequence = self._sequence
        at = command.find("@")
        wire = f"{command[:at + 1]}{sequence:04X},{command[at + 1:]}"
        ack, nack = f"#ACK@{sequence:04X}!", f"#NACK@{sequence:04X},"
        self._drain()
        for _ in range(self.MAX_ATTEMPTS):
            self._write(wire)
            reply = self._read_for(self.ACK_TIMEOUT_S,
                                   until=lambda text: ack in text or nack in text or "[ERROR]" in text
                                   or bool(_NOT_READY.search(text)))
            if match := _NOT_READY.search(reply):
                time.sleep(0.05)
                self._drain()
                raise NotReady(match.group(0))           # not accepted: sequence not advanced
            if ack in reply:
                self._sequence = (sequence + 1) & 0xFFFF
                return reply
            if "[ERROR]" in reply or "#REPLY@MOUSE," in reply or "#REPLY@KEYBOARD," in reply:
                raise Refused(reply.strip())
            busy = re.search(re.escape(nack) + r"([^!]+)!", reply)
            if busy and busy[1] != "BUSY":
                raise Refused(reply.strip())
            time.sleep(0.002)                            # timeout or BUSY: resend the same sequence
        raise LinkLost(f"No acknowledgement after {self.MAX_ATTEMPTS} attempts (sequence {sequence:04X}).")


# ---- finding the board --------------------------------------------------------


def open_board(baud: int, *, sequence: str = "auto", log: Callable[[str], None] = lambda line: None,
               packet_interval_s: float = 0.002, probe_timeout_s: float = 0.7) -> Board:
    """Open every serial port in turn and keep the first that answers `#FF@STATE!`.

    `sequence`: "auto" (sequenced on legacy firmware, plain on v2), "on" or "off".
    Raises LinkLost with every port's reason when none answers.
    """
    ports = list(list_candidate_ports())
    usb = [p for p in ports if getattr(p, "vid", None) is not None]
    ports = usb or ports                                 # skip phantom ttyS* when USB ports exist
    if not ports:
        raise LinkLost("No serial ports found. Is the board's USB cable plugged into this box?")

    reasons = []
    for item in sorted(ports, key=lambda p: p.device):
        ser = None
        try:
            ser = serial_factory(port=None, baudrate=baud, timeout=0.05, write_timeout=1)
            ser.dtr, ser.rts = False, False              # do not reset the board on open
            ser.port = item.device
            ser.open()
            log(f"[PROBE] opened {item.device} @ {baud}")
            time.sleep(0.1)
            probe = Board(ser, "unknown", {}, sequenced=False, log=log)
            probe._drain()
            reply = probe.exchange(STATE_QUERY, probe_timeout_s,
                                   until=lambda text: parse_state(text) is not None)
            parsed = parse_state(reply)
            if parsed is None:
                reasons.append(f"{item.device}: no state reply")
                ser.close()
                continue
            # v2 prints its state over several lines; let the rest arrive.
            reply += probe._read_for(0.15)
            state, style = parse_state(reply)
            sequenced = sequence == "on" or (sequence == "auto" and style == "legacy")
            log(f"[PROBE][PASS] {item.device} style={style} state={state}")
            return Board(ser, style, state, sequenced=sequenced, log=log,
                         packet_interval_s=packet_interval_s)
        except (OSError, serial.SerialException, BoardError) as exc:
            text = str(exc)
            if "Permission denied" in text or "Errno 13" in text:
                text += (" — your user may not use serial ports: sudo usermod -aG dialout $USER, "
                         "then log in again")
            elif "busy" in text.lower() or "Errno 16" in text:
                text += " — another program has this port open (sudo fuser -v " + item.device + ")"
            reasons.append(f"{item.device}: {text}")
            log(f"[PROBE][FAIL] {item.device}: {text}")
            if ser is not None:
                try:
                    ser.close()
                except Exception:                        # noqa: BLE001
                    pass
    raise LinkLost("No board answered #FF@STATE!. " + " | ".join(reasons))
