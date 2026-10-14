"""HTTP KVM over the board's USB bus: the five routes of the Remote Adapter API.

ICR's stock `http-kvm` provider calls these; they call `device`, which owns
the serial port.

* **A 200 means the action finished on the device**, not that it was queued.
* **The error body is what the operator reads.** `errorMessage` is shown
  verbatim, so a NOT_READY from the board arrives as an instruction about the
  USB cable rather than a generic failure.
"""
from __future__ import annotations

import contextlib
import logging

from fastapi import Depends, FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import config
import video
from device import AdapterError, device

log = logging.getLogger("http_kvm_usb")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-5s %(name)s %(message)s")


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    # A missing board must not stop the adapter from starting: /status answers
    # DISCONNECTED with the reason, and the background thread keeps retrying.
    try:
        device.connect()
    except AdapterError as exc:
        log.warning("start-up: %s", exc.message)
    yield
    device.shutdown()


app = FastAPI(title=config.ADAPTER_NAME, version=config.ADAPTER_VERSION,
              lifespan=lifespan, docs_url="/docs", openapi_url="/openapi.json")


# ---- auth ---------------------------------------------------------------


async def authorize(request: Request) -> None:
    """Whatever header the operator was told to enter. Blank key disables it."""
    if not config.API_KEY:
        return
    if request.headers.get(config.API_KEY_HEADER) != config.API_KEY:
        raise AdapterError(401, "unauthorized",
                           f"This adapter requires the {config.API_KEY_HEADER} request header. "
                           f"Add it to the session's request headers in the registry.")


# ---- error envelope -----------------------------------------------------


def _error(status: int, code: str, message: str, retry_after: int | None = None) -> JSONResponse:
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return JSONResponse(status_code=status, headers=headers,
                        content={"errorMessage": message, "code": code})


@app.exception_handler(AdapterError)
async def _adapter_error(_: Request, exc: AdapterError) -> JSONResponse:
    log.warning("%s %s: %s", exc.status, exc.code, exc.message)
    return _error(exc.status, exc.code, exc.message, exc.retry_after)


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    detail = "; ".join(f"{'.'.join(str(p) for p in e['loc'][1:])}: {e['msg']}"
                       for e in jsonable_encoder(exc.errors()))
    return _error(400, "invalid_request", f"The request body is not valid: {detail}")


@app.exception_handler(video.CaptureUnavailable)
async def _capture_unavailable(_: Request, exc: video.CaptureUnavailable) -> JSONResponse:
    # Not device_unreachable: "frame not ready" must not look like "device gone".
    return _error(503, "capture_unavailable", str(exc), retry_after=1)


@app.exception_handler(Exception)
async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled")
    return _error(502, "device_rejected", f"The adapter failed to drive the board: {exc}")


# ---- bodies (unknown fields ignored, per spec) --------------------------


class MouseAction(BaseModel):
    type: str
    x: int = 0
    y: int = 0
    button: int = 1
    clickCount: int = 1
    fromX: int = 0
    fromY: int = 0
    scrollDelta: int = 0


class KeyboardAction(BaseModel):
    type: str
    key: str | None = None
    text: str | None = None
    ctrl: bool = False
    shift: bool = False
    alt: bool = False
    holdTimeoutMs: int = Field(default=2000, ge=0)


# ---- routes -------------------------------------------------------------


@app.get("/status", dependencies=[Depends(authorize)])
def status() -> dict:
    """200 whenever the adapter is running; answers from cache, never blocks."""
    return device.status()


@app.get("/screenshot", dependencies=[Depends(authorize)])
def screenshot() -> Response:
    png, placeholder = video.frame_png()
    return Response(content=png, media_type="image/png",
                    headers={"X-Image-Placeholder": "true" if placeholder else "false"})


@app.get("/screenshot/raw", dependencies=[Depends(authorize)])
def screenshot_raw() -> Response:
    width, height, rgb, placeholder = video.frame_rgb()
    return Response(content=rgb, media_type="application/octet-stream", headers={
        "X-Image-Width": str(width),
        "X-Image-Height": str(height),
        "X-Image-Format": "RGB",
        "X-Image-Bytes-Per-Pixel": "3",
        "X-Image-Placeholder": "true" if placeholder else "false",
    })


@app.post("/mouse", status_code=200, dependencies=[Depends(authorize)])
def mouse(action: MouseAction) -> Response:
    device.mouse(action.model_dump())
    return Response(status_code=200)


@app.post("/keyboard", status_code=200, dependencies=[Depends(authorize)])
def keyboard(action: KeyboardAction) -> Response:
    try:
        device.keyboard(action.model_dump())
    except ValueError as exc:
        raise AdapterError(400, "invalid_request", str(exc)) from exc
    return Response(status_code=200)


# ---- maintenance: not the spec, not reachable from the engine -----------


class UsbProfile(BaseModel):
    profile: str


class Communication(BaseModel):
    mode: str


class DeviceCommand(BaseModel):
    action: str


class RawCommand(BaseModel):
    command: str


@app.get("/_admin/state", dependencies=[Depends(authorize)])
def admin_state() -> dict:
    return device.board_state()


@app.post("/_admin/usb-profile", dependencies=[Depends(authorize)])
def admin_usb_profile(body: UsbProfile) -> dict:
    """`REL` drops the absolute-mouse interface, `ALL` restores it. RAM only."""
    return device.usb_profile(body.profile)


@app.post("/_admin/communication", dependencies=[Depends(authorize)])
def admin_communication(body: Communication) -> dict:
    """Move the local/remote latch by hand — the engine has no way to."""
    return device.communication(body.mode)


@app.post("/_admin/device-command", dependencies=[Depends(authorize)])
def admin_device_command(body: DeviceCommand) -> dict:
    return device.device_command(body.action)


@app.post("/_admin/raw", dependencies=[Depends(authorize)])
def admin_raw(body: RawCommand) -> dict:
    return device.raw(body.command)


@app.get("/_admin/log", dependencies=[Depends(authorize)])
def admin_log() -> dict:
    """The last serial lines in memory. The full history is in INNO_LOG_DIR."""
    return {"lines": device.recent_log()}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT)
