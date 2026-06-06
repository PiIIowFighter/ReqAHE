def check(action, state):
    if action.get("action") != "finish_interview":
        return []
    if state.get("turn_index", 0) < 4 and state.get("max_turns", 0) > 5:
        return [{"type": "premature_finish_guard", "severity": "warn", "message": "Finishing this early may miss implicit requirements."}]
    if state.get("recent_hits", 0) > 0 and state.get("turn_index", 0) < state.get("max_turns", 0) - 1:
        return [{"type": "premature_finish_guard", "severity": "warn", "message": "Recent turns found useful information; consider probing further."}]
    return []
