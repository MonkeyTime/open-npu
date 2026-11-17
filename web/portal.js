import { t, translateError, language } from "./i18n.js";
const selectionKey = "open-npu-project-selection";

export function transferSelection(ids) {
  sessionStorage.setItem(selectionKey, JSON.stringify(ids));
}

export function transferredSelection() {
  try {
    const ids = JSON.parse(sessionStorage.getItem(selectionKey));
    return Array.isArray(ids) && ids.length <= 100 && ids.every((id) => Number.isSafeInteger(id) && id > 0)
      ? [...new Set(ids)] : [];
  } catch {
    return [];
  }
}

export function clearTransferredSelection() {
  sessionStorage.removeItem(selectionKey);
}

export async function api(path, data) {
  const options = data === undefined ? {} : {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(data),
  };
  const response = await fetch(path, {...options, signal: AbortSignal.timeout(15000)});
  const result = response.headers.get("Content-Type")?.includes("application/json") ? await response.json() : {};
  if (!response.ok) throw new Error(translateError(result.error, response.status));
  return result;
}

export function element(tag, value, className) {
  const node = document.createElement(tag);
  node.textContent = value;
  if (className) node.className = className;
  return node;
}

export function handleForm(form, status, send) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector('button:not([type="button"])');
    button.disabled = true;
    status.textContent = "";
    status.classList.remove("error");
    try {
      await send(Object.fromEntries(new FormData(form)));
      form.reset();
    } catch (error) {
      status.textContent = error.message;
      status.classList.add("error");
    } finally {
      button.disabled = false;
    }
  });
}

const projectForm = document.querySelector("#project-form");
if (projectForm) {
  const status = document.querySelector("#submission-status");
  handleForm(projectForm, status, async (data) => {
    const project = await api("/v1/projects", data);
    status.textContent = t("Projet n°{0} envoyé. Il est en attente de validation.", project.id);
  });
}

const contributionForm = document.querySelector("#contribution-form");
if (contributionForm) {
  const status = document.querySelector("#contribution-status");
  handleForm(contributionForm, status, async (data) => {
    const contribution = await api("/v1/contributions", {...data, node_count: Number(data.node_count)});
    status.textContent = t("Offre de contribution n°{0} reçue.", contribution.id);
  });
}

const releaseNotice = document.querySelector("#agent-download-status");
if (releaseNotice) {
  api("/downloads/agent/latest.json").then((release) => {
    const link = document.querySelector("#agent-download-link");
    const setup = document.querySelector("#agent-setup-link");
    const hash = document.querySelector("#agent-download-hash");
    const size = new Intl.NumberFormat(language, {maximumFractionDigits: 1}).format(release.size / 1048576);
    link.href = release.url;
    link.textContent = t("Télécharger la version {0}", release.version);
    if (release.setup_url) {
      setup.href = release.setup_url;
      setup.hidden = false;
      document.querySelector("#agent-invite-info").hidden = true;
      document.querySelector("#agent-public-info").hidden = false;
      link.classList.add("cta-outline");
    }
    hash.querySelector("code").textContent = "ZIP SHA-256 " + release.sha256;
    releaseNotice.textContent = t(release.signed ? "Paquet signé · {0} Mio" : "Essai local · paquet non signé · {0} Mio", size);
    document.querySelector("#agent-download-options").hidden = hash.hidden = false;
  }).catch(() => {
    releaseNotice.textContent = t("Aucun téléchargement disponible sur cette instance.");
  });
}
