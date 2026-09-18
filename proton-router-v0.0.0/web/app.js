const $ = (id) => document.getElementById(id);

let busyKey = null;
let vpnBlockBusy = false;
let vpnProgressTimer = null;
let refreshTimer = null;

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

function showVpnBlockMessage(
  text,
  kind = "success"
) {
  const box = $("vpnBlockMessage");

  box.textContent = text;
  box.className = `message ${kind}`;
  box.hidden = false;

  window.clearTimeout(
    showVpnBlockMessage.timer
  );

  showVpnBlockMessage.timer =
    window.setTimeout(
      () => {
        box.hidden = true;
      },
      6000
    );
}


function readableVpnMode(value) {
  if (!value) {
    return "—";
  }

  return value.replaceAll(
    "-",
    " "
  );
}


function scheduleVpnProgressPoll(
  refreshing
) {
  if (
    !refreshing
    || vpnBlockBusy
  ) {
    if (vpnProgressTimer !== null) {
      window.clearTimeout(
        vpnProgressTimer
      );

      vpnProgressTimer = null;
    }

    return;
  }

  if (vpnProgressTimer !== null) {
    return;
  }

  vpnProgressTimer =
    window.setTimeout(
      async () => {
        vpnProgressTimer = null;

        if (document.hidden) {
          scheduleVpnProgressPoll(
            true
          );

          return;
        }

        await refreshVpnBlockStatus();
      },
      1000
    );
}


function renderVpnBlock(payload) {
  const state =
    payload.vpn_block || {};

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
    $("vpnBlockProgress");

  const progressBar =
    $("vpnBlockProgressBar");

  progressWrap.hidden =
    !(refreshing || vpnBlockBusy);

  progressBar.value =
    progress;

  progressBar.textContent =
    `${progress}%`;

  $("vpnBlockProgressPercent").textContent =
    `${progress}%`;

  $("vpnBlockProgressLabel").textContent =
    progressPhase;

  scheduleVpnProgressPoll(
    refreshing
  );

  $("vpnBlockEndpointCount").textContent =
    state.ipv4_count ?? "—";

  $("vpnBlockCidrCount").textContent =
    state.cidr_count ?? "—";

  const updated =
    state.updated || "";

  $("vpnBlockUpdated").dataset.raw =
    updated;

  if (updated) {
    const parsed =
      new Date(updated);

    $("vpnBlockUpdated").textContent =
      Number.isNaN(
        parsed.getTime()
      )
        ? updated
        : parsed.toLocaleString();

  } else {
    $("vpnBlockUpdated").textContent =
      "—";
  }

  $("vpnBlockNext").textContent =
    state.next_refresh || "—";



  const button =
    $("vpnBlockRefreshButton");

  button.disabled =
    vpnBlockBusy || refreshing;

  button.textContent =
    vpnBlockBusy || refreshing
      ? "Refreshing..."
      : "Refresh Endpoints";
}


async function refreshVpnBlockStatus() {
  try {
    renderVpnBlock(
      await api(
        "/api/vpn-block"
      )
    );

  } catch (_) {

  }
}


async function startVpnBlockRefresh() {
  if (vpnBlockBusy) {
    return;
  }

  vpnBlockBusy = true;

  const button =
    $("vpnBlockRefreshButton");

  const before =
    $("vpnBlockUpdated").dataset.raw
    || "";

  button.disabled = true;

  button.textContent =
    "Refreshing...";

  try {
    const result = await api(
      "/api/vpn-block/refresh",
      {
        method: "POST",
      }
    );

    showVpnBlockMessage(
      result.message
      || "VPN endpoint refresh started."
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
        "/api/vpn-block"
      );

      renderVpnBlock(payload);

      const state =
        payload.vpn_block || {};

      if (
        !state.refreshing
        && state.updated
        && state.updated !== before
      ) {
        completed = true;

        showVpnBlockMessage(
          "VPN endpoint database refreshed."
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
    showVpnBlockMessage(
      error.message
      || "Unable to refresh VPN endpoints.",
      "error"
    );

  } finally {
    vpnBlockBusy = false;

    await refreshVpnBlockStatus();
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
    refreshVpnBlockStatus(),
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


$("refreshButton").addEventListener(
  "click",
  refreshLanClientsFromButton
);

$("vpnBlockRefreshButton").addEventListener(
  "click",
  startVpnBlockRefresh
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
