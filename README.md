# USB/IP Web (server + client)

A Dockerized, web-managed layer on top of Linux's native USB/IP
(`usbip`/`usbipd`/`vhci-hcd`) for sharing USB devices from one server box
to many client boxes (Proxmox nodes, a Home Assistant VM, etc.), with
per-device sharing control, cross-server failover, and Proxmox VM
passthrough.

It does **not** reimplement the USB/IP wire protocol. It wraps the
kernel's own `usbip-host` / `vhci-hcd` drivers and the `usbip` CLI (the
same mechanism a native `usbiphost`/`usbipclient` systemd setup uses) and
adds: authentication, a web UI, a device catalog, multi-server /
multi-client orchestration, and automatic fallback to a backup server.

## Architecture

```
                         ┌─────────────────────────┐
                         │  USB/IP server (1x)      │
                         │  physical USB hardware    │
                         │  usbip-web-server :8000   │  <- web UI + REST API
                         │  usbipd          :3240   │  <- raw USB/IP protocol
                         └─────────────┬────────────┘
                                       │
                 ┌─────────────────────┼─────────────────────┐
                 │                     │                     │
        ┌────────▼────────┐  ┌─────────▼────────┐  ┌─────────▼────────┐
        │ Proxmox node A   │  │ Proxmox node B    │  │ Home Assistant VM │
        │ usbip-web-client │  │ usbip-web-client  │  │ usbip-web-client  │
        │ :8001            │  │ :8001             │  │ :8001              │
        │ -> qm passthrough│  │ -> qm passthrough │  │ -> docker sibling  │
        └──────────────────┘  └───────────────────┘  └────────────────────┘
```

- **Server** (`server/`): runs on the box with the physical USB devices.
  Lists local devices, binds/unbinds them for export, issues a bearer
  token clients use to talk to its API. A device's true state
  (unshared / shared-idle / **in use by a client**) is read straight from
  the kernel (`/sys/bus/usb/devices/<busid>/usbip_status`), not tracked
  separately - see "Exclusivity" below.
- **Client** (`client/`): runs on every box that should be able to pull
  in a shared device (Proxmox nodes, the Home Assistant VM, ...). Browses
  one or more registered servers' devices, attaches/detaches them, and
  optionally passes an attached device through to a Proxmox VM or exposes
  it to a sibling Docker container.

You can run any number of servers and any number of clients. Each client
maintains its own list of servers and its own attachments.

## Exclusivity ("only one client at a time")

This is enforced by the kernel, not by this app: once a client has a
device attached, the server's `usbip-host` driver marks it `usbip_status
= 2` (in use) and refuses a second simultaneous attach. The web UI simply
surfaces that status and the client's own attach logic checks it before
attempting, so you see "in use" instead of racing the kernel. Physically
unplugging/replugging or restarting `usbipd` clears the state.

## Failover ("backup server")

**Status: implemented but not yet verified end-to-end against a real
backup server outage.** Group creation, priority ordering, and the
watchdog's reattach-on-drop path have each been exercised individually;
the full scenario (primary server actually goes offline while a group is
attached, watchdog notices, backup candidate takes over, downstream
container gets restarted) has not. Treat it as unproven until you've run
that drill yourself once against your own hardware.

A **device group** (client-side concept, `client/app/groups.py`) is a
named, ordered list of `(server, busid)` candidates representing "the
same logical device" across a primary and one or more backup servers -
e.g. the same model of Zigbee dongle plugged into two different USB/IP
servers. Attaching a group tries candidates in order, skipping any whose
server is unreachable or whose device is already in use. If you enable
"auto-failover" on a group, a background watchdog on the client detects
when the active attachment drops (primary server went offline) and
automatically attaches the next candidate, logging the event.

This only helps if you actually have redundant hardware (the same device
plugged into a backup server, or a second server holding an equivalent
device) - it can't fail over a single physical dongle to a server it
isn't plugged into.

### USB serial numbers: what they do and don't protect against

Both apps track each device's USB serial number (`/sys/bus/usb/devices/
<busid>/serial`) alongside its busid, to self-heal when busids shift
(Proxmox passthrough reconfig, a device's own USB reset, or a full host
bus renumbering can all move a device to a new busid with no warning -
see the server's `known_serials` snapshot and the client's per-server
relocation fallback in `groups.py`). **This matching is scoped to one
server's own device list.** A server, and each client's view of that
server, will only ever search for a matching serial among *that same
server's* current devices - serials are never compared *across* two
different servers.

That scoping matters for failover groups specifically:

- A group candidate list is built by hand (you pick which busid on the
  backup server corresponds to the same logical device) - the app has no
  way to verify the primary and backup candidates are actually
  equivalent hardware, and doesn't try to. Get the pairing right when you
  create the group.
- Because matching never crosses servers, a duplicate serial on the
  backup server can't cause the primary's relocation logic to pick the
  wrong device, or vice versa. The blast radius of a bad serial is always
  contained to the one server it's plugged into.

Within a single server, though, serial quality genuinely matters: cheap
USB-UART bridge clones (some CH340/CP210x knockoffs in particular) are
known to ship with a factory-programmed serial that's blank or identical
across every unit of that batch, regardless of how many you plug into
the same server. If two shared devices on one server share a serial, a
busid renumbering could relocate to the wrong one. The server dashboard's
device table already flags this - a serial that appears more than once
in that server's list is shown in red/bold. Treat that warning as
"relocation is unreliable for these devices," not just cosmetic; if you
see it, check `udevadm info -a -n /dev/ttyUSBx | grep -i serial` (or the
vendor's own config tool, if one exists) to confirm whether the dongle
really has a duplicate serial or just isn't exposing one, and consider
it a candidate for replacement if it's going into a failover-critical
role.

## Reconnect backoff and stale (zombie) exports

Every attachment's watchdog reconnect attempts are tracked with a
`failure_count`. After 3 consecutive failures the watchdog backs off
exponentially (60s, 120s, 240s, ... capped at 30 minutes) instead of
retrying every 15s tick forever - visible per-attachment on the client
dashboard, and logged to Recent Activity. A server-pushed "this device is
available again" notification always bypasses the backoff and retries
immediately, since it only fires when the server itself just changed
something concrete (rebound the device, an admin re-shared it).

**The recurring root cause behind a stuck backoff:** the client's local
session dies (container restart, network blip, a half-completed attach)
but the *server's* `usbip-host` kernel driver never released the export -
confirmed by `/sys/bus/usb/drivers/usbip-host/<busid>/usbip_status`
reading `2` (in use) with no client actually holding it. Every reattempt
then bounces off `Device busy (exported)` forever, because the client
detaching its own (already-dead) session can't fix a stale export on the
*server's* end - only the server unbinding and rebinding the device at
the kernel level clears it.

**Fixing it by hand:** on the server dashboard, each device row has an
**Unbind** button (next to Share/Unshare) that force-unbinds and rebinds
the device regardless of its currently-reported status - unlike Unshare,
which is disabled while the driver reports the device in use, this
reaches the stuck case specifically. It then re-notifies registered
clients so they retry immediately instead of waiting out the backoff.
Same operation from the CLI: `usbip unbind -b <busid> && usbip bind -b
<busid>` on the server.

**Fixing it automatically:** each attachment's edit panel on the client
dashboard has a checkbox, *"Force a server-side unbind/rebind after
repeated reconnect failures"* (`auto_rebind_on_backoff`). When enabled,
after 6 consecutive reconnect failures the client calls the server's
unbind/rebind endpoint on itself (using its own registered bearer token)
instead of waiting for an admin. Off by default - enable it per
attachment for devices where you've actually seen this failure mode.

The same checkbox also arms a **startup fast path**: when the *client
host* reboots, the server never sees the client leave - the TCP session
just stops - so every device the client had attached is left exported to
a client that no longer exists. Waiting for six failures meant, with the
backoff schedule, roughly eight minutes after every reboot before Home
Assistant & co. got their USB devices back (observed 2026-09-25). So
within the first three minutes after the client process starts, an
attachment that is not live locally and gets `Device busy (exported)`
from the server is treated as exactly that case and rebinds immediately,
once; the six-failure rule remains behind it. A plain client *container*
restart doesn't trigger this - vhci attachments live in the host kernel
and survive it, so those ports still show as live. Note that this is a
deliberate "the device is mine" claim: if another client really did take
the device in the meantime, the rebind takes it back - which is why it's
tied to the same opt-in checkbox rather than on by default.

**A second failure mode this uncovered:** vhci local port numbers are a
small, reused space - freed and reassigned as attachments come and go.
The watchdog and the server-push reconnect handler used to treat "some
attachment is live on port N" as proof that *this* attachment's own
stored port N was still it. In practice, when one attachment silently
dropped and a *different* attachment was later assigned that same port
number, the watchdog saw the number occupied and stopped trying to
reconnect the dead one - permanently, since nothing else would ever
prompt another attempt. Every attachment record now also stores the
local busid (e.g. `2-3`) the kernel assigned it at attach time, and
liveness checks require that to still match what's actually on that port
now, not just the bare port number. Attachments from before this change
fall back to the old port-only check until their next successful
reconnect fills in the new field.

## Security model (read this)

- Each web UI (server, client) has its own bcrypt-hashed admin password,
  set on first run at `/setup`. Sessions are signed cookies
  (`SameSite=Lax`); state-changing requests from the UI's own JS include
  an `X-Requested-With` header that's required server-side as a
  lightweight CSRF guard.
- Clients authenticate to a server's REST API with a bearer token you
  generate from the server's dashboard and paste into the client's "Add
  server" form. Rotating it immediately invalidates the old one. **That
  token travels in plain HTTP** (this app doesn't terminate TLS itself),
  so anyone who can sniff the LAN between a server and client can read it
  - see "WireGuard tunnel" below to close this off along with the next
  point.
- **The raw USB/IP protocol (TCP port 3240) has no authentication at
  all** - this is a property of `usbip`/`usbipd` itself, not something a
  web wrapper can add without a custom kernel driver. Anyone who can
  reach port 3240 on the server can attach any *bound* device directly
  with the stock `usbip` CLI, bypassing this app entirely. Mitigate by:
  - Only binding (sharing) devices you're actively using.
  - Firewalling TCP 3240 on the server to just your known client IPs
    (e.g. `nft`/`iptables`/your router), since `network_mode: host` in
    the provided compose file exposes it on the LAN by default.
  - Keeping this on a trusted LAN/VLAN, not the public internet.
- Containers run `privileged: true` with host `/dev`, `/sys`, and
  `/lib/modules` mounted - this is root-equivalent access to the host.
  That's inherent to manipulating USB kernel drivers and (for the client)
  running `qm`/`docker` against the host. Only deploy on hosts you
  already administer directly (your USB-IP box, your Proxmox nodes, your
  HA VM) - never on a shared/multi-tenant host.
- `passwords.hide` (your own deployment notes) and everything under
  `data/` (persisted admin password hashes, tokens, server registry) are
  gitignored. Never commit them.

## WireGuard tunnel (optional)

Encrypts the USB/IP wire protocol *and* the client API traffic (including
the bearer token, which otherwise travels in plain HTTP) between a server
and any client that opts in - see the two bolded caveats in "Security
model" above. Off by default; nothing changes for a client that never
enables it.

**How it works:** the server is the hub (one WireGuard identity, `wg0`,
generated automatically on first startup - inert with zero peers until a
client joins). Each client keeps one WireGuard identity of its own but
gets a **separate interface per server** (`wg-<server_id>`, not a shared
`wg0`) - WireGuard's anti-spoofing check requires a client's local address
to fall within whatever `AllowedIPs` a given server issued it, which two
different servers' independent IP-pool assignments can't both satisfy on
one shared address. Key exchange rides the same trust you've already
established: registering piggybacks on the client's existing bearer
token, so there's no public key to copy-paste by hand.

**Setup procedure**, once both boxes are running an image built after this
feature landed (`wireguard-tools`, and `nftables` on the server, are baked
into the Dockerfiles - a normal `docker compose up -d --build` picks them
up, nothing extra to install):

1. On the **server** dashboard, nothing to do - the WireGuard card shows
   its own public key and listen port (UDP 51820) automatically once it's
   up. If the card doesn't appear, `wireguard-tools` isn't installed in
   that container (rebuild the image).
2. On the **client** dashboard, find the server in the servers list and
   click **Enable tunnel**. This registers the client's public key with
   that server (the server allocates it an IP from its pool, e.g.
   `10.99.0.2`) and brings up the local `wg-<server_id>` interface. A
   badge appears reading `tunnel up, server @ 10.99.0.1` (or similar) -
   that address, not the client's own tunnel IP, is what belongs in the
   Host field in step 3.
3. Confirm the tunnel actually established before routing anything
   through it: the badge should read `tunnel <age>, server @ ...` where
   `<age>` is a recent handshake time, not "down". If it says "down",
   nothing has traversed the tunnel yet - WireGuard is lazy and won't
   handshake until the first packet tries to go through, which step 4
   below will trigger.
4. Click **Edit** on that same server row, replace the Host / IP field
   with the server address shown in the badge (e.g. `10.99.0.1`), and
   Save. From this point on, every API call and every `usbip attach` for
   this server rides the tunnel - no other setting or code path changes.
   Refresh the badge (or wait for the next poll) to confirm the handshake
   age is advancing.
5. Optional, and **only after confirming step 4 works**: on the server
   dashboard, enable "Require WireGuard for the USB/IP port (3240)". This
   firewalls port 3240 to the tunnel subnet + loopback so a direct-LAN
   attach attempt is rejected - but any client whose Host field you
   haven't switched to its tunnel IP yet loses access the moment you
   enable it. The dashboard/API port (8000) is deliberately never
   restricted by this toggle, both so a brand new client can still
   bootstrap a tunnel via step 2 and so you can't lock yourself out of
   the dashboard itself.

To verify none of this is placebo: `tcpdump -i <lan-iface> port 3240 or
port 8000` on the server while a tunneled client is active should show
**zero** packets - only UDP traffic on the WireGuard listen port should
appear on that interface.

## Deploying the server

On the box with the physical USB devices:

```bash
docker compose -f docker-compose.server.yml up -d --build
```

If a native `usbiphost`/`usbipd` systemd service is already running on
that box, stop and disable it first - both would fight over TCP 3240 and
the usbip-host kernel driver:

```bash
sudo systemctl disable --now usbiphost   # or: usbipd, whatever it's named locally
```

Then open `http://<server-ip>:8000`, set the admin password, and on the
dashboard:
1. Rotate the client access token and copy it somewhere safe (shown once).
2. Share ("bind") the devices you want clients to be able to use.
3. Optionally give the server a friendly name (e.g. `usbip-primary`).

## Deploying a client

On each Proxmox node / the Home Assistant VM / any other box:

```bash
docker compose -f docker-compose.client.yml up -d --build
```

If that box doesn't run Docker containers you want to expose devices to
(pure Proxmox node with no local Docker), you can drop the
`/var/run/docker.sock` volume line - the Docker panel just won't appear.

Open `http://<client-ip>:8001`, set the admin password, then:
1. **Add server**: name, host/IP, API port (8000), USB/IP port (3240),
   role (primary/backup), and the token from that server's dashboard.
   Add your backup server the same way if you have one.
2. **Browse devices** on a server and Attach one directly, or
3. **Create a group** to tie a primary + backup candidate together for
   automatic failover, then Attach the group.
4. Attached devices show up under **Attached devices**, with a "Detach"
   button and, where relevant, Proxmox/Docker follow-up actions.

## Proxmox VM passthrough

If the client detects `qm` on the host (via `nsenter`), attached devices
get a "Send to VM" control: pick a VM from the dropdown and it runs the
equivalent of `qm set <vmid> -usbN host=<local-busid>` on the actual
Proxmox host, using the next free `usb0`-`usb4` slot. "Remove from VM"
undoes it (`qm set <vmid> -delete usbN`). This requires the client
container to run directly on the Proxmox host (not inside a VM/LXC) with
`pid: host`, as set up in `docker-compose.client.yml`.

## Home Assistant (Docker) integration

If Home Assistant runs as a Docker container on the same VM as the
client, an attached device appears on the *host* at
`/dev/bus/usb/<bus>/<dev>`. For the HA container to pick it up live
(without a restart), its own compose/run config should already:

```yaml
services:
  homeassistant:
    devices: []            # don't hardcode a specific /dev/ttyUSBx path -
                            # bus/device numbers can change on reattach
    volumes:
      - /dev/bus/usb:/dev/bus/usb
    device_cgroup_rules:
      - 'c 189:* rmw'       # USB device class, all bus/device numbers
```

With that in place, no action is needed from this app beyond attaching
the device. If your HA setup instead references a fixed device path, use
the client's "Restart container" button (Local Docker containers panel)
after attaching so HA re-scans `/dev`.

If you'd rather point a specific HA integration at a fixed path directly
(many serial-based integrations ask you to type or pick one in their own
setup UI, rather than auto-discovering across all of `/dev/bus/usb`),
mount the stable-symlink directory into HA too and reference the
attachment id from its Details panel on the client dashboard - see
"Stable device paths for downstream containers" below for what that id
is and why it doesn't change across a reattach:

```yaml
services:
  homeassistant:
    volumes:
      - /dev/bus/usb:/dev/bus/usb
      - /dev/usbip-web:/dev/usbip-web   # so HA's own UI can reference a fixed /dev/usbip-web/<id> path
    device_cgroup_rules:
      - 'c 189:* rmw'
```

Then in that integration's own configuration, use
`/dev/usbip-web/<attachment-id>` instead of picking whatever
`/dev/ttyUSBx` happens to be current - it keeps working across a
reattach or relocation with no restart needed at all, since HA opens
that path itself rather than this app restarting the container for it.

## Stable device paths for downstream containers (recommended)

**Why this exists:** a raw `/dev/ttyUSBx` or `/dev/bus/usb/BBB/DDD` path
can change on every reattach - busid relocation, a whole-bus renumbering,
or just a normal reconnect can all hand the same logical device a
different bus/device number. Even udev's own `/dev/serial/by-id/...`
path, while tied to the physical dongle rather than a bus number, is
*recreated by udev on every device add/remove event* and can momentarily
not exist during a reattach cycle - which is enough to break a container
that only resolves the path once at startup.

This bit us for real: after a series of otherwise-routine reattach/
relocation events, three separate downstream containers on the same
Home Assistant box (zigbee2mqtt, zwavejs2mqtt, and an OpenThread border
router) all broke at once, each logging "No such file or directory" for
its serial device and looping on restart - despite the client dashboard
showing every attachment as `Live: yes`. The devices genuinely were
live; each container's device path just no longer pointed at anything.

**What the client already gives you:** every attachment (direct or via a
group) gets a device node at `/dev/usbip-web/<attachment-id>`, created on
attach and re-pointed automatically on every reconnect or relocation -
shown as "Stable path" in that attachment's Details panel. The id is
generated once and never changes for the life of that attachment record,
regardless of which busid, port, or (for a group) which physical dongle
on which server is behind it right now.

It is a real character device node (same major:minor as the `/dev/ttyUSBx`
or `/dev/bus/usb/...` node it mirrors), not a symlink, and the client
remembers what it was so it can recreate it **before the device is back**:

- At its own startup the client recreates the node for every attachment
  that isn't live yet, and with `/etc/tmpfiles.d` mounted in (see
  `docker-compose.client.yml`) it also maintains
  `/etc/tmpfiles.d/usbip-web.conf`, so systemd recreates the nodes at boot
  long before `docker.service` starts.
- Why that matters: at boot Docker starts every restart-policy container
  in parallel - this client, Home Assistant, zigbee2mqtt, ... all at once.
  A `devices:` mapping is resolved when the container *starts*; if the
  host path doesn't exist yet the start fails with
  `error gathering device information while adding custom device
  "/dev/usbip-web/<id>": no such file or directory`, and Docker never
  retries a failed start. Before nodes were persisted, that was every
  reboot: `/dev` is a fresh tmpfs, the client hadn't reattached yet, and
  the three serial-device containers (plus anything with `depends_on`
  them, i.e. Home Assistant itself) stayed down until someone redeployed
  the stack by hand. With the node already present, `docker start`
  succeeds; the app inside gets "no such device" until the attach lands a
  few seconds later and its own `restart:` policy carries it through.
- A reconnect that lands on the same major:minor is invisible to a
  running container (Docker bind-mounted that exact inode, and the kernel
  routes by number). Only when the numbers change does the container need
  a restart - that's what the attachment's restart actions are for.
- Because the entries are real nodes, bind-mounting the whole directory
  (`- /dev/usbip-web:/dev/usbip-web` under `volumes:`, plus
  `device_cgroup_rules` for the relevant majors - `c 188:* rmw` for
  `ttyUSB*`, `c 166:* rmw` for `ttyACM*`, `c 189:* rmw` for raw USB) also
  works, and tolerates the node appearing/changing at any time with no
  container restart at all. The trade-off is that the app inside must
  then be configured with the `/dev/usbip-web/<id>` path itself, since a
  directory mount can't rename a device the way `:targetpath` does.

**How to use it** in a downstream container's `devices:` mapping - map
the stable node to whatever path *that container's own config*
already expects. Here's the actual Home Assistant stack from the
incident above, fixed - three services, three different ways their own
config expects the device to show up, one consistent pattern:

```yaml
services:
  zigbee2mqtt:
    image: koenkk/zigbee2mqtt
    devices:
      # zigbee2mqtt's own adapter setting still says /dev/ttyUSB0 -
      # give it that path, sourced from the stable node instead of
      # the raw (unstable) /dev/ttyUSBx node.
      - /dev/usbip-web/<zigbee-attachment-id>:/dev/ttyUSB0
    volumes:
      - ./zigbee2mqtt-data:/app/data
    restart: unless-stopped

  zwavejs2mqtt:
    image: zwavejs/zwavejs2mqtt:latest
    devices:
      # zwavejs2mqtt's settings.json says /dev/zwave.
      - /dev/usbip-web/<zwave-attachment-id>:/dev/zwave
    volumes:
      - ./zwave-data:/usr/src/app/store
    restart: unless-stopped

  border-router:
    container_name: otbr
    image: openthread/border-router
    devices:
      # otbr has no fixed internal path expectation of its own - it
      # reads OT_RCP_DEVICE below, so the stable node can be mapped
      # in as itself (no :targetpath needed) as long as the env var
      # below references that exact same path.
      - /dev/usbip-web/<thread-attachment-id>
      - /dev/net/tun
    environment:
      - OT_RCP_DEVICE=spinel+hdlc+uart:///dev/usbip-web/<thread-attachment-id>?uart-baudrate=460800
    restart: unless-stopped
```

Get each `<...-attachment-id>` from that device's Details panel on the
client dashboard ("Stable path": `/dev/usbip-web/<id>`).

**The mistake that caused the incident above:** a bare
`- /dev/usbip-web/<attachment-id>` (no `:targetpath`) maps the device to
that *same path* inside the container, not to the path the app is
actually configured to open - so `OT_RCP_DEVICE`, zwavejs2mqtt's
`/dev/zwave` setting, or zigbee2mqtt's `/dev/ttyUSB0` adapter path all
kept pointing at a path that no longer existed inside that container,
even though the stable node itself was completely correct on the
host. Always include the `:targetpath` half, matching whatever the
downstream app's own config says.

**After a reboot, the stack won't start at all** - `docker ps -a` shows
the device containers as `Created`/exited and `journalctl -u docker`
says `error gathering device information while adding custom device
"/dev/usbip-web/<id>": no such file or directory`: the node didn't exist
when Docker tried to start them. Check the attachment's Details panel -
it says whether the node is "recreated at boot". If it isn't, the client
is running without `/etc/tmpfiles.d` mounted in (older
`docker-compose.client.yml`) - add the mount and recreate the client
container. To recover right now, wait for the client's Events log to
show the stable device path again (it may take a few minutes if the
server had a stale export to clear - see "Reconnect backoff and stale
(zombie) exports"), then `docker start` the failed containers or bring
the stack up again. Don't "re-pull and redeploy" the whole stack as a
reflex: it tears down the containers that *did* start, and if it fails
the same way you end up with nothing running.

**If this happens again** - a container logs "No such file or
directory" for its serial device (or loops on restart) despite the
client dashboard showing the attachment as live:

1. Confirm what's actually mapped in vs. what the app expects:
   ```bash
   docker inspect <container> --format '{{range .HostConfig.Devices}}{{.PathOnHost}} -> {{.PathInContainer}}{{println}}{{end}}'
   docker exec <container> ls -la <path the app's own config expects>
   ```
2. Fix the `devices:` entry (or environment variable, e.g. an
   `OT_RCP_DEVICE`-style spinel URL) to point at
   `/dev/usbip-web/<attachment-id>:<path-the-app-expects>` - the
   attachment id is shown as the stable path in the client's Attachment
   Details panel.
3. **Both `devices:` mappings and environment variables are resolved
   once, at container creation - a plain `docker restart` will not pick
   up the change.** Force-recreate the affected service(s):
   ```bash
   docker compose -p <project-name> up -d --force-recreate <service...>
   ```

## Repository layout

```
common/            Shared library (subprocess safety, hashing, config store, web auth)
server/            usbip-web-server: FastAPI app, Dockerfile, entrypoint
client/            usbip-web-client: FastAPI app, Dockerfile, entrypoint
docker-compose.server.yml
docker-compose.client.yml
passwords.hide     Your own local deployment notes - gitignored, never commit
```

## Local development (without Docker)

Each app can run outside Docker for iterating on the UI, but device
binding/attach/proxmox/docker actions need root and the relevant CLIs
(`usbip`, `qm`, `docker`) present, so full functionality really only
shows up on the target boxes.

```bash
cd server && pip install -r requirements.txt
PYTHONPATH=.. USBIP_DATA_DIR=./data uvicorn app.main:app --reload --port 8000
```
