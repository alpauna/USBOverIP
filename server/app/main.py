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

from common import wireguard_helper
from common.procutil import (
    ValidationError,
    validate_busid,
    validate_hostname,
    validate_port_number,
    validate_wg_key,
)
from common.security import (
    PasswordTooLongError,
    generate_token,
    hash_password,
    verify_password,
)
from common.webauth import LoginThrottle, require_safe_ajax, require_session_user

from . import config, firewall_helper
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
    try:
        _ensure_wireguard_up()
    except Exception:
        # The tunnel is an optional layer on top of USB/IP sharing, which
        # must come up regardless - an unexpected failure here (a missing
        # dependency, a permissions issue, anything not already handled
        # inside _ensure_wireguard_up itself) must never block startup the
        # way it did once in production before this try/except existed.
        logger.exception("wireguard startup reconciliation failed; continuing without it")
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
            # "Already bound to usbip-host" lands here too - that is NOT a
            # reason to skip the status check/notify below. A container
            # restart can lose client TCP sessions even when the
            # kernel-level bind survives, so we still need to tell clients
            # the device is available, exactly as if we'd bound it fresh.
            log_event(f"{'bind for ' + busid + ' on startup' if force else 'watchdog: bind for ' + busid} failed: {e}")
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


def _ensure_wireguard_up() -> None:
    """Best-effort, called on every startup: generate this server's own
    keypair the first time, bring wg0 up, and replay every already-known
    peer onto it. Never raises - an older deployed image without
    wireguard-tools installed, or any wg failure, just means the tunnel
    feature stays unavailable; it must not block the rest of startup."""
    if not wireguard_helper.available():
        logger.info("wireguard-tools not installed; tunnel feature unavailable")
        return
    cfg = config.store.read()
    wg_cfg = cfg["wireguard"]
    if not wg_cfg.get("private_key"):
        try:
            private_key, public_key = wireguard_helper.generate_keypair()
        except wireguard_helper.WireguardError as e:
            logger.warning("could not generate wireguard keypair: %s", e)
            return
        config.store.update(
            lambda d, priv=private_key, pub=public_key: d["wireguard"].update(
                private_key=priv, public_key=pub
            )
        )
        wg_cfg = config.store.read()["wireguard"]
        log_event("wireguard: generated server keypair")

    if not wg_cfg.get("endpoint_host"):
        detected = wireguard_helper.detect_lan_ip()
        config.store.update(lambda d, h=detected: d["wireguard"].update(endpoint_host=h))
        wg_cfg = config.store.read()["wireguard"]
        log_event(f"wireguard: auto-detected endpoint address {detected} (set SERVER_WG_ENDPOINT_HOST to override)")

    try:
        wireguard_helper.ensure_interface_up(
            wg_cfg["private_key"], wg_cfg["address"], listen_port=wg_cfg["listen_port"]
        )
    except wireguard_helper.WireguardError as e:
        log_event(f"wireguard: failed to bring up wg0: {e}")
        return

    for client_id, peer in wg_cfg.get("peers", {}).items():
        try:
            wireguard_helper.apply_peer(peer["pubkey"], f"{peer['wg_ip']}/32")
        except wireguard_helper.WireguardError as e:
            log_event(f"wireguard: failed to reapply peer for client {client_id}: {e}")

    if cfg.get("require_wireguard") and firewall_helper.available():
        try:
            firewall_helper.enable(wg_cfg["subnet"], [config.USBIPD_PORT])
        except firewall_helper.FirewallError as e:
            log_event(f"wireguard: failed to reapply tunnel-only firewall rule: {e}")


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


@app.post("/api/devices/{busid}/rebind")
async def api_rebind(busid: str, caller: str = Depends(require_bearer_or_session)):
    """Force-clear a stale/zombie export by unbinding and rebinding the
    device at the kernel level, then re-notifying clients. This is the
    manual escape hatch for the case documented in groups.py's
    reconnect_device(): a client's session died but the usbip-host driver
    here never released the export, so every client reattach attempt
    bounces off "Device busy (exported)" forever. /unshare can't reach
    this - it's disabled in the UI while the driver still reports the
    device in use - so this endpoint runs unbind/bind unconditionally
    regardless of the device's current reported status.

    Callable either from this dashboard (session auth, browser click) or
    by a registered client's own backend (bearer token) - a client with
    auto_rebind_on_backoff enabled calls this on itself after repeated
    reconnect failures instead of waiting on an admin. No AJAX-header
    check, matching /request-share: that check is CSRF protection for
    browser-session calls, and a bearer-token caller can't set it, so
    other bearer-eligible endpoints skip it the same way."""
    try:
        validate_busid(busid)
        unbind_device(busid)
        bind_device(busid)
    except ValidationError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    except RuntimeError as e:
        log_event(f"failed to rebind {busid}: {e}")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    log_event(f"force-rebound {busid} to clear a stale export")
    cfg = config.store.read()
    if busid in cfg.get("shared_devices", []):
        serial = _get_device_serial(busid)
        await notify_clients_device_available(busid, serial=serial or "")
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


@app.post("/api/wireguard/register")
async def api_wireguard_register(body: dict = Body(...), caller: str = Depends(require_bearer_or_session)):
    """Called by a registered client's own backend (bearer-auth, not a
    browser) to join the tunnel - see common/wireguard_helper.py's module
    docstring for why keys don't need to be copy-pasted by hand. Not
    AJAX-header-guarded, same reasoning as /request-share: this is a
    machine-to-machine call authenticated by the bearer token alone."""
    if not caller.startswith("client:"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "must be called by a registered client")
    client_id = caller.split(":", 1)[1]
    if not wireguard_helper.available():
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "wireguard-tools not installed on this server")

    pubkey = str(body.get("pubkey", "")).strip()
    try:
        validate_wg_key(pubkey)
    except ValidationError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))

    cfg = config.store.read()
    wg_cfg = cfg["wireguard"]
    if not wg_cfg.get("private_key"):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "server tunnel not ready yet")
    existing = wg_cfg["peers"].get(client_id)
    if existing and existing["pubkey"] == pubkey:
        wg_ip = existing["wg_ip"]
    else:
        subnet_prefix = ".".join(wg_cfg["subnet"].split(".")[:3])
        wg_ip = f"{subnet_prefix}.{wg_cfg['next_host']}"

        def _save_peer(d, cid=client_id, pk=pubkey, ip=wg_ip):
            d["wireguard"]["peers"][cid] = {"pubkey": pk, "wg_ip": ip, "added_at": _now()}
            d["wireguard"]["next_host"] = d["wireguard"]["next_host"] + 1

        config.store.update(_save_peer)

    try:
        wireguard_helper.apply_peer(pubkey, f"{wg_ip}/32")
    except wireguard_helper.WireguardError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))

    log_event(f"wireguard: registered peer for {caller} at {wg_ip}")
    return {
        "server_pubkey": wg_cfg["public_key"],
        # Deliberately NOT derived from this request's own Host header - a
        # client that's already tunneled and re-registers via its tunnel IP
        # would otherwise get told "reach me at my own tunnel address,"
        # which is circular. endpoint_host is a stable, real LAN address
        # (auto-detected once at startup, see _ensure_wireguard_up).
        "endpoint": f"{wg_cfg['endpoint_host']}:{wg_cfg['listen_port']}",
        "assigned_ip": wg_ip,
        "server_wg_ip": wg_cfg["address"].split("/")[0],
        "subnet": wg_cfg["subnet"],
    }


@app.get("/api/wireguard/status")
async def api_wireguard_status(user=Depends(require_session_user)):
    cfg = config.store.read()
    wg_cfg = cfg["wireguard"]
    available = wireguard_helper.available()
    live = wireguard_helper.interface_status() if available else {"up": False, "peers": []}
    live_by_pubkey = {p["public_key"]: p for p in live["peers"]}
    peers_out = []
    for client_id, peer in wg_cfg.get("peers", {}).items():
        client = cfg.get("clients", {}).get(client_id)
        live_peer = live_by_pubkey.get(peer["pubkey"])
        peers_out.append(
            {
                "client_id": client_id,
                "client_name": client["name"] if client else "(deleted client)",
                "wg_ip": peer["wg_ip"],
                "latest_handshake": live_peer["latest_handshake"] if live_peer else 0,
            }
        )
    return {
        "available": available,
        "up": live["up"],
        "public_key": wg_cfg.get("public_key", ""),
        "listen_port": wg_cfg.get("listen_port"),
        "subnet": wg_cfg.get("subnet"),
        "peers": peers_out,
        "require_wireguard": cfg.get("require_wireguard", False),
    }


@app.post("/api/wireguard/require")
async def api_wireguard_require(
    request: Request, user=Depends(require_session_user), body: dict = Body(...)
):
    """Toggles tunnel-only enforcement for port 3240 (see
    firewall_helper.py's docstring for why the API/dashboard port is
    deliberately left alone). Off by default - the admin should confirm
    at least one client's tunnel actually works (wg show a handshake)
    before flipping this on, same "verify before you can lock yourself
    out" caution as rotating a token mid-session."""
    require_safe_ajax(request)
    enabled = bool(body.get("enabled"))
    if not firewall_helper.available():
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "nftables not installed on this server")
    cfg = config.store.read()
    wg_cfg = cfg["wireguard"]
    try:
        if enabled:
            firewall_helper.enable(wg_cfg["subnet"], [config.USBIPD_PORT])
        else:
            firewall_helper.disable()
    except firewall_helper.FirewallError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    config.store.update(lambda d, v=enabled: d.update(require_wireguard=v))
    log_event(f"wireguard: tunnel-only enforcement for port {config.USBIPD_PORT} {'enabled' if enabled else 'disabled'}")
    return {"ok": True, "require_wireguard": enabled}


@app.get("/api/events")
async def api_events(user=Depends(require_session_user)):
    return {"events": list(EVENTS)}


@app.get("/healthz")
async def healthz():
    return JSONResponse({"status": "ok"})
