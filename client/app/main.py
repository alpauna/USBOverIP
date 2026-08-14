from __future__ import annotations

import asyncio
import datetime
import hmac
import logging
import uuid

from fastapi import Body, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from common import wireguard_helper
from common.procutil import ValidationError, validate_busid, validate_hostname, validate_port_number
from common.security import PasswordTooLongError, hash_password, verify_password
from common.webauth import LoginThrottle, require_safe_ajax, require_session_user

from . import config, docker_helper, groups, proxmox, remote_client, usbip_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("usbip.client")

app = FastAPI(title="USB/IP Client")
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


def _migrate_attachments_to_id_keys() -> None:
    """One-time migration: attachments used to be keyed by local vhci port
    number - a small, reused integer space the kernel assigns dynamically.
    When two attachments dropped and reconnected around the same time, a
    freshly-reattached one could be handed a port number that collided
    with another attachment's still-pending record under that same key,
    silently destroying it (a real production incident). Re-keys by a
    stable generated id instead, moving the old port-number key into an
    explicit "port" field on the record. Idempotent - a no-op once every
    record already has an "id"."""
    cfg = config.store.read()
    migrated = {}
    dirty = False
    for key, att in cfg["attachments"].items():
        if "id" in att:
            migrated[key] = att
            continue
        new_id = uuid.uuid4().hex[:8]
        migrated[new_id] = {**att, "id": new_id, "port": key}
        dirty = True
    if dirty:
        config.store.update(lambda d, m=migrated: d.update(attachments=m))
        logger.info("migrated %d attachment(s) from port-keyed to id-keyed schema", len(migrated))


def _ensure_attachment_symlinks() -> None:
    """Best-effort, called on every startup: any attachment that's
    currently live but has no stable symlink yet - e.g. one that existed
    before the symlink feature shipped, migrated straight across by
    _migrate_attachments_to_id_keys with no attach/reconnect cycle to
    trigger creating one - gets it created now instead of waiting for its
    next drop/reconnect. Never raises."""
    cfg = config.store.read()
    active_ports = {p.port for p in usbip_client.list_ports()}
    for att_id, att in cfg["attachments"].items():
        port = att.get("port")
        if port not in active_ports:
            continue
        key = att.get("group_id") or att_id
        if usbip_client.attachment_symlink_path(key):
            continue
        stable_path = groups.update_symlink_for_port(key, port)
        if stable_path:
            logger.info("created missing stable symlink for existing attachment %s: %s", att_id, stable_path)


@app.on_event("startup")
async def on_startup():
    try:
        _migrate_attachments_to_id_keys()
    except Exception:
        logger.exception("attachment id migration failed; continuing with existing data")
    try:
        _ensure_attachment_symlinks()
    except Exception:
        logger.exception("attachment symlink reconciliation failed; continuing without it")
    try:
        _ensure_wireguard_tunnels_up()
    except Exception:
        # Same reasoning as the server's on_startup: this optional feature
        # must never be able to block the attach/reconnect watchdog below
        # from starting, regardless of what specific error occurs.
        logger.exception("wireguard startup reconciliation failed; continuing without it")
    app.state.watchdog_task = asyncio.create_task(groups.watchdog_loop())


def _ensure_wireguard_tunnels_up() -> None:
    """Best-effort, called on every startup: for every server whose tunnel
    was previously enabled (server record has a "wireguard" sub-object),
    bring its dedicated interface back up and reapply its peer from
    already-stored state - no need to re-call that server's /register,
    since our own keypair and the server's pubkey/endpoint/assigned IP
    were already persisted the first time. Mirrors the server's own
    _ensure_wireguard_up startup reconcile. Never raises."""
    if not wireguard_helper.available():
        return
    cfg = config.store.read()
    wg_cfg = cfg["wireguard"]
    if not wg_cfg.get("private_key"):
        return  # tunnel feature was never used on this client
    for server_id, server in cfg["servers"].items():
        wg_state = server.get("wireguard")
        if not wg_state:
            continue
        try:
            wireguard_helper.ensure_interface_up(
                wg_cfg["private_key"], f"{wg_state['assigned_ip']}/32", interface=wg_state["interface"]
            )
            wireguard_helper.apply_peer(
                wg_state["server_pubkey"],
                f"{wg_state['server_wg_ip']}/32",
                endpoint=wg_state["endpoint"],
                interface=wg_state["interface"],
            )
        except (wireguard_helper.WireguardError, ValidationError) as e:
            logger.warning("could not restore wireguard tunnel for %s: %s", server["name"], e)


@app.on_event("shutdown")
async def on_shutdown():
    task = getattr(app.state, "watchdog_task", None)
    if task:
        task.cancel()


def _has_admin() -> bool:
    return bool(config.store.read()["admin_password_hash"])


def _public_server(s: dict) -> dict:
    return {
        "id": s["id"],
        "name": s["name"],
        "host": s["host"],
        "api_port": s["api_port"],
        "usbip_port": s["usbip_port"],
        "role": s.get("role", "primary"),
        "has_token": bool(s.get("token")),
        "wireguard": s.get("wireguard"),
    }


async def require_server_token(request: Request) -> str:
    """Auth for endpoints a *server* calls on us (currently just the
    reconnect push). The bearer token must match one of our registered
    servers' tokens - the same secret that server issued us to call it, now
    used the other direction to let it call us. Returns that server_id."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        cfg = config.store.read()
        for server_id, server in cfg["servers"].items():
            if server.get("token") and hmac.compare_digest(token, server["token"]):
                return server_id
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")


# ---------------------------------------------------------------- web pages

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not _has_admin():
        return RedirectResponse("/setup")
    if not request.session.get("user"):
        return RedirectResponse("/login")
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "client_name": config.CLIENT_NAME,
            "proxmox_available": proxmox.available(),
            "docker_available": docker_helper.available(),
        },
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
            "setup.html", {"request": request, "error": "Password must be at least 10 characters."}
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
    return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid password."})


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --------------------------------------------------------------- servers API

@app.get("/api/servers")
async def api_list_servers(user=Depends(require_session_user)):
    cfg = config.store.read()
    return {"servers": [_public_server(s) for s in cfg["servers"].values()]}


@app.post("/api/servers")
async def api_add_server(request: Request, user=Depends(require_session_user), body: dict = Body(...)):
    require_safe_ajax(request)
    name = str(body.get("name", "")).strip()[:60]
    host = str(body.get("host", "")).strip()
    role = str(body.get("role", "primary")).strip() or "primary"
    token = str(body.get("token", "")).strip()
    try:
        validate_hostname(host)
        api_port = validate_port_number(body.get("api_port", 8000))
        usbip_port = validate_port_number(body.get("usbip_port", 3240))
    except ValidationError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    if not name or not token:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "name and token are required")
    server_id = uuid.uuid4().hex[:8]
    record = {
        "id": server_id,
        "name": name,
        "host": host,
        "api_port": api_port,
        "usbip_port": usbip_port,
        "token": token,
        "role": role if role in ("primary", "backup") else "primary",
    }
    config.store.update(lambda d: d["servers"].__setitem__(server_id, record))
    return {"server": _public_server(record)}


@app.put("/api/servers/{server_id}")
async def api_update_server(
    server_id: str, request: Request, user=Depends(require_session_user), body: dict = Body(...)
):
    require_safe_ajax(request)
    cfg = config.store.read()
    existing = cfg["servers"].get(server_id)
    if not existing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown server")
    name = str(body.get("name", existing["name"])).strip()[:60] or existing["name"]
    host = str(body.get("host", existing["host"])).strip()
    role = str(body.get("role", existing.get("role", "primary"))).strip() or "primary"
    token = str(body.get("token", "")).strip() or existing["token"]
    try:
        validate_hostname(host)
        api_port = validate_port_number(body.get("api_port", existing["api_port"]))
        usbip_port = validate_port_number(body.get("usbip_port", existing["usbip_port"]))
    except ValidationError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    updated = {
        "id": server_id,
        "name": name,
        "host": host,
        "api_port": api_port,
        "usbip_port": usbip_port,
        "token": token,
        "role": role if role in ("primary", "backup") else "primary",
    }
    config.store.update(lambda d: d["servers"].__setitem__(server_id, updated))
    return {"server": _public_server(updated)}


@app.delete("/api/servers/{server_id}")
async def api_delete_server(server_id: str, request: Request, user=Depends(require_session_user)):
    require_safe_ajax(request)
    cfg = config.store.read()
    if any(a.get("server_id") == server_id for a in cfg["attachments"].values()):
        raise HTTPException(status.HTTP_409_CONFLICT, "server has an active attachment, detach first")
    config.store.update(lambda d: d["servers"].pop(server_id, None))
    return {"ok": True}


@app.get("/api/servers/{server_id}/devices")
async def api_server_devices(server_id: str, user=Depends(require_session_user)):
    cfg = config.store.read()
    server = cfg["servers"].get(server_id)
    if not server:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown server")
    try:
        devices = await remote_client.fetch_devices(server["host"], server["api_port"], server["token"])
    except remote_client.RemoteError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    return {"devices": devices}


@app.get("/api/servers/{server_id}/health")
async def api_server_health(server_id: str, user=Depends(require_session_user)):
    cfg = config.store.read()
    server = cfg["servers"].get(server_id)
    if not server:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown server")
    ok = await remote_client.check_health(server["host"], server["api_port"])
    return {"online": ok}


@app.post("/api/servers/{server_id}/wireguard/enable")
async def api_enable_wireguard(server_id: str, request: Request, user=Depends(require_session_user)):
    """Joins this server's WireGuard tunnel - generates our own keypair the
    first time (reused across every server we tunnel to), registers it
    with the server (which allocates us an IP from its pool), then brings
    up a dedicated local interface (wg-<server_id>, not a shared wg0 - see
    config.py's docstring for why each server needs its own interface).
    After this succeeds, editing this server's `host` field to its
    assigned_ip (PUT /api/servers/{id}) routes all subsequent API + USB/IP
    traffic through the tunnel with no other code change needed."""
    require_safe_ajax(request)
    if not wireguard_helper.available():
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "wireguard-tools not installed on this client")
    cfg = config.store.read()
    server = cfg["servers"].get(server_id)
    if not server:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown server")

    wg_cfg = cfg["wireguard"]
    if not wg_cfg.get("private_key"):
        try:
            private_key, public_key = wireguard_helper.generate_keypair()
        except wireguard_helper.WireguardError as e:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"could not generate keypair: {e}")
        config.store.update(
            lambda d, priv=private_key, pub=public_key: d["wireguard"].update(
                private_key=priv, public_key=pub
            )
        )
        wg_cfg = config.store.read()["wireguard"]

    try:
        reg = await remote_client.register_wireguard(
            server["host"], server["api_port"], server["token"], wg_cfg["public_key"]
        )
    except remote_client.RemoteError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))

    interface = f"wg-{server_id}"
    try:
        wireguard_helper.ensure_interface_up(
            wg_cfg["private_key"], f"{reg['assigned_ip']}/32", interface=interface
        )
        wireguard_helper.apply_peer(
            reg["server_pubkey"], f"{reg['server_wg_ip']}/32", endpoint=reg["endpoint"], interface=interface
        )
    except (wireguard_helper.WireguardError, ValidationError) as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"tunnel came up on the server but failed locally: {e}"
        )

    wg_state = {
        "server_pubkey": reg["server_pubkey"],
        "endpoint": reg["endpoint"],
        "assigned_ip": reg["assigned_ip"],
        "server_wg_ip": reg["server_wg_ip"],
        "subnet": reg["subnet"],
        "interface": interface,
    }
    config.store.update(lambda d, s=wg_state: d["servers"][server_id].__setitem__("wireguard", s))
    groups.log_event(f"wireguard: tunnel enabled for {server['name']} ({interface}, {reg['assigned_ip']})")
    return {"ok": True, "wireguard": wg_state}


@app.get("/api/wireguard/status")
async def api_wireguard_status(user=Depends(require_session_user)):
    cfg = config.store.read()
    available = wireguard_helper.available()
    tunnels = []
    for server_id, server in cfg["servers"].items():
        wg_state = server.get("wireguard")
        if not wg_state:
            continue
        live = (
            wireguard_helper.interface_status(interface=wg_state["interface"])
            if available
            else {"up": False, "peers": []}
        )
        peer = next((p for p in live["peers"] if p["public_key"] == wg_state["server_pubkey"]), None)
        tunnels.append(
            {
                "server_id": server_id,
                "server_name": server["name"],
                "assigned_ip": wg_state["assigned_ip"],
                "server_wg_ip": wg_state["server_wg_ip"],
                "up": live["up"],
                "latest_handshake": peer["latest_handshake"] if peer else 0,
            }
        )
    return {"available": available, "public_key": cfg["wireguard"].get("public_key", ""), "tunnels": tunnels}


# ---------------------------------------------------------- direct attach

def _clean_restart_actions(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    cleaned = []
    for item in raw[:10]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type", "")).strip()
        name = str(item.get("name", "")).strip()[:128]
        if kind in ("docker", "systemd") and name:
            cleaned.append({"type": kind, "name": name})
    return cleaned


@app.post("/api/servers/{server_id}/devices/{busid}/attach")
async def api_direct_attach(
    server_id: str,
    busid: str,
    request: Request,
    user=Depends(require_session_user),
    body: dict = Body(default={}),
):
    require_safe_ajax(request)
    validate_busid(busid)
    cfg = config.store.read()
    server = cfg["servers"].get(server_id)
    if not server:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown server")

    # One-click attach: if the device isn't shared yet, ask the server to
    # share it first rather than requiring the admin to separately visit
    # the server's own dashboard. If it's already claimed by another
    # client, fail clearly instead of attempting (and failing) the attach.
    try:
        devices = await remote_client.fetch_devices(server["host"], server["api_port"], server["token"])
    except remote_client.RemoteError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"could not reach server: {e}")
    device = next((d for d in devices if d["busid"] == busid), None)
    if not device:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device not found on that server")
    if device["status"] == "shared_in_use":
        raise HTTPException(status.HTTP_409_CONFLICT, "device is already attached to another client")
    if device["status"] == "unshared":
        try:
            await remote_client.request_share(server["host"], server["api_port"], server["token"], busid)
        except remote_client.RemoteError as e:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"could not share device on server: {e}")
        groups.log_event(f"requested share of {server['name']}/{busid}")

    try:
        attached = usbip_client.attach(server["host"], server["usbip_port"], busid)
    except usbip_client.UsbipCommandError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    # Default the client-side label to whatever the server has labeled this
    # device as, so it doesn't show up blank just because the Browse
    # Devices "Attach" button never prompts for one. An explicit label in
    # the request body still wins.
    default_label = str(body.get("label") or device.get("label") or "")[:80]
    att_id = uuid.uuid4().hex[:8]
    record = {
        "id": att_id,
        "server_id": server_id,
        "busid": busid,
        "serial": device.get("serial", ""),
        "label": default_label,
        "group_id": None,
        "attached_at": _now(),
        "auto_failover": bool(body.get("auto_failover", True)),
        "auto_rebind_on_backoff": bool(body.get("auto_rebind_on_backoff", False)),
        "restart_actions": _clean_restart_actions(body.get("restart_actions")),
        "port": attached.port,
        "local_busid": attached.local_busid,
    }
    config.store.update(lambda d, i=att_id, r=record: d["attachments"].__setitem__(i, r))
    groups.log_event(f"direct attach: {server['name']}/{busid} -> port {attached.port}")

    stable_path = groups.update_symlink_for_port(att_id, attached.port)
    if stable_path:
        groups.log_event(f"{server['name']}/{busid} stable device path: {stable_path}")

    groups.run_restart_actions(record["restart_actions"])
    return {"port": attached.port, "id": att_id}


def _now() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


# ------------------------------------------------------------- attachments

@app.get("/api/attachments")
async def api_attachments(user=Depends(require_session_user)):
    cfg = config.store.read()
    live_ports = {p.port: p for p in usbip_client.list_ports()}
    out = []
    for att_id, att in cfg["attachments"].items():
        server = cfg["servers"].get(att["server_id"])
        port = att.get("port")
        out.append(
            {
                "id": att_id,
                "port": port,
                "server_id": att["server_id"],
                "server_name": server["name"] if server else "(deleted server)",
                "busid": att["busid"],
                "serial": att.get("serial") or "",
                "local_busid": live_ports[port].local_busid if port in live_ports else None,
                "label": att.get("label") or "",
                "group_id": att.get("group_id"),
                "stable_path": usbip_client.attachment_symlink_path(att.get("group_id") or att_id),
                "attached_at": att.get("attached_at"),
                "auto_failover": att.get("auto_failover", False),
                "auto_rebind_on_backoff": att.get("auto_rebind_on_backoff", False),
                "restart_actions": att.get("restart_actions", []),
                "proxmox": att.get("proxmox"),
                "live": port in live_ports,
                "failure_count": att.get("failure_count", 0),
                "next_retry_at": att.get("next_retry_at"),
            }
        )
    return {"attachments": out}


@app.get("/api/attachments/{att_id}/details")
async def api_attachment_details(att_id: str, user=Depends(require_session_user)):
    cfg = config.store.read()
    att = cfg["attachments"].get(att_id)
    if not att:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such attachment")
    port = att.get("port")
    live_port = next((p for p in usbip_client.list_ports() if p.port == port), None)
    local_busid = live_port.local_busid if live_port else None
    dev_paths = usbip_client.resolve_device_paths(local_busid) if local_busid else {
        "tty": None,
        "by_id": [],
        "raw": None,
    }
    server = cfg["servers"].get(att["server_id"])
    return {
        "id": att_id,
        "port": port,
        "live": live_port is not None,
        "local_busid": local_busid,
        "dev_paths": dev_paths,
        "server_id": att["server_id"],
        "server_name": server["name"] if server else "(deleted server)",
        "busid": att["busid"],
        "serial": att.get("serial") or "",
        "label": att.get("label") or "",
        "group_id": att.get("group_id"),
        "stable_path": usbip_client.attachment_symlink_path(att.get("group_id") or att_id),
        "attached_at": att.get("attached_at"),
        "auto_failover": att.get("auto_failover", True),
        "auto_rebind_on_backoff": att.get("auto_rebind_on_backoff", False),
        "restart_actions": att.get("restart_actions", []),
        "proxmox": att.get("proxmox"),
    }


@app.put("/api/attachments/{att_id}")
async def api_update_attachment(
    att_id: str, request: Request, user=Depends(require_session_user), body: dict = Body(...)
):
    require_safe_ajax(request)
    cfg = config.store.read()
    att = cfg["attachments"].get(att_id)
    if not att:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such attachment")
    updates = {
        "label": str(body.get("label", att.get("label", "")))[:80],
        "auto_failover": bool(body.get("auto_failover", att.get("auto_failover", True))),
        "auto_rebind_on_backoff": bool(
            body.get("auto_rebind_on_backoff", att.get("auto_rebind_on_backoff", False))
        ),
        "restart_actions": _clean_restart_actions(
            body.get("restart_actions", att.get("restart_actions"))
        ),
    }
    config.store.update(lambda d, i=att_id, u=updates: d["attachments"][i].update(u))
    return {"ok": True}


@app.post("/api/attachments/{att_id}/detach")
async def api_detach(att_id: str, request: Request, user=Depends(require_session_user)):
    require_safe_ajax(request)
    cfg = config.store.read()
    att = cfg["attachments"].get(att_id)
    if not att:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such attachment")
    try:
        usbip_client.detach(att["port"])
    except usbip_client.UsbipCommandError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    config.store.update(lambda d, i=att_id: d["attachments"].pop(i, None))
    usbip_client.remove_attachment_symlink(att.get("group_id") or att_id)
    groups.log_event(f"detached port {att['port']}")
    return {"ok": True}


# ------------------------------------------------------------------ groups

@app.get("/api/groups")
async def api_list_groups(user=Depends(require_session_user)):
    cfg = config.store.read()
    active = {att["group_id"]: att.get("port") for att in cfg["attachments"].values() if att.get("group_id")}
    out = []
    for g in cfg["groups"].values():
        out.append({**g, "active_port": active.get(g["id"])})
    return {"groups": out}


@app.post("/api/groups")
async def api_create_group(request: Request, user=Depends(require_session_user), body: dict = Body(...)):
    require_safe_ajax(request)
    name = str(body.get("name", "")).strip()[:60]
    candidates = body.get("candidates") or []
    if not name or not isinstance(candidates, list) or not candidates:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "name and at least one candidate are required")
    cfg = config.store.read()
    clean = []
    for c in candidates:
        sid = str(c.get("server_id", ""))
        busid = str(c.get("busid", ""))
        if sid not in cfg["servers"]:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown server_id {sid}")
        validate_busid(busid)
        clean.append({"server_id": sid, "busid": busid, "label": str(c.get("label", ""))[:60]})
    group_id = uuid.uuid4().hex[:8]
    record = {
        "id": group_id,
        "name": name,
        "candidates": clean,
        "auto_failover": bool(body.get("auto_failover", True)),
        "restart_actions": _clean_restart_actions(body.get("restart_actions")),
    }
    config.store.update(lambda d: d["groups"].__setitem__(group_id, record))
    return {"group": record}


@app.put("/api/groups/{group_id}")
async def api_update_group(
    group_id: str, request: Request, user=Depends(require_session_user), body: dict = Body(...)
):
    require_safe_ajax(request)
    cfg = config.store.read()
    existing = cfg["groups"].get(group_id)
    if not existing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown group")
    if any(a.get("group_id") == group_id for a in cfg["attachments"].values()):
        raise HTTPException(status.HTTP_409_CONFLICT, "group is currently attached, detach first")
    name = str(body.get("name", existing["name"])).strip()[:60] or existing["name"]
    candidates = body.get("candidates", existing["candidates"])
    if not isinstance(candidates, list) or not candidates:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "at least one candidate is required")
    clean = []
    for c in candidates:
        sid = str(c.get("server_id", ""))
        busid = str(c.get("busid", ""))
        if sid not in cfg["servers"]:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown server_id {sid}")
        validate_busid(busid)
        clean.append({"server_id": sid, "busid": busid, "label": str(c.get("label", ""))[:60]})
    updated = {
        "id": group_id,
        "name": name,
        "candidates": clean,
        "auto_failover": bool(body.get("auto_failover", existing.get("auto_failover", True))),
        "restart_actions": _clean_restart_actions(
            body.get("restart_actions", existing.get("restart_actions"))
        ),
    }
    config.store.update(lambda d: d["groups"].__setitem__(group_id, updated))
    return {"group": updated}


@app.delete("/api/groups/{group_id}")
async def api_delete_group(group_id: str, request: Request, user=Depends(require_session_user)):
    require_safe_ajax(request)
    cfg = config.store.read()
    if any(a.get("group_id") == group_id for a in cfg["attachments"].values()):
        raise HTTPException(status.HTTP_409_CONFLICT, "group is currently attached, detach first")
    config.store.update(lambda d: d["groups"].pop(group_id, None))
    return {"ok": True}


@app.post("/api/groups/{group_id}/attach")
async def api_attach_group(group_id: str, request: Request, user=Depends(require_session_user)):
    require_safe_ajax(request)
    try:
        result = await groups.attach_group(group_id)
    except groups.GroupError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    return result


@app.post("/api/groups/{group_id}/detach")
async def api_detach_group(group_id: str, request: Request, user=Depends(require_session_user)):
    require_safe_ajax(request)
    try:
        await groups.detach_group(group_id)
    except groups.GroupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    return {"ok": True}


@app.get("/api/events")
async def api_events(user=Depends(require_session_user)):
    return {"events": list(groups.EVENTS)}


# ----------------------------------------------------------------- proxmox

@app.get("/api/proxmox/available")
async def api_proxmox_available(user=Depends(require_session_user)):
    return {"available": proxmox.available()}


@app.get("/api/proxmox/vms")
async def api_proxmox_vms(user=Depends(require_session_user)):
    try:
        return {"vms": proxmox.list_vms()}
    except proxmox.ProxmoxError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))


@app.post("/api/attachments/{att_id}/proxmox-attach")
async def api_proxmox_attach(
    att_id: str, request: Request, user=Depends(require_session_user), body: dict = Body(...)
):
    require_safe_ajax(request)
    vmid = str(body.get("vmid", ""))
    cfg = config.store.read()
    att = cfg["attachments"].get(att_id)
    if not att:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such attachment")
    local_port = next((p for p in usbip_client.list_ports() if p.port == att.get("port")), None)
    if not local_port or not local_port.local_busid:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "could not resolve local bus id for this port")
    try:
        slot = proxmox.attach_usb_to_vm(vmid, local_port.local_busid)
    except proxmox.ProxmoxError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    config.store.update(
        lambda d, i=att_id, v=vmid, s=slot: d["attachments"][i].__setitem__("proxmox", {"vmid": v, "slot": s})
    )
    groups.log_event(f"port {att.get('port')} passed through to VM {vmid} as {slot}")
    return {"vmid": vmid, "slot": slot}


@app.post("/api/attachments/{att_id}/proxmox-detach")
async def api_proxmox_detach(att_id: str, request: Request, user=Depends(require_session_user)):
    require_safe_ajax(request)
    cfg = config.store.read()
    att = cfg["attachments"].get(att_id)
    if not att or not att.get("proxmox"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no proxmox passthrough recorded for this port")
    try:
        proxmox.detach_usb_from_vm(att["proxmox"]["vmid"], att["proxmox"]["slot"])
    except proxmox.ProxmoxError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    config.store.update(lambda d, i=att_id: d["attachments"][i].pop("proxmox", None))
    return {"ok": True}


# ------------------------------------------------------------------ docker

@app.get("/api/docker/available")
async def api_docker_available(user=Depends(require_session_user)):
    return {"available": docker_helper.available()}


@app.get("/api/docker/containers")
async def api_docker_containers(user=Depends(require_session_user)):
    return {"containers": docker_helper.list_containers()}


@app.post("/api/docker/containers/{name}/restart")
async def api_docker_restart(name: str, request: Request, user=Depends(require_session_user)):
    require_safe_ajax(request)
    try:
        docker_helper.restart_container(name)
    except (ValidationError, RuntimeError) as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    return {"ok": True}


# ------------------------------------------------------------------ remote
# Endpoints called BY a registered server, not by our own browser session -
# see require_server_token above. This is how a server pushes "this device
# is shared/available again" (e.g. after an admin re-shares it, or after
# the server itself rebinds on startup) instead of us only finding out via
# our own watchdog poll.

@app.post("/api/remote/devices/{busid}/reconnect")
async def api_remote_reconnect(
    busid: str, server_id: str = Depends(require_server_token), body: dict = Body(default={})
):
    validate_busid(busid)
    serial = str(body.get("serial", "") or "")
    result = await groups.reconnect_device(server_id, busid, serial=serial)
    if result is None:
        return {"status": "no_attachment_on_record"}
    return result
