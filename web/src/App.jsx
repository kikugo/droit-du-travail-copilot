import { useEffect, useRef, useState } from "react";

import { answerText, blocks, citedIn, errorMessage, methodSteps } from "./answer.js";

const NOTICE = "Ceci n'est pas un avis juridique.";
const EXAMPLES = [
  "Quelle est la durée du préavis de licenciement après 3 ans d'ancienneté ?",
  "Combien de jours de congés payés acquiert-on par mois de travail ?",
  "Quel est le salaire minimum d'un ingénieur position 2.1 dans la convention Syntec ?",
];

// After this long, the first question is probably waking the app from scale-to-zero.
const COLD_START_MS = 5000;
const HISTORY_SIZE = 5;
const HISTORY_KEY = "ddt-history";

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
    const cited = citedIn(part, citations);
    return cited ? (
      <a key={i} href={cited.url} target="_blank" rel="noreferrer">
        <strong>{part}</strong>
      </a>
    ) : (
      <strong key={i}>{part}</strong>
    );
  });
}

function Paragraph({ text, citations }) {
  return blocks(text).map((blk, i) => {
    if (blk.type === "p") return <p key={i}>{withBold(blk.text, citations)}</p>;
    const items = blk.items.map((item, j) => <li key={j}>{withBold(item, citations)}</li>);
    return blk.type === "ul" ? (
      <ul key={i}>{items}</ul>
    ) : (
      <ol key={i} start={blk.start}>
        {items}
      </ol>
    );
  });
}

function CopyButton({ text }) {
  const [state, setState] = useState("");
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setState("Réponse copiée");
    } catch {
      setState("Copie impossible");
    }
    setTimeout(() => setState(""), 2000);
  }
  return (
    <button type="button" className="tool" onClick={copy}>
      <span aria-live="polite">{state || "Copier la réponse"}</span>
    </button>
  );
}

function Method({ toolCalls }) {
  const steps = methodSteps(toolCalls);
  if (!steps.length) return null;
  return (
    <details className="method">
      <summary>Méthode</summary>
      <ol>
        {steps.map((s, i) => (
          <li key={i}>
            <span className="step">{s.label}</span>
            {s.detail && <span className="step-detail"> {s.detail}</span>}
            {s.found && <span className="step-found"> {s.found}</span>}
          </li>
        ))}
      </ol>
    </details>
  );
}

function Answer({ question, result, label = "Réponse" }) {
  const text = body(result.answer);
  const { citations } = result;
  return (
    <article className="answer" aria-label={label}>
      <div className="answer-main">
        <div className="answer-text">
          {text.split(/\n{2,}/).map((p, i) => (
            <Paragraph key={i} text={p} citations={citations} />
          ))}
        </div>
        <div className="answer-tools">
          <CopyButton text={answerText(question, text, citations)} />
          <Method toolCalls={result.tool_calls} />
        </div>
      </div>
      <aside className="notes" aria-label="Sources">
        <h2>Sources</h2>
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

// What a screen reader hears when the answer lands: its first paragraph, without markup.
function announcement(result) {
  const first = body(result.answer).split(/\n{2,}/)[0].replace(/\*\*/g, "");
  const n = result.citations.length;
  return `Réponse reçue. ${first} ${n ? `${n} source${n > 1 ? "s" : ""} citée${n > 1 ? "s" : ""}.` : ""}`;
}

// Answered questions of this tab, newest first: the one on screen plus HISTORY_SIZE before it.
// sessionStorage may be unavailable.
function loadHistory() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(HISTORY_KEY) || "[]");
    return Array.isArray(saved) ? saved.slice(0, HISTORY_SIZE + 1) : [];
  } catch {
    return [];
  }
}

function saveHistory(history) {
  try {
    sessionStorage.setItem(HISTORY_KEY, JSON.stringify(history));
  } catch {
    // Private mode or storage off: the history just lasts until the page closes.
  }
}

export default function App() {
  const [question, setQuestion] = useState("");
  // The question being asked or just answered, kept apart from the editable field.
  const [asked, setAsked] = useState("");
  const [result, setResult] = useState(null);
  const [history, setHistory] = useState(loadHistory);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(false);
  const [slow, setSlow] = useState(false);
  const [announce, setAnnounce] = useState("");
  const controller = useRef(null);

  useEffect(() => {
    if (!loading) return undefined;
    const timer = setTimeout(() => setSlow(true), COLD_START_MS);
    return () => clearTimeout(timer);
  }, [loading]);

  useEffect(() => saveHistory(history), [history]);

  async function ask(q) {
    controller.current?.abort();
    const ctrl = new AbortController();
    controller.current = ctrl;
    setQuestion(q);
    setAsked(q);
    setLoading(true);
    setSlow(false);
    setError("");
    setNotice("");
    setAnnounce("");
    setResult(null);
    try {
      const resp = await fetch("/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: q }),
        signal: ctrl.signal,
      });
      if (!resp.ok) setError(await errorMessage(resp));
      else {
        const data = await resp.json();
        setResult(data);
        setAnnounce(announcement(data));
        setHistory((h) => [{ question: q, result: data }, ...h].slice(0, HISTORY_SIZE + 1));
      }
    } catch (err) {
      if (err.name === "AbortError") {
        if (controller.current === ctrl) setNotice("Question annulée.");
      } else setError("Le service ne répond pas. Réessayez dans un instant.");
    } finally {
      if (controller.current === ctrl) {
        setLoading(false);
        setSlow(false);
        controller.current = null;
      }
    }
  }

  function cancel() {
    controller.current?.abort();
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

  const idle = !asked && !loading;
  // The newest saved answer is the one on screen; the history lists the ones before it.
  const previous = result ? history.slice(1) : history;

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

        {asked && (
          <div className="asked">
            <h2>Question posée</h2>
            <p>{asked}</p>
          </div>
        )}

        <div className="status" aria-live="polite">
          {loading && (
            <p className="loading">Recherche dans le Code du travail et les conventions…</p>
          )}
          {slow && (
            <div className="coldstart">
              <p>
                Démarrage du service… Après une période sans visite, la première question peut
                prendre jusqu'à une minute.
              </p>
              <button type="button" onClick={cancel}>
                Annuler
              </button>
            </div>
          )}
          {notice && <p className="loading">{notice}</p>}
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
        </div>

        {/* Always in the page so the announcement is read when its text changes. */}
        <p className="sr-only" aria-live="polite" aria-atomic="true">
          {announce}
        </p>

        {idle && (
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

        {result && <Answer question={asked} result={result} />}

        {previous.length > 0 && (
          <section className="history" aria-labelledby="history-title">
            <h2 id="history-title">Questions précédentes</h2>
            <ul>
              {previous.map((h, i) => (
                <li key={`${i}-${h.question}`}>
                  <details className="history-item">
                    <summary>{h.question}</summary>
                    <Answer question={h.question} result={h.result} label={`Réponse à : ${h.question}`} />
                  </details>
                </li>
              ))}
            </ul>
          </section>
        )}
      </main>

      <footer className="colophon">
        <p className="notice">{NOTICE}</p>
        <p className="licence">Sources : LEGI et KALI (DILA, Licence Ouverte 2.0).</p>
      </footer>
    </div>
  );
}
