import { t, language } from "./i18n.js";
import {api, element, transferredSelection, clearTransferredSelection} from "./portal.js";

const find = (id) => document.getElementById(id);
const statuses = {pending: t("En attente"), approved: t("Approuvé"), rejected: t("Refusé")};
const taskLabels = {queued: t("En attente"), running: t("En cours"), done: t("Terminés"), failed: t("En échec"), cancelled: t("Annulés")};
const date = new Intl.DateTimeFormat(language, {dateStyle: "short", timeStyle: "short"});

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

function renderContributions() {
  const previous = find("contributions-previous");
  const next = find("contributions-next");
  const refresh = find("contributions-refresh");
  const number = new Intl.NumberFormat(language);
  let page = 1;
  let pages = 1;

  async function load(requested = page) {
    previous.disabled = next.disabled = refresh.disabled = true;
    try {
      const data = await api(`/admin/contributions/data?page=${requested}`);
      ({page, pages} = data);
      rowsOrEmpty(find("contribution-rows"), data.contributions.map((item) => {
        const row = document.createElement("tr");
        for (const value of [item.institution, item.contact, number.format(item.node_count), item.models,
          date.format(new Date(item.created * 1000))]) row.append(element("td", value));
        return row;
      }), 5, t("Aucune offre de contribution."));
      find("contributions-page").textContent = t("Page {0} sur {1}", number.format(page), number.format(pages));
      notice("contributions-notice", "");
    } catch (error) {
      notice("contributions-notice", error.message, true);
      const loading = find("contribution-rows").querySelector("[data-i18n]");
      if (loading) loading.textContent = "—";
    } finally {
      previous.disabled = page <= 1;
      next.disabled = page >= pages;
      refresh.disabled = false;
    }
  }
  previous.addEventListener("click", () => load(page - 1));
  next.addEventListener("click", () => load(page + 1));
  refresh.addEventListener("click", () => load());
  load();
}

if (find("contribution-rows")) renderContributions();

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
    notice("selection-bridge", t("Sélection de la flotte : {0} nœud(s). Ouvrez un projet approuvé pour l’affecter.", selection.length));
  }

  for (const [key, label] of Object.entries({"": t("Tous les projets"), ...statuses})) {
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
    row.append(title, contact, state, element("td", project.dedicated && !project.reserved ? t("À remplacer") : project.reserved), element("td", project.jobs));
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
      find("project-total").textContent = t("Projets : {0}", data.total);
      find("project-page").textContent = t("Page {0} sur {1}", page, pages);
      previous.disabled = page <= 1;
      next.disabled = page >= pages;
      rowsOrEmpty(find("project-rows"), data.projects.map(projectRow), 5, t("Aucun projet ne correspond à ces filtres."));
      notice("list-notice", "");
    } catch (error) {
      if (request === revision) notice("list-notice", t("Impossible de charger la liste. {0}", error.message), true);
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
  const policy = find("policy-form");
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
  let policySnapshot = "";

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
      find("access-state").textContent = t("La proposition attend une décision. L’approbation génère une clé API.");
      actions.append(button(t("Approuver"), () => ask(t("Approuver ce projet ?"), t("Une clé API sera créée pour soumettre des calculs."), t("Approuver"),
        () => mutate("", {action: "approve"}, (result) => { notice("detail-notice", t("Projet approuvé.")); reveal(result.api_key); })), ""),
      button(t("Refuser"), () => ask(t("Refuser ce projet ?"), t("Le projet sera classé comme refusé."), t("Refuser"),
        () => mutate("", {action: "reject"}, () => notice("detail-notice", t("Projet refusé.")))), "danger"));
    } else if (project.status === "approved") {
      find("access-state").textContent = project.has_key ? t("Une clé API est active.") : t("Aucune clé API active.");
      actions.append(button(project.has_key ? t("Remplacer la clé API") : t("Générer une clé API"),
        () => ask(t("Générer une nouvelle clé API ?"), project.has_key
          ? t("L’ancienne clé cessera de fonctionner immédiatement. Les demandes de calcul déjà soumises seront conservées.")
          : t("La nouvelle clé permettra de soumettre et de consulter les calculs de ce projet."), t("Générer la clé"),
          () => mutate("/rotate-key", {}, (result) => {
            notice("detail-notice", t("Nouvelle clé API générée."));
            reveal(result.api_key);
          }))));
    } else {
      find("access-state").textContent = t("La proposition est classée comme refusée.");
    }
  }

  function updateReservation(project) {
    const approved = project.status === "approved";
    const nodes = project.reserved_nodes;
    find("reservation-controls").hidden = !approved;
    find("reservation-state").textContent = !approved ? t("La réservation est disponible après approbation.") : nodes.length
      ? t("{0} nœud(s) réservé(s), dont {1} opérationnel(s).", nodes.length, nodes.filter((node) => node.ready).length)
      : project.dedicated ? t("Placement dédié. Le projet attend une nouvelle réservation.")
      : t("Pool partagé. Vous pouvez réserver des nœuds à ce projet.");
    if (!approved) return;
    if (!countEdited) count.value = String(nodes.length || 1);
    find("release-reservation").hidden = !project.dedicated;
    find("reserved-details").hidden = nodes.length === 0;
    find("reserved-nodes").replaceChildren(...nodes.map((node) => element("li",
      `#${node.id} ${node.name} · ${node.hardware || node.device || t("Profil inconnu")}${node.ready ? "" : t(" · Indisponible")}`)));
    const ids = transferredSelection();
    find("reserve-manual").disabled = !ids.length;
    const transfer = find("transfer-state");
    transfer.textContent = ids.length ? t("Sélection transférée : {0} nœud(s).", ids.length) : "";
    const link = element("a", ids.length ? t(" Modifier la sélection dans la flotte ↗") : t("Sélectionner des nœuds dans la flotte ↗"), "console-link");
    link.href = "/admin#flotte";
    transfer.append(link);
  }

  function render(project) {
    current = project;
    find("project-id").textContent = t("Fiche projet · #{0}", project.id);
    find("project-name").textContent = project.name;
    document.title = `${project.name} · OPEN-NPU`;
    const history = find("project-history");
    history.href = `/admin/history?project=${project.id}`;
    history.hidden = false;
    const status = find("detail-status");
    status.className = `node-status ${project.status}`;
    status.textContent = statuses[project.status];
    const keys = find("scoped-keys");
    keys.href = `/admin/access?project=${project.id}`;
    keys.hidden = project.status !== "approved";
    status.hidden = false;
    find("project-content").hidden = false;
    if (!dirty) for (const input of edit.querySelectorAll("[name]")) input.value = project[input.name] ?? "";
    updateAccess(project);
    updateReservation(project);
    find("project-policy").hidden = project.status !== "approved";
    const rules = JSON.stringify(project.policy);
    if (policySnapshot !== rules) {
      policySnapshot = rules;
      policy.elements.admissions.checked = Boolean(project.policy.admissions);
      policy.elements.max_active.value = project.policy.max_active ?? 20;
      policy.elements.per_minute.value = project.policy.per_minute ?? 30;
      policy.elements.priority.value = project.policy.priority;
    }
    for (const [id, counts] of [["project-metrics", project.tasks], ["project-pipeline", project.pipeline]]) {
      find(id).replaceChildren(...Object.entries(taskLabels).map(([key, label]) => {
        const row = document.createElement("div");
        row.append(element("dt", label), element("dd", counts[key] ?? 0));
        return row;
      }));
    }
    rowsOrEmpty(find("project-jobs"), project.jobs.map((job) => {
      const row = document.createElement("tr");
      const state = document.createElement("td");
      state.append(badge(job.status, {queued: t("En attente"), running: t("En cours"), done: t("Terminée"), failed: t("En échec"), cancelled: t("Annulée")}[job.status]));
      row.append(element("td", job.id), element("td", date.format(new Date(job.created * 1000))), state,
        element("td", `${job.completed} / ${job.total}`));
      return row;
    }), 4, t("Aucune demande de calcul pour ce projet."));
  }

  async function load() {
    const request = ++revision;
    try {
      const data = await api(route + "/data");
      if (request === revision) render(data);
    } catch (error) {
      if (request === revision) notice("detail-notice", t("Impossible d’actualiser la fiche. {0}", error.message), true);
    }
  }

  edit.addEventListener("input", () => { dirty = true; });
  policy.addEventListener("submit", (event) => {
    event.preventDefault();
    const fields = policy.elements;
    mutate("/policy", {admissions: fields.admissions.checked, max_active: Number(fields.max_active.value),
      per_minute: Number(fields.per_minute.value), priority: Number(fields.priority.value)},
    () => notice("detail-notice", t("Règles d’admission enregistrées.")));
  });
  edit.addEventListener("submit", (event) => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(edit));
    const submit = edit.querySelector("button");
    if (busy) return;
    submit.disabled = true;
    mutate("/metadata", values, () => {
      dirty = [...edit.querySelectorAll("[name]")].some((input) => input.value !== values[input.name]);
      notice("detail-notice", t("Informations enregistrées."));
    }).finally(() => { submit.disabled = false; });
  });
  count.addEventListener("input", () => { countEdited = true; });

  function saveReservation(number, ids) {
    const selection = ids ? {node_ids: ids} : {};
    mutate("/reservation", {count: number, ...selection}, (result) => {
      if (ids) clearTransferredSelection();
      countEdited = false;
      const message = number === 0 ? t("Réservation libérée.")
        : t("{0} nœud(s) réservé(s). {1}", number, result.homogeneous ? t("Profil NPU identique.") : t("Profils différents ou incomplets."));
      notice("detail-notice", message);
    });
  }

  function askReservation(ids) {
    if (busy || !reservation.reportValidity()) return;
    const number = count.valueAsNumber;
    if (ids && ids.length !== number) {
      notice("detail-notice", t("La sélection transférée contient {0} nœud(s). Le nombre demandé doit correspondre.", ids.length), true);
      return;
    }
    if (current.reserved_nodes.length) {
      ask(t("Remplacer la réservation ?"), t("Une nouvelle réservation de {0} nœud(s) remplacera le groupe actuel si l’effectif est disponible.", number),
        t("Remplacer"), () => saveReservation(number, ids));
    } else {
      saveReservation(number, ids);
    }
  }

  reservation.addEventListener("submit", (event) => { event.preventDefault(); askReservation(); });
  find("reserve-manual").addEventListener("click", () => askReservation(transferredSelection()));
  find("release-reservation").addEventListener("click", () => ask(t("Libérer les nœuds ?"),
    t("Les prochains lots pourront être pris par le pool partagé. Les lots déjà pris conservent leur bail."),
    t("Libérer"), () => saveReservation(0)));
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
