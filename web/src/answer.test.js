import assert from "node:assert/strict";
import test from "node:test";

import { answerText, blocks, citedIn, errorMessage, methodSteps } from "./answer.js";

const cites = [
  { num: "L1234-1", url: "u1" },
  { num: "L1234-10", url: "u10" },
  { num: "4.2", url: "syntec" },
];

test("bold article links match the exact number", () => {
  assert.equal(citedIn("article L1234-10", cites).url, "u10");
  assert.equal(citedIn("article L1234-1", cites).url, "u1");
  assert.equal(citedIn("L1234-1.", cites).url, "u1");
  assert.equal(citedIn("article L1234-1-1", [cites[0]]), undefined);
  assert.equal(citedIn("article L1234-11", [cites[0]]), undefined);
  assert.equal(citedIn("XL1234-1", [cites[0]]), undefined);
  assert.equal(citedIn("article 4.2", cites), undefined); // too short to trust
});

const resp = (status, body) => ({
  status,
  json: async () => {
    if (typeof body !== "object") throw new SyntaxError("not JSON");
    return body;
  },
});

test("error messages do not depend on a JSON body", async () => {
  assert.equal(await errorMessage(resp(429, { detail: "Demain." })), "Limite atteinte. Demain.");
  assert.match(await errorMessage(resp(429, "<html>")), /Limite atteinte/);
  assert.match(await errorMessage(resp(502, "<html>Bad Gateway")), /indisponible/);
  assert.match(await errorMessage(resp(500, "Internal Server Error")), /indisponible/);
  assert.match(await errorMessage(resp(413, {})), /trop longue/);
});

test("dash and numbered lines become lists, the rest stays text", () => {
  assert.deepEqual(blocks("Deux cas :\n- un CDD\n- un CDI\nSinon rien."), [
    { type: "p", text: "Deux cas :" },
    { type: "ul", items: ["un CDD", "un CDI"] },
    { type: "p", text: "Sinon rien." },
  ]);
  assert.deepEqual(blocks("1. d'abord\n2. ensuite"), [
    { type: "ol", items: ["d'abord", "ensuite"], start: 1 },
  ]);
  assert.deepEqual(blocks("Ligne un\nligne deux"), [{ type: "p", text: "Ligne un\nligne deux" }]);
  // A hyphenated word or a negative figure at the start of a line is not a bullet.
  assert.deepEqual(blocks("-5 % de prime"), [{ type: "p", text: "-5 % de prime" }]);
});

test("tool calls read as method steps", () => {
  const steps = methodSteps([
    { name: "search_code", args: { query: "préavis", source: "IDCC 1486" }, nums: ["4.2"] },
    { name: "get_article", args: { num: "L1234-1" }, nums: ["L1234-1"] },
    { name: "indemnite_licenciement", args: { anciennete_mois: 36, salaire_ref: 3000 }, nums: [] },
    { name: "unknown_tool", args: {}, nums: [] },
  ]);
  assert.deepEqual(steps[0], {
    label: "Recherche dans les textes",
    detail: "« préavis » (Syntec)",
    found: "→ 4.2",
  });
  assert.equal(steps[1].detail, "article L1234-1");
  assert.equal(steps[2].detail, "36 mois d'ancienneté, salaire de référence 3000 €");
  assert.equal(steps[3].label, "unknown_tool");
  assert.deepEqual(methodSteps(undefined), []);
});

test("copied answer carries the question and the source links", () => {
  const text = answerText("Q ?", "Réponse.", [{ num: "L1", url: "https://x" }, { num: "4.2", url: "" }]);
  assert.match(text, /^Question : Q \?\n\nRéponse\.\n\nSources :\n- Article L1 : https:\/\/x\n- Article 4\.2\n/);
});
