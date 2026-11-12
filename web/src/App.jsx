import { useState } from "react";

const NOTICE = "Ceci n'est pas un avis juridique.";
const EXAMPLES = [
  "Quelle est la durée du préavis de licenciement après 3 ans d'ancienneté ?",
  "Combien de jours de congés payés acquiert-on par mois de travail ?",
  "Quel est le salaire minimum d'un ingénieur position 2.1 dans la convention Syntec ?",
];

const SOURCE_LABELS = {
  code: "Code du travail",
  "IDCC 1486": "Convention Syntec (IDCC 1486)",
  "IDCC 1979": "Convention HCR (IDCC 1979)",
};

const DATE_FR = new Intl.DateTimeFormat("fr-FR", {
  day: "numeric",
  month: "long",
  year: "numeric",
  timeZone: "UTC",
});

// "2008-05-01" -> "1er mai 2008"; anything else is shown as it came.
function frenchDate(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  if (!m) return iso;
  const text = DATE_FR.format(new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])));
  return text.replace(/^1 /, "1er ");
}

// The API answer ends with a Sources block and the notice; the page shows those from the
// structured citations instead.
function body(answer) {
  return answer.split("\n\nSources :")[0].replace(NOTICE, "").trim();
}

// The model writes **bold**; show it as bold instead of raw asterisks (React escapes the rest).
// A bold part that names a cited Code article links to it on Légifrance.
function withBold(text, citations) {
  return text.split(/\*\*(.+?)\*\*/g).map((part, i) => {
    if (i % 2 === 0) return part;
    const cited = citations.find((c) => c.url && c.num.length > 3 && part.includes(c.num));
    return cited ? (
      <a key={i} href={cited.url} target="_blank" rel="noreferrer">
        <strong>{part}</strong>
      </a>
    ) : (
      <strong key={i}>{part}</strong>
    );
  });
}

function Answer({ result }) {
  const paragraphs = body(result.answer).split(/\n{2,}/);
  const { citations } = result;
  return (
    <article className="answer" aria-label="Réponse">
      <div className="answer-text">
        {paragraphs.map((p, i) => (
          <p key={i}>{withBold(p, citations)}</p>
        ))}
      </div>
      <aside className="notes" aria-labelledby="sources-title">
        <h2 id="sources-title">Sources</h2>
        {citations.length > 0 ? (
          <ul className="sources">
            {citations.map((c) => (
              <li key={`${c.source}-${c.num}`}>
                {c.url ? (
                  <a className="art" href={c.url} target="_blank" rel="noreferrer">
                    Article {c.num}
                  </a>
                ) : (
                  <span className="art">Article {c.num}</span>
                )}
                <span className="corpus">{SOURCE_LABELS[c.source] ?? c.source}</span>
                {c.date_debut_vigueur && (
                  <span className="vigueur">
                    en vigueur depuis le {frenchDate(c.date_debut_vigueur)}
                  </span>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <p className="none">Aucun article cité.</p>
        )}
      </aside>
    </article>
  );
}

export default function App() {
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function ask(q) {
    setQuestion(q);
    setLoading(true);
    setError("");
    setResult(null);
    try {
      const resp = await fetch("/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: q }),
      });
      const data = await resp.json();
      if (resp.status === 429) setError(`Limite atteinte. ${data.detail}`);
      else if (!resp.ok) setError("La question n'a pas pu être traitée. Réessayez.");
      else setResult(data);
    } catch {
      setError("Le service ne répond pas. Réessayez dans un instant.");
    } finally {
      setLoading(false);
    }
  }

  function send() {
    if (question.trim().length >= 3 && !loading) ask(question.trim());
  }

  function submit(e) {
    e.preventDefault();
    send();
  }

  // Cmd/Ctrl+Entrée envoie, Entrée seule garde le retour à la ligne.
  function onKeyDown(e) {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      send();
    }
  }

  return (
    <div className="page">
      <header className="masthead">
        <h1>Droit du Travail Copilot</h1>
        <p className="scope">
          Recherche dans le Code du travail et les conventions collectives Syntec et HCR.
          Chaque réponse cite ses articles, avec le lien Légifrance et la date d'entrée en
          vigueur.
        </p>
      </header>

      <main className="layout">
        <form className="ask" onSubmit={submit}>
          <label htmlFor="question">Votre question</label>
          <textarea
            id="question"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Par exemple : un salarié en CDD a-t-il droit à une prime de précarité ?"
            rows={3}
            maxLength={2000}
          />
          <div className="ask-row">
            <span className="hint">Ctrl ou ⌘ + Entrée pour envoyer</span>
            <button type="submit" disabled={loading || question.trim().length < 3}>
              {loading ? "Recherche…" : "Demander"}
            </button>
          </div>
        </form>

        <div className="status" aria-live="polite">
          {loading && (
            <p className="loading">Recherche dans le Code du travail et les conventions…</p>
          )}
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
        </div>

        {!result && !loading && (
          <nav className="examples" aria-labelledby="examples-title">
            <h2 id="examples-title">Exemples</h2>
            <ul>
              {EXAMPLES.map((q) => (
                <li key={q}>
                  <button type="button" onClick={() => ask(q)}>
                    {q}
                  </button>
                </li>
              ))}
            </ul>
          </nav>
        )}

        <aside className="coverage" aria-labelledby="coverage-title">
          <h2 id="coverage-title">Textes couverts</h2>
          <ul>
            <li>Code du travail, partie législative</li>
            <li>Convention Syntec (IDCC 1486)</li>
            <li>Convention HCR (IDCC 1979)</li>
          </ul>
          <p>Hors de ces textes, l'outil le dit au lieu de répondre.</p>
        </aside>

        {result && <Answer result={result} />}
      </main>

      <footer className="colophon">
        <p className="notice">{NOTICE}</p>
        <p className="licence">Sources : LEGI et KALI (DILA, Licence Ouverte 2.0).</p>
      </footer>
    </div>
  );
}
