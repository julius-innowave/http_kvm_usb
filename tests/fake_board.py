"""A simulated KM board behind a fake serial port, for tests without hardware.

`FakeFirmware(style)` answers like the real firmwares:
  legacy — one-line state, #REPLY@STATE…, sequenced reports with #ACK@
  v2     — multi-line state, [INFO][SYS] replies, unsequenced reports echoed
           as [USB_ABSMS16] / [USB_KEYBOARD] debug lines (as seen on TSMC-IC-01)
"""
from __future__ import annotations

import re
import threading


class FakeFirmware:
    def __init__(self, style: str = "v2") -> None:
        self.style = style
        self.s = {"PROTOCOL": "USB", "COMM": "REMOTE", "DEV": "KB", "MS_MODE": "ABS", "MS_RES": "16BIT"}
        self.usb_ready = True
        self.profile = "ALL"
        self.hid: list[str] = []          # accepted reports, without sequence numbers
        self.commands: list[str] = []     # every command received
        self._seen: set[tuple[str, str]] = set()

    # ---- state -------------------------------------------------------------

    def state_text(self) -> str:
        s = self.s
        if self.style == "v2":
            out = (f"Protocol: {s['PROTOCOL']}\r\n"
                   f"State: {'Remote mode' if s['COMM'] == 'REMOTE' else 'Local control'}\r\n")
            if s["COMM"] == "REMOTE":
                out += f"Input device: {'mouse' if s['DEV'] == 'MS' else 'keyboard'}\r\n"
                out += (f"Mouse mode: {'absolute' if s['MS_MODE'] == 'ABS' else 'relative'}\r\n"
                        f"Mouse mode: {s['MS_RES'][:-3]}-bit\r\n")
            return out
        line = f"State: {s['PROTOCOL']},{s['COMM']},{'KEYBOARD' if s['DEV'] == 'KB' else 'MOUSE'}"
        if s["DEV"] == "MS":
            line += ",ABSOLUTE" if s["MS_MODE"] == "ABS" else ",RELATIVE"
            if s["PROTOCOL"] == "USB":
                line += "," + s["MS_RES"]
        return line + "\r\n"

    # ---- commands ----------------------------------------------------------

    def handle(self, cmd: str) -> str:
        self.commands.append(cmd)
        m = re.fullmatch(r"#(..)@(.*)!", cmd)
        if not m:
            return "[ERROR] FormatError\r\n"
        op, data = m.groups()
        if op == "FF":
            if data == "STATE":
                return self.state_text()
            if data == "FW":
                return ("INNOKVM Firmware: 18.03.2026\r\n" if self.style == "v2" else "Firmware: 0.9\r\n")
            return "Debug printing disabled.\r\n" if data == "DBG0" else "Debug printing enabled.\r\n"
        return self._v2(op, data) if self.style == "v2" else self._legacy(op, data)

    _LATCHES = {"11": ("COMM", "REMOTE", "LOCAL"), "21": ("PROTOCOL", "USB", "PS2"),
                "31": ("DEV", "KB", "MS"), "41": ("MS_MODE", "ABS", "REL"), "51": ("MS_RES", "8BIT", "16BIT")}

    def _legacy(self, op: str, data: str) -> str:
        long = {"KB": "KEYBOARD", "MS": "MOUSE", "ABS": "ABSOLUTE", "REL": "RELATIVE"}
        if op == "51" and data in ("ALL", "REL"):
            self.profile = data
            if data == "REL":
                self.s["MS_MODE"] = "REL"
            return f"#REPLY@USB,PROFILE,{data},OK!\r\n"
        if op in self._LATCHES and data in ("00", "01"):
            key, a, b = self._LATCHES[op]
            old, new = self.s[key], (a if data == "00" else b)
            self.s[key] = new
            tail = ",UNCHANGED" if old == new else ""
            return f"#REPLY@STATE,{long.get(old, old)}->{long.get(new, new)},OK{tail}!\r\n"
        if op in ("A1", "A4", "B1"):
            if "," not in data:
                return f"#REPLY@{'KEYBOARD' if op == 'B1' else 'MOUSE'},RECEIVED,{op},{data}!\r\n"
            seq, payload = data.split(",", 1)
            kind = "KEYBOARD" if op == "B1" else "MOUSE"
            if self.s["COMM"] != "REMOTE":
                return "[ERROR] Mouse movements invalid in local mode.\r\n"
            if not self.usb_ready:
                return f"#REPLY@{kind},NOT_READY,{op},{seq},{payload}!\r\n#NACK@{seq},NOT_READY!\r\n"
            need = {"A1": ("MS", "REL", "8BIT"), "A4": ("MS", "ABS", "16BIT")}.get(op)
            if (op == "B1" and self.s["DEV"] != "KB") or (need and (
                    self.s["DEV"], self.s["MS_MODE"], self.s["MS_RES"]) != need):
                return "[ERROR] CommandTypeError: Invalid command in this state\r\n"
            if op == "A4" and self.profile != "ALL":
                return f"#REPLY@MOUSE,FORMAT,{op}!\r\n"
            if (op, seq) not in self._seen:
                self._seen.add((op, seq))
                self.hid.append(f"{op}@{payload}")
            return f"#ACK@{seq}!\r\n"
        return f"#REPLY@{op},FORMAT!\r\n"

    def _v2(self, op: str, data: str) -> str:
        if op in ("11", "21", "31") and data in ("00", "01"):
            key, a, b = self._LATCHES[op]
            new = a if data == "00" else b
            if self.s[key] == new:
                return ""                                 # v2 is silent when nothing changes
            self.s[key] = new
            name = {"COMM": "COMMUNICATION", "PROTOCOL": "PROTOCOL", "DEV": "INPUT_DEVICE"}[key]
            shown = {"MS": "MOUSE", "KB": "KEYBOARD"}.get(new, new)
            return f"[INFO][SYS] {name}_CHANGE: {shown}\r\n"
        if op in ("A1", "A4", "B1"):
            if "," in data:
                return "[ERROR] FormatError: unexpected sequence number\r\n"
            if self.s["COMM"] != "REMOTE":
                return "[ERROR] Mouse movements invalid in local mode.\r\n"
            kind = "KEYBOARD" if op == "B1" else "MOUSE"
            if not self.usb_ready:
                return f"#REPLY@{kind},NOT_READY,{op},{data}!\r\n"
            if (op == "B1") != (self.s["DEV"] == "KB"):
                return "[ERROR] CommandTypeError: Invalid command in this state\r\n"
            self.hid.append(f"{op}@{data}")
            if op == "A4":
                return f"[USB_ABSMS16] 06,{data[0:2]},{data[2:6]},{data[6:10]},{data[10:12]}\r\n"
            return f"[USB_KEYBOARD] {data}\r\n"
        return "[ERROR] Unknown command\r\n"


class FakePortInfo:
    def __init__(self, device: str, vid: int | None = 0x1A86) -> None:
        self.device, self.vid = device, vid


class FakeSerial:
    """Enough of pyserial.Serial for board.py. One firmware per port path."""

    firmwares: dict[str, FakeFirmware] = {}
    denied: set[str] = set()

    def __init__(self, port=None, baudrate=115200, timeout=None, write_timeout=None) -> None:
        self.port, self.dtr, self.rts = port, True, True
        self.buf = b""
        self.lock = threading.Lock()
        self.fw: FakeFirmware | None = None

    def open(self) -> None:
        if self.port in self.denied:
            raise OSError(13, f"could not open port {self.port}: [Errno 13] Permission denied: '{self.port}'")
        self.fw = self.firmwares.get(self.port)

    def close(self) -> None:
        pass

    def write(self, data: bytes) -> int:
        for line in data.decode().split("\r\n"):
            if line and self.fw is not None:
                reply = self.fw.handle(line)
                with self.lock:
                    self.buf += reply.encode()
        return len(data)

    def flush(self) -> None:
        pass

    @property
    def in_waiting(self) -> int:
        return len(self.buf)

    def read(self, n: int = 1) -> bytes:
        with self.lock:
            out, self.buf = self.buf[:n], self.buf[n:]
        return out
