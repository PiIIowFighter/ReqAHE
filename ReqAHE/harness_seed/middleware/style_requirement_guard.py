def check(action, state):
    if action.get("action") != "ask_question":
        return []
    turn_index = state.get("turn_index", 0)
    if turn_index >= max(3, state.get("max_turns", 8) // 2) and not state.get("asked_style", False):
        return [{"type": "style_requirement_guard", "severity": "warn", "message": "Consider asking about visual style, layout, theme, colors, or UI preferences."}]
    return []
