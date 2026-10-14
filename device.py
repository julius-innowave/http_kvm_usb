"""The board behind one lock: what the HTTP routes call.

Adds to `board.Board` what the Remote Adapter API needs:

* a pinned bus (the board must be on USB; `#21@` is never sent after start-up),
* the LOCAL/REMOTE latch respected (409 while LOCAL, unless configured otherwise),
* pixels → absolute reports, text → key reports,
* NOT_READY explained as the USB link to the target, not a generic failure,
* a background thread that reopens a board that stopped answering,
* `/status` answered from cache, so a health check never waits on the port.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import board as board_mod
import config
import keymap
import video
from board import Board, BoardError, LinkLost, NotReady, Refused

NOT_READY_ADVICE = (
    "The board answered NOT_READY: the target computer has not detected the board's USB "
    "keyboard/mouse. The serial link and this adapter are fine; the break is between the "
    "board's USB device port and the target. Check the USB cable from the InnoConnect box "
    "to the target, try another USB port on the target, and look for a new HID keyboard/mouse "
    "in the target's Device Manager (Windows) or lsusb (Linux).")


class AdapterError(Exception):
    """A failure with its HTTP status and API error code already chosen."""

    def __init__(self, status: int, code: str, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.retry_after = status, code, message, retry_after


class DailyLog:
    """Append-only `<dir>/YYYY-MM-DD/debug.txt`, Singapore time, thread-safe."""

    def __init__(self, root: str) -> None:
        self.root = Path(root) if root else None
        self.lock = threading.Lock()

    def write(self, line: str) -> None:
        if self.root is None:
            return
        now = datetime.now(timezone(timedelta(hours=8)))
        stamp = now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond // 1000:03d}"
        with self.lock:
            folder = self.root / now.strftime("%Y-%m-%d")
            folder.mkdir(parents=True, exist_ok=True)
            with (folder / "debug.txt").open("a", encoding="utf-8") as fh:
                fh.write(f"[{stamp}] [{threading.current_thread().name}] {line}\n")


class Device:
    """One board, one serial port, one caller at a time."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._board: Board | None = None
        self._file_log = DailyLog(config.LOG_DIR)
        self._log: list[str] = []
        self._status_text = "not connected yet"
        self._state: dict | None = None          # last #FF@STATE!, for /status
        self._state_read_at = 0.0
        self._firmware: str | None = None
        self._profile_note: str | None = None
        self._not_ready: str | None = None
        self._pointer: tuple[int, int] | None = None
        self._homed = False
        self._held_buttons = 0
        # Start-up makes the first attempt; the watcher waits a full interval.
        self._reconnect_at = time.monotonic() + config.RECONNECT_EVERY_S
        self._stop = threading.Event()
        threading.Thread(target=self._reconnect_watch, name="reconnect", daemon=True).start()

    # ---- plumbing -------------------------------------------------------------

    def note(self, line: str) -> None:
        self._log.append(f"{time.strftime('%H:%M:%S')} {line}")
        del self._log[:-300]
        try:
            self._file_log.write(line)
        except OSError:
            pass

    def recent_log(self) -> list[str]:
        return list(self._log)

    @property
    def connected(self) -> bool:
        return self._board is not None

    def _require(self) -> Board:
        if self._board is None:
            raise AdapterError(503, "device_unreachable",
                               f"The control board is not connected: {self._status_text}", retry_after=5)
        return self._board

    def _drive(self, what: str, fn) -> None:
        """Run one input operation and turn what the board said into an HTTP answer."""
        self._require()
        try:
            fn()
        except NotReady as exc:
            self._not_ready = str(exc)
            self._homed = False
            self.note(f"{what}: {exc}")
            raise AdapterError(502, "device_rejected",
                               f"{NOT_READY_ADVICE} (Board reply to the {what}: {exc})") from exc
        except Refused as exc:
            self.note(f"{what}: refused: {exc}")
            raise AdapterError(502, "device_rejected", f"The board refused the {what}: {exc}") from exc
        except LinkLost as exc:
            self._lose(f"serial link failed during the {what}: {exc}")
            raise AdapterError(503, "device_unreachable",
                               f"The serial link to the board failed during the {what}: {exc}. "
                               f"Reconnecting in the background.", retry_after=5) from exc
        self._not_ready = None

    def _lose(self, reason: str) -> None:
        self.note(reason)
        self._status_text = reason
        if self._board is not None:
            self._board.close()
        self._board = None
        self._reconnect_at = time.monotonic() + 2

    # ---- lifecycle --------------------------------------------------------------

    def connect(self) -> None:
        with self._lock:
            self.disconnect()
            self._status_text = "detecting the control port…"
            try:
                b = board_mod.open_board(config.BAUD, sequence=config.SEQUENCE, log=self.note,
                                         packet_interval_s=config.PACKET_INTERVAL_S)
            except BoardError as exc:
                self._status_text = str(exc)
                raise AdapterError(503, "device_unreachable", str(exc), retry_after=5) from exc

            try:
                if b.state["PROTOCOL"] != "USB":
                    if not config.SWITCH_BUS_AT_STARTUP:
                        raise AdapterError(
                            502, "device_rejected",
                            "The board is on the PS2 bus and this is the USB adapter. It does not "
                            "switch the bus on its own, because a PS/2 target loses its keyboard and "
                            "mouse until it reboots. Move the board to USB deliberately (#11@01! "
                            "then #21@00!), or start with INNO_SWITCH_BUS_AT_STARTUP=true.")
                    b.set_state({"COMM": "LOCAL", "PROTOCOL": "USB"})
                    b.read_state()
                self._firmware = (b.exchange("#FF@FW!", 0.3).strip() or None)
                self._ensure_profile(b)
                if b.read_state() is None:
                    raise AdapterError(503, "device_unreachable",
                                       "The board stopped answering #FF@STATE! after the port was opened.",
                                       retry_after=5)
            except AdapterError as exc:
                b.close()
                self._status_text = exc.message
                raise
            except BoardError as exc:
                b.close()
                self._status_text = f"start-up failed: {exc}"
                raise AdapterError(503, "device_unreachable", self._status_text, retry_after=5) from exc

            self._board = b
            self._state, self._state_read_at = dict(b.state), time.monotonic()
            self._pointer, self._homed, self._held_buttons, self._not_ready = None, False, 0, None
            self._status_text = f"connected on {b.port}"
            self.note(f"connected on {b.port}: firmware={self._firmware!r} style={b.style} "
                      f"sequenced={b.sequenced} state={b.state} mouse={config.MOUSE_MODE}"
                      + (f" ({self._profile_note})" if self._profile_note else ""))

    def _ensure_profile(self, b: Board) -> None:
        """Legacy boards: restore the USB profile that has the absolute mouse."""
        self._profile_note = None
        if config.MOUSE_MODE != "ABS" or not config.SET_PROFILE_AT_STARTUP:
            return
        if b.style != "legacy":
            mode = b.state.get("MS_MODE")
            if mode not in (None, "ABS"):
                self._profile_note = (f"board reports MS_MODE={mode} and this firmware has no "
                                      f"command to change it; absolute moves are not available")
            return
        if b.state.get("COMM") != "REMOTE":
            self._profile_note = "board in LOCAL; USB profile left as it is"
            return
        try:
            b.set_state({"DEV": "MS"})
            b.set_usb_profile("ALL")
            self._profile_note = "USB profile set to ALL"
        except Refused as exc:
            self._profile_note = f"#51@ALL! not confirmed ({exc}); absolute moves may be refused"
        b.read_state()

    def disconnect(self) -> None:
        with self._lock:
            if self._board is not None:
                try:
                    self._release_all(self._board)
                except BoardError:
                    pass
                self._board.close()
            self._board = None
            self._state = None
            self._pointer, self._homed = None, False

    def shutdown(self) -> None:
        self._stop.set()
        self.disconnect()

    def _reconnect_watch(self) -> None:
        while not self._stop.wait(1.0):
            if self._board is not None or time.monotonic() < self._reconnect_at:
                continue
            if not self._lock.acquire(blocking=False):
                continue                                 # a connect or an input is running
            try:
                if self._board is not None:
                    continue
                self._reconnect_at = time.monotonic() + config.RECONNECT_EVERY_S
                try:
                    self.connect()
                except AdapterError as exc:
                    self.note(f"reconnect: {exc.message}")
            finally:
                self._lock.release()

    # ---- state / status -----------------------------------------------------

    def _refresh_state(self) -> dict | None:
        b = self._require()
        try:
            state = b.read_state()
        except LinkLost as exc:
            self._lose(f"serial link failed reading the state: {exc}")
            return None
        if state is not None:
            self._state = dict(state)
        self._state_read_at = time.monotonic()
        return state

    def _ensure_armed(self) -> None:
        """Refuse input while the board is in LOCAL (409, the caller may retry)."""
        if (self._state is None or "COMM" not in self._state
                or time.monotonic() - self._state_read_at > config.LOCAL_CHECK_S):
            self._refresh_state()
        self._require()
        state = self._state or {}
        if state.get("PROTOCOL") not in (None, "USB"):
            raise AdapterError(502, "device_rejected",
                               "The board moved to the PS2 bus while this adapter was running. "
                               "Nothing here sends #21@, so another program is on this serial port.")
        if config.RESPECT_LOCAL and state.get("COMM") == "LOCAL":
            raise AdapterError(
                409, "device_busy",
                "The board is in LOCAL and refuses remote input. Switch it to REMOTE with "
                "POST /_admin/communication {\"mode\":\"REMOTE\"}, or start this adapter with "
                "INNO_RESPECT_LOCAL=false to take control automatically.")

    def status(self) -> dict:
        """What /status answers. Never raises, never waits on a busy port."""
        body = {"width": config.SCREEN_WIDTH, "height": config.SCREEN_HEIGHT,
                "capabilities": config.capabilities(),
                "adapter": config.ADAPTER_NAME, "version": config.ADAPTER_VERSION}
        if self._board is None:
            return {**body, "status": "DISCONNECTED",
                    "message": f"The control board is not connected: {self._status_text}. "
                               f"Retried every {config.RECONNECT_EVERY_S:.0f}s."}
        if time.monotonic() - self._state_read_at > config.STATUS_CACHE_S:
            if self._lock.acquire(blocking=False):
                try:
                    if self._board is not None:
                        self._refresh_state()
                finally:
                    self._lock.release()
        if self._board is None:
            return {**body, "status": "DISCONNECTED", "message": self._status_text}
        state = self._state or {}
        if state.get("PROTOCOL") not in (None, "USB"):
            return {**body, "status": "ERROR",
                    "message": "The board moved to the PS2 bus while this adapter was running."}
        if self._not_ready:
            return {**body, "status": "CONNECTED", "message": NOT_READY_ADVICE}
        if config.RESPECT_LOCAL and state.get("COMM") == "LOCAL":
            return {**body, "status": "CONNECTED",
                    "message": "The board is in LOCAL: remote input is refused until it is "
                               "switched to REMOTE."}
        note = video.capture_note()
        if not note and self._profile_note and "not" in self._profile_note:
            note = self._profile_note
        return {**body, "status": "CONNECTED", **({"message": note} if note else {})}

    # ---- mouse ----------------------------------------------------------------

    def _mouse_latches(self) -> dict:
        abs_mode = config.MOUSE_MODE == "ABS"
        return {"COMM": "REMOTE", "PROTOCOL": "USB", "DEV": "MS",
                "MS_MODE": "ABS" if abs_mode else "REL", "MS_RES": "16BIT" if abs_mode else "8BIT"}

    def _abs_report(self, b: Board, x: int, y: int, buttons: int = 0, wheel: int = 0) -> None:
        hx, hy = config.abs_to_wire(x, y)
        b.send_report(f"#A4@{buttons:02X}{hx:04X}{hy:04X}{wheel & 0xFF:02X}!")

    def _rel_report(self, b: Board, dx: int, dy: int, buttons: int = 0, wheel: int = 0) -> None:
        """Split into ±127 reports; the wheel goes with the first."""
        while True:
            sx = max(-config.REL_MAX_DELTA, min(config.REL_MAX_DELTA, dx))
            sy = max(-config.REL_MAX_DELTA, min(config.REL_MAX_DELTA, dy))
            b.send_report(f"#A1@{buttons:02X}{sx & 0xFF:02X}{sy & 0xFF:02X}{wheel & 0xFF:02X}!")
            dx, dy, wheel = dx - sx, dy - sy, 0
            if not dx and not dy:
                return

    def _move_to(self, x: int, y: int) -> None:
        b = self._require()
        x = max(0, min(int(x), config.SCREEN_WIDTH - 1))
        y = max(0, min(int(y), config.SCREEN_HEIGHT - 1))
        b.set_state(self._mouse_latches())
        if config.MOUSE_MODE == "ABS":
            self._abs_report(b, x, y, self._held_buttons)
            self._pointer = (x, y)
            return
        if not self._homed or self._pointer is None:
            far = -config.REL_MAX_DELTA * config.HOME_STEPS
            self._rel_report(b, far, far)
            self._pointer, self._homed = (0, 0), True
        cx, cy = self._pointer
        dx = round((x - cx) / config.SCALE_X) if config.SCALE_X else x - cx
        dy = round((y - cy) / config.SCALE_Y) if config.SCALE_Y else y - cy
        if dx or dy:
            self._rel_report(b, dx, dy, self._held_buttons)
        self._pointer = (x, y)

    def _buttons(self, mask: int, wheel: int = 0) -> None:
        b = self._require()
        b.set_state(self._mouse_latches())
        if config.MOUSE_MODE == "ABS":
            x, y = self._pointer or (config.SCREEN_WIDTH // 2, config.SCREEN_HEIGHT // 2)
            self._abs_report(b, x, y, mask, wheel)
        else:
            self._rel_report(b, 0, 0, mask, wheel)
        self._held_buttons = mask

    def mouse(self, action: dict) -> None:
        with self._lock:
            self._require()
            self._ensure_armed()
            kind = action["type"]
            button = action.get("button", 1)
            if button not in (1, 2, 3):
                raise AdapterError(422, "unsupported", f"This device has no button {button}.")
            mask = {1: 0b001, 3: 0b010, 2: 0b100}[button]
            x, y = action.get("x", 0), action.get("y", 0)

            def run() -> None:
                if kind == "MOVE":
                    self._move_to(x, y)
                elif kind in ("CLICK", "DOUBLE_CLICK"):
                    count = 2 if kind == "DOUBLE_CLICK" else max(1, int(action.get("clickCount", 1)))
                    self._move_to(x, y)
                    for _ in range(count):
                        self._buttons(self._held_buttons | mask)
                        time.sleep(config.CLICK_HOLD_S)
                        self._buttons(self._held_buttons & ~mask)
                        time.sleep(config.CLICK_GAP_S)
                elif kind == "PRESS":
                    self._move_to(x, y)
                    self._buttons(self._held_buttons | mask)
                elif kind == "RELEASE":
                    self._move_to(x, y)
                    self._buttons(self._held_buttons & ~mask)
                elif kind == "DRAG":
                    self._move_to(action.get("fromX", 0), action.get("fromY", 0))
                    self._buttons(self._held_buttons | mask)
                    time.sleep(config.CLICK_HOLD_S)
                    self._move_to(x, y)
                    time.sleep(config.CLICK_HOLD_S)
                    self._buttons(self._held_buttons & ~mask)
                elif kind == "SCROLL":
                    self._move_to(x, y)
                    notches = int(action.get("scrollDelta", 0))
                    step = -1 if notches > 0 else 1       # spec: negative = up; HID wheel: positive = up
                    for _ in range(abs(notches)):
                        self._buttons(self._held_buttons, wheel=step)
                        time.sleep(config.CLICK_GAP_S)
                else:
                    raise AdapterError(400, "invalid_request", f"Unknown mouse type {kind!r}.")

            self._drive(f"mouse {kind}", run)

    # ---- keyboard ---------------------------------------------------------------

    def _release_all(self, b: Board) -> None:
        """Let go of mouse buttons before switching to the keyboard, and vice versa."""
        if self._held_buttons and b.state.get("DEV") == "MS":
            self._buttons(0)

    def _type_keys(self, keys: list[tuple[int, int]]) -> None:
        b = self._require()
        self._release_all(b)
        b.set_state({"COMM": "REMOTE", "PROTOCOL": "USB", "DEV": "KB"})
        for modifiers, usage in keys:
            b.send_report(keymap.report(modifiers, usage) if usage else keymap.report(modifiers))
            time.sleep(config.KEY_HOLD_S)
            b.send_report(keymap.RELEASE)

    def keyboard(self, action: dict) -> None:
        with self._lock:
            self._require()
            self._ensure_armed()
            kind = action["type"]

            if kind == "TYPE_TEXT":
                text = action.get("text") or ""
                if len(text) > 4096:
                    raise AdapterError(400, "invalid_request", "text is longer than 4096 characters.")
                keys, skipped = keymap.text_keys(text)
                if skipped:
                    self.note(f"TYPE_TEXT: skipped characters the US layout cannot type: {skipped!r}")
                self._drive("text", lambda: self._type_keys(keys))

            elif kind in ("KEY_PRESS", "HOTKEY"):
                key = action.get("key") or ""
                if not key:
                    raise AdapterError(400, "invalid_request", "key is required for KEY_PRESS and HOTKEY.")
                parts = [p for p in ("CTRL" if action.get("ctrl") else "",
                                     "SHIFT" if action.get("shift") else "",
                                     "ALT" if action.get("alt") else "") if p]
                parts += key.split("+") if len(key) > 1 else [key]
                try:
                    modifiers, usages = keymap.parse_combo(parts)
                except KeyError as exc:
                    raise AdapterError(422, "unsupported",
                                       f"The USB keymap has no key named {exc.args[0]!r}.") from exc
                except ValueError as exc:
                    raise AdapterError(400, "invalid_request", str(exc)) from exc

                def run() -> None:
                    b = self._require()
                    self._release_all(b)
                    b.set_state({"COMM": "REMOTE", "PROTOCOL": "USB", "DEV": "KB"})
                    b.send_report(keymap.report(modifiers, *usages))
                    time.sleep(config.KEY_HOLD_S)
                    b.send_report(keymap.RELEASE)

                self._drive("keystroke", run)

            elif kind in ("KEY_DOWN", "KEY_UP"):
                raise AdapterError(422, "unsupported",
                                   "This board releases a USB key on its own after each report, so a "
                                   "key cannot be held down between calls. Use KEY_PRESS or HOTKEY.")
            else:
                raise AdapterError(400, "invalid_request", f"Unknown keyboard type {kind!r}.")

    # ---- maintenance ------------------------------------------------------------

    def board_state(self) -> dict:
        with self._lock:
            b = self._require()
            state = self._refresh_state()
            if state is None:
                raise AdapterError(502, "device_rejected", "The board did not answer #FF@STATE!.")
            return {"state": state, "port": b.port, "firmware": self._firmware,
                    "firmwareStyle": b.style, "sequencedReports": b.sequenced,
                    "mouseMode": config.MOUSE_MODE, "absRange": config.ABS_RANGE,
                    "usbProfile": b.usb_profile, "profileNote": self._profile_note,
                    "lastNotReady": self._not_ready, "pointer": self._pointer}

    def communication(self, mode: str) -> dict:
        mode = mode.upper()
        if mode not in {"LOCAL", "REMOTE"}:
            raise AdapterError(400, "invalid_request", "mode must be LOCAL or REMOTE.")
        with self._lock:
            b = self._require()
            self._drive("mode switch", lambda: b.set_state({"COMM": mode}))
            self._state, self._state_read_at = dict(b.state), time.monotonic()
            return {"communication": mode}

    def usb_profile(self, profile: str) -> dict:
        profile = profile.upper()
        if profile not in {"REL", "ALL"}:
            raise AdapterError(400, "invalid_request", "profile must be REL or ALL.")
        with self._lock:
            b = self._require()

            def run() -> None:
                b.set_state({"COMM": "REMOTE", "PROTOCOL": "USB", "DEV": "MS"})
                b.set_usb_profile(profile)

            self._drive("USB profile change", run)
            self._homed = False
            return {"profile": b.usb_profile}

    def device_command(self, action: str) -> dict:
        commands = {"firmware": "#FF@FW!", "debug_on": "#FF@DBG1!", "debug_off": "#FF@DBG0!"}
        if action not in commands:
            raise AdapterError(400, "invalid_request", f"action must be one of {', '.join(commands)}.")
        return self.raw(commands[action])

    def raw(self, command: str) -> dict:
        """One command straight through. `#21@` is refused: the bus is pinned."""
        command = command.strip()
        if not (command.startswith("#") and command.endswith("!") and "@" in command
                and command.isascii() and len(command) <= 31):
            raise AdapterError(400, "invalid_request", "Command format: #XX@data!, ASCII, at most 31 bytes.")
        if command[1:3] == "21":
            raise AdapterError(422, "unsupported",
                               "Refusing #21@: the bus is pinned while the adapter runs. Stop the "
                               "adapter and switch it deliberately.")
        with self._lock:
            b = self._require()
            reply = ""

            def run() -> None:
                nonlocal reply
                reply = b.exchange(command, 1.0)

            self._drive("raw command", run)
            b.state.clear()                              # latches may have moved
            self._state_read_at = 0.0
            self._homed = False
            return {"command": command, "reply": reply}


device = Device()
