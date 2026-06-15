# Skills

Skills are the operational strategy layer.

Each skill lives in its own folder with a `SKILL.md` file, matching Cursor-style skill layout:

```text
skills/
└── <skill-name>/
    └── SKILL.md
```

A skill should contain concrete probing procedures, question patterns, stop conditions, and anti-patterns.
Runtime injects only router-selected skill full text into the interviewer prompt.

Do not append skill content to this README. The refiner creates new skills under `skills/<skill-name>/SKILL.md`.

The seed harness intentionally starts without strategy content.
