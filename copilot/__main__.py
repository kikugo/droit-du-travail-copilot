import argparse
import asyncio
import json
from pathlib import Path

import yaml

from copilot.core import AskResult, Copilot
from copilot.prompts import REFUSAL

SMOKE = Path(__file__).parent / "smoke_questions.yaml"


def judge_in_scope(case: dict, result: AskResult) -> dict:
    cited = {(c["num"], c["source"]) for c in result.citations}
    hit = [e for e in case["expect"] if (str(e["num"]), e["source"]) in cited]
    tools = {c["name"] for c in result.tool_calls}
    tool_ok = "expect_tool" not in case or case["expect_tool"] in tools
    return {"passed": bool(hit) and tool_ok, "expected_cited": bool(hit), "tool_ok": tool_ok}


def judge_out_of_scope(result: AskResult) -> dict:
    refused = REFUSAL.rstrip(".") in result.answer
    return {"passed": refused, "refused": refused}


def print_result(r: AskResult) -> None:
    print(r.answer, "\n")
    for c in r.citations:
        print(f"  cite {c['num']:<10} {c['source']:<10} {c['url']}")
    for t in r.tool_calls:
        print(f"  tool {t['name']}({json.dumps(t['args'], ensure_ascii=False)}) -> {t['nums']}")
    if r.unsupported_nums:
        print(f"  cited without retrieval: {r.unsupported_nums}")
    print(f"  trace {r.trace_id}  tokens {r.usage}  {r.latency_s}s")


async def smoke(copilot: Copilot, out: Path) -> None:
    cases = yaml.safe_load(SMOKE.read_text(encoding="utf-8"))
    rows = []
    for kind, judge in (("in_scope", judge_in_scope), ("out_of_scope", None)):
        for case in cases[kind]:
            result = await copilot.ask(case["question"])
            verdict = judge(case, result) if judge else judge_out_of_scope(result)
            rows.append({"id": case["id"], "kind": kind, **verdict, **result.to_dict()})
            print(f"{'PASS' if verdict['passed'] else 'FAIL'}  {kind:<12} {case['id']}")
    summary = {
        kind: {
            "passed": sum(r["passed"] for r in rows if r["kind"] == kind),
            "total": sum(1 for r in rows if r["kind"] == kind),
        }
        for kind in ("in_scope", "out_of_scope")
    }
    summary["total_tokens"] = sum(r["usage"].get("total_tokens", 0) for r in rows)
    summary["answers_with_unsupported_nums"] = sum(1 for r in rows if r["unsupported_nums"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"summary": summary, "results": rows}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False), f"\nwrote {out}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m copilot")
    parser.add_argument("question", nargs="?", help="a question, or 'smoke' to run the smoke set")
    parser.add_argument("--out", type=Path, default=Path("results/agent_smoke.json"))
    args = parser.parse_args()
    if not args.question:
        parser.error("give a question or 'smoke'")
    asyncio.run(run(args))


async def run(args) -> None:
    copilot = Copilot.from_env()
    try:
        if args.question == "smoke":
            await smoke(copilot, args.out)
        else:
            print_result(await copilot.ask(args.question))
    finally:
        await copilot.aclose()


if __name__ == "__main__":
    main()
