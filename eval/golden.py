"""The golden set's schema, targets and loader (pattern from the unified-rag eval).

`eval/golden.jsonl` holds hand-reviewed questions. Rows are only appended by `eval.review`;
nothing rewrites a row after review. Every metric reads the set through `load_golden`, so a
malformed row fails loudly here instead of quietly skewing a score.

Gold is a list of articles, {num, source}: an article split into pieces counts as one.
"""

import json
from pathlib import Path
from typing import Any

EVAL_DIR = Path(__file__).resolve().parent
GOLDEN_PATH = EVAL_DIR / "golden.jsonl"

TARGET_MIX = {
    "lookup": 35,
    "num_in_question": 20,
    "calculator": 20,
    "convention": 10,
    "refusal": 15,
}
TYPES = tuple(TARGET_MIX)
FIELDS = ("id", "question", "gold", "gold_answer", "type", "answerable")
SOURCES = ("code", "IDCC 1486", "IDCC 1979")
CALC_TOOLS = ("preavis", "indemnite_licenciement")


class GoldenSetError(ValueError):
    pass


def validate_row(row: dict[str, Any]) -> None:
    missing = [f for f in FIELDS if f not in row]
    if missing:
        raise GoldenSetError(f"{row.get('id', '?')}: missing {missing}")
    rid = row["id"]
    if not isinstance(row["question"], str) or not row["question"].strip():
        raise GoldenSetError(f"{rid}: empty question")
    if row["type"] not in TYPES:
        raise GoldenSetError(f"{rid}: unknown type {row['type']!r}")
    gold = row["gold"]
    if not isinstance(gold, list) or not all(
        isinstance(g, dict) and g.get("num") and g.get("source") in SOURCES for g in gold
    ):
        raise GoldenSetError(f"{rid}: gold must be a list of {{num, source}}")
    keys = [(g["num"], g["source"]) for g in gold]
    if len(keys) != len(set(keys)):
        raise GoldenSetError(f"{rid}: duplicate gold articles")
    if row["answerable"] is not bool(gold):
        raise GoldenSetError(f"{rid}: answerable must be true exactly when gold is non-empty")
    if (row["type"] == "refusal") == row["answerable"]:
        raise GoldenSetError(f"{rid}: only refusal rows may have answerable=false")
    if row["answerable"] and not str(row["gold_answer"]).strip():
        raise GoldenSetError(f"{rid}: answerable row needs a gold_answer")
    if row["type"] == "convention" and not any(g["source"] != "code" for g in gold):
        raise GoldenSetError(f"{rid}: convention row needs a Syntec or HCR gold article")
    if row["type"] == "num_in_question" and not any(g["num"] in row["question"] for g in gold):
        raise GoldenSetError(f"{rid}: num_in_question must name a gold article in the question")
    if row["type"] == "calculator":
        calc = row.get("calc") or {}
        if calc.get("tool") not in CALC_TOOLS or "args" not in calc or "expected" not in calc:
            raise GoldenSetError(f"{rid}: calculator row needs calc {{tool, args, expected}}")


def load_golden(path: str | Path = GOLDEN_PATH) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    rows, seen = [], set()
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GoldenSetError(f"line {lineno}: not JSON ({exc})") from exc
        validate_row(row)
        if row["id"] in seen:
            raise GoldenSetError(f"duplicate id {row['id']}")
        seen.add(row["id"])
        rows.append(row)
    return rows


def mix(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = dict.fromkeys(TYPES, 0)
    for row in rows:
        counts[row["type"]] += 1
    return counts


def smoke_subset(rows: list[dict[str, Any]], n: int = 20) -> list[dict[str, Any]]:
    """A fixed, stratified subset for the CI gate: the first rows of each type, in proportion."""
    total = sum(TARGET_MIX.values())
    picked = []
    for t, target in TARGET_MIX.items():
        k = max(1, round(n * target / total))
        picked += [r for r in rows if r["type"] == t][:k]
    return picked[:n]
