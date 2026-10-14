"""End-to-end tests of the HTTP API against simulated boards (no hardware).

    .venv/bin/pip install -r requirements.txt
    .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.update({
    "INNO_VIDEO_DEVICE": "none",
    "INNO_LOG_DIR": tempfile.mkdtemp(prefix="http_kvm_usb_test_"),
    "INNO_RECONNECT_EVERY_S": "3600",
    "INNO_ENV_FILE": "/nonexistent",
    "INNO_SCREEN_WIDTH": "1920",
    "INNO_SCREEN_HEIGHT": "1080",
    "INNO_API_KEY": "",
    "INNO_PACKET_INTERVAL_S": "0",
    "INNO_CLICK_HOLD_S": "0", "INNO_CLICK_GAP_S": "0", "INNO_KEY_HOLD_S": "0",
})
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import board  # noqa: E402
import config  # noqa: E402
import device as device_mod  # noqa: E402
from fake_board import FakeFirmware, FakePortInfo, FakeSerial  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as app_mod  # noqa: E402

board.serial_factory = FakeSerial
board.list_candidate_ports = lambda: [FakePortInfo("/dev/ttyACM0"), FakePortInfo("/dev/ttyACM1")]
RELEASE = "B1@0000000000000000"


def downs(hid: list[str]) -> list[str]:
    return [h for h in hid if h.startswith("B1@") and h != RELEASE]


class _Common:
    STYLE = "v2"

    @classmethod
    def setUpClass(cls):
        cls.fw = FakeFirmware(cls.STYLE)
        FakeSerial.firmwares = {"/dev/ttyACM1": cls.fw}       # ACM0 stays silent
        FakeSerial.denied = set()
        device_mod.device._stop.clear()
        cls.client = TestClient(app_mod.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        self.fw.usb_ready = True
        self.fw.s["COMM"] = "REMOTE"
        self.fw.hid.clear()
        device_mod.device._state_read_at = 0.0           # re-read latches before each test

    def post(self, path, body):
        return self.client.post(path, json=body)

    # ---- tests ---------------------------------------------------------------

    def test_status_connected(self):
        st = self.client.get("/status").json()
        self.assertEqual(st["status"], "CONNECTED", st)
        self.assertNotIn("KEY_HOLD", st["capabilities"])

    def test_firmware_detected(self):
        st = self.client.get("/_admin/state").json()
        self.assertEqual(st["firmwareStyle"], self.STYLE)
        self.assertEqual(st["sequencedReports"], self.STYLE == "legacy")
        self.assertEqual(st["port"], "/dev/ttyACM1")

    def test_move_corners_twos_complement(self):
        self.assertEqual(self.post("/mouse", {"type": "MOVE", "x": 0, "y": 0}).status_code, 200)
        self.assertEqual(self.post("/mouse", {"type": "MOVE", "x": 1919, "y": 1079}).status_code, 200)
        self.assertEqual(self.fw.hid[-2:], ["A4@008001800100", "A4@007FFF7FFF00"])

    def test_click_is_move_press_release(self):
        r = self.post("/mouse", {"type": "CLICK", "x": 960, "y": 540})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([h[3:5] for h in self.fw.hid], ["00", "01", "00"])

    def test_scroll_up_is_positive_wheel(self):
        r = self.post("/mouse", {"type": "SCROLL", "x": 960, "y": 540, "scrollDelta": -2})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([h[-2:] for h in self.fw.hid[-2:]], ["01", "01"])

    def test_type_text_each_key_once(self):
        r = self.post("/keyboard", {"type": "TYPE_TEXT", "text": "Hi!"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(downs(self.fw.hid),
                         ["B1@02000B0000000000", "B1@00000C0000000000", "B1@02001E0000000000"])

    def test_type_text_skips_unsupported(self):
        r = self.post("/keyboard", {"type": "TYPE_TEXT", "text": "a€b"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(downs(self.fw.hid), ["B1@0000040000000000", "B1@0000050000000000"])

    def test_hotkey_ctrl_c(self):
        r = self.post("/keyboard", {"type": "HOTKEY", "key": "c", "ctrl": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(downs(self.fw.hid), ["B1@0100060000000000"])

    def test_combo_string(self):
        r = self.post("/keyboard", {"type": "KEY_PRESS", "key": "ctrl+alt+delete"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(downs(self.fw.hid), ["B1@05004C0000000000"])

    def test_win_alone(self):
        r = self.post("/keyboard", {"type": "KEY_PRESS", "key": "meta"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(downs(self.fw.hid), ["B1@0800000000000000"])

    def test_key_down_unsupported(self):
        self.assertEqual(self.post("/keyboard", {"type": "KEY_DOWN", "key": "a"}).status_code, 422)

    def test_unknown_key(self):
        self.assertEqual(self.post("/keyboard", {"type": "KEY_PRESS", "key": "nosuchkey"}).status_code, 422)

    def test_mouse_then_keyboard_then_mouse(self):
        self.assertEqual(self.post("/mouse", {"type": "MOVE", "x": 10, "y": 10}).status_code, 200)
        self.assertEqual(self.post("/keyboard", {"type": "KEY_PRESS", "key": "enter"}).status_code, 200)
        self.assertEqual(self.post("/mouse", {"type": "MOVE", "x": 20, "y": 20}).status_code, 200)
        self.assertEqual(self.fw.s["DEV"], "MS")

    def test_not_ready_explained_and_recovers(self):
        self.fw.usb_ready = False
        r = self.post("/mouse", {"type": "MOVE", "x": 5, "y": 5})
        self.assertEqual(r.status_code, 502)
        self.assertIn("USB cable", r.json()["errorMessage"])
        st = self.client.get("/status").json()
        self.assertEqual(st["status"], "CONNECTED")
        self.assertIn("NOT_READY", st["message"])
        self.fw.usb_ready = True
        self.assertEqual(self.post("/mouse", {"type": "MOVE", "x": 6, "y": 6}).status_code, 200)
        self.assertNotIn("message", self.client.get("/status").json())

    def test_local_refused_then_remote(self):
        self.assertEqual(self.post("/_admin/communication", {"mode": "LOCAL"}).status_code, 200)
        self.assertEqual(self.post("/mouse", {"type": "MOVE", "x": 1, "y": 1}).status_code, 409)
        self.assertEqual(self.post("/_admin/communication", {"mode": "REMOTE"}).status_code, 200)
        self.assertEqual(self.post("/mouse", {"type": "MOVE", "x": 2, "y": 2}).status_code, 200)

    def test_raw(self):
        self.assertEqual(self.post("/_admin/raw", {"command": "#21@01!"}).status_code, 422)
        r = self.post("/_admin/raw", {"command": "#FF@FW!"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("Firmware", r.json()["reply"])
        self.assertEqual(self.post("/mouse", {"type": "MOVE", "x": 3, "y": 3}).status_code, 200)

    def test_screenshot_placeholder_size(self):
        r = self.client.get("/screenshot/raw")
        self.assertEqual(len(r.content), 1920 * 1080 * 3)
        self.assertEqual(r.headers["x-image-placeholder"], "true")


class V2Firmware(_Common, unittest.TestCase):
    STYLE = "v2"


class LegacyFirmware(_Common, unittest.TestCase):
    STYLE = "legacy"

    def test_profile_restored_at_startup(self):
        self.assertIn("#51@ALL!", self.fw.commands)

    def test_relative_mode(self):
        config.MOUSE_MODE = "REL"
        try:
            device_mod.device._homed = False
            r = self.post("/mouse", {"type": "MOVE", "x": 100, "y": 50})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(self.fw.hid[0], "A1@00818100")      # homing: -127, -127
            self.assertEqual(self.fw.hid[-1], "A1@00643200")     # +100, +50
        finally:
            config.MOUSE_MODE = "ABS"
            self.post("/mouse", {"type": "MOVE", "x": 0, "y": 0})


class StartupRefusals(unittest.TestCase):
    def test_ps2_board_refused_without_switching(self):
        fw = FakeFirmware("v2")
        fw.s.update(PROTOCOL="PS2", COMM="LOCAL")
        FakeSerial.firmwares = {"/dev/ttyACM1": fw}
        with self.assertRaises(device_mod.AdapterError) as ctx:
            device_mod.device.connect()
        self.assertIn("PS2", ctx.exception.message)
        self.assertEqual(fw.s["PROTOCOL"], "PS2")
        self.assertFalse(any(c.startswith("#21@") for c in fw.commands))

    def test_permission_denied_is_explained(self):
        FakeSerial.firmwares = {}
        FakeSerial.denied = {"/dev/ttyACM0", "/dev/ttyACM1"}
        try:
            with self.assertRaises(device_mod.AdapterError):
                device_mod.device.connect()
            msg = device_mod.device.status()["message"]
            self.assertIn("Permission denied", msg)
            self.assertIn("dialout", msg)
        finally:
            FakeSerial.denied = set()


if __name__ == "__main__":
    unittest.main()
