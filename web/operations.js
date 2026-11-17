import { t, language } from "./i18n.js";
import {api, element} from "./portal.js";

const byId = (id) => document.getElementById(id);
const number = new Intl.NumberFormat(language);
const hhmm = new Intl.DateTimeFormat(language, {hour: "2-digit", minute: "2-digit"});
const clock = new Intl.DateTimeFormat(language, {hour: "2-digit", minute: "2-digit", second: "2-digit"});
const states = {working: t("En calcul"), online: t("Disponible"), reserved: t("Réservé"), paused: t("En pause"),
  offline: t("Hors ligne"), new: t("Jamais connecté"), disabled: t("Désactivé"), revoked: t("Révoqué")};
const outcomes = {queued: t("En attente"), running: t("En cours"), done: t("Terminés"), failed: t("En échec"), cancelled: t("Annulés")};
const actions = {node_enrolled: t("Inscription d’un nœud"), node_created: t("Création d’un nœud"),
  enable: t("Réactivation de nœuds"), disable: t("Désactivation de nœuds"), node_revoked: t("Révocation d’un nœud"),
  project_approved: t("Projet approuvé"), project_rejected: t("Projet refusé"), project_updated: t("Projet modifié"),
  key_rotated: t("Clé remplacée"), nodes_reserved: t("Nœuds réservés"), reservation_released: t("Réservation libérée"),
  invite_created: t("Invitation créée"), invite_revoked: t("Invitation révoquée"), admin_created: t("Compte créé"),
  admin_revoked: t("Compte révoqué"), key_created: t("Clé créée"), key_revoked: t("Clé révoquée"),
  job_cancelled: t("Demande annulée"), job_retried: t("Demande relancée"), project_policy_updated: t("Règles de projet modifiées")};
const groups = {all: () => true, working: (state) => state === "working",
  ready: (state) => state === "online" || state === "reserved",
  other: (state) => !["working", "online", "reserved"].includes(state)};
const filters = {all: t("Tous"), working: t("En calcul"), ready: t("Disponibles"), other: t("Autres états")};
const rows = {};
let current;
let serverOffset = 0;
let chosen = "all";
let auto = true;
let fetching = false;
let lastSuccess = null;
let delayed = false;
let selectedMinute;
let graphKey = "";

function format(value) { return number.format(value); }
function set(id, value) { byId(id).textContent = value; }
function sum(values) { return values.reduce((total, value) => total + value, 0); }
function duration(seconds) {
  if (seconds < 60) return `${seconds} s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ${seconds % 60} s`;
  return `${Math.floor(seconds / 3600)} h ${Math.floor(seconds % 3600 / 60)} min`;
}
function liveState() {
  const status = byId("live-state");
  const stamp = lastSuccess === null ? "" : ` · ${clock.format(new Date(lastSuccess * 1000))}`;
  status.dataset.state = delayed ? "error" : !lastSuccess || !auto ? "waiting" : "live";
  status.textContent = delayed ? t("Signal interrompu{0}", stamp)
    : !lastSuccess ? t("Connexion…") : auto ? t("En direct{0}", stamp) : t("Actualisation en pause{0}", stamp);
}

function renderChart() {
  const amount = Number(byId("window").value);
  const series = current.trend.slice(-amount);
  const graph = byId("chart-bars");
  const key = JSON.stringify([amount, series]);
  if (graphKey === key) return;
  graphKey = key;
  const keepFocus = graph.contains(document.activeElement);
  if (!series.some((point) => point.at === selectedMinute)) selectedMinute = series.at(-1)?.at;
  const maximum = Math.max(4, ...series.map((point) => point.completed));
  const scale = Math.ceil(maximum / 4) * 4;
  byId("chart-scale").replaceChildren(...[scale, 3*scale/4, scale/2, scale/4, 0].map((n) => element("span", format(n))));
  graph.style.setProperty("--slots", amount);
  const padding = document.createElement("span");
  padding.style.gridColumn = `span ${amount - series.length}`;
  padding.setAttribute("aria-hidden", "true");
  const bars = series.map((point, index) => {
    const bar = element("button", "", "chart-bar");
    bar.type = "button";
    bar.dataset.filled = String(point.completed > 0);
    bar.dataset.current = String(index === series.length - 1);
    bar.style.setProperty("--height", `${point.completed * 100 / scale}%`);
    const time = hhmm.format(new Date(point.at * 1000));
    const description = t("{0} · Lots terminés : {1}", time, format(point.completed));
    bar.title = description;
    bar.setAttribute("aria-label", description);
    bar.tabIndex = point.at === selectedMinute ? 0 : -1;
    const select = () => {
      selectedMinute = point.at;
      for (const item of graph.querySelectorAll("button")) item.tabIndex = item === bar ? 0 : -1;
      set("chart-note", description + (index === series.length - 1 ? t(" · minute en cours") : ""));
    };
    bar.addEventListener("click", select);
    bar.addEventListener("focus", select);
    bar.addEventListener("keydown", (event) => {
      const step = {ArrowLeft: -1, ArrowRight: 1}[event.key];
      if (!step) return;
      event.preventDefault();
      const buttons = graph.querySelectorAll("button");
      buttons[Math.max(0, Math.min(index + step, buttons.length - 1))]?.focus();
    });
    return bar;
  });
  graph.replaceChildren(...(series.length < amount ? [padding, ...bars] : bars));
  const first = series.length ? hhmm.format(new Date(series[0].at * 1000)) : "";
  set("chart-start", series.length && series.length < amount ? t("Mesure depuis {0}", first) : first);
  set("chart-end", series.length ? hhmm.format(new Date(series.at(-1).at * 1000)) : "");
  const measured = sum(series.map((point) => point.completed));
  if (keepFocus) graph.querySelector('[tabindex="0"]')?.focus({preventScroll: true});
  else set("chart-note", measured ? t("{0} lots sur la fenêtre affichée.", format(measured))
    : t("Aucun lot terminé depuis le début de cette fenêtre de mesure."));
}

for (const [key, label] of Object.entries(filters)) {
  const button = element("button", label, "secondary");
  button.type = "button";
  button.setAttribute("aria-pressed", String(key === chosen));
  button.addEventListener("click", () => { chosen = key; if (current) renderMap(); });
  rows[key] = button;
  byId("state-filters").append(button);
}

function renderMap() {
  const total = sum(Object.values(current.counts));
  const matching = sum(Object.entries(current.counts).filter(([state]) => groups[chosen](state)).map(([, count]) => count));
  for (const [key, button] of Object.entries(rows)) button.setAttribute("aria-pressed", String(key === chosen));
  const map = byId("node-map");
  const visible = current.roster.filter((node) => groups[chosen](node.state));
  const nodes = visible.map((node) => {
    const link = element("a", "", "node");
    link.href = `/admin?q=${node.id}#flotte`;
    const title = element("span", "", "node-heading");
    const badge = element("i", "", `node-dot ${node.state}`);
    badge.setAttribute("aria-hidden", "true");
    title.append(badge, element("strong", node.name));
    link.append(title, element("small", [states[node.state], node.project_name].filter(Boolean).join(" · "), "node-detail"));
    return link;
  });
  if (!nodes.length) nodes.push(element("p", t("Aucun nœud affiché pour cet état."), "muted"));
  const focused = map.contains(document.activeElement) ? document.activeElement.href : null;
  map.replaceChildren(...nodes);
  if (focused) nodes.find((node) => node.href === focused)?.focus({preventScroll:true});
  const displayed = t("Affichés : {0}", format(visible.length));
  set("roster-count", chosen === "all" ? t("{0} sur {1}", displayed, format(total))
    : t("{0} sur {1} pour ce filtre", displayed, format(matching)));
}

function renderQueue(queueId, expiryId, states, late) {
  const active = Math.max(0, (states.running ?? 0) - late);
  const counts = {...states, running: active};
  const total = Math.max(1, sum(Object.values(counts)));
  byId(queueId).replaceChildren(...Object.entries(outcomes).map(([key, label]) => {
    const value = counts[key] ?? 0;
    const row = element("div", "", "queue-row");
    row.dataset.kind = key;
    const bar = document.createElement("progress");
    bar.max = total;
    bar.value = value;
    bar.setAttribute("aria-label", `${label} : ${value}`);
    row.append(element("span", label), bar, element("strong", format(value)));
    return row;
  }));
  const expired = byId(expiryId);
  expired.dataset.warning = String(late > 0);
  expired.textContent = late ? t("{0} bail(s) expiré(s) en attente de reprise.", format(late)) : t("Aucun bail expiré en attente.");
  return active;
}

function renderActive(data) {
  const feed = byId("active-lots");
  const focused = feed.contains(document.activeElement) ? document.activeElement.href : null;
  const active = data.active.map((task) => {
    const item = document.createElement("li");
    const line = element("div", "", "feed-line");
    const link = element("a", task.project_name);
    link.href = task.kind === "v2" ? "/admin/capsules" : `/admin/jobs?q=${encodeURIComponent(task.job)}`;
    const timer = element("span", "", "timer");
    timer.dataset.expires = task.expires;
    line.append(link, timer);
    const stage = task.kind === "v2" ? (task.verifying ? t("Vérification de l’étape") : t("Étape")) : t("Lot");
    item.append(line, element("small", t("{0} · {1} {2} · Tentative {3}", task.node_name, stage, task.position + 1, task.attempts)));
    return item;
  });
  feed.replaceChildren(...(active.length ? active : [element("li", t("Aucun calcul en cours."), "muted")]));
  if (focused) [...feed.querySelectorAll("a")].find((link) => link.href === focused)?.focus({preventScroll:true});
  tick();
}

function tick() {
  if (!current || !auto || delayed || document.hidden) return;
  const now = Date.now() / 1000 + serverOffset;
  for (const timer of document.querySelectorAll("[data-expires]")) {
    const left = Math.max(0, Math.ceil(Number(timer.dataset.expires) - now));
    timer.textContent = left ? t("Bail : {0}", duration(left)) : t("Bail expiré");
  }
}

function render(data) {
  const previous = current;
  current = data;
  const together = ["working", "online", "reserved", "paused"];
  set("operator-role", data.role === "owner" ? t("Superadministration") : t("Administration"));
  set("instance-mode", t("Instance {0} · {1}", data.mode === "pilot" ? t("pilote") : t("locale"), data.capsule));
  set("nodes-online", format(sum(together.map((state) => data.counts[state] ?? 0))));
  const registered = sum(Object.values(data.counts));
  const ready = (data.counts.online ?? 0)+(data.counts.reserved ?? 0);
  set("nodes-total", t("Inscrits : {0}", format(registered)));
  set("nodes-working", format(data.counts.working ?? 0));
  set("nodes-ready", t("Prêts à calculer : {0}", format(ready)));
  set("queued", format(data.tasks.queued ?? 0));
  set("oldest", data.oldest_seconds === null ? t("File d’attente vide") : t("Plus ancien : {0}", duration(data.oldest_seconds)));
  set("completed-recent", format(data.tasks.done ?? 0));
  set("completed-total", t("{0} sur les 10 dernières minutes observées", format(sum(data.trend.slice(-10).map((point) => point.completed)))));
  renderChart();
  if (!previous || JSON.stringify([previous.counts, previous.roster]) !== JSON.stringify([data.counts, data.roster])) renderMap();
  const v1 = renderQueue("queue-breakdown", "expired", data.tasks, data.expired_leases);
  const v2 = renderQueue("pipeline-breakdown", "pipeline-expired", data.pipeline, data.pipeline_expired_leases);
  set("active-count", format(v1 + v2));
  if (!previous || JSON.stringify(previous.active) !== JSON.stringify(data.active)) renderActive(data);
  if (!previous || JSON.stringify(previous.events) !== JSON.stringify(data.events)) {
    const feed = data.events.map((event) => {
      const row = document.createElement("li");
      const line = element("div", "", "feed-line");
      line.append(element("span", actions[event.action] ?? event.action),
        element("small", clock.format(new Date(event.created * 1000))));
      row.append(line, element("small", [event.actor_name || (event.actor === "agent" ? t("Agent") : t("Administration")),
        event.released ? t("{0} lot(s) remis en file", event.released) : ""].filter(Boolean).join(" · ")));
      return row;
    });
    byId("events-feed").replaceChildren(...(feed.length ? feed : [element("li", t("Aucun événement enregistré."), "muted")]));
  }
  byId("platform").replaceChildren(...[
    [t("Contrôle des résultats"), data.verification === "fp16" ? t("Référence FP16") : t("Format")],
    ...data.versions.map((entry) => [t("Agent {0}", entry.version === "inconnue" ? t("inconnue") : entry.version), t("{0} connecté(s)", format(entry.total))]),
  ].map(([key, value]) => {
    const row = document.createElement("div");
    row.append(element("dt", key), element("dd", value));
    return row;
  }));
}

async function refresh() {
  if (fetching) return;
  fetching = true;
  try {
    const data = await api("/admin/operations/data");
    lastSuccess = data.now;
    serverOffset = data.now - Date.now() / 1000;
    delayed = false;
    set("monitor-error", "");
    render(data);
    tick();
  } catch (error) {
    delayed = true;
    set("monitor-error", current ? t("Dernier relevé conservé. {0}", error.message) : error.message);
  } finally {
    fetching = false;
    liveState();
  }
}

byId("window").addEventListener("change", () => { if (current) renderChart(); });
byId("refresh").addEventListener("click", refresh);
byId("auto-toggle").addEventListener("click", () => {
  auto = !auto;
  byId("auto-toggle").setAttribute("aria-pressed", String(auto));
  liveState();
  if (auto) refresh();
});
const fullscreen = byId("fullscreen");
if (document.fullscreenEnabled) {
  fullscreen.hidden = false;
  fullscreen.addEventListener("click", async () => {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await document.documentElement.requestFullscreen();
    } catch { set("monitor-error", t("Le navigateur a refusé le plein écran.")); }
  });
  document.addEventListener("fullscreenchange", () => {
    fullscreen.textContent = document.fullscreenElement ? t("Quitter le plein écran") : t("Plein écran");
  });
}
function poll() { if (auto && !document.hidden) refresh(); }
document.addEventListener("visibilitychange", poll);
setInterval(poll, 5000);
setInterval(tick, 1000);
refresh();
