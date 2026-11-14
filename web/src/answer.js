// Pure helpers for App.jsx, kept free of JSX so `node --test` can run them.

const escapeRegExp = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

// The cited article a bold phrase names, by exact number: "L1234-10" never matches L1234-1.
export function citedIn(part, citations) {
  return citations.find((c) => {
    if (!c.url || c.num.length <= 3) return false;
    return new RegExp(`(^|[^\\w.-])${escapeRegExp(c.num)}(?![\\w-]|\\.\\d)`).test(part);
  });
}

// What to show for a non-2xx answer. Only 429 and 413 carry a message meant for people; a 500
// or a 502 from the ingress may not even be JSON.
export async function errorMessage(resp) {
  if (resp.status === 429) {
    const detail = await resp
      .json()
      .then((d) => d.detail)
      .catch(() => "");
    return `Limite atteinte. ${detail || "Réessayez plus tard."}`.trim();
  }
  if (resp.status === 413 || resp.status === 422) {
    return "La question est trop longue ou invalide.";
  }
  if (resp.status >= 500) {
    return "Le service est momentanément indisponible. Réessayez dans un instant.";
  }
  return "La question n'a pas pu être traitée. Réessayez.";
}

const BULLET = /^\s*[-*•]\s+(.*)$/;
const NUMBERED = /^\s*(\d+)[.)]\s+(.*)$/;

// A paragraph of the model's text as blocks: runs of "- " lines become a bullet list, runs of
// "1. " lines a numbered list, everything else stays text. Only structure is parsed here; the
// text itself is still rendered (and escaped) by React.
export function blocks(paragraph) {
  const out = [];
  for (const line of paragraph.split("\n")) {
    const b = BULLET.exec(line);
    const n = b ? null : NUMBERED.exec(line);
    const kind = b ? "ul" : n ? "ol" : "p";
    const text = b ? b[1] : n ? n[2] : line;
    const last = out[out.length - 1];
    if (kind !== "p" && last?.type === kind) last.items.push(text);
    else if (kind === "p" && last?.type === "p") last.text += `\n${text}`;
    else if (kind === "p") out.push({ type: "p", text });
    else out.push({ type: kind, items: [text], ...(n ? { start: Number(n[1]) } : {}) });
  }
  return out.filter((blk) => blk.type !== "p" || blk.text.trim());
}

const TOOL_LABELS = {
  search_code: "Recherche dans les textes",
  get_article: "Lecture d'un article",
  preavis: "Calcul du préavis",
  indemnite_licenciement: "Calcul de l'indemnité de licenciement",
};

const SOURCE_SHORT = { code: "Code du travail", "IDCC 1486": "Syntec", "IDCC 1979": "HCR" };

// The agent's tool calls as lines a reader can follow: what ran, on what, and what it found.
export function methodSteps(toolCalls = []) {
  return toolCalls.map(({ name, args = {}, nums = [] }) => {
    let detail = "";
    if (name === "search_code") {
      detail = args.query ? `« ${args.query} »` : "";
      if (args.source) detail += ` (${SOURCE_SHORT[args.source] ?? args.source})`;
    } else if (name === "get_article") {
      detail = args.num ? `article ${args.num}` : "";
    } else if (name === "preavis" || name === "indemnite_licenciement") {
      const parts = [];
      if (args.anciennete_mois != null) parts.push(`${args.anciennete_mois} mois d'ancienneté`);
      if (args.salaire_ref != null) parts.push(`salaire de référence ${args.salaire_ref} €`);
      detail = parts.join(", ");
    }
    return {
      label: TOOL_LABELS[name] ?? name,
      detail: detail.trim(),
      found: nums.length ? `→ ${nums.join(", ")}` : "",
    };
  });
}

// Plain text for the clipboard: the answer, then its sources with their links.
export function answerText(question, text, citations) {
  const lines = [`Question : ${question}`, "", text];
  if (citations.length) {
    lines.push("", "Sources :");
    for (const c of citations) lines.push(`- Article ${c.num}${c.url ? ` : ${c.url}` : ""}`);
  }
  lines.push("", "Ceci n'est pas un avis juridique.");
  return lines.join("\n");
}
