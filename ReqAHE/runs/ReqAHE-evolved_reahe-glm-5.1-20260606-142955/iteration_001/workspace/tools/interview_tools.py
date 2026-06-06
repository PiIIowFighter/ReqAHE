"""Tool stubs documented for the harness workspace.

Runtime execution is handled by reqahe.rollout.runner so the interviewer never
receives direct evaluator access.
"""


def ask_question(question: str) -> str:
    raise RuntimeError("ask_question is executed by the ReqAHE runtime.")


def finish_interview(summary: str) -> dict:
    raise RuntimeError("finish_interview is executed by the ReqAHE runtime.")
