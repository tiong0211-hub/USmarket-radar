"""OpenAI wrapper: strict structured output, retry policy and the daily token budget.

Error text from the API is never propagated: run logs are committed to a public repo.
"""
import json
import os
import threading
import time
from pathlib import Path

import openai

from radar.config import ROOT


class LLMRetryExhausted(Exception):
    """Transient failure or invalid output after all attempts. Other batches may still work."""


class LLMFatal(Exception):
    """Authentication, other 4xx or budget exhausted. Further calls cannot succeed."""


class TokenBudget:
    def __init__(self, limits: dict[str, int]):
        self.limits = limits
        self.used: dict[str, int] = {}
        self._lock = threading.Lock()

    def check(self, model: str, estimate: int) -> None:
        with self._lock:
            if self.used.get(model, 0) + estimate > self.limits[model]:
                raise LLMFatal(f"daily token budget exceeded for {model}")

    def add(self, model: str, tokens: int) -> None:
        with self._lock:
            self.used[model] = self.used.get(model, 0) + tokens


def load_schema(name: str) -> dict:
    return json.loads((ROOT / "schemas" / "llm" / f"{name}.json").read_text(encoding="utf-8"))


def load_prompt(name: str) -> str:
    return (ROOT / "prompts" / f"{name}.md").read_text(encoding="utf-8")


class LLMClient:
    def __init__(self, budget: TokenBudget, retries: int = 1, api: openai.OpenAI | None = None):
        self.budget = budget
        self.retries = retries
        if api:
            self.api = api
            return
        # Secrets pasted with a trailing newline break the auth header (httpx LocalProtocolError).
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            raise LLMFatal("OPENAI_API_KEY is not set")
        if not key.isascii():
            raise LLMFatal("OPENAI_API_KEY contains non-ASCII characters")
        self.api = openai.OpenAI(api_key=key, max_retries=0, timeout=120)

    def _once(self, cfg: dict, name: str, system: str, user: str) -> dict:
        arguments = {"temperature": cfg["temperature"]} if "temperature" in cfg else {}
        response = self.api.chat.completions.create(
            model=cfg["model"],
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": name, "strict": True, "schema": load_schema(name)},
            },
            **arguments,
        )
        if response.usage:
            self.budget.add(cfg["model"], response.usage.total_tokens)
        return json.loads(response.choices[0].message.content or "")

    def structured(self, cfg: dict, name: str, system: str, user: str, validate) -> object:
        """Returns validate(parsed JSON). Retries 429, 5xx, timeouts and invalid output."""
        estimate = (len(system) + len(user)) // 2  # rough: Korean-heavy text, 2 chars per token
        for attempt in range(self.retries + 1):
            self.budget.check(cfg["model"], estimate)
            wait = 2.0
            try:
                return validate(self._once(cfg, name, system, user))
            except openai.RateLimitError as exc:
                wait = float(exc.response.headers.get("retry-after", wait))
                reason = "RateLimitError"
            except (openai.APITimeoutError, openai.APIConnectionError, openai.InternalServerError) as exc:
                cause = exc.__cause__
                reason = type(exc).__name__ + (f"({type(cause).__name__})" if cause else "")
            except openai.OpenAIError as exc:
                raise LLMFatal(type(exc).__name__) from None
            except ValueError as exc:  # JSONDecodeError and pydantic ValidationError
                reason = type(exc).__name__
            if attempt < self.retries:
                time.sleep(wait)
        raise LLMRetryExhausted(reason)
