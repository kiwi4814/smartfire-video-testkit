# SmartFire Video TestKit Agent Instructions

## Project purpose

This repository provides deterministic video integration test doubles for SmartFire. It contains a Fake Video Provider and a GB28181 Device Simulator in one deployable process while keeping their public interfaces independent.

Read before changing behavior:

- `CONTEXT.md` for canonical terms;
- `docs/adr/` for accepted architecture decisions;
- `docs/project/SMARTFIRE-VIDEO-TESTKIT-IMPLEMENTATION-PLAN.md` for delivery slices and acceptance gates;
- `docs/project/VERIFICATION-BASELINE.md` for the last verified state;
- the SmartFire Provider Contract version named in `README.md` before changing `/provider/v1`.

## Engineering rules

- Target Python 3.11 or newer and preserve the `uv` workflow and `uv.lock`.
- Keep the two public seams stable: Provider HTTP under `/provider/v1`, TestKit control HTTP under `/testkit/v1`. SIP/UDP/TCP packets are the protocol seam.
- Test behavior only through HTTP or real network packets. Do not make acceptance tests call private implementation methods.
- Keep scenarios deterministic, resettable and bounded by explicit timeouts. Do not add fixed long sleeps.
- Do not make unit tests depend on WVP, Gateway, ZLMediaKit, SmartFire, Internet access or customer devices.
- Never commit credentials, customer GB IDs, private addresses, video samples without redistribution rights, or captured packets containing secrets.
- Preserve the distinction between simulator conformance and real-vendor compatibility.
- Prefer one complete tracer-bullet slice over broad protocol scaffolding.

## Required verification

```bash
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
uv build
```

For a release candidate, also install the wheel into a clean virtual environment and verify the installed `video-testkit` entry point and both health endpoints.

## Agent skills

### Issue tracker

Work is tracked in this repository's GitHub Issues. Specs and tracer-bullet tickets are published through the configured Matt Pocock skills. See `docs/agents/issue-tracker.md`.

### Short-prompt execution router

When the user says only `实现 #<N>`、`继续 #<N>`、`修复 #<N>` or otherwise names a GitHub Issue:

1. Fetch that Issue's full body, comments, labels and blocking references from `kiwi4814/smartfire-video-testkit`.
2. Read `CONTEXT.md`, both ADRs, the Implementation Plan, the Verification Baseline, and the Provider Contract version named in `README.md`; the user does not need to repeat these paths.
3. Refuse to start implementation when any blocker is open or the Issue lacks `ready-for-agent`; report the exact next ready Issue instead.
4. Use the repository's engineering skills for implementation, TDD, module/interface design and simplicity; do not require the user to name skills.
5. Modify only the named Issue, verify through the public seams and required Python matrix, update the Verification Baseline, then stop.

### Domain docs

This repository uses a single-context layout: one root `CONTEXT.md` and repository-wide ADRs under `docs/adr/`. See `docs/agents/domain.md`.
