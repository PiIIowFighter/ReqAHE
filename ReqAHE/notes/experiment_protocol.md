# Experiment Protocol

1. Use `inspect` to record source repository facts.
2. Use `seed_freeform` on train/test task mode for smoke tests.
3. Use `evolve` on train or train+val only.
4. Freeze the best workspace before final evaluation.
5. Keep `internal_holdout_result` separate from `paper_style_result`.
6. Mark paper-style runs as `not strictly paper-fair` if evolution used any ReqElicitGym scenario feedback.

`approx_ESR` is not treated as the original paper ESR unless a source ESR implementation is found.
