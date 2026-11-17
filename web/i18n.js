export const languages = {
  fr: "Français", en: "English", es: "Español", pt: "Português", de: "Deutsch",
  it: "Italiano", ar: "العربية", hi: "हिन्दी", bn: "বাংলা", zh: "简体中文",
  ja: "日本語", ko: "한국어", ru: "Русский", id: "Bahasa Indonesia",
  tr: "Türkçe", vi: "Tiếng Việt", ur: "اردو",
};

const preferenceKey = "open-npu-language";
const url = new URL(location.href);
const mount = document.querySelector("[data-language-picker]");
let saved;
try { saved = localStorage.getItem(preferenceKey); } catch {}
const supported = (value) => value && Object.hasOwn(languages, value);
const browser = navigator.languages.map((tag) => tag.toLowerCase().split("-")[0]).find(supported);
const explicit = url.searchParams.get("lang");
export let language = mount ? [explicit, saved, browser].find(supported) ?? "fr" : "fr";
const root = document.documentElement;
const sourceLanguage = root.lang;
let messages = {};

export function t(message, ...values) {
  const translated = Object.hasOwn(messages, message) ? messages[message] : message;
  return translated.replace(/\{(\d+)\}/g, (match, index) => Number(index) < values.length ? String(values[index]) : match);
}

export function translateError(message, status) {
  if (language === "fr" && message) return message;
  if (Object.hasOwn(messages, message)) return t(message);
  const patterns = [
    [/^Champ invalide \((.+)\)\.$/, "Champ invalide ({0})."],
    [/^Choisissez un nombre de nœuds entre 0 et (\d+)\.$/, "Choisissez un nombre de nœuds entre 0 et {0}."],
    [/^(\d+) nœud\(s\) éligible\(s\) pour (\d+) demandé\(s\)\.$/, "{0} nœud(s) éligible(s) pour {1} demandé(s)."],
    [/^Sélectionnez de 1 à (\d+) nœuds\.$/, "Sélectionnez de 1 à {0} nœuds."],
  ];
  for (const [pattern, label] of patterns) {
    const match = pattern.exec(message);
    if (match) return t(label, ...match.slice(1));
  }
  const defaults = {
    400: "La requête est invalide.", 401: "Connexion requise ou identifiants invalides.",
    403: "Vous n’avez pas accès à cette opération.", 404: "La ressource est introuvable.",
    409: "La requête est incompatible avec l’état actuel de la ressource.",
    413: "La taille maximale de la requête est dépassée.", 415: "Le format de la requête n’est pas pris en charge.",
    429: "Trop de requêtes. Réessayez après le délai indiqué.", 503: "Le service est temporairement indisponible.",
  };
  return t(defaults[status] ?? "La requête a échoué (HTTP {0}).", status);
}

function localizedContent(element, message) {
  if (!message.includes("__")) {
    element.textContent = message;
    return true;
  }
  const slots = [];
  function register(parent) {
    for (const child of parent.children) {
      const nested = ["A","ABBR","B","EM","MARK","SMALL","SPAN","STRONG","SUP"].includes(child.tagName)
        && child.childNodes.length && !child.id && [...child.children].every((node) =>
          ["A","ABBR","B","BR","CODE","EM","I","INPUT","KBD","MARK","SMALL","SPAN","STRONG","SUP","WBR"].includes(node.tagName));
      slots.push({child, nested});
      if (nested) register(child);
    }
  }
  register(element);
  const fragment = document.createDocumentFragment();
  const stack = [{id: -1, target: fragment}];
  const used = new Set();
  for (const part of message.split(/(__\w+?\d+__)/g)) {
    const match = /^__(OPEN|CLOSE|KEEP)(\d+)__$/.exec(part);
    const current = stack.at(-1);
    if (!match) {
      if (part) current.target.append(document.createTextNode(part));
      continue;
    }
    const [, operation, idText] = match;
    const id = Number(idText);
    const slot = slots[id];
    if (!slot) return false;
    if (operation === "CLOSE") {
      if (current.id !== id) return false;
      stack.pop();
    } else {
      if (used.has(id) || slot.nested !== (operation === "OPEN")) return false;
      used.add(id);
      const target = slot.child.cloneNode(operation === "KEEP");
      current.target.append(target);
      if (operation === "OPEN") stack.push({id, target});
    }
  }
  if (stack.length !== 1 || used.size !== slots.length) return false;
  element.replaceChildren(fragment);
  return true;
}

if (mount) {
  if (supported(explicit)) {
    try { localStorage.setItem(preferenceKey, explicit); } catch {}
  }
  root.lang = language;
  root.dir = ["ar", "ur"].includes(language) ? "rtl" : "ltr";
  const selector = mount.querySelector("select");
  selector.replaceChildren();
  for (const [code, name] of Object.entries(languages)) {
    const option = new Option(name, code);
    option.lang = code;
    selector.add(option);
  }
  selector.value = language;
  selector.addEventListener("change", () => {
    try { localStorage.setItem(preferenceKey, selector.value); } catch {}
    url.searchParams.set("lang", selector.value);
    location.assign(url.href);
  });
}

if (language !== "fr") {
  try {
    const response = await fetch(`/assets/locales/${language}.json`, {signal: AbortSignal.timeout(5000)});
    if (!response.ok) throw new Error("Language catalogue unavailable");
    messages = await response.json();
  } catch {
    language = sourceLanguage;
    root.lang = sourceLanguage;
    root.dir = "ltr";
    if (mount) mount.querySelector("select").value = "fr";
  }
}

for (const element of document.querySelectorAll("[data-i18n]")) {
  const message = messages[element.dataset.i18n];
  if (typeof message === "string") localizedContent(element, message);
}
for (const attr of ["aria-label", "placeholder", "title", "alt", "content"]) {
  for (const element of document.querySelectorAll(`[data-i18n-${attr}]`)) {
    const message = messages[element.getAttribute(`data-i18n-${attr}`)];
    if (typeof message === "string") element.setAttribute(attr, message);
  }
}
if (mount) mount.querySelector("select").setAttribute("aria-label", t("Langue"));
for (const link of document.querySelectorAll('a[href="/docs/openapi.json"]')) {
  if (language !== "fr") link.href = `/docs/openapi.json?lang=${language}`;
}
