# Recommended Next Harness Edits

- Add a content_requirement_guard middleware that prompts the agent to ask about specific data, information, and customization requirements
- Enforce a category-coverage schedule: probe interaction, content, AND style within the first 5-6 turns
- Add a stuck-detection heuristic: after 2-3 consecutive turns with no new requirement hits on the same topic, force a category switch
- Cap detail follow-ups: after hitting a requirement, allow at most 1-2 clarifying turns before mandating a move to a new requirement category
- Upgrade style_requirement_guard from warn to error mode or add a turn-based trigger that fires by turn 6 if style has not been probed
- Add a requirement-type checklist to the system prompt ensuring the agent explicitly covers interaction, content, and style categories
- Reduce multi-part questions: enforce one_question_guard more strictly to get clearer oracle responses
