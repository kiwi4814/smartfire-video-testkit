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

Work is tracked as local Markdown under `.scratch/smartfire-video-testkit/`. GitHub Issues are migration-era history only. See `docs/agents/issue-tracker.md`.

### Short-prompt execution router

When the user says only `实现 VT-<N>`、`继续 VT-<N>`、`修复 VT-<N>` or otherwise names a local TestKit Issue:

1. Resolve the Issue under `.scratch/smartfire-video-testkit/issues/`, then read its feature `spec.md`, full body, blockers and comments.
2. Read `CONTEXT.md`, both ADRs, the Implementation Plan, the Verification Baseline, and the Provider Contract version named in `README.md`; the user does not need to repeat these paths.
3. Start only when `Status: planned` and every blocker is `done`; mark it `in-progress` before editing. If blocked, report the exact next executable Issue from the cross-project local index.
4. Use the repository's engineering skills for implementation, TDD, module/interface design and simplicity; do not require the user to name skills.
5. Modify only the named Issue, verify through the public seams and required Python matrix, append evidence, mark the local Issue `done`, update the Verification Baseline, create one focused local commit, then stop.

### Domain docs

This repository uses a single-context layout: one root `CONTEXT.md` and repository-wide ADRs under `docs/adr/`. See `docs/agents/domain.md`.
