# RE Agent Debugger Overview

- Mean IRE 0.517 and approx_ESR 0.444 indicate roughly half of implicit requirements are being uncovered across 3-turn sessions.
- Type coverage is imbalanced: interaction 0.5, content 0.333, style 0.333 — style and content requirements are systematically missed.
- TKQR exceeds ontoagent threshold, suggesting turn quality is acceptable but not translating to full requirement discovery.
- One-question-guard warnings fired in 5 of 9 turns but agent ignored them, continuing to ask compound questions.
- Agent fails to pivot domains after oracle expresses uncertainty, wasting remaining turns on unproductive lines of inquiry.
- Oracle redirect hints (e.g., 'Could you ask about styling?') are consistently ignored.
