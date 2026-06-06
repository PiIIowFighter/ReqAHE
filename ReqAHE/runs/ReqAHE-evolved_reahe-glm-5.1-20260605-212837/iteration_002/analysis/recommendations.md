# Recommended Next Harness Edits

- Upgrade one_question_guard from warn to block mode or add a prompt-level instruction to always ask a single focused question
- Add a turn-budget awareness strategy: never call finish_interview when turns remain and content requirements are likely unexplored
- Inject a content-requirement probing heuristic — after eliciting style, explicitly pivot to functional/feature/content questions
- When oracle redirects with 'ask more specifically about X', treat it as a strong signal to probe that domain rather than abandoning it
- Add a requirement-category checklist to the system prompt ensuring interaction and content types are probed before style
- Reduce compound questions by restructuring the prompt to forbid conjunctions like 'and' joining distinct inquiry topics
