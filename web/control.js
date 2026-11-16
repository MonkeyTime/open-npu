import {api, element, handleForm} from "./portal.js";

const byId = (id) => document.getElementById(id);
const notice = byId("control-notice");
const time = new Intl.DateTimeFormat("fr-FR", {dateStyle: "short", timeStyle: "short"});
const labels = {queued: "En attente", running: "En cours", done: "Terminée", failed: "En échec", cancelled: "Annulée"};
const roles = {owner: "Propriétaire", operator: "Opérateur", reader: "Lecture seule"};
const date = (seconds) => time.format(new Date(seconds * 1000));
const actionDialog = byId("action-dialog");
const credentialDialog = byId("credential-dialog");
let pendingAction;

function message(text = "", error = false) {
  notice.textContent = text;
  notice.classList.toggle("error", error);
}

function action(text, handler) {
  const button = element("button", text, "secondary");
  button.type = "button";
  button.addEventListener("click", handler);
  return button;
}

function table(id, rows, width) {
  if (!rows.length) rows = [["", "Aucun élément."]];
  byId(id).replaceChildren(...rows.map((values) => {
    const row = document.createElement("tr");
    if (values.length !== width) {
      const empty = element("td", values[1], "muted");
      empty.colSpan = width;
      row.append(empty);
    } else for (const value of values) {
      const cell = document.createElement("td");
      if (value instanceof Node) cell.append(value);
      else cell.textContent = value ?? "—";
      row.append(cell);
    }
    return row;
  }));
}

function confirmAction(label, description, run) {
  pendingAction = run;
  byId("action-title").textContent = label;
  byId("action-text").textContent = description;
  actionDialog.returnValue = "";
  actionDialog.showModal();
}

actionDialog?.addEventListener("close", async () => {
  const run = pendingAction;
  pendingAction = null;
  if (actionDialog.returnValue !== "confirm" || !run) return;
  try { await run(); } catch (error) { message(error.message, true); }
});

function reveal(title, value) {
  byId("credential-title").textContent = title;
  const field = byId("credential-value");
  field.value = value;
  credentialDialog.showModal();
  field.focus();
  field.select();
}
credentialDialog?.addEventListener("close", () => { byId("credential-value").value = ""; });

function access() {
  const invite = byId("invite-form");
  const lookup = byId("lookup-form");
  const key = byId("key-form");
  const accounts = byId("account-form");
  let projectId;
  let role = "reader";

  async function refreshKeys(id = projectId) {
    if (!id) return;
    const data = await api(`/admin/projects/${id}/keys`);
    projectId = id;
    byId("keys-title").textContent = `Clés API · Projet #${id}`;
    key.hidden = role === "reader";
    byId("key-table").hidden = false;
    table("key-rows", data.keys.map((item) => {
      const active = !item.revoked && item.expires > Date.now() / 1000;
      return [`#${item.id} · ${item.label}`, item.scope === "read" ? "Lecture" : "Lecture et écriture",
        date(item.expires), item.revoked ? "Révoquée" : active ? "Active" : "Expirée",
        active && role !== "reader" ? action("Révoquer", () => confirmAction("Révoquer la clé ?",
          "Les clients qui utilisent cette clé perdront l’accès immédiatement.", async () => {
            await api(`/admin/projects/${id}/keys/${item.id}/revoke`, {});
            await refreshKeys(id);
            message("Clé révoquée.");
          })) : null];
    }), 5);
  }

  async function load() {
    const [access, invitations] = await Promise.all([api("/admin/access/data"), api("/admin/enrollment-invites")]);
    role = access.role;
    invite.hidden = role === "reader";
    accounts.hidden = role !== "owner";
    byId("accounts-table").hidden = role !== "owner";
    byId("account-info").textContent = `Connecté en tant que ${access.username} · ${roles[role]}`;
    byId("logout").hidden = access.username === "superadmin";
    table("invite-rows", invitations.invites.map((item) => [
      `#${item.id} · ${item.label}`, date(item.expires), `${item.uses} / ${item.max_uses}`,
      item.revoked ? "Révoquée" : item.expires <= Date.now() / 1000 ? "Expirée" : item.uses >= item.max_uses ? "Utilisée" : "Active",
      !item.revoked && role !== "reader" ? action("Révoquer", () => confirmAction("Révoquer l’invitation ?",
        "Les postes déjà inscrits restent enregistrés.", async () => {
          await api(`/admin/enrollment-invites/${item.id}/revoke`, {});
          await load();
          message("Invitation révoquée.");
        })) : null,
    ]), 5);
    table("account-rows", access.accounts.map((account) => [
      account.name, roles[account.role], account.mfa ? "Activé" : "Désactivé", account.revoked ? "Révoqué" : "Actif",
      !account.revoked && account.name !== access.username ? action("Révoquer", () => confirmAction("Révoquer le compte ?",
        `Le compte ${account.name} perdra son accès à l’administration.`, async () => {
          await api(`/admin/access/accounts/${encodeURIComponent(account.name)}/revoke`, {});
          await load();
          message("Compte révoqué.");
        })) : null,
    ]), 5);
    await refreshKeys();
  }

  handleForm(invite, notice, async (data) => {
    const issued = await api("/admin/enrollment-invites", {...data, hours: Number(data.hours), max_uses: Number(data.max_uses)});
    reveal("Invitation à placer dans un fichier JSON privé", JSON.stringify({enrollment_key: issued.enrollment_key}, null, 2));
    await load();
  });
  handleForm(accounts, notice, async (data) => {
    const issued = await api("/admin/access/accounts", {...data, mfa: data.mfa === "on"});
    const credentials = issued.totp
      ? `Mot de passe : ${issued.password}\nClé à ajouter dans l’application d’authentification (TOTP, 6 chiffres, 30 s) : ${issued.totp}`
      : issued.password;
    reveal(`Accès pour ${issued.username}`, credentials);
    await load();
  });
  handleForm(key, notice, async (data) => {
    const issued = await api(`/admin/projects/${projectId}/keys`, {...data, days: Number(data.days)});
    reveal("Clé API du projet", issued.api_key);
    await refreshKeys();
  });
  byId("logout").addEventListener("click", async () => {
    try { await api("/admin/session/logout", {}); location.assign("/login"); }
    catch (error) { message(error.message, true); }
  });
  lookup.addEventListener("submit", async (event) => {
    event.preventDefault();
    try { await refreshKeys(Number(lookup.elements.project.value)); message(); }
    catch (error) { message(error.message, true); }
  });
  const initial = new URLSearchParams(location.search).get("project");
  if (initial && /^[1-9][0-9]{0,14}$/.test(initial)) {
    projectId = Number(initial);
    lookup.elements.project.value = initial;
  }
  return load;
}

function jobs() {
  const form = byId("jobs-filters");
  const previous = byId("job-prev");
  const next = byId("job-next");
  let page = 1;
  let pages = 1;
  let sequence = 0;
  form.elements.q.value = (new URLSearchParams(location.search).get("q") || "").slice(0, 100);

  async function load() {
    const revision = ++sequence;
    const params = new URLSearchParams(new FormData(form));
    params.set("page", page);
    const data = await api(`/admin/jobs/data?${params}`);
    if (revision !== sequence) return;
    page = data.page;
    pages = data.pages;
    previous.disabled = page <= 1;
    next.disabled = page >= pages;
    byId("job-page").textContent = `${data.total} demande(s) · Page ${page} sur ${pages}`;
    table("job-rows", data.jobs.map((job) => {
      const project = element("a", job.project_name, "project-directory-link");
      project.href = `/admin/projects/${job.project}`;
      const current = ["queued", "running"].includes(job.status);
      const canRetry = ["failed", "cancelled"].includes(job.status);
      const verb = current ? "Annuler" : "Relancer";
      const control = data.writable && (current || canRetry) ? action(verb, () => confirmAction(
        `${verb} cette demande ?`, current ? "Les lots non terminés seront annulés." : "Une nouvelle demande reprendra les mêmes entrées.",
        async () => {
          const result = await api(`/admin/jobs/${job.id}/${current ? "cancel" : "retry"}`, {});
          await load();
          message(current ? "Demande annulée." : `Demande créée : ${result.id}`);
        })) : null;
      return [job.id, project, date(job.created), labels[job.status], `${job.completed} / ${job.total}`, control];
    }), 6);
  }

  const refresh = () => load().catch((error) => message(error.message, true));
  form.addEventListener("submit", (event) => { event.preventDefault(); page = 1; message(); refresh(); });
  previous.addEventListener("click", () => { page = Math.max(1, page-1); refresh(); });
  next.addEventListener("click", () => { page = Math.min(pages, page+1); refresh(); });
  return load;
}

function metrics(id, pairs) {
  byId(id).replaceChildren(...pairs.map(([label, value]) => {
    const row = document.createElement("div");
    row.append(element("dt", label), element("dd", value));
    return row;
  }));
}

function operations() {
  async function load() {
    const data = await api("/admin/operations/data");
    metrics("instance-metrics", [
      ["Mode", data.mode], ["Capsule", data.capsule], ["Contrôle des résultats", data.verification],
      ["Âge du plus ancien lot en attente", data.oldest_seconds === null ? "—" : `${data.oldest_seconds} s`],
      ["Baux expirés en attente de reprise", data.expired_leases],
    ]);
    metrics("task-metrics", Object.entries(labels).map(([key, label]) => [label, data.tasks[key] ?? 0]));
    metrics("version-metrics", data.versions.length ? data.versions.map((item) => [item.version, item.total]) : [["Aucun agent en ligne", "—"]]);
  }
  setInterval(() => { if (!document.hidden) load().then(() => message(), (error) => message(error.message, true)); }, 15000);
  return load;
}

const views = {access, jobs, operations};
const load = views[byId("admin").dataset.control]();
const refresh = () => load().then(() => message(), (error) => message(error.message, true));
byId("refresh-control").addEventListener("click", refresh);
refresh();
