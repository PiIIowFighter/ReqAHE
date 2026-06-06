from __future__ import annotations

from typing import Any

from reqahe.llm.client import OpenAICompatibleClient


class SeedInterviewer:
    def __init__(self, harness: dict[str, str], llm: OpenAICompatibleClient, model: str = ""):
        self.harness = harness
        self.llm = llm
        self.model = model

    def next_action(
        self,
        initial_req: str,
        history: list[dict[str, str]],
        warnings: list[str],
        max_turns: int,
    ) -> dict[str, Any]:
        prompt = self._build_prompt(initial_req, history, warnings, max_turns)
        parsed = self.llm.json_chat(
            [
                {"role": "system", "content": self.harness.get("system_prompt", "")},
                {"role": "user", "content": prompt},
            ],
            model=self.model,
            purpose="interviewer action generation",
        )
        return _validate_action(parsed)

    def _build_prompt(self, initial_req: str, history: list[dict[str, str]], warnings: list[str], max_turns: int) -> str:
        rendered_history = "\n".join(f"Interviewer: {h.get('question','')}\nUser: {h.get('answer','')}" for h in history)
        return (
            f"Initial requirement:\n{initial_req}\n\n"
            f"Turn budget: {max_turns}\n"
            f"History:\n{rendered_history or '(none)'}\n\n"
            f"Middleware warnings:\n" + "\n".join(warnings[-5:]) + "\n\n"
            f"Tool descriptions:\n{self.harness.get('tools','')}\n\n"
            f"Skills:\n{self.harness.get('skills','')}\n\n"
            "Return strict JSON only."
        )

def _validate_action(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("action") not in {"ask_question", "finish_interview"}:
        raise RuntimeError("interviewer action generation failed: action must be ask_question or finish_interview")
    if data["action"] == "ask_question" and not str(data.get("question") or "").strip():
        raise RuntimeError("interviewer action generation failed: ask_question requires a non-empty question")
    if data["action"] == "finish_interview" and not str(data.get("finish_summary") or "").strip():
        raise RuntimeError("interviewer action generation failed: finish_interview requires finish_summary")
    return {
        "thought_summary": str(data.get("thought_summary") or ""),
        "action": data["action"],
        "question": str(data.get("question") or ""),
        "finish_summary": str(data.get("finish_summary") or ""),
    }
