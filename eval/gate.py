"""The CI eval gate: compare a smoke20 run with results/baseline.json, row by row.

    uv run python -m eval.gate                    # exit 1 on a regression
    uv run python -m eval.gate --write-baseline   # after a run on main

Counts, not rates: smoke20 has 3 refusal rows, so one flipped answer moves refusal accuracy by
33 points and the guide's "more than 5 points" rule would fail on noise. The gate fails when
2 or more answerable rows lose their citation, or any refusal row now answers, or any calculator
row goes wrong.
"""

import argparse
import json
import os
import sys
from pathlib import Path

CURRENT = Path("results/agent_eval_smoke20.json")
BASELINE = Path("results/baseline.json")
MAX_CITATION_REGRESSIONS = 1  # tolerated; the second one fails


def baseline_from(run: dict) -> dict:
    return {
        "config": run["config"],
        "rows": {
            r["id"]: {
                "type": r["type"],
                "answerable": r["answerable"],
                "citation_accuracy": r["scores"]["citation_accuracy"],
                "refused": r["scores"]["refused"],
                **(
                    {"calculator_correct": r["scores"]["calculator_correct"]}
                    if "calculator_correct" in r["scores"]
                    else {}
                ),
            }
            for r in run["results"]
        },
    }


def compare(baseline: dict, run: dict) -> tuple[bool, list[str]]:
    """(passed, report lines)."""
    current = baseline_from(run)["rows"]
    base = baseline["rows"]
    if set(current) != set(base):
        hint = "The smoke20 rows differ from the baseline (golden set changed?); rerun"
        return False, [f"{hint} `make baseline` on main."]
    citation, refusal, calc = [], [], []
    for rid, b in base.items():
        c = current[rid]
        if b["answerable"] and b["citation_accuracy"] == 1 and c["citation_accuracy"] == 0:
            citation.append(rid)
        if not b["answerable"] and b["refused"] and not c["refused"]:
            refusal.append(rid)
        if b.get("calculator_correct") == 1 and c.get("calculator_correct") == 0:
            calc.append(rid)
    failed = len(citation) > MAX_CITATION_REGRESSIONS or refusal or calc
    lines = [
        f"citation regressions: {len(citation)} (fail at {MAX_CITATION_REGRESSIONS + 1}) {citation}",
        f"refusal regressions: {len(refusal)} (fail at 1) {refusal}",
        f"calculator regressions: {len(calc)} (fail at 1) {calc}",
    ]
    answers = {r["id"]: r for r in run["results"]}
    for rid in citation + refusal + calc:
        r = answers[rid]
        lines.append(f"\n{rid} ({r['type']}): {r['question']}\n{r['answer'][:600]}")
    return not failed, lines


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m eval.gate")
    parser.add_argument("--write-baseline", action="store_true")
    args = parser.parse_args(argv)
    run = json.loads(CURRENT.read_text(encoding="utf-8"))
    if args.write_baseline:
        BASELINE.write_text(
            json.dumps(baseline_from(run), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"wrote {BASELINE}")
        return
    passed, lines = compare(json.loads(BASELINE.read_text(encoding="utf-8")), run)
    report = ("PASS" if passed else "FAIL") + "\n" + "\n".join(lines)
    print(report)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"## Eval gate\n\n```\n{report}\n```\n")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
