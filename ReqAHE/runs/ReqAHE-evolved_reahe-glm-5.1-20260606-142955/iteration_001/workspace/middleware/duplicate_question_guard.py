from difflib import SequenceMatcher


def check(action, state):
    question = (action.get("question") or "").strip().lower()
    if action.get("action") != "ask_question" or not question:
        return []
    for past in state.get("questions", []):
        ratio = SequenceMatcher(None, question, past.lower()).ratio()
        if ratio >= 0.82:
            return [{"type": "duplicate_question_guard", "severity": "warn", "message": "The question is too similar to an earlier one."}]
    return []
