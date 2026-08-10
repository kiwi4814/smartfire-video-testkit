---
status: accepted
---

# Keep two independent test interfaces in one process

## Context

SmartFire development needs two different test doubles: a Fake Video Provider for the SmartFire consumer and a GB28181 Device Simulator for WVP/Gateway implementations. They benefit from one executable, one scenario store and one operator entry point, but they represent opposite sides of the integration and must not share a public interface.

## Decision

- Ship one TestKit process.
- Expose the Fake Video Provider through the versioned `/provider/v1` interface.
- Expose deterministic scenario control through `/testkit/v1`.
- Exercise GB28181 through real UDP/TCP packets rather than controller methods.
- Keep scenario state internal and resettable; production systems must never depend on `/testkit/v1`.

## Consequences

- One process is simple to start and coordinate in local development and CI.
- Tests can create a scenario through the control interface and observe Provider behavior without importing implementation internals.
- Shared storage must not collapse the semantic distinction between Provider resources and simulated protocol devices.
- Either interface can evolve internally, but breaking its external behavior requires an explicit contract or control-interface version decision.

## Rejected alternatives

- **Separate services immediately**: adds orchestration without improving the current test seam.
- **One combined HTTP interface**: confuses Provider behavior with test-only control and risks production consumers depending on simulator operations.
- **In-process protocol calls**: would not test parsing, addressing, timeout or transport behavior.
