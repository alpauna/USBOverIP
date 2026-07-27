from __future__ import annotations

import asyncio
import datetime
import hmac
import logging
import uuid

import httpx
from fastapi import Body, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from common.procutil import ValidationError, validate_busid, validate_hostname, validate_port_number
from common.security import (
    PasswordTooLongError,
    generate_token,
    hash_password,
    verify_password,
)
from common.webauth import LoginThrottle, require_safe_ajax, require_session_user

from . import config
from .events import EVENTS, log_event
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
    await _ensure_shared_devices_bound(force=True)
    app.state.watchdog_task = asyncio.create_task(_watchdog_loop())


@app.on_event("shutdown")
async def on_shutdown():
    await supervisor.stop()
    task = getattr(app.state, "watchdog_task", None)
    if task:
        task.cancel()


async def _ensure_shared_devices_bound(force: bool) -> None:
    """Make sure every device the admin has shared is actually bound right
    now, relocating it automatically if its busid changed.

    `force=True` (startup): re-binds everything and notifies unconditionally
    - a container/daemon restart can lose client TCP sessions even when the
    kernel-level bind survives, so "already bound" isn't a reason to skip
    the notification.

    `force=False` (the recurring watchdog): only touches devices that
    aren't currently fine, so a healthy system doesn't generate rebind
    calls or log noise every tick.

    Busid relocation: if a tracked busid is missing entirely, we look up
    the serial we last saw there (known_serials) and search current
    devices for a match. A whole-bus USB renumbering (seen in the wild
    after a host-level USB reset) can shift every device to a different
    busid with no warning even though nothing physically changed -
    without this, that requires an admin to notice and remap everything
    by hand. shared_devices/device_labels are corrected in place so the
    fix sticks."""
    cfg = config.store.read()
    shared = list(cfg.get("shared_devices", []))
    if not shared:
        return

    current = list_local_devices()
    by_busid = {d.busid: d for d in current}
    by_serial = {d.serial: d for d in current if d.serial}
    known_serials = cfg.get("known_serials", {})

    renames: dict[str, str] = {}
    for busid in shared:
        if busid in by_busid:
            continue
        remembered = known_serials.get(busid)
        moved = by_serial.get(remembered) if remembered else None
        if moved and moved.busid not in shared:
            log_event(f"{busid} not found; relocated to {moved.busid} (same serial {remembered})")
            renames[busid] = moved.busid

    if renames:
        def _apply_renames(d):
            labels = d.setdefault("device_labels", {})
            slist = d.setdefault("shared_devices", [])
            for old, new in renames.items():
                if old in slist:
                    slist.remove(old)
                if new not in slist:
                    slist.append(new)
                if old in labels and new not in labels:
                    labels[new] = labels.pop(old)

        cfg = config.store.update(_apply_renames)
        shared = list(cfg.get("shared_devices", []))
        by_busid = {d.busid: d for d in list_local_devices()}

    for busid in shared:
        dev = by_busid.get(busid)
        if not force and dev and dev.status in ("shared_idle", "shared_in_use"):
            continue  # already fine, leave it alone (watchdog mode)
        try:
            bind_device(busid)
            if force:
                log_event(f"rebound {busid} on startup")
        except RuntimeError as e:
            log_event(f"{'bind for ' + busid + ' on startup' if force else 'watchdog: bind for ' + busid} failed: {e}")
            continue
        refreshed = {d.busid: d for d in list_local_devices()}
        dev = refreshed.get(busid)
        if dev and dev.status in ("shared_idle", "shared_in_use"):
            if not force:
                log_event(f"watchdog: rebound {busid} (was unbound while server was running)")
            await notify_clients_device_available(busid, serial=dev.serial)
        else:
            log_event(f"could not confirm {busid} is shared after bind attempt")

    # Refresh the serial snapshot for everything currently tracked, so a
    # future relocation has an up-to-date "last seen serial" to search by.
    final = {d.busid: d for d in list_local_devices()}
    new_known = {b: s for b, s in known_serials.items() if b in shared}
    dirty = new_known != known_serials
    for busid in shared:
        dev = final.get(busid)
        if dev and dev.serial and new_known.get(busid) != dev.serial:
            new_known[busid] = dev.serial
            dirty = True
    if dirty:
        config.store.update(lambda d: d.update(known_serials=new_known))


async def _watchdog_loop(interval: int = 15) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await _ensure_shared_devices_bound(force=False)
        except Exception:
            logger.exception("watchdog tick failed")


def _now() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def _get_device_serial(busid: str) -> str | None:
    for d in list_local_devices():
        if d.busid == busid:
            return d.serial or None
    return None


def _public_client(c: dict) -> dict:
    return {
        "id": c["id"],
        "name": c["name"],
        "host": c["host"],
        "api_port": c["api_port"],
        "created_at": c["created_at"],
        "last_seen": c.get("last_seen"),
        "has_token": bool(c.get("token")),
    }


async def notify_clients_device_available(busid: str, serial: str = "") -> None:
    """Best-effort push telling every registered client that `busid` just
    became shared/available, so clients with a matching (now-dropped)
    attachment can reconnect immediately instead of waiting for their own
    watchdog poll. Never raises - a client being unreachable must not
    block the share/rebind action that triggered this.

    `serial` is included so a client whose own stored busid for this
    device is stale (the same relocation problem this server just solved
    for itself) can still recognize it by serial and self-correct."""
    cfg = config.store.read()
    for client in cfg.get("clients", {}).values():
        if not client.get("token"):
            continue
        url = f"http://{client['host']}:{client['api_port']}/api/remote/devices/{busid}/reconnect"
        try:
            async with httpx.AsyncClient(timeout=5) as http:
                resp = await http.post(
                    url,
                    headers={"Authorization": f"Bearer {client['token']}"},
                    json={"serial": serial},
                )
            if resp.status_code != 200:
                log_event(f"notify {client['name']} about {busid}: HTTP {resp.status_code}")
        except Exception as e:
            log_event(f"could not notify {client['name']} about {busid}: {e}")


def _has_admin() -> bool:
    return bool(config.store.read()["admin_password_hash"])


async def require_bearer_or_session(request: Request) -> str:
    """API auth for /api/devices: either a logged-in browser session
    (server's own web UI) or a registered client's bearer token."""
    user = request.session.get("user")
    if user:
        return "session:" + user
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        cfg = config.store.read()
        for client_id, client in cfg.get("clients", {}).items():
            if client.get("token") and hmac.compare_digest(token, client["token"]):
                config.store.update(
                    lambda d, cid=client_id: d["clients"][cid].__setitem__("last_seen", _now())
                )
                return "client:" + client_id
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")


# ---------------------------------------------------------------- web pages

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not _has_admin():
        return RedirectResponse("/setup")
    if not request.session.get("user"):
        return RedirectResponse("/login")
    return templates.TemplateResponse("dashboard.html", {"request": request})


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
    log_event(f"failed login attempt from {client_ip}")
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
        log_event(f"failed to share {busid}: {e}")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    serial = _get_device_serial(busid)
    config.store.update(
        lambda d: (
            d.update(shared_devices=sorted(set(d.get("shared_devices", [])) | {busid})),
            d.setdefault("known_serials", {}).update({busid: serial} if serial else {}),
        )
    )
    log_event(f"shared {busid}")
    await notify_clients_device_available(busid, serial=serial or "")
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
        log_event(f"failed to unshare {busid}: {e}")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    config.store.update(
        lambda d: d.update(
            shared_devices=[b for b in d.get("shared_devices", []) if b != busid]
        )
    )
    log_event(f"unshared {busid}")
    return {"ok": True}


@app.post("/api/devices/{busid}/request-share")
async def api_request_share(busid: str, caller: str = Depends(require_bearer_or_session)):
    """Lets a registered client ask the server to share a device it can see
    but that isn't shared yet, so the client's own "Attach" flow can be one
    click instead of requiring the admin to separately visit this
    dashboard first. No AJAX-header check here (unlike the admin's own
    /share) - this is a machine-to-machine, bearer-token-authenticated
    call from a client's backend, not a browser form post."""
    try:
        validate_busid(busid)
        bind_device(busid)
    except ValidationError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    except RuntimeError as e:
        log_event(f"{caller} failed to share {busid}: {e}")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    serial = _get_device_serial(busid)
    config.store.update(
        lambda d: (
            d.update(shared_devices=sorted(set(d.get("shared_devices", [])) | {busid})),
            d.setdefault("known_serials", {}).update({busid: serial} if serial else {}),
        )
    )
    log_event(f"{caller} requested share of {busid}")
    await notify_clients_device_available(busid, serial=serial or "")
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
    log_event(f"labeled {busid} as '{label}'")
    return {"ok": True}


@app.get("/api/clients")
async def api_list_clients(user=Depends(require_session_user)):
    cfg = config.store.read()
    return {"clients": [_public_client(c) for c in cfg.get("clients", {}).values()]}


@app.post("/api/clients")
async def api_add_client(request: Request, user=Depends(require_session_user), body: dict = Body(...)):
    require_safe_ajax(request)
    name = str(body.get("name", "")).strip()[:60]
    host = str(body.get("host", "")).strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "name is required")
    try:
        validate_hostname(host)
        api_port = validate_port_number(body.get("api_port", 8001))
    except ValidationError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    client_id = uuid.uuid4().hex[:8]
    token = generate_token()
    record = {
        "id": client_id,
        "name": name,
        "host": host,
        "api_port": api_port,
        "token": token,
        "created_at": _now(),
        "last_seen": None,
    }
    config.store.update(lambda d: d["clients"].__setitem__(client_id, record))
    log_event(f"registered client '{name}' ({host}:{api_port})")
    # Plaintext token is returned exactly once here; paste it into that
    # client's "Add server" form.
    return {"client": _public_client(record), "token": token}


@app.post("/api/clients/{client_id}/rotate")
async def api_rotate_client(client_id: str, request: Request, user=Depends(require_session_user)):
    require_safe_ajax(request)
    cfg = config.store.read()
    client = cfg.get("clients", {}).get(client_id)
    if not client:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown client")
    new_token = generate_token()
    config.store.update(lambda d, t=new_token: d["clients"][client_id].__setitem__("token", t))
    log_event(f"rotated token for client '{client['name']}'")
    return {"token": new_token}


@app.delete("/api/clients/{client_id}")
async def api_delete_client(client_id: str, request: Request, user=Depends(require_session_user)):
    require_safe_ajax(request)
    cfg = config.store.read()
    client = cfg.get("clients", {}).get(client_id)
    config.store.update(lambda d: d["clients"].pop(client_id, None))
    if client:
        log_event(f"removed client '{client['name']}'")
    return {"ok": True}


@app.get("/api/events")
async def api_events(user=Depends(require_session_user)):
    return {"events": list(EVENTS)}


@app.get("/healthz")
async def healthz():
    return JSONResponse({"status": "ok"})
