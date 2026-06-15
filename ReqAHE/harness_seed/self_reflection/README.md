# Self Reflection Runtime Checks

Self-reflection contains evolved Python runtime checks for candidate interviewer actions.

## Allowed hooks

- `question_candidate` — inspect a generated question before it is sent to ReqElicitGym
- `finish_candidate` — inspect a generated finish summary before the interview ends

## Bundle structure

Each check is a bundle:

```text
self_reflection/<reflection_id>/check.py
self_reflection/<reflection_id>/PROMPT.md
```

- `check.py` receives the candidate action and runtime state, detects quality problems, and returns concise warn/enforce events with `message` and optional `suggestion`.
- `PROMPT.md` provides same-turn retry instructions when a warn/enforce check triggers regeneration. It is injected only when the runtime requests a retry.

## Registry fields

Register checks in `registry.yaml`. Each entry includes:

- `id` — must equal the bundle folder name
- `hook` — `question_candidate` or `finish_candidate`
- `applies_when` — when the check should run (see below)
- `severity` — optional; runtime derives severity from `mode` when omitted
- `entrypoint` — relative bundle path via `file` (`<reflection_id>/check.py`) and `prompt` (`<reflection_id>/PROMPT.md`)
- `mode` — `observe`, `warn`, or `enforce`
- `priority` — optional ordering hint

Supported `applies_when` values:

- `always`
- `early_turn`
- `late_turn`
- `has_history`
- `no_history`
- `candidate_is_question`
- `candidate_is_finish`

## Runtime behavior

1. After the interviewer generates a candidate action and before it is submitted to ReqElicitGym, the runtime executes matching Python checks for the candidate hook.
2. Checks with `mode=observe` only record events.
3. Checks with `mode=warn` or `mode=enforce` may trigger same-turn retry. The runtime injects warn/enforce feedback into the current-turn prompt, retries up to a configured limit, then continues with the final candidate if retries are exhausted.
4. Python checks inspect candidate quality only. They do not modify ReqElicitGym state.
5. Self-reflection is independent from memory.

The seed does not ship concrete checks; evolved bundles are added by the refiner.
