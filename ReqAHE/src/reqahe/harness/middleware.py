from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any


class MiddlewareRuntime:
    def __init__(self, workspace_dir: str | Path, mode: str = "warn"):
        self.workspace_dir = Path(workspace_dir)
        self.mode = mode
        self.modules = self._load_modules()

    def _load_modules(self):
        modules = []
        folder = self.workspace_dir / "middleware"
        if not folder.exists():
            return modules
        for path in sorted(folder.glob("*.py")):
            spec = importlib.util.spec_from_file_location(f"reqahe_workspace_{path.stem}", path)
            if not spec or not spec.loader:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if hasattr(module, "check"):
                modules.append((path.name, module))
        return modules

    def check(self, action: dict[str, Any], state: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        events: list[dict[str, Any]] = []
        for name, module in self.modules:
            for event in module.check(action, state) or []:
                event = dict(event)
                event.setdefault("middleware", name)
                event.setdefault("mode", self.mode)
                events.append(event)
        if self.mode == "enforce" and any(e.get("severity") in {"error", "warn"} for e in events):
            if action.get("action") == "finish_interview":
                action = {**action, "action": "ask_question", "question": "Before we finish, what visual style or interaction detail is important for this system?"}
            elif action.get("action") == "ask_question":
                question = str(action.get("question") or "")
                action = {**action, "question": question.split("?")[0].strip() + "?"}
        return action, events
