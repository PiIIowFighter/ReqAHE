import re


def check(action, state):
    question = (action.get("question") or "").strip()
    if action.get("action") != "ask_question":
        return []
    marks = len(re.findall(r"\?", question))
    if marks > 1 or " and " in question.lower():
        return [{"type": "one_question_guard", "severity": "warn", "message": "Ask only one focused question this turn."}]
    return []
