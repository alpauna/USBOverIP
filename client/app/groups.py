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
import time
import uuid
from collections import deque

from . import config, docker_helper, remote_client, systemd_helper, usbip_client

logger = logging.getLogger("usbip.client.groups")

EVENTS: deque[dict] = deque(maxlen=50)

# Backoff for the watchdog's blind polling of a repeatedly-failing
# attachment (e.g. a device that's physically unplugged): after
# BACKOFF_THRESHOLD consecutive reconnect failures, space out further
# *watchdog-driven* attempts exponentially instead of hammering every
# watchdog tick forever. A server-pushed reconnect notice always bypasses
# this - it only fires when the server itself just changed something
# (rebound the device, an admin re-shared it), which is a concrete reason
# to try again regardless of recent history, not blind repetition.
BACKOFF_THRESHOLD = 3
BACKOFF_BASE_SECONDS = 60
BACKOFF_MAX_SECONDS = 1800

# If an attachment has auto_rebind_on_backoff set, a run of this many
# consecutive reconnect failures (two backoff cycles in) is treated as
# evidence of a stale/zombie export on the server rather than a transient
# blip, and triggers one force unbind/rebind request to the server (see
# main.py's /api/devices/{busid}/rebind) instead of waiting on an admin.
# Fires once per drop-out - _note_reconnect_failure resets the counter on
# the next successful reconnect, which re-arms it for next time.
FORCE_REBIND_AFTER_FAILURES = 6

# Startup fast path for the same flag. When this *host* reboots, the server
# never sees the client go away - the TCP session just stops - so its
# usbip-host driver keeps every device we had "exported" (in use) to a
# client that no longer exists. Every reattach then bounces off "Device
# busy (exported)" until the 6-failure rule above finally fires, which
# with the backoff schedule is ~8 minutes of Home Assistant & co. having
# no USB devices after every reboot (observed 2026-09-25). Within this
# many seconds of the client process starting, an attachment that is not
# live here and gets "busy" from the server is treated as that exact
# situation and rebinds immediately, once. A plain container restart
# doesn't trigger it: vhci attachments live in the host kernel and
# survive the container, so those ports still show as live.
STARTUP_REBIND_GRACE_SECONDS = 180
_PROCESS_STARTED = time.monotonic()
_startup_rebind_requested: set[str] = set()


class GroupError(Exception):
    pass


def log_event(message: str) -> None:
    EVENTS.appendleft({"time": datetime.datetime.utcnow().isoformat() + "Z", "message": message})
    logger.info(message)


def _note_reconnect_failure(att_id: str) -> int:
    result_count = 0

    def _bump(d, i=att_id):
        nonlocal result_count
        att = d["attachments"].get(i)
        if not att:
            return
        count = att.get("failure_count", 0) + 1
        att["failure_count"] = count
        result_count = count
        if count >= BACKOFF_THRESHOLD:
            delay = min(BACKOFF_BASE_SECONDS * (2 ** (count - BACKOFF_THRESHOLD)), BACKOFF_MAX_SECONDS)
            next_retry = datetime.datetime.utcnow() + datetime.timedelta(seconds=delay)
            att["next_retry_at"] = next_retry.isoformat() + "Z"
            log_event(
                f"{att.get('label') or att.get('busid')}: {count} consecutive reconnect failures, "
                f"backing off the watchdog for {delay}s (a server push still retries immediately)"
            )

    config.store.update(_bump)
    return result_count


def _is_busy_error(error: str) -> bool:
    # `usbip attach` wording: "Attach Request for 1-2 failed - Device busy (exported)"
    return "busy" in error.lower()


async def _maybe_force_rebind(
    att: dict, att_id: str, server: dict, busid: str, failure_count: int, error: str
) -> None:
    if not att.get("auto_rebind_on_backoff"):
        return
    label = att.get("label") or busid
    if failure_count == FORCE_REBIND_AFTER_FAILURES:
        why = f"{failure_count} consecutive failures"
    elif (
        _is_busy_error(error)
        and att_id not in _startup_rebind_requested
        and time.monotonic() - _PROCESS_STARTED < STARTUP_REBIND_GRACE_SECONDS
    ):
        # See STARTUP_REBIND_GRACE_SECONDS. The one-shot set is per process
        # on purpose: if the rebind doesn't clear it, the regular
        # 6-failure rule is still there behind it.
        _startup_rebind_requested.add(att_id)
        why = "'device busy' right after this client started (stale export from before a reboot)"
    else:
        return
    try:
        await remote_client.request_rebind(server["host"], server["api_port"], server["token"], busid)
        log_event(
            f"{label}: {why}, requested a force unbind/rebind on {server['name']} to clear a possible stale export"
        )
    except remote_client.RemoteError as e:
        log_event(f"{label}: force rebind request to {server['name']} failed: {e}")


def _note_reconnect_success(att_id: str) -> None:
    def _reset(d, i=att_id):
        att = d["attachments"].get(i)
        if att and (att.get("failure_count") or att.get("next_retry_at")):
            att["failure_count"] = 0
            att["next_retry_at"] = None

    config.store.update(_reset)


def _watchdog_should_skip(att: dict) -> bool:
    next_retry_at = att.get("next_retry_at")
    if not next_retry_at:
        return False
    try:
        return datetime.datetime.fromisoformat(next_retry_at.rstrip("Z")) > datetime.datetime.utcnow()
    except ValueError:
        return False


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


def update_node_for_port(key: str, port: str, att_id: str) -> str | None:
    """Resolve whatever device node `port` currently produced and make the
    stable node for `key` mirror it - shared by group attach, direct
    attach, reconnect and startup, so all four keep the node in sync the
    same way. What the node is (major:minor, owner, mode) is persisted on
    the attachment record `att_id` so it can be recreated before the
    device is back after a reboot (see restore_persisted_nodes)."""
    live = next((p for p in usbip_client.list_ports() if p.port == port), None)
    dev_paths = (
        usbip_client.resolve_device_paths(live.local_busid)
        if live and live.local_busid
        else {"tty": None, "by_id": [], "raw": None}
    )
    result = usbip_client.update_attachment_node(key, dev_paths)
    if not result:
        return None
    stable_path, node = result

    def _persist(d, i=att_id, n=node.to_record()):
        att = d["attachments"].get(i)
        if att is not None:
            att["stable_node"] = n

    config.store.update(_persist)
    sync_tmpfiles_conf()
    return stable_path


def _persisted_nodes(cfg: dict) -> dict[str, usbip_client.StableNode]:
    nodes: dict[str, usbip_client.StableNode] = {}
    for att_id, att in cfg["attachments"].items():
        node = usbip_client.StableNode.from_record(att.get("stable_node"))
        if node:
            nodes[att.get("group_id") or att_id] = node
    return nodes


def sync_tmpfiles_conf() -> None:
    """Rewrite the host's tmpfiles.d entry from the current attachment
    records, so a reboot recreates exactly the current set of nodes. No-op
    (logged at startup, not here) if /etc/tmpfiles.d isn't mounted in."""
    usbip_client.write_tmpfiles_conf(_persisted_nodes(config.store.read()))


def restore_persisted_nodes() -> None:
    """Called once at startup, before the watchdog's first tick. Live
    attachments (a container restart - vhci ports survive it) get their
    node refreshed; everything else gets its node recreated from the
    persisted major:minor so downstream containers whose `devices:`
    mapping points at it can be started *now*, not after the reconnect
    lands. Never raises."""
    cfg = config.store.read()
    active_ports = {p.port for p in usbip_client.list_ports()}
    for att_id, att in cfg["attachments"].items():
        key = att.get("group_id") or att_id
        label = att.get("label") or att.get("busid") or att_id
        try:
            if att.get("port") in active_ports:
                stable_path = update_node_for_port(key, att["port"], att_id)
                if stable_path:
                    logger.info("refreshed stable device node for live attachment %s: %s", label, stable_path)
                continue
            node = usbip_client.StableNode.from_record(att.get("stable_node"))
            if not node:
                continue
            stable_path = usbip_client.place_persisted_node(key, node)
            if stable_path:
                log_event(
                    f"{label}: recreated stable device node {stable_path} ({node.major}:{node.minor}) "
                    f"ahead of its reconnect"
                )
        except Exception:
            logger.exception("stable device node restore failed for attachment %s", att_id)
    sync_tmpfiles_conf()
    if cfg["attachments"] and not usbip_client.tmpfiles_available():
        logger.warning(
            "%s is not mounted into this container: stable device nodes will only exist once this "
            "client has started, so containers with devices: mappings on them can fail to start at "
            "boot - mount it as in docker-compose.client.yml",
            usbip_client.TMPFILES_DIR,
        )


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


def _active_attachment_for_group(cfg: dict, group_id: str) -> tuple[str, dict] | None:
    for att_id, att in cfg["attachments"].items():
        if att.get("group_id") == group_id:
            return att_id, att
    return None


async def attach_group(group_id: str) -> dict:
    cfg = config.store.read()
    group = cfg["groups"].get(group_id)
    if not group:
        raise GroupError("unknown group")
    if _active_attachment_for_group(cfg, group_id):
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
            attached = usbip_client.attach(server["host"], server["usbip_port"], candidate["busid"])
        except usbip_client.UsbipCommandError as e:
            errors.append(f"{server['name']}/{candidate['busid']}: {e}")
            continue
        now = datetime.datetime.utcnow().isoformat() + "Z"
        att_id = uuid.uuid4().hex[:8]
        record = {
            "id": att_id,
            "server_id": server["id"],
            "busid": candidate["busid"],
            "serial": serial,
            "label": candidate.get("label") or group["name"],
            "group_id": group_id,
            "attached_at": now,
            "auto_failover": bool(group.get("auto_failover", True)),
            "restart_actions": [],  # group-level actions are run below, not stored per-attachment
            "port": attached.port,
            "local_busid": attached.local_busid,
        }
        config.store.update(lambda d, i=att_id, r=record: d["attachments"].__setitem__(i, r))
        log_event(f"group '{group['name']}' attached via {server['name']}/{candidate['busid']} (port {attached.port})")

        stable_path = update_node_for_port(group_id, attached.port, att_id)
        if stable_path:
            log_event(f"group '{group['name']}' stable device path: {stable_path}")

        run_restart_actions(group.get("restart_actions", []))
        return {"port": attached.port, "server_id": server["id"], "server": server["name"], "busid": candidate["busid"]}

    log_event(f"group '{group['name']}' attach failed: no candidate available")
    raise GroupError("no candidate available: " + "; ".join(errors))


async def detach_group(group_id: str) -> None:
    cfg = config.store.read()
    found = _active_attachment_for_group(cfg, group_id)
    if not found:
        raise GroupError("group is not currently attached")
    att_id, att = found
    port = att["port"]
    usbip_client.detach(port)
    config.store.update(lambda d, i=att_id: d["attachments"].pop(i, None))
    usbip_client.remove_attachment_node(group_id)
    sync_tmpfiles_conf()
    log_event(f"group detached (was on port {port})")


async def _restore_dropped_attachment(att_id: str, att: dict) -> dict:
    """Common recovery path for one attachment that just dropped, used by
    both the watchdog and the server-pushed reconnect notice.

    Keyed by att_id, a stable id generated once when the attachment was
    first created - NOT by local vhci port number. Port numbers are a
    small, reused integer space the kernel assigns dynamically; keying by
    port meant that when two attachments dropped and reconnected around
    the same time, a freshly-reattached one could be handed a port number
    that collided with another attachment's still-pending record under
    that same key, silently destroying it (a real incident). The port is
    now just a field on the record, updated in place on every reattach.

    Important: the record is only removed once a reattach actually
    succeeds. A failed attempt (e.g. it fires before the server has
    finished rebinding) leaves the record in place so the *next* watchdog
    tick - or a later push notification - gets another chance instead of
    silently giving up after one race-prone try."""
    port = att.get("port")
    log_event(f"port {port} not attached (server={att.get('server_id')} busid={att.get('busid')}); attempting reconnect")

    if not att.get("auto_failover", True):
        config.store.update(lambda d, i=att_id: d["attachments"].pop(i, None))
        return {"status": "dropped"}

    if att.get("group_id"):
        # attach_group() refuses to run if it sees *any* attachment record
        # for this group_id, live or not - pop the dead one first so a
        # retry isn't permanently blocked by its own stale record, and put
        # it back unchanged if this attempt also fails.
        config.store.update(lambda d, i=att_id: d["attachments"].pop(i, None))
        try:
            result = await attach_group(att["group_id"])
        except GroupError as e:
            log_event(f"auto-reconnect failed: {e}")
            config.store.update(lambda d, i=att_id, r=att: d["attachments"].__setitem__(i, r))
            _note_reconnect_failure(att_id)
            return {"status": "failed", "error": str(e)}
        log_event(f"auto-reconnect: reattached via {result['server']}/{result['busid']}")
        return {"status": "reattached", **result}

    cfg = config.store.read()
    server = cfg["servers"].get(att.get("server_id"))
    if not server:
        log_event(f"auto-reconnect failed: server {att.get('server_id')} is no longer registered")
        _note_reconnect_failure(att_id)
        return {"status": "failed", "error": "server not registered"}

    target_busid = att["busid"]
    try:
        attached = usbip_client.attach(server["host"], server["usbip_port"], target_busid)
    except usbip_client.UsbipCommandError as e:
        # The device may have relocated to a different busid on the server
        # (a whole-bus USB renumbering shifts everything, with no warning,
        # even though nothing physically changed). If we know its serial,
        # ask the server for its current device list and retry once
        # wherever that same serial actually is now.
        relocated_busid = await _find_relocated_busid(server, att.get("serial"), target_busid)
        if not relocated_busid:
            log_event(f"auto-reconnect failed for {server['name']}/{target_busid}: {e}")
            count = _note_reconnect_failure(att_id)
            await _maybe_force_rebind(att, att_id, server, target_busid, count, str(e))
            return {"status": "failed", "error": str(e)}
        log_event(
            f"{server['name']}/{target_busid} not found; relocated to {relocated_busid} "
            f"(same serial {att.get('serial')}) - retrying"
        )
        try:
            attached = usbip_client.attach(server["host"], server["usbip_port"], relocated_busid)
        except usbip_client.UsbipCommandError as e2:
            log_event(f"auto-reconnect failed for {server['name']}/{relocated_busid}: {e2}")
            count = _note_reconnect_failure(att_id)
            await _maybe_force_rebind(att, att_id, server, relocated_busid, count, str(e2))
            return {"status": "failed", "error": str(e2)}
        target_busid = relocated_busid

    now = datetime.datetime.utcnow().isoformat() + "Z"
    new_record = {
        **att,
        "busid": target_busid,
        "attached_at": now,
        "port": attached.port,
        "local_busid": attached.local_busid,
    }
    config.store.update(lambda d, i=att_id, r=new_record: d["attachments"].__setitem__(i, r))
    _note_reconnect_success(att_id)
    log_event(f"auto-reconnect: reattached {server['name']}/{target_busid} on port {attached.port}")

    stable_path = update_node_for_port(att_id, attached.port, att_id)
    if stable_path:
        log_event(f"{server['name']}/{target_busid} stable device path: {stable_path}")

    run_restart_actions(att.get("restart_actions", []))
    return {"status": "reattached", "port": attached.port, "server": server["name"], "busid": target_busid}


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
    itself. Only used as a fallback when there's no direct busid match.

    Deliberately does NOT trust local port occupancy as proof the
    connection is actually alive: this push arrives specifically because
    the server just (re)bound this device, and - confirmed by two real
    production incidents - that can leave an existing local attachment
    silently dead ("zombie": vhci_hcd still reports the port as occupied,
    but the underlying session is gone) with no way for either side to
    notice on its own. So any existing port for this attachment gets
    force-detached before reattaching, rather than skipped as
    already-live. If the server also still thinks the device is in use
    (a stale export that survived on its end too - seen when the
    underlying transport was severed abnormally, e.g. by a firewall rule
    dropping packets instead of resetting the connection), the reattach
    below will fail with "device busy" and this still needs a manual
    `usbip unbind`/`usbip bind` on the server to clear - detaching our
    own zombie can't fix a zombie on the other end.

    Port numbers get reused as attachments come and go, so "this
    attachment's stored port is currently occupied" isn't proof it's
    occupied by *this* attachment - it may just as well be a different,
    genuinely-live attachment that was assigned the same number later. We
    only force-detach when the local_busid actually attached to that port
    right now still matches what we recorded at attach time (or we never
    recorded one, e.g. an older attachment from before this check
    existed) - otherwise detaching would kill someone else's live
    connection instead of clearing our own zombie."""
    cfg = config.store.read()
    live = usbip_client.live_port_map()

    for att_id, att in cfg["attachments"].items():
        if att.get("server_id") != server_id or att.get("busid") != busid:
            continue
        port = att.get("port")
        stored_local_busid = att.get("local_busid")
        if port in live:
            if stored_local_busid is None or stored_local_busid == live[port]:
                try:
                    usbip_client.detach(port)
                    log_event(f"reconnect: detached possibly-stale port {port} before reattaching")
                except usbip_client.UsbipCommandError as e:
                    log_event(f"reconnect: could not detach possibly-stale port {port}: {e}")
            else:
                log_event(
                    f"reconnect: port {port} is live but now belongs to a different local "
                    f"device than this attachment last used - not touching it"
                )
        return await _restore_dropped_attachment(att_id, att)

    if serial:
        for att_id, att in cfg["attachments"].items():
            if att.get("server_id") != server_id or att.get("serial") != serial:
                continue
            port = att.get("port")
            stored_local_busid = att.get("local_busid")
            if port in live and (stored_local_busid is None or stored_local_busid == live[port]):
                continue  # something else is already live on this port
            config.store.update(lambda d, i=att_id, b=busid: d["attachments"][i].__setitem__("busid", b))
            return await _restore_dropped_attachment(att_id, {**att, "busid": busid})

    return None


async def _watchdog_tick() -> None:
    cfg = config.store.read()
    live = usbip_client.live_port_map()
    for att_id, att in list(cfg["attachments"].items()):
        port = att.get("port")
        stored_local_busid = att.get("local_busid")
        # Live only if something is actually attached on that port number
        # *and* (when we know it) it's still the same local device we
        # attached there - a bare port-number match can't tell "still
        # ours" apart from "port number got reused by a different
        # attachment after ours silently dropped".
        if port in live and (stored_local_busid is None or stored_local_busid == live[port]):
            continue
        if _watchdog_should_skip(att):
            continue
        await _restore_dropped_attachment(att_id, att)


async def watchdog_loop(interval: int = 15) -> None:
    # First tick runs immediately: after a host reboot every attachment
    # needs restoring and each second of delay is a second Home Assistant
    # & co. spend without their devices. Subsequent ticks are paced.
    while True:
        try:
            await _watchdog_tick()
        except Exception:
            logger.exception("watchdog tick failed")
        await asyncio.sleep(interval)
