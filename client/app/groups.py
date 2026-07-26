"""Device groups: a named, ordered list of (server, busid) candidates that
represent "the same logical USB device" across a primary server and one or
more backups.

Attaching a group tries candidates in priority order, skipping any whose
server is unreachable or whose device the server reports as already in
use. The kernel's usbip-host driver is the final authority on the "only
one client at a time" rule (a device already attached elsewhere is
reported back to us with status shared_in_use, and a raw attach attempt
against an in-use device is rejected by the server) - this module just
avoids racing it and adds the fallback-to-backup behaviour on top.

This module also owns auto-reconnect: any attachment (direct or via a
group) with auto_failover set gets restored here - either by the
background watchdog noticing it dropped, or by the server proactively
pushing a "this device is available again" notice (see
main.py:/api/remote/devices/{busid}/reconnect). Both paths funnel through
_restore_dropped_attachment so behavior is identical either way. On a
successful (re)attach, any configured restart_actions (Docker container /
systemd service) are run so a downstream consumer that had the device open
notices the reconnect instead of silently sitting on a dead device node.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
from collections import deque

from . import config, docker_helper, remote_client, systemd_helper, usbip_client

logger = logging.getLogger("usbip.client.groups")

EVENTS: deque[dict] = deque(maxlen=50)


class GroupError(Exception):
    pass


def log_event(message: str) -> None:
    EVENTS.appendleft({"time": datetime.datetime.utcnow().isoformat() + "Z", "message": message})
    logger.info(message)


def run_restart_actions(actions: list[dict] | None) -> None:
    for action in actions or []:
        kind = action.get("type")
        name = action.get("name")
        if not name:
            continue
        try:
            if kind == "docker":
                docker_helper.restart_container(name)
            elif kind == "systemd":
                systemd_helper.restart_service(name)
            else:
                log_event(f"skipping restart action with unknown type {kind!r}")
                continue
            log_event(f"restarted {kind} '{name}'")
        except Exception as e:
            log_event(f"failed to restart {kind} '{name}': {e}")


async def _candidate_status(server: dict, busid: str) -> tuple[bool, str]:
    try:
        devices = await remote_client.fetch_devices(server["host"], server["api_port"], server["token"])
    except remote_client.RemoteError as e:
        return False, f"unreachable ({e})"
    for d in devices:
        if d["busid"] == busid:
            if d["status"] == "shared_idle":
                return True, "available"
            return False, f"status is {d['status']}"
    return False, "device not currently shared on that server"


def _active_port_for_group(cfg: dict, group_id: str) -> str | None:
    for port, att in cfg["attachments"].items():
        if att.get("group_id") == group_id:
            return port
    return None


async def attach_group(group_id: str) -> dict:
    cfg = config.store.read()
    group = cfg["groups"].get(group_id)
    if not group:
        raise GroupError("unknown group")
    if _active_port_for_group(cfg, group_id):
        raise GroupError("group is already attached")

    errors: list[str] = []
    for candidate in group["candidates"]:
        server = cfg["servers"].get(candidate["server_id"])
        if not server:
            errors.append(f"{candidate['server_id']}: server not registered")
            continue
        ok, reason = await _candidate_status(server, candidate["busid"])
        if not ok:
            errors.append(f"{server['name']}/{candidate['busid']}: {reason}")
            continue
        try:
            port = usbip_client.attach(server["host"], server["usbip_port"], candidate["busid"])
        except usbip_client.UsbipCommandError as e:
            errors.append(f"{server['name']}/{candidate['busid']}: {e}")
            continue
        now = datetime.datetime.utcnow().isoformat() + "Z"
        record = {
            "server_id": server["id"],
            "busid": candidate["busid"],
            "label": candidate.get("label") or group["name"],
            "group_id": group_id,
            "attached_at": now,
            "auto_failover": bool(group.get("auto_failover", True)),
            "restart_actions": [],  # group-level actions are run below, not stored per-attachment
        }
        config.store.update(lambda d, p=port, r=record: d["attachments"].__setitem__(p, r))
        log_event(f"group '{group['name']}' attached via {server['name']}/{candidate['busid']} (port {port})")
        run_restart_actions(group.get("restart_actions", []))
        return {"port": port, "server_id": server["id"], "server": server["name"], "busid": candidate["busid"]}

    log_event(f"group '{group['name']}' attach failed: no candidate available")
    raise GroupError("no candidate available: " + "; ".join(errors))


async def detach_group(group_id: str) -> None:
    cfg = config.store.read()
    port = _active_port_for_group(cfg, group_id)
    if not port:
        raise GroupError("group is not currently attached")
    usbip_client.detach(port)
    config.store.update(lambda d: d["attachments"].pop(port, None))
    log_event(f"group detached (was on port {port})")


async def _restore_dropped_attachment(port: str, att: dict) -> dict:
    """Common recovery path for one attachment that just dropped, used by
    both the watchdog and the server-pushed reconnect notice."""
    config.store.update(lambda d, p=port: d["attachments"].pop(p, None))
    log_event(f"attachment on port {port} dropped (server={att.get('server_id')} busid={att.get('busid')})")

    if not att.get("auto_failover", True):
        return {"status": "dropped"}

    if att.get("group_id"):
        try:
            result = await attach_group(att["group_id"])
            log_event(f"auto-reconnect: reattached via {result['server']}/{result['busid']}")
            return {"status": "reattached", **result}
        except GroupError as e:
            log_event(f"auto-reconnect failed: {e}")
            return {"status": "failed", "error": str(e)}

    cfg = config.store.read()
    server = cfg["servers"].get(att.get("server_id"))
    if not server:
        log_event(f"auto-reconnect failed: server {att.get('server_id')} is no longer registered")
        return {"status": "failed", "error": "server not registered"}
    try:
        new_port = usbip_client.attach(server["host"], server["usbip_port"], att["busid"])
    except usbip_client.UsbipCommandError as e:
        log_event(f"auto-reconnect failed for {server['name']}/{att['busid']}: {e}")
        return {"status": "failed", "error": str(e)}
    now = datetime.datetime.utcnow().isoformat() + "Z"
    new_record = {**att, "attached_at": now}
    config.store.update(lambda d, p=new_port, r=new_record: d["attachments"].__setitem__(p, r))
    log_event(f"auto-reconnect: reattached {server['name']}/{att['busid']} on port {new_port}")
    run_restart_actions(att.get("restart_actions", []))
    return {"status": "reattached", "port": new_port, "server": server["name"], "busid": att["busid"]}


async def reconnect_device(server_id: str, busid: str) -> dict | None:
    """Called from the /api/remote/devices/{busid}/reconnect push receiver.
    Returns None if we have no record of ever attaching this device (the
    push is a no-op for us), otherwise a status dict."""
    cfg = config.store.read()
    live_ports = {p.port for p in usbip_client.list_ports()}
    for port, att in cfg["attachments"].items():
        if att.get("server_id") != server_id or att.get("busid") != busid:
            continue
        if port in live_ports:
            return {"status": "already_live", "port": port}
        return await _restore_dropped_attachment(port, att)
    return None


async def _watchdog_tick() -> None:
    cfg = config.store.read()
    active_ports = {p.port for p in usbip_client.list_ports()}
    for port, att in list(cfg["attachments"].items()):
        if port in active_ports:
            continue
        await _restore_dropped_attachment(port, att)


async def watchdog_loop(interval: int = 15) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await _watchdog_tick()
        except Exception:
            logger.exception("watchdog tick failed")
