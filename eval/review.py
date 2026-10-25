"""Terminal review of drafted candidates into the golden set (same flow as the unified-rag eval).

    uv run python -m eval.review

One candidate at a time, with the source article, the evidence highlighted and its Légifrance
link so you can check it there. k keep · e edit then keep · d drop · s skip for now · q quit.

Every decision is appended to eval/review_log.jsonl, so quitting loses nothing and the next run
resumes. Kept rows are appended to eval/golden.jsonl and never rewritten. A type stops being
offered once it reaches its target in TARGET_MIX.

Only refusal candidates show what the system retrieves (top 5, hybrid), so you can check the
corpus really doesn't answer them. Answerable rows never show system output, so it can't anchor
your judgment.
"""

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from eval.draft_questions import CANDIDATES_PATH
from eval.golden import EVAL_DIR, GOLDEN_PATH, TARGET_MIX, load_golden, mix, validate_row

LOG_PATH = EVAL_DIR / "review_log.jsonl"
BOLD, YELLOW, RED, DIM, RESET = "\033[1m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def highlight(text: str, quote: str) -> str:
    if quote and quote in text:
        return text.replace(quote, f"{YELLOW}{BOLD}{quote}{RESET}", 1)
    return text


def pending(candidates, log, golden) -> list[dict[str, Any]]:
    decided = {e["cid"] for e in log if e["decision"] in ("keep", "edit", "drop")}
    counts = mix(golden)
    return [
        c
        for c in candidates
        if c["cid"] not in decided and counts[c["type"]] < TARGET_MIX[c["type"]]
    ]


def to_golden(candidate: dict[str, Any], next_id: int, edits: dict | None = None) -> dict:
    """The golden row for a kept candidate; `edits` can replace question, gold_answer or gold."""
    edits = edits or {}
    row = {
        "id": f"g{next_id:03d}",
        "question": edits.get("question", candidate["question"]),
        "gold": edits.get("gold", candidate["gold"]),
        "gold_answer": edits.get("gold_answer", candidate["gold_answer"]),
        "type": candidate["type"],
        "answerable": candidate["type"] != "refusal",
        "source_candidate": candidate["cid"],
    }
    if "calc" in candidate:
        row["calc"] = {k: candidate["calc"][k] for k in ("tool", "args", "expected")}
    validate_row(row)
    return row


def parse_gold(text: str, default: list[dict]) -> list[dict]:
    """'L1234-1, 4.2@IDCC 1486' -> [{num, source}]; empty keeps the default."""
    if not text.strip():
        return default
    out = []
    for item in text.split(","):
        num, _, source = item.strip().partition("@")
        out.append({"num": num.strip(), "source": source.strip() or "code"})
    return out


def show(c: dict[str, Any], retrieved: list | None, remaining: dict[str, int]) -> None:
    print("\n" + "=" * 100)
    left = ", ".join(f"{t} {n}" for t, n in remaining.items())
    print(f"{BOLD}{c['cid']}  [{c['type']}]{RESET}   {DIM}still needed: {left}{RESET}")
    print(f"\n{BOLD}Q:{RESET} {c['question']}")
    if c["gold"]:
        print(f"{BOLD}Gold:{RESET} " + ", ".join(f"{g['num']} ({g['source']})" for g in c["gold"]))
    if c.get("gold_answer"):
        print(f"{BOLD}Answer:{RESET} {c['gold_answer']}")
    if "calc" in c:
        print(
            f"{BOLD}Calc:{RESET} {c['calc']['tool']}({c['calc']['args']}) = {c['calc']['expected']}"
            f"   {DIM}(from copilot.calculators; check against service-public.fr){RESET}"
        )
    if c.get("url"):
        print(f"{BOLD}Légifrance:{RESET} {c['url']}")
        print(f"{DIM}{c.get('path', '')}{RESET}")
        if c.get("quote_found") is False:
            print(
                f"{RED}Evidence quote not found verbatim in the article:{RESET} {c['evidence_quote']}"
            )
        print("\n" + highlight(c.get("source_text", ""), c.get("evidence_quote", "")))
    if retrieved is not None:
        print(f"\n{BOLD}The system retrieves (check none of these answers it):{RESET}")
        for h in retrieved:
            print(f"  {h.num:<12} {h.source:<10} {h.path[-80:]}")


def review(
    candidates: list[dict],
    ask: Callable[[str], str] = input,
    retrieve: Callable[[str], list] | None = None,
    log_path: Path = LOG_PATH,
    golden_path: Path = GOLDEN_PATH,
) -> dict[str, int]:
    golden = load_golden(golden_path)
    log = read_jsonl(log_path)
    for c in pending(candidates, log, golden):
        golden = load_golden(golden_path)
        counts = mix(golden)
        if counts[c["type"]] >= TARGET_MIX[c["type"]]:
            continue
        remaining = {t: TARGET_MIX[t] - counts[t] for t in TARGET_MIX if TARGET_MIX[t] > counts[t]}
        retrieved = retrieve(c["question"]) if (retrieve and c["type"] == "refusal") else None
        show(c, retrieved, remaining)
        choice = ask("\n[k]eep [e]dit [d]rop [s]kip [q]uit > ").strip().lower()[:1]
        if choice == "q":
            break
        if choice == "s":
            append_jsonl(log_path, {"cid": c["cid"], "decision": "skip"})
            continue
        if choice == "d":
            reason = ask("reason (optional) > ").strip()
            append_jsonl(log_path, {"cid": c["cid"], "decision": "drop", "reason": reason})
            continue
        edits = {}
        if choice == "e":
            q = ask("question (enter keeps) > ").strip()
            a = ask("gold answer (enter keeps) > ").strip()
            g = ask("gold, e.g. 'L1234-1, 4.2@IDCC 1486' (enter keeps) > ")
            edits = {k: v for k, v in (("question", q), ("gold_answer", a)) if v}
            edits["gold"] = parse_gold(g, c["gold"])
        elif choice != "k":
            continue
        row = to_golden(c, len(golden) + 1, edits)
        append_jsonl(golden_path, row)
        append_jsonl(
            log_path, {"cid": c["cid"], "decision": "edit" if edits else "keep", "id": row["id"]}
        )
    return mix(load_golden(golden_path))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m eval.review")
    parser.add_argument("--no-retrieval", action="store_true", help="don't search for refusal rows")
    args = parser.parse_args(argv)
    retrieve = None
    if not args.no_retrieval:
        from retrieval.config import Clients, Settings
        from retrieval.search import Mode, search

        clients = Clients(Settings.from_env())
        retrieve = lambda q: search(clients, q, Mode.HYBRID, top=5)
    final = review(read_jsonl(CANDIDATES_PATH), retrieve=retrieve)
    print(f"\ngolden mix: {final} (targets {TARGET_MIX})")


if __name__ == "__main__":
    main()
