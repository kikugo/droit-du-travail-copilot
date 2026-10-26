import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from copilot.prompts import INSTRUCTIONS, NOTICE
from copilot.tools import Corpus, ToolCall, ToolContext, make_tools

ARTICLES = Path("data/articles.jsonl")
CODE_NUM = re.compile(r"\b[LRDA]\.?\s?\d{3,4}(?:-\d+)*\b")

# Real trace ids now; Phase 6 adds the Azure Monitor exporter to this provider.
if not isinstance(trace.get_tracer_provider(), TracerProvider):
    trace.set_tracer_provider(TracerProvider())
tracer = trace.get_tracer("copilot")


@dataclass
class AskResult:
    question: str
    answer: str
    citations: list[dict]
    tool_calls: list[dict]
    trace_id: str
    unsupported_nums: list[str] = field(default_factory=list)  # cited but never retrieved
    contexts: list[str] = field(default_factory=list)  # tool outputs, for faithfulness
    usage: dict = field(default_factory=dict)
    latency_s: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


SOURCE_NAMES = {
    "code": "Code du travail",
    "IDCC 1486": "convention Syntec (IDCC 1486)",
    "IDCC 1979": "convention HCR (IDCC 1979)",
}
SOURCE_KEYWORDS = {"IDCC 1486": ("syntec", "1486"), "IDCC 1979": ("hcr", "1979")}
LINK = re.compile(r"\(?<?https?://(?:www\.)?legifrance\.gouv\.fr/\S*?>?\)?(?=[\s,;.]|$)")


def ensure_notice(answer: str) -> str:
    answer = (answer or "").strip()
    return answer if NOTICE in answer else f"{answer}\n\n{NOTICE}"


def collapse_repeats(text: str) -> str:
    """The model occasionally emits its whole answer twice; keep one copy."""
    t = text.strip()
    half = len(t) // 2
    for cut in range(max(half - 3, 1), min(half + 4, len(t))):
        first, second = t[:cut].strip(), t[cut:].strip()
        if first and first == second:
            return first
    return t


def strip_links(text: str) -> str:
    """Links come from tool results in the Sources block; drop any the model typed itself."""
    text = LINK.sub("", text)
    text = re.sub(r"\s*[—-]\s*lien Légifrance\s*:?", "", text)
    return re.sub(r"[ \t]+\n", "\n", text)


def _normalise_num(num: str) -> str:
    return re.sub(r"[.\s]", "", num).upper()


def extract_citations(answer: str, calls: list[ToolCall]) -> list[dict]:
    """Articles returned by a tool that the answer cites: code articles by number, convention
    articles as "article 4.2" plus the convention's name, or either by an exact link."""
    cited_nums = {_normalise_num(m) for m in CODE_NUM.findall(answer)}
    lower = answer.lower()
    seen, out = set(), []
    for call in calls:
        for art in call.articles:
            key = (art["num"], art["source"])
            if key in seen:
                continue
            if art["source"] == "code":
                cited = _normalise_num(art["num"]) in cited_nums
            else:
                pattern = rf"\b(?:article|art\.)\s*{re.escape(art['num'])}(?![\d.])"
                cited = bool(re.search(pattern, answer, re.IGNORECASE)) and any(
                    k in lower for k in SOURCE_KEYWORDS.get(art["source"], ())
                )
            if cited or (art["url"] and art["url"] in answer):
                seen.add(key)
                out.append(art)
    return out


def sources_block(citations: list[dict]) -> str:
    lines = []
    for c in citations:
        parts = [f"article {c['num']}", SOURCE_NAMES.get(c["source"], c["source"])]
        if c.get("date_debut_vigueur"):
            parts.append(f"en vigueur depuis le {c['date_debut_vigueur']}")
        if c["url"]:
            parts.append(c["url"])
        lines.append("- " + " — ".join(parts))
    return "Sources :\n" + "\n".join(lines) if lines else ""


SOURCES_HEADER = re.compile(r"^\W*(sources?|articles? (cités?|utilisés?))\W*:?\W*$", re.IGNORECASE)


def strip_model_sources(body: str) -> str:
    """Drop a trailing sources list the model wrote itself (header + bullet lines only)."""
    lines = body.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if SOURCES_HEADER.match(lines[i].strip()):
            tail = [line.strip() for line in lines[i + 1 :] if line.strip()]
            if all(line.startswith(("-", "*", "•")) for line in tail):
                return "\n".join(lines[:i]).strip()
            break
    return body


def unsupported_nums(body: str, calls: list[ToolCall]) -> list[str]:
    """Code article numbers in the answer that no tool returned: claims from model memory."""
    returned = {
        _normalise_num(a["num"]) for c in calls for a in c.articles if a["source"] == "code"
    }
    seen = []
    for m in CODE_NUM.findall(body):
        n = _normalise_num(m)
        if n not in returned and n not in seen:
            seen.append(n)
    return seen


def finalize(text: str, calls: list[ToolCall]) -> tuple[str, list[dict], list[str]]:
    body = strip_links(collapse_repeats(text or ""))
    body = "\n".join(line for line in body.splitlines() if NOTICE not in line).strip()
    body = strip_model_sources(body)
    citations = extract_citations(body, calls)
    blocks = [body, sources_block(citations), NOTICE]
    return "\n\n".join(b for b in blocks if b), citations, unsupported_nums(body, calls)


def usage_of(response) -> dict:
    usage = getattr(response, "usage_details", None) or {}
    get = usage.get if isinstance(usage, dict) else lambda k, d=None: getattr(usage, k, d)
    return {
        "input_tokens": get("input_token_count", 0) or 0,
        "output_tokens": get("output_token_count", 0) or 0,
        "total_tokens": get("total_token_count", 0) or 0,
    }


class Copilot:
    def __init__(self, chat_client, ctx: ToolContext, agent_factory=None, closers=()):
        self.chat_client = chat_client
        self.ctx = ctx
        self.agent_factory = agent_factory or self._default_agent
        self._closers = list(closers)

    async def aclose(self) -> None:
        """Close the async credential and HTTP sessions opened by from_env()."""
        for closer in self._closers:
            result = closer()
            if hasattr(result, "__await__"):
                await result

    def _default_agent(self, tools):
        from agent_framework import Agent

        return Agent(
            client=self.chat_client, name="copilot", instructions=INSTRUCTIONS, tools=tools
        )

    @classmethod
    def from_env(cls) -> "Copilot":
        from agent_framework.foundry import FoundryChatClient
        from azure.identity.aio import DefaultAzureCredential
        from dotenv import load_dotenv

        from copilot.piste import PisteClient
        from retrieval.config import Clients, Settings
        from retrieval.search import Mode, search

        load_dotenv()
        credential = DefaultAzureCredential()
        chat = FoundryChatClient(
            project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
            model=os.environ["CHAT_DEPLOYMENT"],
            credential=credential,
        )
        chat.function_invocation_configuration.update(
            {"max_iterations": 6, "max_function_calls": 10}
        )
        search_clients = Clients(Settings.from_env())
        with ARTICLES.open(encoding="utf-8") as f:
            from ingest.models import Article

            corpus = Corpus([Article.from_json(line) for line in f])

        def do_search(query, source):
            # hybrid: the free search tier has no semantic ranker
            return search(search_clients, query, Mode.HYBRID, top=5, source=source)

        closers = [getattr(chat, "close", None), credential.close]
        return cls(
            chat,
            ToolContext(search=do_search, corpus=corpus, piste=PisteClient.from_env()),
            closers=[c for c in closers if c is not None],
        )

    async def ask(self, question: str) -> AskResult:
        calls: list[ToolCall] = []
        agent = self.agent_factory(make_tools(self.ctx, calls))
        start = time.perf_counter()
        with tracer.start_as_current_span("ask") as span:
            span.set_attribute("copilot.question_chars", len(question))
            trace_id = format(span.get_span_context().trace_id, "032x")
            response = await agent.run(question)
        answer, citations, unsupported = finalize(response.text, calls)
        return AskResult(
            question=question,
            answer=answer,
            citations=citations,
            tool_calls=[{"name": c.name, "args": c.args, "nums": c.nums} for c in calls],
            trace_id=trace_id,
            unsupported_nums=unsupported,
            contexts=[t for c in calls for t in c.texts],
            usage=usage_of(response),
            latency_s=round(time.perf_counter() - start, 2),
        )
