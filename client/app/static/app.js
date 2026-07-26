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
    const item = el("div", { class: "list-item" }, [
      el("div", { class: "stack" }, [
        el("div", { html: `<strong>${s.name}</strong> <span class="pill">${s.role}</span>` }),
        el("div", { class: "muted tiny", text: `${s.host}:${s.api_port} (usbip port ${s.usbip_port})` }),
      ]),
      el("div", { class: "actions" }, [badge, del]),
    ]);
    list.appendChild(item);
  }
}

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
    const attachBtn = el("button", { text: "Attach" });
    attachBtn.disabled = dev.status !== "shared_idle";
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
  try {
    await ajax("/api/groups", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: form.name.value,
        auto_failover: form.auto_failover.checked,
        candidates,
      }),
    });
    form.reset();
    document.getElementById("candidate-rows").innerHTML = "";
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
    list.appendChild(
      el("div", { class: "list-item" }, [
        el("div", { class: "stack" }, [
          el("div", { html: `<strong>${g.name}</strong>${g.auto_failover ? ' <span class="pill">auto-failover</span>' : ""}` }),
          el("div", { class: "muted tiny", text: chain }),
        ]),
        el("div", { class: "actions" }, [badge, attachBtn, detachBtn, delBtn]),
      ])
    );
  }
}

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

    tbody.appendChild(
      el("tr", {}, [
        el("td", { text: att.port }),
        el("td", { text: att.server_name }),
        el("td", { text: att.busid }),
        el("td", { text: att.label || (att.group_id ? "(group)" : "") }),
        el("td", {}, [statusBadge]),
        el("td", { class: "actions" }, [detachBtn, ...extraActions]),
      ])
    );
  }
}

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
  await Promise.all([loadGroups(), loadAttachments(), loadEvents()]);
  if (CFG.dockerAvailable) await loadDockerContainers();
}

refreshAll();
setInterval(() => {
  loadAttachments();
  loadEvents();
}, 8000);
