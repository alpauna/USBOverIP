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

async function loadTokenStatus() {
  const status = document.getElementById("token-status");
  try {
    const data = await ajax("/api/token/status");
    status.textContent = data.has_token
      ? `Token active (created ${new Date(data.created_at).toLocaleString()})`
      : "No token generated yet. Clients cannot connect until you rotate one.";
  } catch (e) {
    status.textContent = "Unable to load token status.";
  }
}

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

document.getElementById("rotate-token-btn")?.addEventListener("click", async () => {
  if (!confirm("Rotating the token immediately invalidates the old one for every client. Continue?")) return;
  const data = await ajax("/api/token/rotate", { method: "POST" });
  const box = document.getElementById("token-value");
  box.hidden = false;
  box.textContent = `New token (copy it now, it will not be shown again):\n${data.token}`;
  await loadTokenStatus();
});

loadDevices();
loadTokenStatus();
loadName();
setInterval(loadDevices, 8000);
