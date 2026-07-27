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


async def _candidate_status(server: dict, busid: str) -> tuple[bool, str, str]:
    try:
        devices = await remote_client.fetch_devices(server["host"], server["api_port"], server["token"])
    except remote_client.RemoteError as e:
        return False, f"unreachable ({e})", ""
    for d in devices:
        if d["busid"] == busid:
            if d["status"] == "shared_idle":
                return True, "available", d.get("serial", "")
            return False, f"status is {d['status']}", d.get("serial", "")
    return False, "device not currently shared on that server", ""


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
        ok, reason, serial = await _candidate_status(server, candidate["busid"])
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
            "serial": serial,
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
    both the watchdog and the server-pushed reconnect notice.

    Important: the record is only removed once a reattach actually
    succeeds. A failed attempt (e.g. it fires before the server has
    finished rebinding) leaves the record in place so the *next* watchdog
    tick - or a later push notification - gets another chance instead of
    silently giving up after one race-prone try."""
    log_event(f"port {port} not attached (server={att.get('server_id')} busid={att.get('busid')}); attempting reconnect")

    if not att.get("auto_failover", True):
        config.store.update(lambda d, p=port: d["attachments"].pop(p, None))
        return {"status": "dropped"}

    if att.get("group_id"):
        # attach_group() refuses to run if it sees *any* attachment record
        # for this group_id, live or not - pop the dead one first so a
        # retry isn't permanently blocked by its own stale record, and put
        # it back unchanged if this attempt also fails.
        config.store.update(lambda d, p=port: d["attachments"].pop(p, None))
        try:
            result = await attach_group(att["group_id"])
        except GroupError as e:
            log_event(f"auto-reconnect failed: {e}")
            config.store.update(lambda d, p=port, r=att: d["attachments"].__setitem__(p, r))
            return {"status": "failed", "error": str(e)}
        log_event(f"auto-reconnect: reattached via {result['server']}/{result['busid']}")
        return {"status": "reattached", **result}

    cfg = config.store.read()
    server = cfg["servers"].get(att.get("server_id"))
    if not server:
        log_event(f"auto-reconnect failed: server {att.get('server_id')} is no longer registered")
        return {"status": "failed", "error": "server not registered"}

    target_busid = att["busid"]
    try:
        new_port = usbip_client.attach(server["host"], server["usbip_port"], target_busid)
    except usbip_client.UsbipCommandError as e:
        # The device may have relocated to a different busid on the server
        # (a whole-bus USB renumbering shifts everything, with no warning,
        # even though nothing physically changed). If we know its serial,
        # ask the server for its current device list and retry once
        # wherever that same serial actually is now.
        relocated_busid = await _find_relocated_busid(server, att.get("serial"), target_busid)
        if not relocated_busid:
            log_event(f"auto-reconnect failed for {server['name']}/{target_busid}: {e}")
            return {"status": "failed", "error": str(e)}
        log_event(
            f"{server['name']}/{target_busid} not found; relocated to {relocated_busid} "
            f"(same serial {att.get('serial')}) - retrying"
        )
        try:
            new_port = usbip_client.attach(server["host"], server["usbip_port"], relocated_busid)
        except usbip_client.UsbipCommandError as e2:
            log_event(f"auto-reconnect failed for {server['name']}/{relocated_busid}: {e2}")
            return {"status": "failed", "error": str(e2)}
        target_busid = relocated_busid

    now = datetime.datetime.utcnow().isoformat() + "Z"
    new_record = {**att, "busid": target_busid, "attached_at": now}

    def _replace(d, old_port=port, new_port=new_port, record=new_record):
        d["attachments"].pop(old_port, None)
        d["attachments"][new_port] = record

    config.store.update(_replace)
    log_event(f"auto-reconnect: reattached {server['name']}/{target_busid} on port {new_port}")
    run_restart_actions(att.get("restart_actions", []))
    return {"status": "reattached", "port": new_port, "server": server["name"], "busid": target_busid}


async def _find_relocated_busid(server: dict, serial: str | None, old_busid: str) -> str | None:
    if not serial:
        return None
    try:
        devices = await remote_client.fetch_devices(server["host"], server["api_port"], server["token"])
    except remote_client.RemoteError:
        return None
    match = next(
        (d for d in devices if d.get("serial") == serial and d["busid"] != old_busid),
        None,
    )
    if match and match["status"] == "shared_idle":
        return match["busid"]
    return None


async def reconnect_device(server_id: str, busid: str, serial: str = "") -> dict | None:
    """Called from the /api/remote/devices/{busid}/reconnect push receiver.
    Returns None if we have no record of ever attaching this device (the
    push is a no-op for us), otherwise a status dict.

    `serial` (passed by the server alongside the push) lets us recognize
    a device we have an attachment for even when *our* stored busid for
    it is stale - the same relocation the server already corrected for
    itself. Only used as a fallback when there's no direct busid match."""
    cfg = config.store.read()
    live_ports = {p.port for p in usbip_client.list_ports()}

    for port, att in cfg["attachments"].items():
        if att.get("server_id") != server_id or att.get("busid") != busid:
            continue
        if port in live_ports:
            return {"status": "already_live", "port": port}
        return await _restore_dropped_attachment(port, att)

    if serial:
        for port, att in cfg["attachments"].items():
            if att.get("server_id") != server_id or att.get("serial") != serial:
                continue
            if port in live_ports:
                continue  # something else is already live on this port
            config.store.update(lambda d, p=port, b=busid: d["attachments"][p].__setitem__("busid", b))
            return await _restore_dropped_attachment(port, {**att, "busid": busid})

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
