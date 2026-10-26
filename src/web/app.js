const $ = (id) => document.getElementById(id);

let busyKey = null;
let ipBlockBusy = false;
let ipProgressTimer = null;
let refreshTimer = null;
let updateBusy = false;

function setDot(element, state) {
  element.className = `status-dot ${state}`;
}

function showMessage(text, kind = "success") {
  const box = $("message");
  box.textContent = text;
  box.className = `message ${kind}`;
  box.hidden = false;
  window.clearTimeout(showMessage.timer);
  showMessage.timer = window.setTimeout(() => { box.hidden = true; }, 5000);
}

function showUpdateMessage(text, kind = "success", timeout = 6000) {
  const box = $("updateMessage");
  box.textContent = text;
  box.className = `message ${kind}`;
  box.hidden = false;
  window.clearTimeout(showUpdateMessage.timer);

  if (timeout > 0) {
    showUpdateMessage.timer = window.setTimeout(
      () => { box.hidden = true; },
      timeout
    );
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    headers: options.body ? { "Content-Type": "application/json" } : {},
    ...options,
  });

  let payload = {};

  try {
    payload = await response.json();
  } catch (_) {}

  if (!response.ok || payload.ok === false) {
    throw new Error(payload.error || `Request failed (${response.status})`);
  }

  return payload;
}

function renderStatus(payload) {
  const proton = payload.proton || {};
  const wan = payload.wan || {};
  const lan = payload.lan || {};

  $("routerVersion").textContent = payload.version || "v0.0.1";

  $("protonServer").textContent = proton.server || "—";
  $("protonProtocol").textContent = proton.protocol || "—";
  $("protonLoad").textContent = proton.load || "—";

  setDot(
    $("protonDot"),
    proton.connected ? "good" : "bad"
  );


  $("wanGateway").textContent = wan.gateway || "—";
  $("lanAddress").textContent = lan.address || "—";
  $("lanInterface").textContent = lan.interface || "—";

  setDot(
    $("wanDot"),
    wan.ready ? "good" : "bad"
  );
    $("clientsCard").hidden = !(
      proton.connected && wan.ready
    );

}

function textCell(value, className = "") {
  const cell = document.createElement("td");

  if (className) {
    cell.className = className;
  }

  cell.textContent = value;
  return cell;
}

function badge(text, kind) {
  const span = document.createElement("span");
  span.className = `badge ${kind}`;
  span.textContent = text;
  return span;
}

function renderClients(payload) {
  const clients = payload.clients || [];
  const counts = payload.counts || {};

  $("totalCount").textContent =
    counts.total ?? clients.length;

  $("onlineCount").textContent =
    counts.online ?? 0;

  $("bypassCount").textContent =
    counts.bypass ?? 0;

  const body = $("clientRows");
  body.replaceChildren();

  if (!clients.length) {
    const row = document.createElement("tr");
    const cell = textCell(
      "No LAN clients have been observed yet.",
      "empty"
    );

    cell.colSpan = 6;
    row.appendChild(cell);
    body.appendChild(row);
    return;
  }

  for (const client of clients) {
    const key = `${client.mac}|${client.ip}`;
    const row = document.createElement("tr");

    row.appendChild(
      textCell(
        client.hostname || "Unknown device",
        "device-name"
      )
    );

    row.appendChild(
      textCell(
        client.ip || "—",
        "mono"
      )
    );

    row.appendChild(
      textCell(
        client.mac || "—",
        "mono"
      )
    );

    const statusCell = document.createElement("td");

    statusCell.appendChild(
      badge(
        client.online ? "Online" : "Offline",
        client.online ? "online" : "offline"
      )
    );

    row.appendChild(statusCell);

    const routeCell = document.createElement("td");

    routeCell.appendChild(
      badge(
        client.bypass ? "Bypassed Proton" : "Proton",
        client.bypass ? "bypass" : "proton"
      )
    );

    row.appendChild(routeCell);

    const actionCell = document.createElement("td");
    actionCell.className = "action-column";

    const button = document.createElement("button");
    button.type = "button";

    button.className =
      `toggle-button ${client.bypass ? "remove" : "add"}`;

    button.textContent =
      busyKey === key
        ? "Working..."
        : (client.bypass ? "Use Proton" : "Bypass");

    button.disabled = busyKey !== null;

    button.addEventListener(
      "click",
      () => changeBypass(client)
    );

    actionCell.appendChild(button);
    row.appendChild(actionCell);

    body.appendChild(row);
  }
}

async function changeBypass(client) {
  const key = `${client.mac}|${client.ip}`;

  if (busyKey) {
    return;
  }

  busyKey = key;

  try {
    await refreshClients();

    const method =
      client.bypass ? "DELETE" : "POST";

    const payload = await api(
      "/api/bypass",
      {
        method,
        body: JSON.stringify({
          mac: client.mac,
          ip: client.ip,
        }),
      }
    );

    showMessage(
      payload.message || "Routing updated."
    );
  } catch (error) {
    showMessage(
      error.message || "Unable to update routing.",
      "error"
    );
  } finally {
    busyKey = null;
    await refreshClients();
  }
}

function showIpBlockMessage(
  text,
  kind = "success"
) {
  const box = $("ipBlockMessage");

  box.textContent = text;
  box.className = `message ${kind}`;
  box.hidden = false;

  window.clearTimeout(
    showIpBlockMessage.timer
  );

  showIpBlockMessage.timer =
    window.setTimeout(
      () => {
        box.hidden = true;
      },
      6000
    );
}


function readableIpMode(value) {
  if (!value) {
    return "—";
  }

  return value.replaceAll(
    "-",
    " "
  );
}


function scheduleIpProgressPoll(
  refreshing
) {
  if (
    !refreshing
    || ipBlockBusy
  ) {
    if (ipProgressTimer !== null) {
      window.clearTimeout(
        ipProgressTimer
      );

      ipProgressTimer = null;
    }

    return;
  }

  if (ipProgressTimer !== null) {
    return;
  }

  ipProgressTimer =
    window.setTimeout(
      async () => {
        ipProgressTimer = null;

        if (document.hidden) {
          scheduleIpProgressPoll(
            true
          );

          return;
        }

        await refreshIpBlockStatus();
      },
      1000
    );
}


function renderIpBlock(payload) {
  const state =
    payload.ip_block || {};

  const ready =
    Boolean(state.ready);

  const refreshing =
    Boolean(state.refreshing);

  const progress =
    Math.max(
      0,
      Math.min(
        100,
        Number(state.progress) || 0
      )
    );

  const progressPhase =
    state.progress_phase
    || "Starting refresh...";



  const progressWrap =
    $("ipBlockProgress");

  const progressBar =
    $("ipBlockProgressBar");

  progressWrap.hidden =
    !(refreshing || ipBlockBusy);

  progressBar.value =
    progress;

  progressBar.textContent =
    `${progress}%`;

  $("ipBlockProgressPercent").textContent =
    `${progress}%`;

  $("ipBlockProgressLabel").textContent =
    progressPhase;

  scheduleIpProgressPoll(
    refreshing
  );

  $("ipBlockEndpointCount").textContent =
    state.ipv4_count ?? "—";

  $("ipBlockCidrCount").textContent =
    state.cidr_count ?? "—";

  const updated =
    state.updated || "";

  $("ipBlockUpdated").dataset.raw =
    updated;

  if (updated) {
    const parsed =
      new Date(updated);

    $("ipBlockUpdated").textContent =
      Number.isNaN(
        parsed.getTime()
      )
        ? updated
        : parsed.toLocaleString();

  } else {
    $("ipBlockUpdated").textContent =
      "—";
  }

  $("ipBlockNext").textContent =
    state.next_refresh || "—";



  const button =
    $("ipBlockRefreshButton");

  button.disabled =
    ipBlockBusy || refreshing;

  button.textContent =
    ipBlockBusy || refreshing
      ? "Refreshing..."
      : "Refresh Endpoints";
}


async function refreshIpBlockStatus() {
  try {
    renderIpBlock(
      await api(
        "/api/ip-block"
      )
    );

  } catch (_) {

  }
}


async function startIpBlockRefresh() {
  if (ipBlockBusy) {
    return;
  }

  ipBlockBusy = true;

  const button =
    $("ipBlockRefreshButton");

  const before =
    $("ipBlockUpdated").dataset.raw
    || "";

  button.disabled = true;

  button.textContent =
    "Refreshing...";

  try {
    const result = await api(
      "/api/ip-block/refresh",
      {
        method: "POST",
      }
    );

    showIpBlockMessage(
      result.message
      || "IP blocklist refresh started."
    );

    let completed = false;

    for (
      let attempt = 0;
      attempt < 60;
      attempt += 1
    ) {
      await new Promise(
        (resolve) => {
          window.setTimeout(
            resolve,
            1000
          );
        }
      );

      const payload = await api(
        "/api/ip-block"
      );

      renderIpBlock(payload);

      const state =
        payload.ip_block || {};

      if (
        !state.refreshing
        && state.updated
        && state.updated !== before
      ) {
        completed = true;

        showIpBlockMessage(
          "IP blocklist refreshed."
        );

        break;
      }
    }

    if (!completed) {
      throw new Error(
        "Refresh did not publish a new database within 60 seconds."
      );
    }

  } catch (error) {
    showIpBlockMessage(
      error.message
      || "Unable to refresh IP blocklist.",
      "error"
    );

  } finally {
    ipBlockBusy = false;

    await refreshIpBlockStatus();
  }
}


async function refreshStatus() {
  try {
    renderStatus(
      await api("/api/status")
    );
  } catch (error) {
      "Status unavailable";

    setDot(
      $("protonDot"),
      "bad"
    );

    setDot(
      $("wanDot"),
      "bad"
    );
  }
}

async function refreshClients() {
  try {
    renderClients(
      await api("/api/clients")
    );

    return true;
  } catch (error) {
    const body = $("clientRows");
    body.replaceChildren();

    const row = document.createElement("tr");

    const cell = textCell(
      error.message || "Unable to load clients.",
      "empty"
    );

    cell.colSpan = 6;
    row.appendChild(cell);
    body.appendChild(row);

    return false;
  }
}

async function refreshAll() {

  await Promise.all([
    refreshStatus(),
    refreshClients(),
    refreshIpBlockStatus(),
  ]);

}

async function refreshLanClientsFromButton() {
  const refreshed = await refreshClients();

  if (refreshed) {
    showMessage(
      "LAN clients refreshed."
    );
  } else {
    showMessage(
      "Unable to refresh LAN clients.",
      "error"
    );
  }
}

async function checkForUpdate() {
  if (updateBusy) {
    return;
  }

  updateBusy = true;

  const button = $("updateButton");
  button.disabled = true;
  button.textContent = "Checking...";

  let updateStarted = false;

  try {
    const result = await api("/api/update/check");

    if (!result.update_available) {
      showUpdateMessage("Up to date!");
      return;
    }

    button.textContent = "Updating...";
    showUpdateMessage(
      `Installing ${result.latest_version}. The router will reboot when the update is ready.`,
      "success",
      0
    );

    const started = await api(
      "/api/update",
      { method: "POST" }
    );

    updateStarted = true;
    button.textContent = "Update Started";
    showUpdateMessage(
      started.message
      || "Update started. The router will reboot automatically.",
      "success",
      0
    );
  } catch (error) {
    showUpdateMessage(
      error.message || "Unable to check for updates.",
      "error"
    );
  } finally {
    if (!updateStarted) {
      updateBusy = false;
      button.disabled = false;
      button.textContent = "Check for Update";
    }
  }
}


$("refreshButton").addEventListener(
  "click",
  refreshLanClientsFromButton
);

$("ipBlockRefreshButton").addEventListener(
  "click",
  startIpBlockRefresh
);

$("updateButton").addEventListener(
  "click",
  checkForUpdate
);

refreshAll();

refreshTimer = window.setInterval(
  () => {
    if (!document.hidden && busyKey === null) {
      refreshAll();
    }
  },
  15000
);
