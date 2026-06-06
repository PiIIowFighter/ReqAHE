PHRASES = ["anything else", "any other", "what else", "还有别", "其他需求"]


def check(action, state):
    question = (action.get("question") or "").strip().lower()
    if action.get("action") != "ask_question":
        return []
    if state.get("turn_index", 0) < 5 and any(p in question for p in PHRASES):
        return [{"type": "broad_question_guard", "severity": "warn", "message": "Avoid broad catch-all questions early in the interview."}]
    return []
