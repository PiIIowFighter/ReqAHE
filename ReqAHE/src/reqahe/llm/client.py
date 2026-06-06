from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass
class LLMResult:
    ok: bool
    text: str
    error: str | None = None


class OpenAICompatibleClient:
    def __init__(self, api_key: str = "", base_url: str = "", model: str = "", temperature: float = 0.2, max_tokens: int = 1200):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    def chat(self, messages: list[dict], model: str | None = None) -> LLMResult:
        if not self.api_key or not (model or self.model):
            return LLMResult(False, "", "Missing OPENAI_API_KEY or model.")
        try:
            from openai import OpenAI

            client = OpenAI(api_key=self.api_key, base_url=self.base_url or None)
            response = client.chat.completions.create(
                model=model or self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
            return LLMResult(True, response.choices[0].message.content or "")
        except Exception as exc:  # pragma: no cover - external API failures are environment-dependent.
            return LLMResult(False, "", str(exc))

    def require_chat(self, messages: list[dict], model: str | None = None, purpose: str = "LLM call") -> str:
        result = self.chat(messages, model=model)
        if not result.ok:
            raise RuntimeError(f"{purpose} failed: {result.error}")
        if not result.text.strip():
            raise RuntimeError(f"{purpose} failed: empty model response")
        return result.text

    def json_chat(
        self,
        messages: list[dict],
        model: str | None = None,
        purpose: str = "LLM JSON call",
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        attempts = max(1, max_attempts)
        current_messages = list(messages)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            text = self.require_chat(current_messages, model=model, purpose=purpose)
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
