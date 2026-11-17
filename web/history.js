import { t, language } from "./i18n.js";
import {api, element} from "./portal.js";

const actions = {
  node_enrolled: t("Inscription automatique"),
  node_created: t("Création manuelle"),
  enable: t("Réactivation de nœuds"),
  disable: t("Désactivation de nœuds"),
  project_approved: t("Approbation du projet"),
  project_rejected: t("Refus du projet"),
  project_updated: t("Modification du projet"),
  key_rotated: t("Remplacement de clé API"),
  nodes_reserved: t("Réservation de nœuds"),
  reservation_released: t("Libération de réservation"),
  invite_created: t("Création d’invitation"),
  invite_revoked: t("Révocation d’invitation"),
  admin_created: t("Création d’un compte"),
  admin_revoked: t("Révocation d’un compte"),
  key_created: t("Création de clé API"),
  key_revoked: t("Révocation de clé API"),
  node_revoked: t("Révocation de nœud"),
  job_cancelled: t("Annulation de calcul"),
  job_retried: t("Relance de calcul"),
  project_policy_updated: t("Modification de l’admission et de la priorité"),
};
const origins = {admin: t("Administration"), agent: t("Agent")};
const formatDate = new Intl.DateTimeFormat(language, {dateStyle: "short", timeStyle: "medium"});
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
  switch (event.action) {
    case "node_enrolled":
    case "node_created": return t("Nœud ajouté à la flotte");
    case "enable": return t("Nœuds réactivés : {0}", count);
    case "disable": return t("Nœuds désactivés : {0} · Lots remis en file : {1}", count, event.released);
    case "nodes_reserved": return t("Nœuds dans la nouvelle réservation : {0}", count);
    case "reservation_released": return t("Nœuds rendus au pool partagé : {0}", count);
    case "project_approved": return t("Accès API accordé");
    case "project_rejected": return t("Projet classé comme refusé");
    case "project_updated": return t("Informations enregistrées");
    case "key_rotated": return t("Ancienne clé invalidée");
    case "invite_created": return t("Invitation disponible");
    case "invite_revoked": return t("Nouvelles inscriptions bloquées");
    case "admin_created": return t("Compte nominal créé");
    case "admin_revoked": return t("Accès administratif retiré");
    case "key_created": return t("Nouvel accès API");
    case "key_revoked": return t("Accès de cette clé retiré");
    case "node_revoked": return t("Identité invalidée · lots remis en file : {0}", event.released);
    case "job_cancelled": return t("Lots non terminés annulés");
    case "job_retried": return t("Nouvelle demande créée");
    case "project_policy_updated": return t("Nouvelles règles d’admission et de priorité");
    default: return "—";
  }
}

function targets(event) {
  const cell = document.createElement("td");
  const entities = [
    ...event.projects.map((project) => ({...project, prefix: t("Projet"), url: `/admin/projects/${project.id}`})),
    ...event.nodes.map((node) => ({...node, prefix: t("Nœud"), url: `/admin?q=${node.id}#flotte`})),
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
    if (event.projects.length) pieces.push(t("Projets : {0}", event.projects.length));
    if (event.nodes.length) pieces.push(t("Nœuds : {0}", event.nodes.length));
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
    notice(t("La date de fin doit suivre la date de début."), true);
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
    find("history-page").textContent = t("Page {0} sur {1}", page, pages);
    find("history-total").textContent = t("Actions : {0}", data.total);
    previous.disabled = page <= 1;
    next.disabled = page >= pages;
    const rows = data.events.map(row);
    if (!rows.length) {
      const empty = document.createElement("tr");
      const cell = element("td", t("Aucune action ne correspond à ces filtres."), "muted");
      cell.colSpan = 5;
      empty.append(cell);
      rows.push(empty);
    }
    find("history-rows").replaceChildren(...rows);
    notice("");
  } catch (error) {
    if (request === revision) notice(t("Impossible de charger le journal. {0}", error.message), true);
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
