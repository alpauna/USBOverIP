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

## Security model (read this)

- Each web UI (server, client) has its own bcrypt-hashed admin password,
  set on first run at `/setup`. Sessions are signed cookies
  (`SameSite=Lax`); state-changing requests from the UI's own JS include
  an `X-Requested-With` header that's required server-side as a
  lightweight CSRF guard.
- Clients authenticate to a server's REST API with a bearer token you
  generate from the server's dashboard and paste into the client's "Add
  server" form. Rotating it immediately invalidates the old one.
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
