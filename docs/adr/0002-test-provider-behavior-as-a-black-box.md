---
status: accepted
---

# Test Provider behavior as a black box

## Context

The same contract must validate a Python Fake Provider, a Java WVP Provider and a Go sipgo Gateway. Tests coupled to one implementation language or internal class graph cannot serve as shared conformance evidence.

## Decision

- Shared contract tests invoke only Provider HTTP and Provider event callbacks.
- Protocol scenarios communicate with the Provider Under Test through real SIP and RTP transports.
- TestKit control operations are allowed only for arranging and observing deterministic test scenarios.
- Internal unit tests may exist, but they do not count as shared Provider conformance.
- Every network wait is bounded and based on observable state; long fixed sleeps are prohibited.

## Consequences

- The same runner can validate WVP and sipgo implementations.
- Failures identify an externally observable contract or protocol mismatch.
- Tests require explicit process lifecycle, dynamic ports and cleanup.
- Some low-level algorithm defects need additional implementation-local unit tests.

## Rejected alternatives

- **Generated-client-only tests**: clients can normalize or hide malformed raw HTTP behavior.
- **Import Provider modules into tests**: prevents cross-language reuse and bypasses serialization and network behavior.
- **Snapshot all responses**: creates brittle tests that obscure semantic assertions.
