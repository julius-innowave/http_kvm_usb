"""Frames for the two screenshot routes.

From the HDMI capture card (V4L2 via OpenCV) when one is configured, resized to
the target's screen size; a flat placeholder when not. The spec allows a
placeholder, marked `X-Image-Placeholder: true`, so a box without video still
gets keyboard and mouse.
"""
from __future__ import annotations

import struct
import threading
import time
import zlib

import config


class CaptureUnavailable(Exception):
    """The card is there, the frame is not ready yet. Distinct from "gone"."""


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + tag + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))


def encode_png(width: int, height: int, rgb: bytes) -> bytes:
    """RGB bytes, tightly packed, to a PNG. No filtering: speed over size."""
    stride = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw += rgb[y * stride:(y + 1) * stride]
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + _chunk(b"IEND", b""))


class _Placeholder:
    """A flat frame of the configured size. Built once."""

    def __init__(self) -> None:
        self.rgb = bytes((0x10, 0x14, 0x1C)) * (config.SCREEN_WIDTH * config.SCREEN_HEIGHT)
        self.png = encode_png(config.SCREEN_WIDTH, config.SCREEN_HEIGHT, self.rgb)


class _Capture:
    """The capture card, opened lazily and kept open. OpenCV is optional."""

    def __init__(self, device: str) -> None:
        import cv2                                      # noqa: PLC0415 — optional dependency

        self._cv2 = cv2
        index: int | str = int(device) if device.isdigit() else device
        self._cap = cv2.VideoCapture(index)
        if not self._cap.isOpened():
            raise RuntimeError(f"capture device {device!r} would not open")
        # Pixel format before size: a card's mode is a (format, size, rate)
        # triple, and asking for the size first can pin the wrong one.
        if config.VIDEO_PIXEL in ("MJPG", "YUY2", "YUYV"):
            fourcc = "MJPG" if config.VIDEO_PIXEL == "MJPG" else "YUYV"
            self._cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, config.CAPTURE_WIDTH)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.CAPTURE_HEIGHT)
        if config.VIDEO_FPS:
            self._cap.set(cv2.CAP_PROP_FPS, config.VIDEO_FPS)
        self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self._lock = threading.Lock()

    def rgb(self) -> tuple[int, int, bytes]:
        with self._lock:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                ok, frame = self._cap.read()
                if ok and frame is not None:
                    h, w = frame.shape[:2]
                    if (w, h) != (config.SCREEN_WIDTH, config.SCREEN_HEIGHT):
                        # The screen is the operator's number and the /mouse
                        # coordinate space; the frame is brought to it.
                        frame = self._cv2.resize(
                            frame, (config.SCREEN_WIDTH, config.SCREEN_HEIGHT),
                            interpolation=self._cv2.INTER_AREA
                            if w > config.SCREEN_WIDTH else self._cv2.INTER_LINEAR)
                        h, w = frame.shape[:2]
                    return w, h, self._cv2.cvtColor(frame, self._cv2.COLOR_BGR2RGB).tobytes()
                time.sleep(0.02)
            raise CaptureUnavailable("the capture card produced no frame within 2 seconds")


_placeholder = _Placeholder()
_capture: _Capture | None = None
_capture_error: str | None = None


def _source() -> _Capture | None:
    global _capture, _capture_error
    if not config.VIDEO_DEVICE:
        return None
    if _capture is None and _capture_error is None:
        try:
            _capture = _Capture(config.VIDEO_DEVICE)
        except Exception as exc:                        # noqa: BLE001 — any failure means placeholder
            _capture_error = str(exc)
    return _capture


def capture_note() -> str | None:
    """Why screenshots are placeholders when they should not be, for /status."""
    if config.VIDEO_DEVICE and _capture_error:
        return (f"The capture card at {config.VIDEO_DEVICE!r} could not be opened "
                f"({_capture_error}), so screenshots are placeholders. Keyboard and mouse "
                f"are unaffected.")
    return None


def frame_rgb() -> tuple[int, int, bytes, bool]:
    """(width, height, RGB bytes, is_placeholder)."""
    source = _source()
    if source is None:
        return config.SCREEN_WIDTH, config.SCREEN_HEIGHT, _placeholder.rgb, True
    w, h, rgb = source.rgb()
    return w, h, rgb, False


def frame_png() -> tuple[bytes, bool]:
    """(PNG bytes, is_placeholder)."""
    source = _source()
    if source is None:
        return _placeholder.png, True
    w, h, rgb = source.rgb()
    return encode_png(w, h, rgb), False
