from __future__ import annotations

import datetime
import logging

from fastapi import Body, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from common.procutil import ValidationError, validate_busid
from common.security import (
    PasswordTooLongError,
    generate_token,
    hash_password,
    hash_token,
    verify_password,
    verify_token,
)
from common.webauth import LoginThrottle, require_safe_ajax, require_session_user

from . import config
from .usb_devices import bind_device, list_local_devices, unbind_device
from .usbipd_supervisor import UsbipdSupervisor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("usbip.server")

app = FastAPI(title="USB/IP Server")
app.add_middleware(
    SessionMiddleware,
    secret_key=config.store.read()["session_secret"],
    same_site="lax",
    https_only=config.HTTPS_ONLY_COOKIE,
    max_age=60 * 60 * 12,
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

throttle = LoginThrottle()
supervisor = UsbipdSupervisor(port=config.USBIPD_PORT)


@app.on_event("startup")
async def on_startup():
    await supervisor.start()


@app.on_event("shutdown")
async def on_shutdown():
    await supervisor.stop()


def _has_admin() -> bool:
    return bool(config.store.read()["admin_password_hash"])


async def require_bearer_or_session(request: Request) -> str:
    """API auth for /api/devices: either a logged-in browser session
    (server's own web UI) or a valid client bearer token."""
    user = request.session.get("user")
    if user:
        return "session:" + user
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        cfg = config.store.read()
        if verify_token(token, cfg["token_salt"], cfg["token_hash"]):
            return "client-token"
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")


# ---------------------------------------------------------------- web pages

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not _has_admin():
        return RedirectResponse("/setup")
    if not request.session.get("user"):
        return RedirectResponse("/login")
    cfg = config.store.read()
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "has_token": bool(cfg["token_hash"])},
    )


@app.get("/setup", response_class=HTMLResponse)
async def setup_form(request: Request):
    if _has_admin():
        return RedirectResponse("/login")
    return templates.TemplateResponse("setup.html", {"request": request, "error": None})


@app.post("/setup", response_class=HTMLResponse)
async def setup_submit(request: Request):
    if _has_admin():
        return RedirectResponse("/login")
    form = await request.form()
    password = (form.get("password") or "").strip()
    confirm = (form.get("confirm") or "").strip()
    if len(password) < 10:
        return templates.TemplateResponse(
            "setup.html",
            {"request": request, "error": "Password must be at least 10 characters."},
        )
    if password != confirm:
        return templates.TemplateResponse(
            "setup.html", {"request": request, "error": "Passwords do not match."}
        )
    try:
        password_hash = hash_password(password)
    except PasswordTooLongError as e:
        return templates.TemplateResponse("setup.html", {"request": request, "error": str(e)})
    config.store.update(lambda d: d.update(admin_password_hash=password_hash))
    request.session["user"] = "admin"
    return RedirectResponse("/", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if not _has_admin():
        return RedirectResponse("/setup")
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request):
    client_ip = request.client.host if request.client else "unknown"
    throttle.check(client_ip)
    form = await request.form()
    password = (form.get("password") or "").strip()
    cfg = config.store.read()
    if verify_password(password, cfg["admin_password_hash"] or ""):
        throttle.reset(client_ip)
        request.session["user"] = "admin"
        return RedirectResponse("/", status_code=303)
    throttle.record_failure(client_ip)
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "Invalid password."}
    )


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ------------------------------------------------------------------- API

@app.get("/api/health")
async def health():
    return {"status": "ok", "usbipd_running": supervisor.is_running()}


@app.get("/api/info")
async def api_info():
    cfg = config.store.read()
    return {"name": cfg["server_name"], "usbip_port": config.USBIPD_PORT}


@app.post("/api/settings/name")
async def api_set_name(request: Request, user=Depends(require_session_user), body: dict = Body(...)):
    require_safe_ajax(request)
    name = str(body.get("name", "")).strip()[:60] or "usbip-server"
    config.store.update(lambda d: d.update(server_name=name))
    return {"ok": True, "name": name}


@app.get("/api/devices")
async def api_list_devices(_=Depends(require_bearer_or_session)):
    cfg = config.store.read()
    devices = list_local_devices(labels=cfg.get("device_labels", {}))
    return {"devices": [d.to_dict() for d in devices]}


@app.post("/api/devices/{busid}/share")
async def api_share(busid: str, request: Request, user=Depends(require_session_user)):
    require_safe_ajax(request)
    try:
        validate_busid(busid)
        bind_device(busid)
    except ValidationError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    except RuntimeError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    return {"ok": True}


@app.post("/api/devices/{busid}/unshare")
async def api_unshare(busid: str, request: Request, user=Depends(require_session_user)):
    require_safe_ajax(request)
    try:
        validate_busid(busid)
        unbind_device(busid)
    except ValidationError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    except RuntimeError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    return {"ok": True}


@app.post("/api/devices/{busid}/label")
async def api_label(
    busid: str,
    request: Request,
    user=Depends(require_session_user),
    body: dict = Body(...),
):
    require_safe_ajax(request)
    validate_busid(busid)
    label = str(body.get("label", ""))[:80]
    config.store.update(lambda d: d["device_labels"].__setitem__(busid, label))
    return {"ok": True}


@app.get("/api/token/status")
async def api_token_status(request: Request, user=Depends(require_session_user)):
    cfg = config.store.read()
    return {
        "has_token": bool(cfg["token_hash"]),
        "created_at": cfg["token_created_at"],
    }


@app.post("/api/token/rotate")
async def api_token_rotate(request: Request, user=Depends(require_session_user)):
    require_safe_ajax(request)
    cfg = config.store.read()
    new_token = generate_token()
    token_hash = hash_token(new_token, cfg["token_salt"])
    now = datetime.datetime.utcnow().isoformat() + "Z"
    config.store.update(
        lambda d: d.update(token_hash=token_hash, token_created_at=now)
    )
    # Plaintext token is returned exactly once; only its hash is persisted.
    return {"token": new_token, "created_at": now}


@app.get("/healthz")
async def healthz():
    return JSONResponse({"status": "ok"})
