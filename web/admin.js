import {api, element, transferSelection} from "./portal.js";

const find = (id) => document.getElementById(id);
const labels = {working: "En calcul", online: "Disponible", reserved: "Réservé", paused: "En pause", offline: "Hors ligne",
                new: "Jamais connecté", disabled: "Désactivé"};
const date = new Intl.DateTimeFormat("fr-FR", {dateStyle: "short", timeStyle: "medium"});
const number = new Intl.NumberFormat("fr-FR", {maximumFractionDigits: 3});
const selected = new Set();
const limit = 100;
const nodes = find("nodes");
const search = find("search");
search.value = (new URLSearchParams(location.search).get("q") || "").slice(0, 160);
const state = find("node-state");
const size = find("page-size");
const pageSelect = find("select-page");
const connection = find("connection-status");
const confirmDialog = find("confirm-action");
let fleetData = null;
let page = 1;
let fleetRequest = 0;
let overviewRequest = 0;
let searchTimer;
let busy = false;
let pendingAction;

function notice(id, message, error = false) {
  const output = find(id);
  if (output.textContent !== message) output.textContent = message;
  output.classList.toggle("error", error);
}

function button(label, onClick, className = "secondary") {
  const item = element("button", label, className);
  item.type = "button";
  item.addEventListener("click", onClick);
  return item;
}

const summaryCards = new Map();
for (const [key, label] of Object.entries({"": "Tous les nœuds", ...labels})) {
  const count = element("strong", "—");
  const card = button(label, () => {
    state.value = key;
    changeFilter();
  });
  card.append(count);
  summaryCards.set(key, {card, count});
  find("fleet-summary").append(card);
}

function formatSeen(seen) {
  return seen === null ? "—" : date.format(new Date(seen * 1000));
}

function updateSelection() {
  const visible = fleetData?.nodes ?? [];
  const checked = visible.filter((node) => selected.has(node.id)).length;
  for (const checkbox of nodes.querySelectorAll("input[data-node]")) {
    checkbox.checked = selected.has(Number(checkbox.dataset.node));
    checkbox.disabled = busy;
  }
  for (const action of nodes.querySelectorAll("button")) action.disabled = busy;
  pageSelect.checked = visible.length > 0 && checked === visible.length;
  pageSelect.indeterminate = checked > 0 && checked < visible.length;
  pageSelect.disabled = busy || !visible.length;
  find("enable-selected").disabled = busy || selected.size === 0;
  find("disable-selected").disabled = busy || selected.size === 0;
  find("transfer-project").disabled = busy || selected.size === 0;
  find("clear-selection").hidden = selected.size === 0;
  find("clear-selection").disabled = busy;
  find("selection-count").textContent = selected.size
    ? `${selected.size} nœud${selected.size === 1 ? "" : "s"} sélectionné${selected.size === 1 ? "" : "s"}, dont ${checked} sur cette page`
    : "Aucun nœud sélectionné";
}

function selectNodes(ids, include) {
  if (include && new Set([...selected, ...ids]).size > limit) {
    notice("fleet-notice", `Sélection limitée à ${limit} nœuds par action.`, true);
  } else {
    for (const id of ids) {
      if (include) selected.add(id);
      else selected.delete(id);
    }
    notice("fleet-notice", "");
  }
  updateSelection();
}

function networkCell(node, data) {
  const cell = element("td", "", "node-network");
  const rate = (value) => value === null ? "—" : `${number.format(value)} Mbit/s`;
  if (node.sampled_at === null) {
    cell.append(element("span", "Aucune mesure"));
  } else {
    const age = Math.max(0, data.now - node.sampled_at);
    const ageText = age < 60 ? "moins d’une minute" : `${Math.floor(age / 60)} min`;
    if (node.local_link) {
      cell.append(element("span", "Connexion locale"));
    } else {
      cell.append(element("span", `↑ ${rate(node.upload_mbps)} · ↓ ${rate(node.download_mbps)}`));
    }
    if (node.rtt_ms !== null) cell.append(element("small", `Latence serveur : ${number.format(node.rtt_ms)} ms`));
    cell.append(element("small", `Mesurée il y a ${ageText}${age > data.sample_valid_seconds ? " · expirée" : ""}`));
  }
  cell.append(element("small", node.upload_limit_mbps === null ? "Aucun plafond déclaré" :
    `Plafond d’envoi : ${rate(node.upload_limit_mbps)}`));
  return cell;
}

function nodeRow(node, data) {
  const row = document.createElement("tr");
  const selection = document.createElement("td");
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.id = `select-node-${node.id}`;
  checkbox.dataset.node = node.id;
  checkbox.setAttribute("aria-label", `Sélectionner ${node.name} (nœud ${node.id})`);
  checkbox.addEventListener("change", () => selectNodes([node.id], checkbox.checked));
  selection.append(checkbox);

  const name = document.createElement("td");
  name.append(element("strong", node.name), element("small", `#${node.id}`));
  if (node.project_name) name.append(element("small", `Projet · ${node.project_name}`));
  const status = document.createElement("td");
  status.append(element("span", labels[node.state], `node-status ${node.state}`));
  const actionCell = document.createElement("td");
  const action = node.state === "disabled" ? "enable" : "disable";
  const actionButton = button(action === "enable" ? "Réactiver" : "Désactiver",
    () => confirmAction(action, [node.id], node.name));
  actionButton.id = `action-node-${node.id}`;
  actionButton.setAttribute("aria-label", `${actionButton.textContent} ${node.name} (nœud ${node.id})`);
  actionCell.append(actionButton);
  row.append(selection, name, status, element("td", node.hardware || node.device || "—"), networkCell(node, data),
    element("td", formatSeen(node.seen)), actionCell);
  return row;
}

function renderFleet(data, filter) {
  const previous = fleetData;
  fleetData = data;
  page = data.page;
  for (const [key, {card, count}] of summaryCards) {
    count.textContent = key ? data.counts[key] ?? 0 : Object.values(data.counts).reduce((sum, value) => sum + value, 0);
    card.setAttribute("aria-pressed", String(key === filter));
  }
  if (!previous || Math.floor(data.now / 60) !== Math.floor(previous.now / 60) ||
      JSON.stringify(data.nodes) !== JSON.stringify(previous.nodes)) {
    const focused = nodes.contains(document.activeElement) ? document.activeElement.id : "";
    const rows = data.nodes.map((node) => nodeRow(node, data));
    if (rows.length === 0) {
      const row = document.createElement("tr");
      const cell = element("td", "Aucun nœud ne correspond à ces filtres.");
      cell.colSpan = 7;
      row.append(cell);
      rows.push(row);
    }
    nodes.replaceChildren(...rows);
    updateSelection();
    if (focused && !busy) {
      const restored = find(focused) ?? state;
      restored.focus({preventScroll: true});
    }
  } else {
    updateSelection();
  }
  find("node-total").textContent = `${data.total} nœud${data.total === 1 ? "" : "s"}`;
  find("page-info").textContent = `Page ${data.page} sur ${data.pages}`;
  find("previous").disabled = data.page <= 1;
  find("next").disabled = data.page >= data.pages;
  if (!previous || JSON.stringify(data.events) !== JSON.stringify(previous.events)) {
    const events = data.events.map((event) => {
      const action = event.action === "enable" ? "Réactivation" : "Désactivation";
      const count = event.node_ids.length;
      const item = element("li", `${action} · ${count} nœud${count === 1 ? "" : "s"}`);
      const ids = event.node_ids.map((id) => `#${id}`).join(", ");
      item.append(element("small", `${formatSeen(event.created)} · ${ids}`));
      if (event.released) item.append(element("small", `Lots remis en file : ${event.released}`));
      return item;
    });
    find("events").replaceChildren(...(events.length ? events : [element("li", "Aucune action enregistrée.", "muted")]));
  }
}

async function loadFleet(manual = false) {
  const revision = ++fleetRequest;
  const filter = state.value;
  const params = new URLSearchParams({q: search.value.trim(), status: filter, size: size.value, page});
  try {
    const data = await api(`/admin/fleet?${params}`);
    if (revision !== fleetRequest) return;
    renderFleet(data, filter);
    notice("connection-status", manual ? "Flotte actualisée." : "Flotte synchronisée · actualisation automatique");
  } catch (error) {
    if (revision !== fleetRequest) return;
    notice("connection-status", fleetData
      ? `Actualisation impossible. Les dernières données restent affichées. ${error.message}`
      : `Flotte indisponible. ${error.message}`, true);
  }
}

function changeFilter() {
  clearTimeout(searchTimer);
  page = 1;
  loadFleet();
}

find("filters").addEventListener("submit", (event) => {
  event.preventDefault();
  changeFilter();
});
search.addEventListener("input", () => {
  clearTimeout(searchTimer);
  fleetRequest++;
  searchTimer = setTimeout(changeFilter, 250);
});
state.addEventListener("change", changeFilter);
size.addEventListener("change", changeFilter);
pageSelect.addEventListener("change", () => selectNodes(fleetData.nodes.map((node) => node.id), pageSelect.checked));
find("transfer-project").addEventListener("click", () => {
  try {
    transferSelection([...selected]);
    location.assign("/admin/projects?status=approved");
  } catch {
    notice("fleet-notice", "Impossible de transférer la sélection dans ce navigateur.", true);
  }
});
find("clear-selection").addEventListener("click", () => selectNodes([...selected], false));
find("previous").addEventListener("click", () => { page = Math.max(1, page - 1); loadFleet(); });
find("next").addEventListener("click", () => { page = Math.min(fleetData.pages, page + 1); loadFleet(); });
find("enable-selected").addEventListener("click", () => confirmAction("enable", [...selected]));
find("disable-selected").addEventListener("click", () => confirmAction("disable", [...selected]));

for (const close of document.querySelectorAll("[data-close]")) {
  close.addEventListener("click", () => {
    if (!busy) find(close.dataset.close).close();
  });
}

function confirmAction(action, ids, name) {
  pendingAction = {action, node_ids: ids};
  const disabling = action === "disable";
  const verb = disabling ? "Désactiver" : "Réactiver";
  find("confirm-title").textContent = name ? `${verb} ${name}` : `${verb} la sélection`;
  find("confirm-detail").textContent = disabling
    ? `${ids.length} nœud(s). Les lots en cours seront remis dans la file pour être repris.`
    : `${ids.length} nœud(s). Les agents restés ouverts reprendront à leur prochaine connexion.`;
  find("confirm-button").textContent = verb;
  find("confirm-button").classList.toggle("danger", disabling);
  notice("confirm-notice", "");
  confirmDialog.showModal();
}

confirmDialog.addEventListener("cancel", (event) => {
  if (busy) event.preventDefault();
});
find("confirm-button").addEventListener("click", async () => {
  const submit = find("confirm-button");
  submit.disabled = true;
  busy = true;
  fleetRequest++;
  updateSelection();
  try {
    const result = await api("/admin/fleet/actions", pendingAction);
    for (const id of pendingAction.node_ids) selected.delete(id);
    confirmDialog.close();
    const message = result.changed
      ? `Nœuds modifiés : ${result.changed}. Lots remis en file : ${result.released}.`
      : "Les nœuds sélectionnés étaient déjà dans cet état.";
    notice("fleet-notice", message);
    await Promise.all([loadFleet(), loadOverview()]);
  } catch (error) {
    notice("confirm-notice", error.message, true);
  } finally {
    busy = false;
    submit.disabled = false;
    updateSelection();
    if (!confirmDialog.open) find("search").focus({preventScroll: true});
  }
});

async function loadOverview() {
  const revision = ++overviewRequest;
  try {
    const data = await api("/admin/state?include_nodes=0&include_projects=0");
    if (revision !== overviewRequest) return;
    const counters = {queued: "En attente", running: "En cours", done: "Terminés", failed: "En échec", cancelled: "Annulés"};
    find("metrics").replaceChildren(...Object.entries(counters).map(([key, label]) => {
      const item = document.createElement("div");
      item.append(element("dt", label), element("dd", data.tasks[key] ?? 0));
      return item;
    }));
    notice("queue-notice", "");
  } catch (error) {
    if (revision === overviewRequest) notice("queue-notice", `Actualisation impossible. ${error.message}`, true);
  }
}

function refresh() {
  if (busy || document.hidden || document.querySelector("dialog[open]")) return;
  loadFleet();
  loadOverview();
}

find("refresh").addEventListener("click", () => {
  loadFleet(true);
  loadOverview();
});
document.addEventListener("visibilitychange", refresh);
setInterval(refresh, 10000);
loadFleet();
loadOverview();
