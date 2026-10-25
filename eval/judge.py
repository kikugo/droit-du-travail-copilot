"""The faithfulness judge: gpt-4.1-mini on Foundry, a different model from the generator.

DeepEval calls `generate(prompt, schema=...)`; this wrapper asks for JSON, validates it against
the schema, and caches every prompt/response pair on disk so reruns cost nothing. Temperature 0.
"""

import json
import os
from typing import Any

from deepeval.models.base_model import DeepEvalBaseLLM

from eval.cache import DiskCache, key

os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "1")
DEFAULT_JUDGE = "gpt-4.1-mini"


class FoundryJudge(DeepEvalBaseLLM):
    def __init__(self, client, deployment: str = DEFAULT_JUDGE, cache: DiskCache | None = None):
        self.client = client
        self.deployment = deployment
        self.cache = cache or DiskCache()
        self.usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
        super().__init__(deployment)

    def load_model(self):
        return self.client

    def get_model_name(self) -> str:
        return f"{self.deployment} (Foundry)"

    def supports_temperature(self) -> bool:
        return True

    def _complete(self, prompt: str, json_mode: bool) -> str:
        k = key("judge", self.deployment, prompt, json_mode)
        cached = self.cache.get("judge", k)
        if cached is not None:
            return cached
        kwargs: dict[str, Any] = {"response_format": {"type": "json_object"}} if json_mode else {}
        resp = self.client.chat.completions.create(
            model=self.deployment,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            **kwargs,
        )
        text = resp.choices[0].message.content
        self.usage["input_tokens"] += resp.usage.prompt_tokens
        self.usage["output_tokens"] += resp.usage.completion_tokens
        self.usage["calls"] += 1
        self.cache.put("judge", k, text)
        return text

    def generate(self, prompt: str, schema=None, **kwargs):
        text = self._complete(prompt, json_mode=schema is not None)
        if schema is None:
            return text
        return schema.model_validate(json.loads(text))

    async def a_generate(self, prompt: str, schema=None, **kwargs):
        return self.generate(prompt, schema=schema)


def faithfulness(judge: FoundryJudge, question: str, answer: str, contexts: list[str]) -> dict:
    """DeepEval faithfulness of `answer` against the tool outputs. Score in [0, 1]."""
    from deepeval.metrics import FaithfulnessMetric
    from deepeval.test_case import LLMTestCase

    if not contexts:
        return {"score": None, "reason": "no retrieval context"}
    metric = FaithfulnessMetric(model=judge, include_reason=True, async_mode=False, threshold=0.5)
    metric.measure(LLMTestCase(input=question, actual_output=answer, retrieval_context=contexts))
    return {"score": metric.score, "reason": metric.reason}
