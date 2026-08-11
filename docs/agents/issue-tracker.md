# Issue Tracker

## Tracker

Development specifications and progress live under `.scratch/smartfire-video-testkit/`. The directory is intentionally ignored by Git and is the local execution tracker. The accepted implementation plan and verification baseline under `docs/project/` remain the durable recovery sources.

Use local Markdown Issues for:

- accepted specifications;
- tracer-bullet delivery tickets;
- defects discovered by contract or protocol tests;
- explicit follow-up work and blocking relationships.

Pull requests are implementation and review artifacts, not a separate requirements inbox. Every non-trivial pull request should reference its issue.

## Skill behavior

- `to-spec` writes an accepted feature specification to `.scratch/smartfire-video-testkit/spec.md`.
- Task slicing writes one local Issue per approved tracer-bullet slice and records scoped blocker identifiers.
- Use `planned`, `in-progress`, `blocked` and `done`; no remote label controls execution.
- Do not silently rewrite the parent specification when child Issues complete.

## Lifecycle

Start only a `planned` Issue whose blockers are all `done`. Append verification evidence under `## Comments` before marking `done`. GitHub URLs in migrated files are historical metadata only and never need network access during implementation.
