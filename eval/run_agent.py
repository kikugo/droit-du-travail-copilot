"""Answer metrics for the agent over the golden set.

    uv run python -m eval.run_agent                  # all 100 rows
    uv run python -m eval.run_agent --subset smoke20  # the fixed CI subset

Answers are cached on disk keyed by question, instructions, chat model, search index and a hash
of the copilot code, so a rerun is free unless something that changes the answer changed.
Faithfulness uses the gpt-4.1-mini judge (cached too) against what the tools returned.
"""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import yaml

from copilot.core import AskResult
from copilot.prompts import INSTRUCTIONS, NOTICE
from eval.cache import DiskCache, key
from eval.golden import load_golden, smoke_subset
from eval.metrics import (
    bootstrap_ci,
    calculator_correct,
    citation_accuracy,
    citation_hit,
    cost_usd,
    keys,
    mean,
    percentile,
    refused,
)

PRICES = Path(__file__).parent / "prices.yaml"
COPILOT_DIR = Path(__file__).resolve().parents[1] / "copilot"


def code_version() -> str:
    """Hash of the agent's code, so an edited tool or prompt invalidates cached answers."""
    h = hashlib.sha256()
    for path in sorted(COPILOT_DIR.glob("*.py")):
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


def answer_body(answer: str) -> str:
    """The answer without the generated Sources block and the notice (what the judge scores)."""
    return answer.split("\n\nSources :")[0].replace(NOTICE, "").strip()


async def ask_cached(copilot, cache: DiskCache, question: str, config: dict) -> tuple[dict, bool]:
    k = key("agent", question, INSTRUCTIONS, config)
    cached = cache.get("agent", k)
    if cached is not None:
        return cached, True
    result: AskResult = await copilot.ask(question)
    data = result.to_dict()
    cache.put("agent", k, data)
    return data, False


def score_row(row: dict, res: dict, judge=None) -> dict[str, Any]:
    cited = keys(res["citations"])
    gold = keys(row["gold"])
    out = {
        "citation_accuracy": citation_accuracy(cited, gold, row["answerable"]),
        "citation_hit": citation_hit(cited, gold) if row["answerable"] else None,
        "refused": refused(res["answer"]),
        "unsupported": bool(res.get("unsupported_nums")),
    }
    if row["type"] == "calculator":
        out["calculator_correct"] = calculator_correct(
            row["calc"], res["tool_calls"], res["answer"]
        )
    if judge is not None and row["answerable"] and not out["refused"]:
        from eval.judge import faithfulness

        out["faithfulness"] = faithfulness(
            judge, row["question"], answer_body(res["answer"]), res.get("contexts", [])
        )
    return out


def totals(rows: list[dict], price: dict) -> dict[str, Any]:
    answerable = [r for r in rows if r["answerable"]]
    refusal = [r for r in rows if not r["answerable"]]
    calc = [r for r in rows if "calculator_correct" in r["scores"]]
    faith = [
        r["scores"]["faithfulness"]["score"]
        for r in rows
        if r["scores"].get("faithfulness", {}).get("score") is not None
    ]
    cit = [r["scores"]["citation_accuracy"] for r in rows]
    latencies = [r["latency_s"] for r in rows]
    tin = sum(r["usage"].get("input_tokens", 0) for r in rows)
    tout = sum(r["usage"].get("output_tokens", 0) for r in rows)
    return {
        "n": len(rows),
        "citation_accuracy": mean(cit),
        "citation_accuracy_ci": bootstrap_ci(cit),
        "citation_hit": mean([r["scores"]["citation_hit"] for r in answerable]),
        "refusal_accuracy": mean([float(r["scores"]["refused"]) for r in refusal]),
        "false_refusal_rate": mean([float(r["scores"]["refused"]) for r in answerable]),
        "calculator_correctness": mean([r["scores"]["calculator_correct"] for r in calc]),
        "faithfulness": mean(faith),
        "faithfulness_ci": bootstrap_ci(faith),
        "faithfulness_n": len(faith),
        "unsupported_citation_rate": mean([float(r["scores"]["unsupported"]) for r in rows]),
        "p50_s": percentile(latencies, 50),
        "p95_s": percentile(latencies, 95),
        "tokens_per_query": (tin + tout) / len(rows) if rows else 0,
        "usd_per_1000_queries": cost_usd(tin, tout, price) / len(rows) * 1000 if rows else 0,
    }


async def run(copilot, rows, cache, config, judge=None, log=print) -> list[dict]:
    out = []
    for i, row in enumerate(rows, start=1):
        res, hit = await ask_cached(copilot, cache, row["question"], config)
        out.append(
            {
                "id": row["id"],
                "type": row["type"],
                "answerable": row["answerable"],
                "question": row["question"],
                "cached": hit,
                **res,
                "scores": score_row(row, res, judge),
            }
        )
        if i % 10 == 0:
            log(f"{i}/{len(rows)}")
    return out


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m eval.run_agent")
    parser.add_argument("--subset", choices=["all", "smoke20"], default="all")
    parser.add_argument("--no-judge", action="store_true", help="skip faithfulness")
    args = parser.parse_args(argv)

    from copilot.core import Copilot
    from eval.judge import FoundryJudge
    from retrieval.config import Clients, Settings

    rows = load_golden()
    if args.subset == "smoke20":
        rows = smoke_subset(rows)
    cache = DiskCache()
    settings = Settings.from_env()
    model = os.environ["CHAT_DEPLOYMENT"]
    config = {
        "chat": model,
        "index": settings.search_index,
        "search": settings.search_endpoint,
        "code": code_version(),
    }
    judge = None
    if not args.no_judge:
        judge_model = os.environ.get("JUDGE_DEPLOYMENT", "gpt-4.1-mini")
        judge = FoundryJudge(Clients(settings).openai, judge_model, cache)

    async def go():
        copilot = Copilot.from_env()
        try:
            return await run(copilot, rows, cache, config, judge)
        finally:
            await copilot.aclose()

    results = asyncio.run(go())
    prices = yaml.safe_load(PRICES.read_text(encoding="utf-8"))
    summary = totals(results, prices["models"][model])
    if judge is not None:
        jp = prices["models"][judge.deployment]
        summary["judge"] = {
            "model": judge.deployment,
            **judge.usage,
            "usd_this_run": cost_usd(judge.usage["input_tokens"], judge.usage["output_tokens"], jp),
        }
    name = "agent_eval" if args.subset == "all" else f"agent_eval_{args.subset}"
    out = Path(f"results/{name}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "config": config,
                "prices_checked": prices["checked"],
                "summary": summary,
                "results": results,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    fresh = sum(1 for r in results if not r["cached"])
    print(json.dumps({k: v for k, v in summary.items() if k != "judge"}, ensure_ascii=False))
    print(f"wrote {out}; {fresh} fresh answers, {len(results) - fresh} from cache")


if __name__ == "__main__":
    main()
