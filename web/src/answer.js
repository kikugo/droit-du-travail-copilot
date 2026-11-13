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
