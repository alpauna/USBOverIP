const STATUS_LABEL = {
  unshared: "Not shared",
  shared_idle: "Shared (idle)",
  shared_in_use: "Shared (in use)",
  unknown: "Unknown",
};

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

async function loadDevices() {
  const tbody = document.querySelector("#devices-table tbody");
  const empty = document.getElementById("devices-empty");
  try {
    const data = await ajax("/api/devices");
    tbody.innerHTML = "";
    empty.hidden = data.devices.length !== 0;
    for (const dev of data.devices) {
      const badge = el("span", {
        class: `badge ${dev.status}`,
        text: STATUS_LABEL[dev.status] || dev.status,
      });
      const labelInput = el("input", { value: dev.label || "", placeholder: "optional label" });
      labelInput.addEventListener("change", async () => {
        await ajax(`/api/devices/${dev.busid}/label`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ label: labelInput.value }),
        });
      });
      const actionBtn = el("button", {
        class: dev.status === "unshared" ? "" : "secondary",
        text: dev.status === "unshared" ? "Share" : "Unshare",
      });
      actionBtn.disabled = dev.status === "shared_in_use";
      actionBtn.title = dev.status === "shared_in_use" ? "Detach client first" : "";
      actionBtn.addEventListener("click", async () => {
        actionBtn.disabled = true;
        try {
          const action = dev.status === "unshared" ? "share" : "unshare";
          await ajax(`/api/devices/${dev.busid}/${action}`, { method: "POST" });
          await loadDevices();
        } catch (e) {
          alert(e.message);
          actionBtn.disabled = false;
        }
      });
      const row = el("tr", {}, [
        el("td", { text: dev.busid }),
        el("td", { text: dev.description }),
        el("td", { text: `${dev.vendor_id}:${dev.product_id}` }),
        el("td", {}, [labelInput]),
        el("td", {}, [badge]),
        el("td", {}, [actionBtn]),
      ]);
      tbody.appendChild(row);
    }
  } catch (e) {
    console.error(e);
  }
}

async function loadClients() {
  const list = document.getElementById("clients-list");
  try {
    const data = await ajax("/api/clients");
    list.innerHTML = "";
    if (data.clients.length === 0) {
      list.appendChild(el("p", { class: "muted", text: "No clients registered yet." }));
      return;
    }
    for (const c of data.clients) {
      const lastSeen = c.last_seen ? new Date(c.last_seen).toLocaleString() : "never";
      const rotateBtn = el("button", { class: "secondary", text: "Rotate token" });
      rotateBtn.addEventListener("click", async () => {
        if (!confirm(`Rotate the token for "${c.name}"? Its old token stops working immediately.`)) return;
        const data = await ajax(`/api/clients/${c.id}/rotate`, { method: "POST" });
        const box = document.getElementById("new-client-token");
        box.hidden = false;
        box.textContent = `New token for ${c.name} (copy it now, it will not be shown again):\n${data.token}`;
      });
      const removeBtn = el("button", { class: "secondary", text: "Remove" });
      removeBtn.addEventListener("click", async () => {
        if (!confirm(`Remove client "${c.name}"?`)) return;
        await ajax(`/api/clients/${c.id}`, { method: "DELETE" });
        await loadClients();
      });
      list.appendChild(
        el("div", { class: "list-item" }, [
          el("div", { class: "stack" }, [
            el("div", { html: `<strong>${c.name}</strong>` }),
            el("div", { class: "muted tiny", text: `${c.host}:${c.api_port} • last seen ${lastSeen}` }),
          ]),
          el("div", { class: "actions" }, [rotateBtn, removeBtn]),
        ])
      );
    }
  } catch (e) {
    list.innerHTML = "";
    list.appendChild(el("p", { class: "muted", text: "Unable to load clients." }));
  }
}

document.getElementById("add-client-form")?.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.target;
  const fd = new FormData(form);
  try {
    const data = await ajax("/api/clients", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: fd.get("name"),
        host: fd.get("host"),
        api_port: fd.get("api_port"),
      }),
    });
    form.reset();
    const box = document.getElementById("new-client-token");
    box.hidden = false;
    box.textContent = `Token for ${data.client.name} (copy it now, it will not be shown again):\n${data.token}`;
    await loadClients();
  } catch (e) {
    alert(e.message);
  }
});

document.getElementById("refresh-clients-btn")?.addEventListener("click", loadClients);

async function loadName() {
  try {
    const data = await ajax("/api/info");
    document.getElementById("server-name-input").value = data.name;
  } catch (e) {}
}

document.getElementById("save-name-btn")?.addEventListener("click", async () => {
  const name = document.getElementById("server-name-input").value.trim();
  await ajax("/api/settings/name", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
});

document.getElementById("refresh-btn")?.addEventListener("click", loadDevices);

loadDevices();
loadClients();
loadName();
setInterval(loadDevices, 8000);
