import assert from "node:assert/strict";
import test from "node:test";

import { citedIn, errorMessage } from "./answer.js";

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
