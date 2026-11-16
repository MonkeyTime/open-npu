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
  if (!response.ok) throw new Error(result.error || "La requête a échoué.");
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
    status.textContent = `Projet n°${project.id} envoyé. Il est en attente de validation.`;
  });
}
