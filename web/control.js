import { t, language } from "./i18n.js";
import {api, element, handleForm} from "./portal.js";

const byId = (id) => document.getElementById(id);
const notice = byId("control-notice");
const time = new Intl.DateTimeFormat(language, {dateStyle: "short", timeStyle: "short"});
const labels = {queued: t("En attente"), running: t("En cours"), done: t("Terminée"), failed: t("En échec"), cancelled: t("Annulée")};
const roles = {owner: t("Propriétaire"), operator: t("Opérateur"), reader: t("Lecture seule")};
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
  if (!rows.length) rows = [["", t("Aucun élément.")]];
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
  const choice = lookup.elements.project;
  const search = lookup.elements.search;
  const key = byId("key-form");
  const accounts = byId("account-form");
  let projectId;
  let projectName;
  let role = "reader";
  let searchVersion = 0;
  let keysVersion = 0;
  let searchTimer;

  async function listProjects() {
    const version = ++searchVersion;
    const term = search.value.trim();
    const params = new URLSearchParams({status: "approved", size: "25", q: term});
    const data = await api(`/admin/projects/data?${params}`);
    if (version !== searchVersion) return;
    const selected = choice.value || (projectId ? String(projectId) : "");
    const former = [...choice.options].find((option) => option.value === selected);
    const choices = data.projects.map((project) => {
      const option = element("option", t("{0} (#{1})", project.name, project.id));
      option.value = String(project.id);
      option.dataset.name = project.name;
      return option;
    });
    if (former && selected && !choices.some((option) => option.value === selected)) choices.unshift(former);
    const placeholder = element("option", t("Choisir un projet"));
    placeholder.value = "";
    choice.replaceChildren(placeholder, ...choices);
    choice.value = selected;
    const note = byId("project-lookup-note");
    note.textContent = data.total > data.projects.length
      ? t("{0} projets sur {1}. Affinez la recherche.", data.projects.length, data.total)
      : data.total === 0 ? (term ? t("Aucun projet approuvé ne correspond à la recherche.") : t("Aucun projet approuvé.")) : "";
  }

  async function refreshKeys(id = projectId) {
    if (!id) return;
    const version = ++keysVersion;
    const data = await api(`/admin/projects/${id}/keys`);
    if (version !== keysVersion) return;
    projectId = id;
    projectName = choice.selectedOptions[0]?.dataset.name ?? projectName;
    byId("keys-title").textContent = projectName ? t("Clés API · {0} (#{1})", projectName, id) : t("Clés API · Projet #{0}", id);
    key.hidden = role === "reader";
    byId("key-table").hidden = false;
    table("key-rows", data.keys.map((item) => {
      const active = !item.revoked && item.expires > Date.now() / 1000;
      return [`#${item.id} · ${item.label}`, item.scope === "read" ? t("Lecture") : t("Lecture et écriture"),
        date(item.expires), item.revoked ? t("Révoquée") : active ? t("Active") : t("Expirée"),
        active && role !== "reader" ? action(t("Révoquer"), () => confirmAction(t("Révoquer la clé ?"),
          t("Les clients qui utilisent cette clé perdront l’accès immédiatement."), async () => {
            await api(`/admin/projects/${id}/keys/${item.id}/revoke`, {});
            await refreshKeys(id);
            message(t("Clé révoquée."));
          })) : null];
    }), 5);
  }

  async function load() {
    const [access, invitations] = await Promise.all([api("/admin/access/data"), api("/admin/enrollment-invites")]);
    role = access.role;
    invite.hidden = role === "reader";
    accounts.hidden = role !== "owner";
    byId("accounts-table").hidden = role !== "owner";
    byId("account-info").textContent = t("Connecté en tant que {0} · {1}", access.username, roles[role]);
    byId("logout").hidden = access.username === "superadmin";
    table("invite-rows", invitations.invites.map((item) => [
      `#${item.id} · ${item.label}`, date(item.expires), `${item.uses} / ${item.max_uses}`,
      item.revoked ? t("Révoquée") : item.expires <= Date.now() / 1000 ? t("Expirée") : item.uses >= item.max_uses ? t("Utilisée") : t("Active"),
      !item.revoked && role !== "reader" ? action(t("Révoquer"), () => confirmAction(t("Révoquer l’invitation ?"),
        t("Les postes déjà inscrits restent enregistrés."), async () => {
          await api(`/admin/enrollment-invites/${item.id}/revoke`, {});
          await load();
          message(t("Invitation révoquée."));
        })) : null,
    ]), 5);
    table("account-rows", access.accounts.map((account) => [
      account.name, roles[account.role], account.mfa ? t("Activé") : t("Désactivé"), account.revoked ? t("Révoqué") : t("Actif"),
      !account.revoked && account.name !== access.username ? action(t("Révoquer"), () => confirmAction(t("Révoquer le compte ?"),
        t("Le compte {0} perdra son accès à l’administration.", account.name), async () => {
          await api(`/admin/access/accounts/${encodeURIComponent(account.name)}/revoke`, {});
          await load();
          message(t("Compte révoqué."));
        })) : null,
    ]), 5);
    await listProjects();
    await refreshKeys();
  }

  handleForm(invite, notice, async (data) => {
    const issued = await api("/admin/enrollment-invites", {...data, hours: Number(data.hours), max_uses: Number(data.max_uses)});
    reveal(t("Invitation à placer dans un fichier JSON privé"), JSON.stringify({enrollment_key: issued.enrollment_key}, null, 2));
    await load();
  });
  handleForm(accounts, notice, async (data) => {
    const issued = await api("/admin/access/accounts", {...data, mfa: data.mfa === "on"});
    const credentials = issued.totp
      ? t("Mot de passe : {0}\nClé à ajouter dans l’application d’authentification (TOTP, 6 chiffres, 30 s) : {1}", issued.password, issued.totp)
      : issued.password;
    reveal(t("Accès pour {0}", issued.username), credentials);
    await load();
  });
  handleForm(key, notice, async (data) => {
    const issued = await api(`/admin/projects/${projectId}/keys`, {...data, days: Number(data.days)});
    reveal(t("Clé API du projet"), issued.api_key);
    await refreshKeys();
  });
  byId("logout").addEventListener("click", async () => {
    try { await api("/admin/session/logout", {}); location.assign("/login"); }
    catch (error) { message(error.message, true); }
  });
  async function chooseProject() {
    keysVersion++;
    projectId = undefined;
    projectName = undefined;
    key.hidden = true;
    byId("key-table").hidden = true;
    byId("keys-title").textContent = t("Clés API par projet");
    if (!choice.value) return;
    try { await refreshKeys(Number(choice.value)); message(); }
    catch (error) { message(error.message, true); }
  }
  lookup.addEventListener("submit", (event) => {
    event.preventDefault();
    chooseProject();
  });
  choice.addEventListener("change", chooseProject);
  search.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchVersion++;
    searchTimer = setTimeout(() => listProjects().catch((error) => message(error.message, true)), 250);
  });
  const initial = new URLSearchParams(location.search).get("project");
  return async () => {
    if (!choice.dataset.initialized && initial && /^[1-9][0-9]{0,14}$/.test(initial)) {
      choice.dataset.initialized = "true";
      const project = await api(`/admin/projects/${initial}/data`);
      if (project.status === "approved") {
        projectId = project.id;
        projectName = project.name;
        const option = element("option", t("{0} (#{1})", project.name, project.id));
        option.value = initial;
        option.dataset.name = project.name;
        choice.append(option);
        choice.value = initial;
      }
    }
    return load();
  };
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
    byId("job-page").textContent = t("{0} demande(s) · Page {1} sur {2}", data.total, page, pages);
    table("job-rows", data.jobs.map((job) => {
      const project = element("a", job.project_name, "project-directory-link");
      project.href = `/admin/projects/${job.project}`;
      const current = ["queued", "running"].includes(job.status);
      const canRetry = ["failed", "cancelled"].includes(job.status);
      const verb = current ? t("Annuler") : t("Relancer");
      const control = data.writable && (current || canRetry) ? action(verb, () => confirmAction(
        t("{0} cette demande ?", verb), current ? t("Les lots non terminés seront annulés.") : t("Une nouvelle demande reprendra les mêmes entrées."),
        async () => {
          const result = await api(`/admin/jobs/${job.id}/${current ? "cancel" : "retry"}`, {});
          await load();
          message(current ? t("Demande annulée.") : t("Demande créée : {0}", result.id));
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

const views = {access, jobs};
const load = views[byId("admin").dataset.control]();
const refresh = () => load().then(() => message(), (error) => message(error.message, true));
byId("refresh-control").addEventListener("click", refresh);
refresh();
