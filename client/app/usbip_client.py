"""Wrapper around the local `usbip` CLI and the vhci-hcd kernel driver for
attaching/detaching remote devices to this host's virtual USB controller.

Note: the server's TCP port is a *global* usbip option (--tcp-port), not a
per-subcommand flag - `usbip --tcp-port <port> attach -r <host> -b <busid>`.

We do not rely on `usbip port`'s text output to discover what's attached:
in the field it has proven unreliable on at least one real Debian install
("libusbip: error: fopen" / "read_record" - it fails to read its own
state-tracking file and its listing lags behind reality). Instead we read
the kernel's own /sys/devices/platform/vhci_hcd.*/status directly, which is
always live and exact, and is also what `usbip port` is documented to be
based on. This also means we never trust attach's stdout for the assigned
port (some usbip builds only print "using port <TCP-PORT>", which is the
*remote* TCP port, not the local one) - we snapshot ports before/after and
diff instead.

Known limitation: each vhci_hcd.N status file numbers its own ports
starting at 0, so if a system ever attaches enough devices to spill into a
second vhci_hcd instance, port numbers could collide across instances.
Realistic homelab use (a handful of shared devices) stays within the first
instance's ~30 ports, so this isn't handled beyond reading every instance.
"""
from __future__ import annotations

import glob
import logging
import os
import re
import stat
import time
from dataclasses import dataclass

from common.procutil import run, validate_busid, validate_hostname, validate_port_number

logger = logging.getLogger("usbip.client.usbip_client")

_VHCI_STATUS_GLOB = "/sys/devices/platform/vhci_hcd.*/status"
_STATUS_LINE_RE = re.compile(
    r"^\s*\S+\s+(?P<port>\d+)\s+(?P<sta>\d+)\s+\S+\s+\S+\s+\S+\s+(?P<busid>\S+)\s*$"
)
# Fallback text parser for `usbip port`, used only if the sysfs status
# files aren't present on this system at all.
_PORT_BLOCK_RE = re.compile(
    r"^Port (?P<port>\d+):.*$\n"
    r"(?:^[ \t].*$\n?)*?"
    r"^\s*(?P<local_busid>\d+-\d+(?:\.\d+)*)\s*->",
    re.MULTILINE,
)


class UsbipCommandError(RuntimeError):
    pass


@dataclass
class AttachedPort:
    port: str
    local_busid: str | None


def attach(host: str, usbip_port: int, busid: str) -> AttachedPort:
    """Returns the newly-attached AttachedPort (port number *and* the local
    busid the kernel assigned it) - callers should persist both, not just
    the port number. Port numbers are a small, reused space (see the module
    docstring's cross-vhci_hcd-instance caveat, and simply because ports get
    freed and reused as attachments come and go) - the local_busid is what
    actually lets a caller later tell "is the device I attached still the
    one sitting on this port" apart from "some other device now reuses the
    same port number", which a bare port-number comparison can't do."""
    validate_hostname(host)
    usbip_port = validate_port_number(usbip_port)
    validate_busid(busid)
    before_ports = {p.port for p in list_ports()}
    proc = run(
        ["usbip", "--tcp-port", str(usbip_port), "attach", "-r", host, "-b", busid],
        timeout=30,
    )
    output = f"{proc.stdout}\n{proc.stderr}"
    if proc.returncode != 0:
        raise UsbipCommandError(output.strip() or "usbip attach failed")

    # The kernel briefly lags behind a successful `usbip attach` before the
    # new port shows up in sysfs - poll for it instead of checking once.
    new_ports: list[AttachedPort] = []
    for _ in range(10):
        after_ports = list_ports()
        new_ports = [p for p in after_ports if p.port not in before_ports]
        if new_ports:
            break
        time.sleep(0.3)
    if len(new_ports) == 1:
        return new_ports[0]
    if len(new_ports) > 1:
        # Concurrent attach elsewhere raced us; pick the lowest new port and
        # log it so it's visible if this ever actually happens.
        new_ports.sort(key=lambda p: int(p.port))
        logger.warning(
            "multiple new vhci ports appeared after attach (%s); using the lowest",
            [p.port for p in new_ports],
        )
        return new_ports[0]
    raise UsbipCommandError(
        f"usbip attach reported success but no new local port appeared: {output.strip()}"
    )


def detach(local_port: str) -> None:
    if not local_port.isdigit():
        raise UsbipCommandError(f"invalid local port: {local_port!r}")
    proc = run(["usbip", "detach", "-p", local_port], timeout=20)
    if proc.returncode != 0:
        raise UsbipCommandError((proc.stderr or proc.stdout or "usbip detach failed").strip())


def _list_ports_via_sysfs() -> list[AttachedPort] | None:
    status_files = sorted(glob.glob(_VHCI_STATUS_GLOB))
    if not status_files:
        return None
    ports: list[AttachedPort] = []
    for path in status_files:
        try:
            with open(path) as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in lines[1:]:  # skip "hub port sta spd dev sockfd local_busid" header
            m = _STATUS_LINE_RE.match(line)
            if not m:
                continue
            busid = m.group("busid")
            if busid == "0-0":
                continue  # empty slot
            ports.append(AttachedPort(port=str(int(m.group("port"))), local_busid=busid))
    return ports


def _list_ports_via_cli() -> list[AttachedPort]:
    proc = run(["usbip", "port"], timeout=10)
    ports: list[AttachedPort] = []
    for m in _PORT_BLOCK_RE.finditer(proc.stdout):
        ports.append(
            AttachedPort(port=str(int(m.group("port"))), local_busid=m.group("local_busid"))
        )
    return ports


def list_ports() -> list[AttachedPort]:
    ports = _list_ports_via_sysfs()
    if ports is not None:
        return ports
    return _list_ports_via_cli()


def live_port_map() -> dict[str, str | None]:
    """{port: local_busid} for every currently-attached port - the shape
    callers need to check "is *my* attachment still the thing on this port"
    rather than just "is this port number attached to something"."""
    return {p.port: p.local_busid for p in list_ports()}


def is_port_active(local_port: str) -> bool:
    return any(p.port == local_port for p in list_ports())


def resolve_device_paths(local_busid: str) -> dict:
    """Best-effort mapping from a local (post-attach) busid to the actual
    /dev nodes it produced, so an admin can wire a new Docker container's
    `devices:` mapping straight to it. Every USB device gets a raw usbfs
    node (/dev/bus/usb/BBB/DDD); a serial-class device (the common case
    here - USB-UART bridges) also gets a /dev/ttyUSBx and, if udev created
    one, a stable /dev/serial/by-id/... symlink."""
    result: dict = {"tty": None, "by_id": [], "raw": None}
    if not local_busid:
        return result

    # The interface subdirectory (busid:config.interface) holds the tty
    # device - as either <iface>/ttyUSBx/ directly, or <iface>/tty/ttyUSBx/
    # depending on kernel/driver version. `tty?*` (not bare `tty`) avoids
    # matching the nested-layout directory itself as if it were the device.
    # Both patterns are a single, bounded glob level - deliberately NOT a
    # recursive/`**` glob into sysfs, which can hang for a very long time
    # chasing its symlink-heavy structure.
    tty_dirs = glob.glob(f"/sys/bus/usb/devices/{local_busid}:*/tty?*") or glob.glob(
        f"/sys/bus/usb/devices/{local_busid}:*/tty/tty?*"
    )
    if tty_dirs:
        result["tty"] = "/dev/" + tty_dirs[0].rstrip("/").rsplit("/", 1)[-1]

    try:
        with open(f"/sys/bus/usb/devices/{local_busid}/busnum") as f:
            busnum = int(f.read().strip())
        with open(f"/sys/bus/usb/devices/{local_busid}/devnum") as f:
            devnum = int(f.read().strip())
        result["raw"] = f"/dev/bus/usb/{busnum:03d}/{devnum:03d}"
    except (FileNotFoundError, OSError, ValueError):
        pass

    if result["tty"]:
        target = result["tty"].rsplit("/", 1)[-1]
        for link in glob.glob("/dev/serial/by-id/*"):
            try:
                if os.path.realpath(link).rsplit("/", 1)[-1] == target:
                    result["by_id"].append(link)
            except OSError:
                continue

    return result


# Where we put our own stable, app-managed device nodes for attachments
# (see update_attachment_node below). Distinct from /dev/serial/by-id,
# which is udev's - keyed off the physical dongle's own vendor/serial
# strings, so it changes identity whenever the underlying dongle changes
# (a group failing over to a backup server, or - since busids/ports are
# not stable identifiers - even a direct attachment relocating after a
# bus renumbering or reconnecting on a fresh port).
NODE_DIR = "/dev/usbip-web"

# systemd-tmpfiles config we maintain on the *host* (docker-compose
# mounts /etc/tmpfiles.d in) so the stable nodes get recreated at boot,
# before Docker starts any container. See write_tmpfiles_conf for why.
TMPFILES_DIR = "/etc/tmpfiles.d"
TMPFILES_PATH = f"{TMPFILES_DIR}/usbip-web.conf"


@dataclass
class StableNode:
    """What a stable node currently is, in the form the attachment record
    persists so the node can be recreated (identically) before the device
    itself is back - after a host reboot, /dev is a fresh tmpfs."""

    target: str  # the real node this mirrors right now, e.g. /dev/ttyUSB0 (informational)
    major: int
    minor: int
    mode: int  # permission bits only (0o660), not the file type
    uid: int
    gid: int

    def to_record(self) -> dict:
        return {
            "target": self.target,
            "major": self.major,
            "minor": self.minor,
            "mode": self.mode,
            "uid": self.uid,
            "gid": self.gid,
        }

    @classmethod
    def from_record(cls, rec: dict | None) -> StableNode | None:
        if not rec:
            return None
        try:
            return cls(
                target=str(rec.get("target") or ""),
                major=int(rec["major"]),
                minor=int(rec["minor"]),
                mode=int(rec.get("mode", 0o660)),
                uid=int(rec.get("uid", 0)),
                gid=int(rec.get("gid", 0)),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _place_node(key: str, node: StableNode) -> str | None:
    """Make NODE_DIR/<key> a character device node with node's major:minor,
    atomically, and only if it isn't one already. Leaving an already-correct
    node's inode alone matters: a downstream container's `devices:` mapping
    is a bind mount of that exact inode, so a reconnect that lands on the
    same major:minor keeps working in a running container with no restart.
    Falls back to a plain symlink if mknod isn't permitted here (the
    container isn't privileged), which still works for `devices:` mappings
    while the target exists. Returns the stable path, or None on failure."""
    os.makedirs(NODE_DIR, exist_ok=True)
    stable_path = f"{NODE_DIR}/{key}"
    rdev = os.makedev(node.major, node.minor)
    try:
        cur = os.lstat(stable_path)
        if stat.S_ISCHR(cur.st_mode) and cur.st_rdev == rdev:
            if stat.S_IMODE(cur.st_mode) != node.mode or (cur.st_uid, cur.st_gid) != (node.uid, node.gid):
                try:
                    os.chmod(stable_path, node.mode)
                    os.chown(stable_path, node.uid, node.gid)
                except OSError:
                    pass
            return stable_path
    except FileNotFoundError:
        pass

    tmp_path = f"{stable_path}.tmp-{os.getpid()}"
    try:
        try:
            os.mknod(tmp_path, stat.S_IFCHR | node.mode, rdev)
            os.chmod(tmp_path, node.mode)  # mknod applies the umask; mirror the target's mode exactly
            os.chown(tmp_path, node.uid, node.gid)
        except PermissionError:
            if not node.target:
                raise
            logger.warning(
                "mknod not permitted in this container; falling back to a symlink for %s "
                "(boot-time node persistence unavailable - run the client privileged)",
                key,
            )
            os.symlink(node.target, tmp_path)
        os.replace(tmp_path, stable_path)  # atomic rename over whatever was there (old node, legacy symlink)
    except OSError:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        logger.exception("failed to update stable device node for %s", key)
        return None
    return stable_path


def update_attachment_node(key: str, dev_paths: dict) -> tuple[str, StableNode] | None:
    """Mirror whichever real device node this attachment's current session
    produced (tty preferred - the common case for this app - falling back
    to the raw usbfs node) as a device node at NODE_DIR/<key>. `key` is
    whatever stable identity the caller has for "this logical attachment"
    across relocation - a group's own id for group attachments (stable
    across a primary/backup failover to a different physical dongle), or
    a direct attachment's own generated id (stable across busid/port
    changes on reconnect - see groups.py's id-keyed attachment records).
    Downstream config (a Docker `devices:` mapping, a fixed HA path) can
    reference this one path indefinitely instead of needing to be
    hand-edited every time the underlying device node changes.

    It's a real node (same major:minor as the target), not a symlink, for
    two reasons: it can exist *before* the device does - see
    place_persisted_node - so a `devices:` mapping can be resolved by
    Docker at boot even though the usbip attach hasn't happened yet; and
    a directory bind mount of NODE_DIR into a container gives that
    container a working device (a symlink to /dev/ttyUSBx would dangle
    inside a container that doesn't also have /dev/ttyUSBx).

    Returns (stable_path, node) or None if there's no device node yet."""
    target = dev_paths.get("tty") or dev_paths.get("raw")
    if not target:
        return None
    try:
        st = os.stat(target)
    except OSError:
        logger.warning("device node %s for %s vanished before it could be mirrored", target, key)
        return None
    if not stat.S_ISCHR(st.st_mode):
        return None
    node = StableNode(
        target=target,
        major=os.major(st.st_rdev),
        minor=os.minor(st.st_rdev),
        mode=stat.S_IMODE(st.st_mode),
        uid=st.st_uid,
        gid=st.st_gid,
    )
    try:
        stable_path = _place_node(key, node)
    except OSError:
        logger.exception("failed to update stable device node for %s", key)
        return None
    return (stable_path, node) if stable_path else None


def place_persisted_node(key: str, node: StableNode) -> str | None:
    """Recreate a stable node from what the attachment record persisted,
    without the device being live. Called at startup for every attachment
    that isn't (yet) attached, so downstream containers whose `devices:`
    mappings point at NODE_DIR/<key> can be started by Docker right away
    (a missing host path is a hard "failed to start container" error that
    Docker never retries on its own at boot). Opening the node before the
    device is attached just fails with ENXIO, which is the normal
    "device not there" error those apps already handle by retrying."""
    try:
        return _place_node(key, node)
    except OSError:
        logger.exception("failed to recreate persisted stable device node for %s", key)
        return None


def attachment_node_path(key: str) -> str | None:
    """The stable path for this key, if update_attachment_node (or the
    boot-time recreation) has created it and it hasn't since been removed.
    A legacy symlink from before nodes shipped counts too - it gets
    replaced by a node on the next attach/reconnect/startup."""
    path = f"{NODE_DIR}/{key}"
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return path if stat.S_ISCHR(st.st_mode) or stat.S_ISLNK(st.st_mode) else None


def remove_attachment_node(key: str) -> None:
    try:
        os.remove(f"{NODE_DIR}/{key}")
    except FileNotFoundError:
        pass
    except OSError:
        logger.exception("failed to remove stable device node for %s", key)


def tmpfiles_available() -> bool:
    """True when the host's /etc/tmpfiles.d is mounted into this container
    (see docker-compose.client.yml) so boot-time node recreation is
    possible at all."""
    return os.path.isdir(TMPFILES_DIR) and os.access(TMPFILES_DIR, os.W_OK)


def write_tmpfiles_conf(nodes: dict[str, StableNode]) -> bool:
    """Write a systemd-tmpfiles config on the host that recreates every
    stable node at boot. Why this exists: at boot, Docker starts *every*
    restart-policy container in parallel - this client, Home Assistant,
    zigbee2mqtt, ... all at once. A container whose `devices:` mapping
    points at NODE_DIR/<key> is resolved by Docker at start; if the path
    doesn't exist yet it fails with "error gathering device information
    ... no such file or directory" and stays down (start failures aren't
    retried by the restart policy). Recreating the nodes in-process at
    our own startup still loses that race. systemd-tmpfiles-setup-dev
    runs long before docker.service and creates `c` entries under /dev
    from this file, so the nodes are already there when Docker starts -
    `docker start` succeeds, the app inside gets ENXIO until the device is
    actually attached (a few seconds later, or minutes if the server has a
    zombie export to clear), and its own restart policy carries it through.

    Returns False (after logging once) when /etc/tmpfiles.d isn't mounted."""
    if not tmpfiles_available():
        return False
    lines = [
        "# Managed by usbip-web-client - do not edit; rewritten whenever an attachment changes.",
        "# Recreates this client's stable USB device nodes at boot, before Docker starts,",
        "# so downstream containers with devices: mappings on these paths can start even",
        "# though the usbip attach itself happens later. See usbip-web README.",
        f"d {NODE_DIR} 0755 root root -",
    ]
    for key in sorted(nodes):
        n = nodes[key]
        if n.target:
            lines.append(f"# {key}: last mirrored {n.target}")
        lines.append(f"c {NODE_DIR}/{key} {n.mode:04o} {n.uid} {n.gid} - {n.major}:{n.minor}")
    content = "\n".join(lines) + "\n"
    try:
        try:
            with open(TMPFILES_PATH) as f:
                if f.read() == content:
                    return True
        except OSError:
            pass
        tmp_path = f"{TMPFILES_PATH}.tmp-{os.getpid()}"
        with open(tmp_path, "w") as f:
            f.write(content)
        os.replace(tmp_path, TMPFILES_PATH)
    except OSError:
        logger.exception("failed to write %s", TMPFILES_PATH)
        return False
    return True
