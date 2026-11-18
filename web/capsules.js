import { t } from "./i18n.js";
import {api, element} from "./portal.js";

const find = (id) => document.getElementById(id);
const dialog = find("capsule-dialog");
const labels = {queued: t("En attente"), running: t("En calcul"), done: t("Terminé"), failed: t("En échec"), cancelled: t("Annulé")};
let selection;
let pending = false;

function table(id, data, columns) {
  const body = find(id);
  if (!data.length) {
    const row = document.createElement("tr");
    const empty = element("td", t("Aucun élément."), "muted");
    empty.colSpan = columns;
    row.append(empty);
    body.replaceChildren(row);
    return;
  }
  body.replaceChildren(...data.map((cells) => {
    const row = document.createElement("tr");
    for (const value of cells) {
      const cell = document.createElement("td");
      if (value instanceof Node) cell.append(value);
      else cell.textContent = value;
      row.append(cell);
    }
    return row;
  }));
}

async function refresh() {
  if (pending) return;
  pending = true;
  const notice = find("capsule-notice");
  try {
    const [catalog, queue] = await Promise.all([api("/admin/capsules/data"), api("/admin/capsules/jobs")]);
    find("catalog-help").textContent = catalog.configured
      ? t("Approuvez une version importée pour permettre son exécution.")
      : t("Configurez la clé publique du signataire sur le serveur, puis importez une capsule signée.");
    table("catalog-rows", catalog.capsules.map((capsule) => {
      let button = "—";
      if (catalog.owner && (capsule.available || capsule.approved)) {
        button = element("button", capsule.approved ? t("Retirer") : t("Approuver"), "secondary");
        button.type = "button";
        button.addEventListener("click", () => {
          selection = `/admin/capsules/${capsule.digest}/${capsule.approved ? "withdraw" : "approve"}`;
          find("dialog-title").textContent = t("{0} {1} v{2} ?", capsule.approved ? t("Retirer") : t("Approuver"), capsule.id, capsule.version);
          find("dialog-description").textContent = capsule.approved
            ? t("Les calculs ouverts de cette version seront interrompus.")
            : t("La signature et les fichiers seront vérifiés avant l’ouverture aux projets.");
          dialog.returnValue = "";
          dialog.showModal();
        });
      }
      const kind = capsule.kind === "decoder" ? t("Décodeur Transformer") : t("Chaîne de tenseurs");
      const format = capsule.format === 2 ? "ONNX" : "OpenVINO IR";
      return [capsule.id, capsule.version, capsule.available ? `${kind} · ${format}` : "—",
        capsule.stages ?? "—", !capsule.available ? t("Indisponible") : capsule.approved ? t("Approuvée") : t("En attente"), button];
    }), 6);
    table("pipeline-rows", queue.jobs.map((job) => {
      const active = ["queued", "running"].includes(job.status);
      let action = "—";
      if (queue.writable && active) {
        action = element("button", t("Annuler"), "secondary");
        action.type = "button";
        action.addEventListener("click", () => {
          selection = `/admin/capsules/jobs/${job.id}/cancel`;
          find("dialog-title").textContent = t("Annuler cette demande ?");
          find("dialog-description").textContent = t("La demande sera retirée de la file. Les résultats en cours seront ignorés.");
          dialog.returnValue = "";
          dialog.showModal();
        });
      }
      const status = job.error ? `${labels[job.status]} · ${t(job.error)}`
        : job.verifying && active ? t("Vérification du résultat") : labels[job.status];
      return [job.id, job.project, `${job.capsule} v${job.version}`, status,
        active ? `${job.stage + 1}${job.node ? ` · ${job.node}` : ""}` : "—", job.assigned, action];
    }), 7);
    find("capsule-workers").textContent = t("{0} agent(s) compatible(s) récemment actif(s)", queue.workers);
    notice.textContent = "";
  } catch (error) {
    notice.textContent = error.message;
  } finally { pending = false; }
}

dialog.addEventListener("close", async () => {
  if (dialog.returnValue !== "confirm" || !selection) return;
  try {
    await api(selection, {});
    await refresh();
  } catch (error) { find("capsule-notice").textContent = error.message; }
});
find("refresh-capsules").addEventListener("click", refresh);
setInterval(() => { if (!document.hidden && !dialog.open) refresh(); }, 5000);
refresh();
