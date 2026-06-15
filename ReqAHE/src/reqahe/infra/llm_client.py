from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

_RETRYABLE_ERROR_MARKERS = (
    "connection error",
    "connection reset",
    "timed out",
    "timeout",
    "rate limit",
    "429",
    "502",
    "503",
    "504",
    "temporarily unavailable",
    "overloaded",
    "server error",
    "api connection",
    "connecttimeout",
    "readtimeout",
    "remotedisconnected",
    "broken pipe",
    "name or service not known",
    "failed to establish a new connection",
)


def _is_retryable_error(error: str) -> bool:
    lowered = (error or "").lower()
    if "empty model response" in lowered:
        return True
    return any(marker in lowered for marker in _RETRYABLE_ERROR_MARKERS)


@dataclass
class LLMResult:
    ok: bool
    text: str
    error: str | None = None


class OpenAICompatibleClient:
    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        model: str = "",
        temperature: float = 0.2,
        max_tokens: int = 1200,
        max_retries: int = 8,
        retry_base_seconds: float = 2.0,
        trust_env: bool = False,
        timeout: float = 120.0,
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.max_retries = max(1, max_retries)
        self.retry_base_seconds = max(0.5, retry_base_seconds)
        self.trust_env = trust_env
        self.timeout = max(10.0, float(timeout))
        self._openai_client = None

    def _get_openai_client(self):
        if self._openai_client is None:
            import httpx
            from openai import OpenAI

            self._openai_client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url or None,
                http_client=httpx.Client(
                    trust_env=self.trust_env,
                    timeout=httpx.Timeout(self.timeout),
                ),
            )
        return self._openai_client

    def close(self) -> None:
        client = self._openai_client
        self._openai_client = None
        if client is None:
            return
        close = getattr(client, "close", None)
        if callable(close):
            close()

    def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        *,
        max_tokens: int | None = None,
    ) -> LLMResult:
        if not self.api_key or not (model or self.model):
            return LLMResult(False, "", "Missing OPENAI_API_KEY or model.")
        try:
            response = self._get_openai_client().chat.completions.create(
                model=model or self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=max_tokens if max_tokens is not None else self.max_tokens,
            )
            choice = response.choices[0]
            content = choice.message.content or ""
            if not content.strip():
                finish_reason = str(getattr(choice, "finish_reason", "") or "unknown")
                return LLMResult(False, "", f"empty model response (finish_reason={finish_reason})")
            return LLMResult(True, content)
        except Exception as exc:  # pragma: no cover - external API failures are environment-dependent.
            return LLMResult(False, "", str(exc))

    def require_chat(
        self,
        messages: list[dict],
        model: str | None = None,
        purpose: str = "LLM call",
        max_attempts: int | None = None,
        *,
        max_tokens: int | None = None,
    ) -> str:
        attempts = max(1, max_attempts if max_attempts is not None else self.max_retries)
        last_error = ""
        for attempt in range(1, attempts + 1):
            result = self.chat(messages, model=model, max_tokens=max_tokens)
            if result.ok and result.text.strip():
                return result.text
            last_error = result.error or "empty model response"
            if attempt >= attempts or not _is_retryable_error(last_error):
                break
            wait_seconds = self.retry_base_seconds * (2 ** (attempt - 1))
            print(
                f"[llm] {purpose} attempt={attempt}/{attempts} retryable_error={last_error} "
                f"retry_in={wait_seconds:.1f}s",
                flush=True,
            )
            time.sleep(wait_seconds)
        raise RuntimeError(f"{purpose} failed: {last_error}")

    def json_chat(
        self,
        messages: list[dict],
        model: str | None = None,
        purpose: str = "LLM JSON call",
        max_attempts: int = 3,
        transport_max_attempts: int | None = None,
        *,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        attempts = max(1, max_attempts)
        current_messages = list(messages)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            text = self.require_chat(
                current_messages,
                model=model,
                purpose=purpose,
                max_attempts=transport_max_attempts,
                max_tokens=max_tokens,
            )
            try:
                data = json.loads(_strip_json_fence(text))
            except json.JSONDecodeError as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                current_messages = [
                    *messages,
                    {
                        "role": "assistant",
                        "content": text[:12000],
                    },
                    {
                        "role": "user",
                        "content": (
                            "Your previous response was not valid JSON. "
                            f"JSON parser error: {exc}. "
                            "Regenerate the complete response as one compact valid JSON object only. "
                            "Do not use Markdown fences, comments, trailing commas, or unescaped newlines inside strings."
                        ),
                    },
                ]
                continue
            if not isinstance(data, dict):
                last_error = RuntimeError("expected a JSON object")
                if attempt >= attempts:
                    break
                current_messages = [
                    *messages,
                    {
                        "role": "assistant",
                        "content": text[:12000],
                    },
                    {
                        "role": "user",
                        "content": "Regenerate the complete response as one compact valid JSON object only.",
                    },
                ]
                continue
            return data
        raise RuntimeError(f"{purpose} failed: response was not valid JSON after {attempts} attempts: {last_error}")


def _strip_json_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    return cleaned
