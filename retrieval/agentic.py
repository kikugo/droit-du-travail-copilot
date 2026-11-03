"""Foundry IQ (agentic retrieval) over the existing index, through the Search REST API.

Docs: learn.microsoft.com/azure/search/agentic-retrieval-how-to-create-knowledge-base,
agentic-knowledge-source-how-to-search-index and agentic-retrieval-how-to-retrieve.
Raw REST because the stable azure-search-documents release lacks the 2026-08-01-preview
fields used here (LLM query planning, retrievalReasoningEffort). Entra ID tokens, no keys.

Two knowledge bases over one search-index knowledge source:
  ddt-kb-minimal  no LLM: semantic multi-query retrieval from the question as written
  ddt-kb-low      gpt-5.4-mini plans the subqueries (retrievalReasoningEffort: low)
Both return extractive data only; the agent writes the answer.
"""

import time
from dataclasses import dataclass, field

import httpx

from retrieval.index import SEMANTIC_CONFIG, doc_key

API_VERSION = "2026-08-01-preview"
SEARCH_SCOPE = "https://search.azure.com/.default"
KNOWLEDGE_SOURCE = "ddt-articles-ks"
VECTORIZER = "aoai-embedding"
KNOWLEDGE_BASES = {"minimal": "ddt-kb-minimal", "low": "ddt-kb-low"}
MAX_DOCUMENTS = 10  # same depth as the classic modes (MRR@10)


def vectorizer_patch(index: dict, openai_endpoint: str, deployment: str) -> dict:
    """The index with an Azure OpenAI vectorizer on its HNSW profile, so agentic retrieval can
    embed subqueries itself. No key: the search service calls Foundry as its managed identity."""
    vs = index["vectorSearch"]
    vs["vectorizers"] = [
        {
            "name": VECTORIZER,
            "kind": "azureOpenAI",
            "azureOpenAIParameters": {
                "resourceUri": openai_endpoint.rstrip("/"),
                "deploymentId": deployment,
                "modelName": deployment,
            },
        }
    ]
    for profile in vs["profiles"]:
        profile["vectorizer"] = VECTORIZER
    return index


def knowledge_source_body(index_name: str) -> dict:
    return {
        "name": KNOWLEDGE_SOURCE,
        "kind": "searchIndex",
        "description": "Code du travail (LEGI) and Syntec/HCR conventions (KALI), one doc per chunk.",
        "searchIndexParameters": {
            "searchIndexName": index_name,
            "semanticConfigurationName": SEMANTIC_CONFIG,
            "sourceDataFields": [{"name": "num"}, {"name": "source"}],
        },
    }


def knowledge_base_body(effort: str, openai_endpoint: str, planner: str) -> dict:
    body = {
        "name": KNOWLEDGE_BASES[effort],
        "description": f"Agentic retrieval over the labour-law index, {effort} reasoning effort.",
        "knowledgeSources": [{"name": KNOWLEDGE_SOURCE}],
        "outputMode": "extractiveData",
        "retrievalReasoningEffort": {"kind": effort},
    }
    if effort != "minimal":
        body["models"] = [
            {
                "kind": "azureOpenAI",
                "azureOpenAIParameters": {
                    "resourceUri": openai_endpoint.rstrip("/"),
                    "deploymentId": planner,
                    "modelName": planner,
                },
            }
        ]
    return body


def retrieve_body(question: str, effort: str) -> dict:
    # Minimal effort has no planner, so it takes the query as an intent; the others plan
    # from the conversation.
    if effort == "minimal":
        query = {"intents": [{"type": "semantic", "search": question}]}
    else:
        query = {"messages": [{"role": "user", "content": [{"type": "text", "text": question}]}]}
    return {
        **query,
        "includeActivity": True,
        "knowledgeSourceParams": [
            {
                "knowledgeSourceName": KNOWLEDGE_SOURCE,
                "kind": "searchIndex",
                "alwaysQuerySource": True,
                "includeReferences": True,
                "maxOutputDocuments": MAX_DOCUMENTS,
            }
        ],
    }


@dataclass
class Retrieval:
    ranked: list[tuple[str, str]]  # (num, source), best first
    input_tokens: int = 0
    output_tokens: int = 0
    unknown_keys: list[str] = field(default_factory=list)


def parse_retrieve(payload: dict, by_key: dict[str, tuple[str, str]]) -> Retrieval:
    refs = [r for r in payload.get("references") or [] if r.get("docKey")]
    if any("rerankerScore" in r for r in refs):
        refs = sorted(refs, key=lambda r: r.get("rerankerScore") or 0.0, reverse=True)
    ranked, unknown = [], []
    for r in refs:
        hit = by_key.get(r["docKey"])
        if hit is None:
            unknown.append(r["docKey"])
        else:
            ranked.append(hit)
    activity = payload.get("activity") or []
    return Retrieval(
        ranked=ranked,
        input_tokens=sum(a.get("inputTokens") or 0 for a in activity),
        output_tokens=sum(a.get("outputTokens") or 0 for a in activity),
        unknown_keys=unknown,
    )


def keys_by_doc(articles) -> dict[str, tuple[str, str]]:
    """Search document key -> (article num, source), the same keys retrieval/index.py uploads."""
    return {doc_key(a.id): (a.num, a.source) for a in articles}


class AgenticClient:
    def __init__(self, endpoint: str, token, http: httpx.Client | None = None):
        """`token` returns a bearer token string (lets tests skip Entra)."""
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.http = http or httpx.Client(timeout=120)

    @classmethod
    def from_credential(cls, endpoint: str, credential) -> "AgenticClient":
        return cls(endpoint, lambda: credential.get_token(SEARCH_SCOPE).token)

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        resp = self.http.request(
            method,
            f"{self.endpoint}{path}",
            params={"api-version": API_VERSION},
            headers={"Authorization": f"Bearer {self.token()}"},
            json=body,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}")
        return resp.json() if resp.content else {}

    def add_vectorizer(self, index_name: str, openai_endpoint: str, deployment: str) -> None:
        index = self._call("GET", f"/indexes/{index_name}")
        index.pop("@odata.context", None)
        index.pop("@odata.etag", None)
        self._call(
            "PUT", f"/indexes/{index_name}", vectorizer_patch(index, openai_endpoint, deployment)
        )

    def setup(self, index_name: str, openai_endpoint: str, planner: str) -> list[str]:
        self._call(
            "PUT", f"/knowledgesources/{KNOWLEDGE_SOURCE}", knowledge_source_body(index_name)
        )
        done = [KNOWLEDGE_SOURCE]
        for effort, name in KNOWLEDGE_BASES.items():
            self._call(
                "PUT",
                f"/knowledgebases/{name}",
                knowledge_base_body(effort, openai_endpoint, planner),
            )
            done.append(name)
        return done

    def retrieve(self, effort: str, question: str, by_key) -> tuple[Retrieval, float]:
        start = time.perf_counter()
        payload = self._call(
            "POST",
            f"/knowledgebases/{KNOWLEDGE_BASES[effort]}/retrieve",
            retrieve_body(question, effort),
        )
        return parse_retrieve(payload, by_key), time.perf_counter() - start


def main(argv: list[str] | None = None) -> None:
    import argparse
    import os
    from dataclasses import replace

    from retrieval.__main__ import load_articles
    from retrieval.config import Clients, Settings

    parser = argparse.ArgumentParser(prog="python -m retrieval.agentic")
    parser.add_argument("--search-endpoint", required=True, help="the Basic benchmark service")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="vectorizer, knowledge source and both knowledge bases")
    q = sub.add_parser("query", help="one retrieve call")
    q.add_argument("q")
    q.add_argument("--effort", choices=list(KNOWLEDGE_BASES), default="low")
    args = parser.parse_args(argv)

    s = replace(Settings.from_env(), search_endpoint=args.search_endpoint)
    client = AgenticClient.from_credential(s.search_endpoint, Clients(s).credential)
    if args.cmd == "setup":
        client.add_vectorizer(s.search_index, s.openai_endpoint, s.embedding_deployment)
        print("vectorizer added to", s.search_index)
        print(
            "created or updated:",
            ", ".join(
                client.setup(s.search_index, s.openai_endpoint, os.environ["CHAT_DEPLOYMENT"])
            ),
        )
    else:
        got, elapsed = client.retrieve(args.effort, args.q, keys_by_doc(load_articles()))
        print(f"{elapsed:.2f}s planner tokens in/out {got.input_tokens}/{got.output_tokens}")
        for num, source in got.ranked:
            print(f"  {num:<14} {source}")
        if got.unknown_keys:
            print("  unknown doc keys:", got.unknown_keys[:5])


if __name__ == "__main__":
    main()
