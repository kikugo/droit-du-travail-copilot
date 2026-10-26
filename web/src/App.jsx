import { useState } from "react";

const NOTICE = "Ceci n'est pas un avis juridique.";
const EXAMPLES = [
  "Quelle est la durée du préavis de licenciement après 3 ans d'ancienneté ?",
  "Combien de jours de congés payés acquiert-on par mois de travail ?",
  "Quel est le salaire minimum d'un ingénieur position 2.1 dans la convention Syntec ?",
];

const SOURCE_LABELS = { code: "Code du travail", "IDCC 1486": "Syntec", "IDCC 1979": "HCR" };

// The API answer ends with a Sources block and the notice; the page shows those from the
// structured citations instead.
function body(answer) {
  return answer.split("\n\nSources :")[0].replace(NOTICE, "").trim();
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

  function submit(e) {
    e.preventDefault();
    if (question.trim().length >= 3 && !loading) ask(question.trim());
  }

  return (
    <main>
      <h1>Droit du Travail Copilot</h1>
      <p className="lead">
        Questions sur le Code du travail et les conventions Syntec et HCR. Chaque réponse cite
        ses articles avec un lien Légifrance.
      </p>

      <form onSubmit={submit}>
        <textarea
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Posez votre question en français…"
          rows={3}
          maxLength={2000}
        />
        <button type="submit" disabled={loading || question.trim().length < 3}>
          {loading ? "Recherche…" : "Demander"}
        </button>
      </form>

      {!result && !loading && (
        <ul className="examples">
          {EXAMPLES.map((q) => (
            <li key={q}>
              <button type="button" onClick={() => ask(q)}>
                {q}
              </button>
            </li>
          ))}
        </ul>
      )}

      {error && <p className="error">{error}</p>}

      {result && (
        <section className="answer">
          <p className="text">{body(result.answer)}</p>
          {result.citations.length > 0 && (
            <>
              <h2>Sources</h2>
              <ul className="sources">
                {result.citations.map((c) => (
                  <li key={`${c.source}-${c.num}`}>
                    {c.url ? (
                      <a href={c.url} target="_blank" rel="noreferrer">
                        {c.source === "code" ? `Article ${c.num}` : c.num}
                      </a>
                    ) : (
                      c.num
                    )}{" "}
                    <span className="meta">
                      {SOURCE_LABELS[c.source] ?? c.source}
                      {c.date_debut_vigueur && `, en vigueur depuis le ${c.date_debut_vigueur}`}
                    </span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </section>
      )}

      <footer>
        <p>{NOTICE}</p>
        <p className="meta">Sources : LEGI et KALI (DILA, Licence Ouverte 2.0).</p>
      </footer>
    </main>
  );
}
