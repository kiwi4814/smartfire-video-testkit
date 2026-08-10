# Issue Tracker

## Tracker

This repository uses GitHub Issues in `kiwi4814/smartfire-video-testkit` as the authoritative work tracker.

Use GitHub Issues for:

- accepted specifications;
- tracer-bullet delivery tickets;
- defects discovered by contract or protocol tests;
- explicit follow-up work and blocking relationships.

Pull requests are implementation and review artifacts, not a separate requirements inbox. Every non-trivial pull request should reference its issue.

## Skill behavior

- `to-spec` publishes an accepted feature specification as a GitHub issue.
- `to-tickets` publishes one issue per approved tracer-bullet slice and records blocking issue references.
- Tickets ready for an implementation agent use the `ready-for-agent` label.
- Do not silently close parent specifications when child tickets complete.

## Local notes

Transient investigation notes may use `.scratch/`, which remains local and must not replace GitHub Issues as the authoritative tracker.
