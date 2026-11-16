import {api, element} from "./portal.js";

const actions = {
  node_enrolled: "Inscription automatique",
  node_created: "Création manuelle",
  enable: "Réactivation de nœuds",
  disable: "Désactivation de nœuds",
  project_approved: "Approbation du projet",
  project_rejected: "Refus du projet",
  project_updated: "Modification du projet",
  key_rotated: "Remplacement de clé API",
  nodes_reserved: "Réservation de nœuds",
  reservation_released: "Libération de réservation",
  invite_created: "Création d’invitation",
  invite_revoked: "Révocation d’invitation",
  admin_created: "Création d’un compte",
  admin_revoked: "Révocation d’un compte",
  key_created: "Création de clé API",
  key_revoked: "Révocation de clé API",
  node_revoked: "Révocation de nœud",
  job_cancelled: "Annulation de calcul",
  job_retried: "Relance de calcul",
  project_policy_updated: "Modification de l’admission et de la priorité",
};
const origins = {admin: "Administration", agent: "Agent"};
const formatDate = new Intl.DateTimeFormat("fr-FR", {dateStyle: "short", timeStyle: "medium"});
const find = (id) => document.getElementById(id);
const form = find("history-filters");
const previous = find("history-previous");
const next = find("history-next");
const initial = new URLSearchParams(location.search);
let page = 1;
let pages = 1;
let revision = 0;

for (const [id, label] of Object.entries(actions)) {
  const option = element("option", label);
  option.value = id;
  find("action").append(option);
}
for (const name of ["project", "node"]) find(name).value = (initial.get(name) || "").slice(0, 160);

function midnight(value, extraDays = 0) {
  const [year, month, day] = value.split("-").map(Number);
  const date = new Date(0);
  date.setFullYear(year, month - 1, day + extraDays);
  date.setHours(0, 0, 0, 0);
  return Math.floor(date.getTime() / 1000);
}

function effect(event) {
  const count = event.quantity ?? 0;
  const plural = count === 1 ? "" : "s";
  switch (event.action) {
    case "node_enrolled":
    case "node_created": return "Nœud ajouté à la flotte";
    case "enable": return `${count} nœud${plural} réactivé${plural}`;
    case "disable": return `${count} nœud${plural} désactivé${plural} · ${event.released} lot${event.released === 1 ? "" : "s"} remis en file`;
    case "nodes_reserved": return `${count} nœud${plural} dans la nouvelle réservation`;
    case "reservation_released": return `${count} nœud${plural} rendu${plural} au pool partagé`;
    case "project_approved": return "Accès API accordé";
    case "project_rejected": return "Projet classé comme refusé";
    case "project_updated": return "Informations enregistrées";
    case "key_rotated": return "Ancienne clé invalidée";
    case "invite_created": return "Invitation disponible";
    case "invite_revoked": return "Nouvelles inscriptions bloquées";
    case "admin_created": return "Compte nominal créé";
    case "admin_revoked": return "Accès administratif retiré";
    case "key_created": return "Nouvel accès API";
    case "key_revoked": return "Accès de cette clé retiré";
    case "node_revoked": return `Identité invalidée · lots remis en file : ${event.released}`;
    case "job_cancelled": return "Lots non terminés annulés";
    case "job_retried": return "Nouvelle demande créée";
    case "project_policy_updated": return "Nouvelles règles d’admission et de priorité";
    default: return "—";
  }
}

function targets(event) {
  const cell = document.createElement("td");
  const entities = [
    ...event.projects.map((project) => ({...project, prefix: "Projet", url: `/admin/projects/${project.id}`})),
    ...event.nodes.map((node) => ({...node, prefix: "Nœud", url: `/admin?q=${node.id}#flotte`})),
  ];
  if (!entities.length) {
    cell.textContent = "—";
    return cell;
  }
  const list = element("ul", "", "history-targets");
  for (const entity of entities) {
    const item = document.createElement("li");
    const link = element("a", `${entity.prefix} #${entity.id} · ${entity.name}`);
    link.href = entity.url;
    item.append(link);
    list.append(item);
  }
  if (entities.length > 2) {
    const details = document.createElement("details");
    const pieces = [];
    if (event.projects.length) pieces.push(`${event.projects.length} projet${event.projects.length === 1 ? "" : "s"}`);
    if (event.nodes.length) pieces.push(`${event.nodes.length} nœud${event.nodes.length === 1 ? "" : "s"}`);
    details.append(element("summary", pieces.join(" · ")), list);
    cell.append(details);
  } else {
    cell.append(list);
  }
  return cell;
}

function row(event) {
  const entry = document.createElement("tr");
  const instant = new Date(event.created * 1000);
  const time = element("time", formatDate.format(instant));
  time.dateTime = instant.toISOString();
  const when = document.createElement("td");
  when.append(time);
  entry.append(when, element("td", actions[event.action] ?? event.action),
    element("td", event.actor_name ?? origins[event.actor] ?? event.actor), targets(event), element("td", effect(event)));
  return entry;
}

function notice(message, error = false) {
  const output = find("history-notice");
  output.textContent = message;
  output.classList.toggle("error", error);
}

async function load() {
  const request = ++revision;
  const params = new URLSearchParams(new FormData(form));
  const from = find("from").value;
  const until = find("until").value;
  if (from && until && from > until) {
    notice("La date de fin doit suivre la date de début.", true);
    return;
  }
  if (from) params.set("from", midnight(from));
  if (until) params.set("until", midnight(until, 1));
  params.set("page", page);
  try {
    const data = await api(`/admin/history/data?${params}`);
    if (request !== revision) return;
    page = data.page;
    pages = data.pages;
    find("history-page").textContent = `Page ${page} sur ${pages}`;
    find("history-total").textContent = `${data.total} action${data.total === 1 ? "" : "s"}`;
    previous.disabled = page <= 1;
    next.disabled = page >= pages;
    const rows = data.events.map(row);
    if (!rows.length) {
      const empty = document.createElement("tr");
      const cell = element("td", "Aucune action ne correspond à ces filtres.", "muted");
      cell.colSpan = 5;
      empty.append(cell);
      rows.push(empty);
    }
    find("history-rows").replaceChildren(...rows);
    notice("");
  } catch (error) {
    if (request === revision) notice(`Impossible de charger le journal. ${error.message}`, true);
  }
}

form.addEventListener("submit", (event) => { event.preventDefault(); page = 1; load(); });
form.addEventListener("reset", () => {
  queueMicrotask(() => { page = 1; load(); });
});
previous.addEventListener("click", () => { page = Math.max(1, page - 1); load(); });
next.addEventListener("click", () => { page = Math.min(pages, page + 1); load(); });
find("history-refresh").addEventListener("click", load);
load();
