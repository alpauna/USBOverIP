const CFG = window.__USBIP_CLIENT__ || { proxmoxAvailable: false, dockerAvailable: false };

let SERVERS = [];
let PROXMOX_VMS = null;

async function ajax(url, opts = {}) {
  opts.headers = Object.assign({ "X-Requested-With": "usbip-web" }, opts.headers || {});
  const res = await fetch(url, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const body = await res.json();
      msg = body.detail || msg;
    } catch (_) {}
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "text") node.textContent = v;
    else if (k === "html") node.innerHTML = v;
    else node.setAttribute(k, v);
  }
  for (const child of children) node.appendChild(child);
  return node;
}

// --------------------------------------------------------------- servers

async function loadServers() {
  const data = await ajax("/api/servers");
  SERVERS = data.servers;
  renderServers();
  renderBrowseServerSelect();
}

function renderServers() {
  const list = document.getElementById("servers-list");
  list.innerHTML = "";
  if (SERVERS.length === 0) {
    list.appendChild(el("p", { class: "muted", text: "No servers registered yet." }));
    return;
  }
  for (const s of SERVERS) {
    const badge = el("span", { class: "badge offline", text: "checking..." });
    checkServerHealth(s.id, badge);
    const del = el("button", { class: "secondary", text: "Remove" });
    del.addEventListener("click", async () => {
      if (!confirm(`Remove server "${s.name}"?`)) return;
      try {
        await ajax(`/api/servers/${s.id}`, { method: "DELETE" });
        await loadServers();
      } catch (e) {
        alert(e.message);
      }
    });
    const editBtn = el("button", { class: "secondary", text: "Edit" });
    editBtn.addEventListener("click", () => openServerEditPanel(s));

    const actions = [badge, editBtn, del];
    const infoLines = [
      el("div", { class: "muted tiny", text: `${s.host}:${s.api_port} (usbip port ${s.usbip_port})` }),
    ];
    if (s.wireguard) {
      const wgBadge = el("span", { class: "badge idle", id: `wg-badge-${s.id}`, text: "tunnel: checking..." });
      actions.splice(1, 0, wgBadge);
      infoLines.push(
        el("div", { class: "muted tiny" }, [
          document.createTextNode("Server tunnel IP: "),
          codeField(s.wireguard.server_wg_ip),
          document.createTextNode(" — put this in Host / IP (Edit) to route traffic through the tunnel"),
        ]),
        el("div", { class: "muted tiny" }, [
          document.createTextNode("Client tunnel IP: "),
          codeField(s.wireguard.assigned_ip),
          document.createTextNode(
            " — this client's own address on that server's tunnel; for reference/troubleshooting, " +
              "e.g. matching it up against the server's peer list if more than one client is connected"
          ),
        ])
      );
    } else {
      const enableBtn = el("button", { class: "secondary", text: "Enable tunnel" });
      enableBtn.addEventListener("click", async () => {
        enableBtn.disabled = true;
        enableBtn.textContent = "Enabling...";
        try {
          await ajax(`/api/servers/${s.id}/wireguard/enable`, { method: "POST" });
          await loadServers();
        } catch (e) {
          alert(e.message);
          enableBtn.disabled = false;
          enableBtn.textContent = "Enable tunnel";
        }
      });
      actions.splice(1, 0, enableBtn);
    }

    const item = el("div", { class: "list-item" }, [
      el("div", { class: "stack" }, [
        el("div", { html: `<strong>${s.name}</strong> <span class="pill">${s.role}</span>` }),
        ...infoLines,
      ]),
      el("div", { class: "actions" }, actions),
    ]);
    list.appendChild(item);
  }
}

let EDITING_SERVER_ID = null;

function openServerEditPanel(s) {
  EDITING_SERVER_ID = s.id;
  document.getElementById("server-edit-title").textContent = s.name;
  document.getElementById("server-edit-host").value = s.host;
  document.getElementById("server-edit-token").value = "";

  const hint = document.getElementById("server-edit-wg-hint");
  hint.innerHTML = "";
  if (s.wireguard) {
    hint.appendChild(document.createTextNode("Tunnel is enabled - server tunnel IP: "));
    hint.appendChild(codeField(s.wireguard.server_wg_ip));
    hint.appendChild(
      document.createTextNode(" — put that in Host / IP above to route traffic through the tunnel.")
    );
  } else {
    hint.textContent =
      'Click "Enable tunnel" on this server in the list first, then reopen Edit here to switch this to the ' +
      "assigned tunnel IP so subsequent API + USB/IP traffic rides the tunnel instead of the LAN.";
  }
  document.getElementById("server-edit-panel").hidden = false;
}

document.getElementById("server-edit-cancel-btn")?.addEventListener("click", () => {
  document.getElementById("server-edit-panel").hidden = true;
  EDITING_SERVER_ID = null;
});

document.getElementById("server-edit-save-btn")?.addEventListener("click", async () => {
  if (!EDITING_SERVER_ID) return;
  const host = document.getElementById("server-edit-host").value.trim();
  const token = document.getElementById("server-edit-token").value.trim();
  if (!host) return alert("Host / IP is required.");
  const body = { host };
  if (token) body.token = token;
  try {
    await ajax(`/api/servers/${EDITING_SERVER_ID}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    document.getElementById("server-edit-panel").hidden = true;
    EDITING_SERVER_ID = null;
    await loadServers();
  } catch (e) {
    alert(e.message);
  }
});

async function checkServerHealth(serverId, badgeEl) {
  try {
    const data = await ajax(`/api/servers/${serverId}/health`);
    badgeEl.textContent = data.online ? "online" : "offline";
    badgeEl.className = `badge ${data.online ? "online" : "offline"}`;
  } catch (e) {
    badgeEl.textContent = "offline";
    badgeEl.className = "badge offline";
  }
}

document.getElementById("add-server-form")?.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.target;
  const fd = new FormData(form);
  try {
    await ajax("/api/servers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: fd.get("name"),
        host: fd.get("host"),
        api_port: fd.get("api_port"),
        usbip_port: fd.get("usbip_port"),
        role: fd.get("role"),
        token: fd.get("token"),
      }),
    });
    form.reset();
    await loadServers();
  } catch (e) {
    alert(e.message);
  }
});

document.getElementById("refresh-servers-btn")?.addEventListener("click", loadServers);

function handshakeAge(epochSeconds) {
  if (!epochSeconds) return "no handshake yet";
  const ageSec = Date.now() / 1000 - epochSeconds;
  if (ageSec < 90) return "just now";
  if (ageSec < 3600) return `${Math.round(ageSec / 60)}m ago`;
  return `${Math.round(ageSec / 3600)}h ago`;
}

async function loadWireguardStatus() {
  try {
    const data = await ajax("/api/wireguard/status");
    for (const t of data.tunnels) {
      const badge = document.getElementById(`wg-badge-${t.server_id}`);
      if (!badge) continue;
      badge.textContent = t.up ? `tunnel: ${handshakeAge(t.latest_handshake)}` : "tunnel: down";
      badge.className = `badge ${t.up ? "online" : "offline"}`;
    }
  } catch (e) {
    // best-effort - leave whatever the badges already showed
  }
}

// ---------------------------------------------------------------- browse

function renderBrowseServerSelect() {
  const select = document.getElementById("browse-server-select");
  const current = select.value;
  select.innerHTML = "";
  for (const s of SERVERS) {
    select.appendChild(el("option", { value: s.id, text: s.name }));
  }
  if (current) select.value = current;
}

async function loadBrowseDevices() {
  const serverId = document.getElementById("browse-server-select").value;
  const tbody = document.querySelector("#browse-table tbody");
  tbody.innerHTML = "";
  if (!serverId) return;
  let devices;
  try {
    const data = await ajax(`/api/servers/${serverId}/devices`);
    devices = data.devices;
  } catch (e) {
    tbody.appendChild(el("tr", {}, [el("td", { colspan: "5", class: "muted", text: e.message })]));
    return;
  }
  for (const dev of devices) {
    const badge = el("span", { class: `badge ${dev.status}`, text: dev.status });
    // "unshared" is still clickable: attaching will ask the server to
    // share it first. Only a device already claimed by another client is
    // truly blocked.
    const attachBtn = el("button", { text: dev.status === "unshared" ? "Share & Attach" : "Attach" });
    attachBtn.disabled = dev.status === "shared_in_use";
    if (dev.status === "shared_in_use") attachBtn.title = "Already attached to another client";
    attachBtn.addEventListener("click", async () => {
      attachBtn.disabled = true;
      try {
        await ajax(`/api/servers/${serverId}/devices/${dev.busid}/attach`, { method: "POST" });
        await Promise.all([loadBrowseDevices(), loadAttachments()]);
      } catch (e) {
        alert(e.message);
        attachBtn.disabled = false;
      }
    });
    tbody.appendChild(
      el("tr", {}, [
        el("td", { text: dev.busid }),
        el("td", { text: dev.description || dev.label || "" }),
        el("td", { text: `${dev.vendor_id}:${dev.product_id}` }),
        el("td", {}, [badge]),
        el("td", {}, [attachBtn]),
      ])
    );
  }
}

document.getElementById("browse-refresh-btn")?.addEventListener("click", loadBrowseDevices);
document.getElementById("browse-server-select")?.addEventListener("change", loadBrowseDevices);

// --------------------------------------------------------- restart actions

function addRestartActionRow(container, initial = {}) {
  const typeSelect = el("select", {});
  typeSelect.appendChild(el("option", { value: "docker", text: "docker container" }));
  typeSelect.appendChild(el("option", { value: "systemd", text: "systemd service" }));
  if (initial.type) typeSelect.value = initial.type;
  const nameInput = el("input", { placeholder: "name e.g. zigbee2mqtt", value: initial.name || "" });
  const removeBtn = el("button", { type: "button", class: "secondary", text: "✕" });
  const row = el("div", { class: "restart-action-row" }, [typeSelect, nameInput, removeBtn]);
  removeBtn.addEventListener("click", () => row.remove());
  row._get = () => ({ type: typeSelect.value, name: nameInput.value.trim() });
  container.appendChild(row);
  return row;
}

function collectRestartActions(container) {
  return Array.from(container.children)
    .map((r) => r._get())
    .filter((a) => a.name);
}

function restartActionsSummary(actions) {
  if (!actions || actions.length === 0) return "";
  return actions.map((a) => `${a.type}:${a.name}`).join(", ");
}

document.getElementById("add-group-restart-row-btn")?.addEventListener("click", () => {
  addRestartActionRow(document.getElementById("group-restart-rows"));
});

// ---------------------------------------------------------------- groups

document.getElementById("add-candidate-row-btn")?.addEventListener("click", () => addCandidateRow());

function addCandidateRow() {
  const container = document.getElementById("candidate-rows");
  const select = el("select", { name: "candidate_server" });
  for (const s of SERVERS) select.appendChild(el("option", { value: s.id, text: s.name }));
  const busidInput = el("input", { placeholder: "busid e.g. 1-1", required: "required" });
  const labelInput = el("input", { placeholder: "label (optional)" });
  const removeBtn = el("button", { type: "button", class: "secondary", text: "✕" });
  const row = el("div", { class: "candidate-row" }, [select, busidInput, labelInput, removeBtn]);
  removeBtn.addEventListener("click", () => row.remove());
  row._get = () => ({ server_id: select.value, busid: busidInput.value.trim(), label: labelInput.value.trim() });
  container.appendChild(row);
}

document.getElementById("add-group-form")?.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.target;
  const rows = Array.from(document.getElementById("candidate-rows").children);
  const candidates = rows.map((r) => r._get());
  if (candidates.length === 0) {
    alert("Add at least one candidate server/device.");
    return;
  }
  const restartActions = collectRestartActions(document.getElementById("group-restart-rows"));
  try {
    await ajax("/api/groups", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: form.name.value,
        auto_failover: form.auto_failover.checked,
        candidates,
        restart_actions: restartActions,
      }),
    });
    form.reset();
    document.getElementById("candidate-rows").innerHTML = "";
    document.getElementById("group-restart-rows").innerHTML = "";
    await loadGroups();
  } catch (e) {
    alert(e.message);
  }
});

async function loadGroups() {
  const data = await ajax("/api/groups");
  const list = document.getElementById("groups-list");
  list.innerHTML = "";
  if (data.groups.length === 0) {
    list.appendChild(el("p", { class: "muted", text: "No groups yet." }));
    return;
  }
  for (const g of data.groups) {
    const chain = g.candidates.map((c) => {
      const server = SERVERS.find((s) => s.id === c.server_id);
      return `${server ? server.name : c.server_id}/${c.busid}`;
    }).join(" → ");
    const isActive = !!g.active_port;
    const badge = el("span", { class: `badge ${isActive ? "attached" : "idle"}`, text: isActive ? `attached (port ${g.active_port})` : "not attached" });
    const attachBtn = el("button", { text: "Attach" });
    attachBtn.disabled = isActive;
    attachBtn.addEventListener("click", async () => {
      attachBtn.disabled = true;
      try {
        await ajax(`/api/groups/${g.id}/attach`, { method: "POST" });
        await Promise.all([loadGroups(), loadAttachments()]);
      } catch (e) {
        alert(e.message);
        attachBtn.disabled = false;
      }
    });
    const detachBtn = el("button", { class: "secondary", text: "Detach" });
    detachBtn.disabled = !isActive;
    detachBtn.addEventListener("click", async () => {
      try {
        await ajax(`/api/groups/${g.id}/detach`, { method: "POST" });
        await Promise.all([loadGroups(), loadAttachments()]);
      } catch (e) {
        alert(e.message);
      }
    });
    const delBtn = el("button", { class: "secondary", text: "Delete" });
    delBtn.disabled = isActive;
    delBtn.addEventListener("click", async () => {
      if (!confirm(`Delete group "${g.name}"?`)) return;
      try {
        await ajax(`/api/groups/${g.id}`, { method: "DELETE" });
        await loadGroups();
      } catch (e) {
        alert(e.message);
      }
    });
    const editBtn = el("button", { class: "secondary", text: "Edit" });
    editBtn.addEventListener("click", () => openGroupEditPanel(g));
    const restartSummary = restartActionsSummary(g.restart_actions);
    list.appendChild(
      el("div", { class: "list-item" }, [
        el("div", { class: "stack" }, [
          el("div", { html: `<strong>${g.name}</strong>${g.auto_failover ? ' <span class="pill">auto-failover</span>' : ""}` }),
          el("div", { class: "muted tiny", text: chain }),
          restartSummary ? el("div", { class: "muted tiny", text: `restarts: ${restartSummary}` }) : el("span"),
        ]),
        el("div", { class: "actions" }, [badge, attachBtn, detachBtn, editBtn, delBtn]),
      ])
    );
  }
}

let EDITING_GROUP_ID = null;

function openGroupEditPanel(g) {
  EDITING_GROUP_ID = g.id;
  document.getElementById("group-edit-title").textContent = g.name;
  document.getElementById("group-edit-auto-failover").checked = !!g.auto_failover;
  const rows = document.getElementById("group-edit-restart-rows");
  rows.innerHTML = "";
  for (const action of g.restart_actions || []) addRestartActionRow(rows, action);
  document.getElementById("group-edit-panel").hidden = false;
}

document.getElementById("group-edit-add-restart-row-btn")?.addEventListener("click", () => {
  addRestartActionRow(document.getElementById("group-edit-restart-rows"));
});

document.getElementById("group-edit-cancel-btn")?.addEventListener("click", () => {
  document.getElementById("group-edit-panel").hidden = true;
  EDITING_GROUP_ID = null;
});

document.getElementById("group-edit-save-btn")?.addEventListener("click", async () => {
  if (!EDITING_GROUP_ID) return;
  try {
    await ajax(`/api/groups/${EDITING_GROUP_ID}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        auto_failover: document.getElementById("group-edit-auto-failover").checked,
        restart_actions: collectRestartActions(document.getElementById("group-edit-restart-rows")),
      }),
    });
    document.getElementById("group-edit-panel").hidden = true;
    EDITING_GROUP_ID = null;
    await loadGroups();
  } catch (e) {
    alert(e.message);
  }
});

// ----------------------------------------------------------- attachments

async function loadProxmoxVms() {
  if (PROXMOX_VMS !== null) return PROXMOX_VMS;
  try {
    const data = await ajax("/api/proxmox/vms");
    PROXMOX_VMS = data.vms;
  } catch (e) {
    PROXMOX_VMS = [];
  }
  return PROXMOX_VMS;
}

async function loadAttachments() {
  const data = await ajax("/api/attachments");
  const tbody = document.querySelector("#attachments-table tbody");
  const empty = document.getElementById("attachments-empty");
  tbody.innerHTML = "";
  empty.hidden = data.attachments.length !== 0;

  let vms = [];
  if (CFG.proxmoxAvailable) vms = await loadProxmoxVms();

  for (const att of data.attachments) {
    const statusBadge = el("span", { class: `badge ${att.live ? "attached" : "offline"}`, text: att.live ? "active" : "stale" });
    const detachBtn = el("button", { class: "secondary", text: "Detach" });
    detachBtn.addEventListener("click", async () => {
      try {
        await ajax(`/api/attachments/${att.port}/detach`, { method: "POST" });
        await loadAttachments();
      } catch (e) {
        alert(e.message);
      }
    });

    const extraActions = [];
    if (CFG.proxmoxAvailable) {
      if (att.proxmox) {
        const removeBtn = el("button", { class: "secondary", text: `Remove from VM ${att.proxmox.vmid}` });
        removeBtn.addEventListener("click", async () => {
          try {
            await ajax(`/api/attachments/${att.port}/proxmox-detach`, { method: "POST" });
            await loadAttachments();
          } catch (e) {
            alert(e.message);
          }
        });
        extraActions.push(removeBtn);
      } else {
        const vmSelect = el("select", {});
        for (const vm of vms) vmSelect.appendChild(el("option", { value: vm.vmid, text: `${vm.vmid} (${vm.name})` }));
        const sendBtn = el("button", { class: "secondary", text: "Send to VM" });
        sendBtn.addEventListener("click", async () => {
          if (!vmSelect.value) return alert("No VM selected.");
          try {
            await ajax(`/api/attachments/${att.port}/proxmox-attach`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ vmid: vmSelect.value }),
            });
            await loadAttachments();
          } catch (e) {
            alert(e.message);
          }
        });
        extraActions.push(vmSelect, sendBtn);
      }
    }

    const autoBadge = el("span", {
      class: `badge ${att.auto_failover ? "attached" : "idle"}`,
      text: att.auto_failover ? "on" : "off",
    });
    const detailsBtn = el("button", { class: "secondary", text: "Details" });
    detailsBtn.addEventListener("click", () => openAttachmentDetailsPanel(att.port));
    const actionCells = [detailsBtn, detachBtn, ...extraActions];
    if (!att.group_id) {
      const editBtn = el("button", { class: "secondary", text: "Edit" });
      editBtn.addEventListener("click", () => openAttachmentEditPanel(att));
      actionCells.push(editBtn);
    }

    tbody.appendChild(
      el("tr", {}, [
        el("td", { text: att.port }),
        el("td", { text: att.server_name }),
        el("td", { text: att.busid }),
        el("td", { text: att.label || (att.group_id ? "(group)" : "") }),
        el("td", {}, [autoBadge]),
        el("td", {}, [statusBadge]),
        el("td", { class: "actions" }, actionCells),
      ])
    );
  }
}

let EDITING_ATTACHMENT_PORT = null;

function openAttachmentEditPanel(att) {
  EDITING_ATTACHMENT_PORT = att.port;
  document.getElementById("attachment-edit-title").textContent = `${att.server_name}/${att.busid} (port ${att.port})`;
  document.getElementById("attachment-edit-label").value = att.label || "";
  document.getElementById("attachment-edit-auto-failover").checked = !!att.auto_failover;
  const rows = document.getElementById("attachment-edit-restart-rows");
  rows.innerHTML = "";
  for (const action of att.restart_actions || []) addRestartActionRow(rows, action);
  document.getElementById("attachment-edit-panel").hidden = false;
}

document.getElementById("attachment-edit-add-restart-row-btn")?.addEventListener("click", () => {
  addRestartActionRow(document.getElementById("attachment-edit-restart-rows"));
});

document.getElementById("attachment-edit-cancel-btn")?.addEventListener("click", () => {
  document.getElementById("attachment-edit-panel").hidden = true;
  EDITING_ATTACHMENT_PORT = null;
});

document.getElementById("attachment-edit-save-btn")?.addEventListener("click", async () => {
  if (!EDITING_ATTACHMENT_PORT) return;
  try {
    await ajax(`/api/attachments/${EDITING_ATTACHMENT_PORT}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        label: document.getElementById("attachment-edit-label").value,
        auto_failover: document.getElementById("attachment-edit-auto-failover").checked,
        restart_actions: collectRestartActions(document.getElementById("attachment-edit-restart-rows")),
      }),
    });
    document.getElementById("attachment-edit-panel").hidden = true;
    EDITING_ATTACHMENT_PORT = null;
    await loadAttachments();
  } catch (e) {
    alert(e.message);
  }
});

function codeField(text) {
  const node = el("code", { text, class: "tiny" });
  node.style.cursor = "pointer";
  node.title = "Click to select";
  node.addEventListener("click", () => {
    const range = document.createRange();
    range.selectNodeContents(node);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  });
  return node;
}

async function openAttachmentDetailsPanel(port) {
  const panel = document.getElementById("attachment-details-panel");
  const body = document.getElementById("attachment-details-body");
  document.getElementById("attachment-details-title").textContent = `Port ${port}`;
  body.innerHTML = "";
  body.appendChild(el("p", { class: "muted", text: "Loading..." }));
  panel.hidden = false;
  try {
    const d = await ajax(`/api/attachments/${port}/details`);
    body.innerHTML = "";
    const rows = [
      ["Server", `${d.server_name} / ${d.busid}`],
      ["Local bus ID", d.local_busid || "—"],
      ["Live", d.live ? "yes" : "no (stale)"],
    ];
    for (const [k, v] of rows) {
      body.appendChild(el("div", { text: `${k}: ${v}` }));
    }
    if (d.stable_path) {
      body.appendChild(
        el("div", {}, [
          document.createTextNode("Stable path (survives failover): "),
          codeField(d.stable_path),
        ])
      );
    } else if (d.group_id) {
      body.appendChild(
        el("p", { class: "muted", text: "No stable path yet (device not live)." })
      );
    }
    body.appendChild(el("div", { class: "muted", text: "Device paths (for a new container's devices: mapping):" }));
    if (d.dev_paths.tty) {
      body.appendChild(el("div", {}, [document.createTextNode("Serial (tty): "), codeField(d.dev_paths.tty)]));
    }
    for (const link of d.dev_paths.by_id) {
      body.appendChild(el("div", {}, [document.createTextNode("Stable by-id: "), codeField(link)]));
    }
    if (d.dev_paths.raw) {
      body.appendChild(el("div", {}, [document.createTextNode("Raw USB node: "), codeField(d.dev_paths.raw)]));
    }
    if (!d.dev_paths.tty && !d.dev_paths.raw) {
      body.appendChild(el("p", { class: "muted", text: "No device path could be resolved (device may not be live)." }));
    }
    if (d.restart_actions.length) {
      body.appendChild(
        el("div", { class: "muted", text: `Restart actions: ${restartActionsSummary(d.restart_actions)}` })
      );
    }
  } catch (e) {
    body.innerHTML = "";
    body.appendChild(el("p", { class: "muted", text: e.message }));
  }
}

document.getElementById("attachment-details-close-btn")?.addEventListener("click", () => {
  document.getElementById("attachment-details-panel").hidden = true;
});

document.getElementById("refresh-attachments-btn")?.addEventListener("click", loadAttachments);

// ------------------------------------------------------------------ docker

async function loadDockerContainers() {
  const tbody = document.querySelector("#docker-table tbody");
  if (!tbody) return;
  const data = await ajax("/api/docker/containers");
  tbody.innerHTML = "";
  for (const c of data.containers) {
    const restartBtn = el("button", { class: "secondary", text: "Restart" });
    restartBtn.addEventListener("click", async () => {
      if (!confirm(`Restart container "${c.name}"?`)) return;
      try {
        await ajax(`/api/docker/containers/${c.name}/restart`, { method: "POST" });
        await loadDockerContainers();
      } catch (e) {
        alert(e.message);
      }
    });
    tbody.appendChild(
      el("tr", {}, [
        el("td", { text: c.name }),
        el("td", { text: c.image }),
        el("td", { text: c.status }),
        el("td", {}, [restartBtn]),
      ])
    );
  }
}

document.getElementById("refresh-docker-btn")?.addEventListener("click", loadDockerContainers);

// ------------------------------------------------------------------ events

async function loadEvents() {
  const list = document.getElementById("events-list");
  if (!list) return;
  const data = await ajax("/api/events");
  list.innerHTML = "";
  for (const e of data.events) {
    list.appendChild(el("li", { text: `${new Date(e.time).toLocaleTimeString()} — ${e.message}` }));
  }
}

// ------------------------------------------------------------------- init

async function refreshAll() {
  await loadServers();
  await Promise.all([loadGroups(), loadAttachments(), loadEvents(), loadWireguardStatus()]);
  if (CFG.dockerAvailable) await loadDockerContainers();
}

refreshAll();
setInterval(() => {
  loadAttachments();
  loadEvents();
  loadWireguardStatus();
}, 8000);
