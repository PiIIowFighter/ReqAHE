# AHE to RE-AHE Mapping

| AHE | RE-AHE |
|---|---|
| Code Agent | Interviewer Agent |
| Terminal/repo environment | ReqElicitGym environment |
| pass@1 | IRE, TKQR, coverage, turn quality |
| raw trace | dialogue trace |
| Agent Debugger | RE Agent Debugger |
| Evolve Agent | RE Evolve Agent |
| NexAU harness | RE-NexAU-style harness workspace |
| change_manifest.json | change_manifest.json |
| rollback | attribution-driven workspace rollback |

The first runnable version uses rule fallback debugger/evolver so the loop can run before stronger LLM evolution is added.
