# Recommended Next Harness Edits

- Enforce single-question-per-turn constraint more aggressively — escalate one_question_guard from warn to block or add post-processing to split compound questions.
- Add a domain-pivot rule: when oracle responds with uncertainty, immediately switch to a different requirement category (style → content → interaction).
- Insert oracle-redirect detection: parse oracle responses for phrases like 'Could you ask about X' and make the next question target X directly.
- Add a requirement-type checklist prompt that ensures the agent probes interaction, content, AND style categories within the turn budget.
- Prioritize style/UI questions earlier in the session since style requirements are missed in 2 of 3 tasks.
- Reduce time spent on speculative domains (security, compliance, industry classification) that oracles consistently lack opinions on.
