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
"""
from __future__ import annotations

import asyncio
import datetime
import logging
from collections import deque

from . import config, remote_client, usbip_client

logger = logging.getLogger("usbip.client.groups")

EVENTS: deque[dict] = deque(maxlen=50)


class GroupError(Exception):
    pass


def log_event(message: str) -> None:
    EVENTS.appendleft({"time": datetime.datetime.utcnow().isoformat() + "Z", "message": message})
    logger.info(message)


# internal alias kept for readability at call sites within this module
_log_event = log_event


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
            "auto_failover": bool(group.get("auto_failover", False)),
        }
        config.store.update(lambda d, p=port, r=record: d["attachments"].__setitem__(p, r))
        _log_event(f"group '{group['name']}' attached via {server['name']}/{candidate['busid']} (port {port})")
        return {"port": port, "server_id": server["id"], "server": server["name"], "busid": candidate["busid"]}

    _log_event(f"group '{group['name']}' attach failed: no candidate available")
    raise GroupError("no candidate available: " + "; ".join(errors))


async def detach_group(group_id: str) -> None:
    cfg = config.store.read()
    port = _active_port_for_group(cfg, group_id)
    if not port:
        raise GroupError("group is not currently attached")
    usbip_client.detach(port)
    config.store.update(lambda d: d["attachments"].pop(port, None))
    _log_event(f"group detached (was on port {port})")


async def _watchdog_tick() -> None:
    cfg = config.store.read()
    active_ports = {p.port for p in usbip_client.list_ports()}
    for port, att in list(cfg["attachments"].items()):
        if port in active_ports:
            continue
        config.store.update(lambda d, p=port: d["attachments"].pop(p, None))
        _log_event(f"attachment on port {port} dropped (server={att.get('server_id')} busid={att.get('busid')})")
        if att.get("auto_failover") and att.get("group_id"):
            try:
                result = await attach_group(att["group_id"])
                _log_event(f"auto-failover: reattached via {result['server']}/{result['busid']}")
            except GroupError as e:
                _log_event(f"auto-failover failed: {e}")


async def watchdog_loop(interval: int = 15) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await _watchdog_tick()
        except Exception:
            logger.exception("watchdog tick failed")
