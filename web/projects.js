import {api, element, transferredSelection, clearTransferredSelection} from "./portal.js";

const find = (id) => document.getElementById(id);
const statuses = {pending: "En attente", approved: "Approuvé", rejected: "Refusé"};
const taskLabels = {queued: "En attente", running: "En cours", done: "Terminés", failed: "En échec", cancelled: "Annulés"};
const date = new Intl.DateTimeFormat("fr-FR", {dateStyle: "short", timeStyle: "short"});

function notice(id, message, error = false) {
  const target = find(id);
  target.textContent = message;
  target.classList.toggle("error", error);
}

function button(label, run, className = "secondary") {
  const control = element("button", label, className);
  control.type = "button";
  control.addEventListener("click", run);
  return control;
}

function badge(state, label = statuses[state]) {
  return element("span", label, `node-status ${state}`);
}

function rowsOrEmpty(body, rows, columns, message) {
  if (!rows.length) {
    const row = document.createElement("tr");
    const cell = element("td", message, "muted");
    cell.colSpan = columns;
    row.append(cell);
    rows.push(row);
  }
  body.replaceChildren(...rows);
}

function renderList() {
  const search = find("project-search");
  const status = find("project-status");
  const reservation = find("project-reservation");
  const sort = find("project-sort");
  const size = find("project-size");
  const previous = find("project-previous");
  const next = find("project-next");
  const currentStatus = new URLSearchParams(location.search).get("status");
  if (Object.hasOwn(statuses, currentStatus)) status.value = currentStatus;
  let page = 1;
  let pages = 1;
  let revision = 0;
  let timer;
  const cards = new Map();

  const selection = transferredSelection();
  if (selection.length) {
    notice("selection-bridge", `Sélection de la flotte : ${selection.length} nœud(s). Ouvrez un projet approuvé pour l’affecter.`);
  }

  for (const [key, label] of Object.entries({"": "Tous les projets", ...statuses})) {
    const card = button(label, () => {
      status.value = key;
      filter();
    });
    const count = element("strong", "—");
    card.append(count);
    cards.set(key, {card, count});
    find("project-summary").append(card);
  }

  function projectRow(project) {
    const row = document.createElement("tr");
    const title = document.createElement("td");
    const link = element("a", project.name);
    link.href = `/admin/projects/${project.id}`;
    title.append(link, element("small", `#${project.id}`));
    const contact = element("td", project.affiliation || "—");
    if (project.contact) contact.append(element("small", project.contact));
    const state = document.createElement("td");
    state.append(badge(project.status));
    row.append(title, contact, state, element("td", project.reserved), element("td", project.jobs));
    return row;
  }

  async function load() {
    const request = ++revision;
    const filters = new URLSearchParams({
      q: search.value.trim(), status: status.value, reservation: reservation.value,
      sort: sort.value, size: size.value, page,
    });
    try {
      const data = await api(`/admin/projects/data?${filters}`);
      if (request !== revision) return;
      page = data.page;
      pages = data.pages;
      for (const [key, {card, count}] of cards) {
        count.textContent = key ? data.counts[key] ?? 0 : Object.values(data.counts).reduce((sum, n) => sum + n, 0);
        card.setAttribute("aria-pressed", String(key === status.value));
      }
      const suffix = data.total === 1 ? "" : "s";
      find("project-total").textContent = `${data.total} projet${suffix}`;
      find("project-page").textContent = `Page ${page} sur ${pages}`;
      previous.disabled = page <= 1;
      next.disabled = page >= pages;
      rowsOrEmpty(find("project-rows"), data.projects.map(projectRow), 5, "Aucun projet ne correspond à ces filtres.");
      notice("list-notice", "");
    } catch (error) {
      if (request === revision) notice("list-notice", `Impossible de charger la liste. ${error.message}`, true);
    }
  }

  function filter() {
    clearTimeout(timer);
    page = 1;
    load();
  }

  search.addEventListener("input", () => {
    clearTimeout(timer);
    revision++;
    timer = setTimeout(filter, 250);
  });
  for (const field of [status, reservation, sort, size]) field.addEventListener("change", filter);
  find("project-filters").addEventListener("submit", (event) => { event.preventDefault(); filter(); });
  find("project-filters").addEventListener("reset", () => queueMicrotask(filter));
  previous.addEventListener("click", () => { page = Math.max(1, page - 1); load(); });
  next.addEventListener("click", () => { page = Math.min(pages, page + 1); load(); });
  load();
}

function renderDetail() {
  const route = location.pathname.replace(/\/$/, "");
  const edit = find("metadata-form");
  const reservation = find("reservation-form");
  const count = find("reservation-count");
  const confirm = find("project-confirm");
  const secret = find("project-secret");
  let current;
  let pending;
  let dirty = false;
  let countEdited = false;
  let busy = false;
  let revision = 0;
  let accessSnapshot = "";

  function ask(title, description, label, run) {
    if (busy) return;
    pending = run;
    find("confirm-heading").textContent = title;
    find("confirm-description").textContent = description;
    find("confirm-run").textContent = label;
    confirm.returnValue = "";
    confirm.showModal();
  }

  confirm.addEventListener("close", () => {
    const run = pending;
    pending = null;
    if (confirm.returnValue === "confirm") run?.();
  });
  secret.addEventListener("close", () => { find("project-key").value = ""; });

  function reveal(key) {
    const input = find("project-key");
    input.value = key;
    secret.showModal();
    input.focus();
    input.select();
  }

  async function mutate(path, data, success) {
    if (busy) return;
    busy = true;
    revision++;
    notice("detail-notice", "");
    try {
      const result = await api(route + path, data);
      success(result);
      await load();
    } catch (error) {
      notice("detail-notice", error.message, true);
    } finally {
      busy = false;
    }
  }

  function updateAccess(project) {
    const snapshot = `${project.status}:${project.has_key}`;
    if (snapshot === accessSnapshot) return;
    accessSnapshot = snapshot;
    const actions = find("access-actions");
    actions.replaceChildren();
    if (project.status === "pending") {
      find("access-state").textContent = "La proposition attend une décision. L’approbation génère une clé API.";
      actions.append(button("Approuver", () => ask("Approuver ce projet ?", "Une clé API sera créée pour soumettre des calculs.", "Approuver",
        () => mutate("", {action: "approve"}, (result) => { notice("detail-notice", "Projet approuvé."); reveal(result.api_key); })), ""),
      button("Refuser", () => ask("Refuser ce projet ?", "Le projet sera classé comme refusé.", "Refuser",
        () => mutate("", {action: "reject"}, () => notice("detail-notice", "Projet refusé."))), "danger"));
    } else if (project.status === "approved") {
      find("access-state").textContent = project.has_key ? "Une clé API est active." : "Aucune clé API active.";
      actions.append(button(project.has_key ? "Remplacer la clé API" : "Générer une clé API",
        () => ask("Générer une nouvelle clé API ?", project.has_key
          ? "L’ancienne clé cessera de fonctionner immédiatement. Les demandes de calcul déjà soumises seront conservées."
          : "La nouvelle clé permettra de soumettre et de consulter les calculs de ce projet.", "Générer la clé",
          () => mutate("/rotate-key", {}, (result) => {
            notice("detail-notice", "Nouvelle clé API générée.");
            reveal(result.api_key);
          }))));
    } else {
      find("access-state").textContent = "La proposition est classée comme refusée.";
    }
  }

  function updateReservation(project) {
    const approved = project.status === "approved";
    const nodes = project.reserved_nodes;
    find("reservation-controls").hidden = !approved;
    find("reservation-state").textContent = !approved ? "La réservation est disponible après approbation." : nodes.length
      ? `${nodes.length} nœud(s) réservé(s), dont ${nodes.filter((node) => node.ready).length} opérationnel(s).`
      : "Pool partagé. Vous pouvez réserver des nœuds à ce projet.";
    if (!approved) return;
    if (!countEdited) count.value = String(nodes.length || 1);
    find("release-reservation").hidden = nodes.length === 0;
    find("reserved-details").hidden = nodes.length === 0;
    find("reserved-nodes").replaceChildren(...nodes.map((node) => element("li",
      `#${node.id} ${node.name} · ${node.hardware || node.device || "Profil inconnu"}${node.ready ? "" : " · Indisponible"}`)));
    const ids = transferredSelection();
    find("reserve-manual").disabled = !ids.length;
    const transfer = find("transfer-state");
    transfer.textContent = ids.length ? `Sélection transférée : ${ids.length} nœud(s).` : "";
    const link = element("a", ids.length ? " Modifier la sélection dans la flotte ↗" : "Sélectionner des nœuds dans la flotte ↗", "console-link");
    link.href = "/admin#flotte";
    transfer.append(link);
  }

  function render(project) {
    current = project;
    find("project-id").textContent = `Fiche projet · #${project.id}`;
    find("project-name").textContent = project.name;
    document.title = `${project.name} · OPEN-NPU`;
    const history = find("project-history");
    history.href = `/admin/history?project=${project.id}`;
    history.hidden = false;
    const status = find("detail-status");
    status.className = `node-status ${project.status}`;
    status.textContent = statuses[project.status];
    status.hidden = false;
    find("project-content").hidden = false;
    if (!dirty) for (const input of edit.querySelectorAll("[name]")) input.value = project[input.name] ?? "";
    updateAccess(project);
    updateReservation(project);
    find("project-metrics").replaceChildren(...Object.entries(taskLabels).map(([key, label]) => {
      const row = document.createElement("div");
      row.append(element("dt", label), element("dd", project.tasks[key] ?? 0));
      return row;
    }));
    rowsOrEmpty(find("project-jobs"), project.jobs.map((job) => {
      const row = document.createElement("tr");
      const state = document.createElement("td");
      state.append(badge(job.status, {queued: "En attente", running: "En cours", done: "Terminée", failed: "En échec", cancelled: "Annulée"}[job.status]));
      row.append(element("td", job.id), element("td", date.format(new Date(job.created * 1000))), state,
        element("td", `${job.completed} / ${job.total}`));
      return row;
    }), 4, "Aucune demande de calcul pour ce projet.");
  }

  async function load() {
    const request = ++revision;
    try {
      const data = await api(route + "/data");
      if (request === revision) render(data);
    } catch (error) {
      if (request === revision) notice("detail-notice", `Impossible d’actualiser la fiche. ${error.message}`, true);
    }
  }

  edit.addEventListener("input", () => { dirty = true; });
  edit.addEventListener("submit", (event) => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(edit));
    const submit = edit.querySelector("button");
    if (busy) return;
    submit.disabled = true;
    mutate("/metadata", values, () => {
      dirty = [...edit.querySelectorAll("[name]")].some((input) => input.value !== values[input.name]);
      notice("detail-notice", "Informations enregistrées.");
    }).finally(() => { submit.disabled = false; });
  });
  count.addEventListener("input", () => { countEdited = true; });

  function saveReservation(number, ids) {
    const selection = ids ? {node_ids: ids} : {};
    mutate("/reservation", {count: number, ...selection}, (result) => {
      if (ids) clearTransferredSelection();
      countEdited = false;
      const message = number === 0 ? "Réservation libérée."
        : `${number} nœud(s) réservé(s). ${result.homogeneous ? "Profil NPU identique." : "Profils différents ou incomplets."}`;
      notice("detail-notice", message);
    });
  }

  function askReservation(ids) {
    if (busy || !reservation.reportValidity()) return;
    const number = count.valueAsNumber;
    if (ids && ids.length !== number) {
      notice("detail-notice", `La sélection transférée contient ${ids.length} nœud(s). Le nombre demandé doit correspondre.`, true);
      return;
    }
    if (current.reserved_nodes.length) {
      ask("Remplacer la réservation ?", `Une nouvelle réservation de ${number} nœud(s) remplacera le groupe actuel si l’effectif est disponible.`,
        "Remplacer", () => saveReservation(number, ids));
    } else {
      saveReservation(number, ids);
    }
  }

  reservation.addEventListener("submit", (event) => { event.preventDefault(); askReservation(); });
  find("reserve-manual").addEventListener("click", () => askReservation(transferredSelection()));
  find("release-reservation").addEventListener("click", () => ask("Libérer les nœuds ?",
    "Les prochains lots pourront être pris par le pool partagé. Les lots déjà pris conservent leur bail.",
    "Libérer", () => saveReservation(0)));
  find("reload-project").addEventListener("click", () => {
    if (!busy) { notice("detail-notice", ""); load(); }
  });
  setInterval(() => {
    if (!document.hidden && !busy && !confirm.open && !secret.open) load();
  }, 15000);
  load();
}

if (find("admin").dataset.page === "list") renderList();
else renderDetail();
