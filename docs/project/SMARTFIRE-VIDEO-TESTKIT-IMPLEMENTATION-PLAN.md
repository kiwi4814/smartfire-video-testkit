# SmartFire Video TestKit Implementation Plan

## 1. Purpose

The TestKit provides repeatable evidence for two independent development paths:

1. SmartFire can develop its video compatibility layer against a deterministic Fake Video Provider.
2. WVP Provider and sipgo Gateway can be developed against deterministic GB28181 protocol peers without physical cameras.

The project is a maintained test product, not a production Provider and not a substitute for real-vendor acceptance.

## 2. Current state

Version `0.1.0` is a completed tracer bullet:

- one installable Python process;
- Fake Provider core contract behavior;
- deterministic IPC and four-channel NVR fixtures;
- TestKit control interface and reset;
- real UDP REGISTER/Digest success, wrong-password and timeout flows;
- 52 automated black-box/network tests;
- lint, format, typing, build and clean-wheel startup checks.

See `VERIFICATION-BASELINE.md` for the exact evidence. Future agents must not infer unimplemented protocol capability from the presence of a Fake Provider endpoint.

## 3. Architecture and seams

### 3.1 External seams

| Seam | Caller | Purpose | Test rule |
|---|---|---|---|
| Provider Interface `/provider/v1` | SmartFire/Contract Runner | Shared Provider behavior | Assert raw HTTP behavior |
| TestKit Control Interface `/testkit/v1` | Operator/test runner | Arrange/reset/observe scenarios | Never expose as production behavior |
| SIP UDP/TCP | Provider Under Test | GB28181 signaling | Use real sockets and parsed packets |
| RTP transport | Provider/ZLM | Media behavior | Use deterministic fixtures and real packets |
| Provider event callback | Fake/WVP/Gateway | State-change delivery | Use a controlled HTTP sink |

The interface is the test surface. Acceptance tests must not reach through it to inspect Store, ProviderService, DeviceSimulator or Registrar internals.

### 3.2 Internal modules

- **Scenario Store**: deterministic device, channel, operation, stream and event state;
- **Fake Provider**: shared contract implementation over the Scenario Store;
- **Device Simulator**: GB28181 device-side state machines;
- **Protocol Codec**: SIP/XML/SDP/RTP-PS encode and decode;
- **Contract Runner**: external Provider invocation and assertions;
- **Control Adapter**: scenario arrangement and observation;
- **Report Writer**: machine-readable and human-readable evidence.

New internal seams should only be introduced when two concrete implementations need to vary. Do not add a framework-like plugin system in advance.

## 4. Global quality rules

- Every slice must be demonstrable through a public seam and fit one fresh implementation context.
- Red-to-green tests precede behavior changes at the agreed seam.
- All ports are dynamic by default in tests and released during cleanup.
- Network operations have explicit timeouts and bounded retry.
- Scenario reset returns the process to a known state without restarting it.
- IDs, clocks, random values and media fixtures are deterministic or captured in the test report.
- Optional capabilities are advertised accurately; an unimplemented capability is false, not a successful no-op.
- Logs and reports redact credentials and customer identifiers.
- Tests cannot claim Vendor Compatibility without physical-device evidence.

## 5. Delivery slices

### VT-01 — Publish and consume the machine-readable Provider Contract

**Outcome:** the TestKit validates any configured Provider Base URL against a pinned OpenAPI version.

Work:

- consume the canonical OpenAPI 3.1 contract and examples;
- record contract version and source checksum;
- validate requests, responses, errors and examples;
- run the current behavior suite against either the in-process Fake Provider or an external Provider;
- produce JUnit XML and JSON summary with Provider implementation metadata;
- detect breaking contract changes before tests execute.

Acceptance:

- a single command targets Fake, WVP or sipgo by configuration;
- deliberate removal or type corruption of a required field fails with the contract operation and request ID;
- tests do not import the external Provider implementation;
- the report identifies contract version, Provider version, capabilities and skipped optional tests.

Blocked by: publication of the canonical machine-readable Provider Contract.

### VT-02 — Complete the GB28181 registration lifecycle

**Outcome:** a Provider can be tested for long-running device registration behavior rather than one successful handshake.

Work:

- registration refresh before expiry;
- explicit unregister (`Expires: 0`);
- Provider restart and device re-registration;
- wrong realm, nonce refresh and stale challenge scenarios;
- duplicate and delayed UDP responses;
- deterministic clock controls where expiration is involved.

Acceptance:

- success and each failure state are observable through the control interface;
- refresh does not create duplicate device identities;
- unregister leads to an eventual offline observation;
- no test waits for real 3600-second expiry.

Blocked by: none.

### VT-03 — Add Keepalive and online/offline scenarios

**Outcome:** a Provider can prove that device liveness converges under normal and failed heartbeats.

Work:

- device-side Keepalive MESSAGE and XML body;
- Provider response parsing;
- start, pause, resume and malformed-heartbeat controls;
- sequence number and encoding fixtures;
- offline timeout and re-online scenarios.

Acceptance:

- the control interface can trigger normal, dropped and malformed Keepalive;
- Provider status eventually becomes ONLINE/OFFLINE without fixed sleeps;
- event duplication and ordering are observable;
- Fake Provider fields follow the same common status vocabulary.

Blocked by: VT-02.

### VT-04 — Add deterministic Catalog discovery

**Outcome:** WVP/Gateway discovers one IPC and one multi-channel NVR through real SIP MESSAGE traffic.

Work:

- parse Catalog query SN and DeviceID;
- generate GB28181 XML for IPC and NVR;
- support one-message and multi-message responses;
- inject duplicate, delayed, missing, malformed and out-of-order items;
- support Chinese names and configurable charset;
- expose scenario revision and response progress.

Acceptance:

- a Provider discovers stable Device/Channel IDs and correct channel count;
- repeated Catalog is idempotent;
- PARTIAL/timeout paths preserve valid items;
- a missing channel can reappear with the same Protocol Source Identity;
- the TestKit never asserts SmartFire business deletion from a Provider omission.

Blocked by: VT-03.

### VT-05 — Add live signaling without media

**Outcome:** a Provider can complete INVITE/SDP/ACK/BYE state transitions against a deterministic device.

Work:

- receive and validate INVITE;
- generate SDP and 200 response;
- observe ACK and BYE;
- expose Dialog, target address, SSRC and expected media endpoint as redacted diagnostics;
- inject rejection, delayed response, no ACK and duplicate BYE.

Acceptance:

- a normal Dialog reaches ESTABLISHED then TERMINATED;
- timeout/rejection maps to stable Provider failure;
- repeated stop is safe;
- failed cases leave no permanent socket or Dialog state.

Blocked by: VT-04.

### VT-06 — Send deterministic H.264 RTP/PS to ZLMediaKit

**Outcome:** live start is proven through real media arrival, not only successful SIP signaling.

Work:

- use a redistribution-safe short H.264 fixture;
- MPEG-PS mux and RTP packetization;
- sequence, timestamp, marker and SSRC handling;
- UDP media first; TCP media is later;
- integrate with a configured ZLM and observe stream-online state;
- inject no-media, wrong-SSRC, packet loss and mid-stream stop.

Acceptance:

- ZLM reports the expected app/stream online;
- Provider only returns STREAMING after the expected media is observable;
- BYE/stop ends sending and releases runtime resources;
- fixture checksum, codec, duration and licensing source are recorded.

Blocked by: VT-05 and an available ZLM integration environment.

### VT-07 — Add device RecordInfo queries

**Outcome:** a Provider can query deterministic device-side recording catalogs.

Work:

- RecordInfo MESSAGE parsing and response XML;
- empty, one-item and multi-item fixtures;
- multi-message aggregation, duplicate and out-of-order responses;
- PARTIAL and timeout scenarios;
- UTC/device-time offset controls.

Acceptance:

- query results contain stable opaque record keys and correct time ranges;
- no-record is a successful empty result;
- partial results remain usable and are identified as partial;
- retries with the same idempotency key do not create a second query.

Blocked by: VT-04.

### VT-08 — Add playback signaling and media

**Outcome:** a Provider starts and stops a deterministic device-record playback stream.

Work:

- validate playback INVITE/SDP time range;
- bind record key to Device/Channel/time;
- stream the matching deterministic RTP/PS fixture;
- stop and cleanup;
- mismatch, missing-record and timeout scenarios.

Acceptance:

- successful playback reaches ZLM and exposes a PLAYBACK stream reference;
- mismatched identity/time returns the common stable error;
- repeated start/stop is idempotent;
- no runtime or port remains after cleanup.

Blocked by: VT-06 and VT-07.

### VT-09 — Add TCP, H.265 and audio capability slices

**Outcome:** optional transport and codec capabilities are tested independently and advertised accurately.

Work is split into separate sub-slices when implemented:

- SIP over TCP;
- RTP over TCP where required;
- H.265 fixture and packetization;
- audio-bearing PS fixture.

Acceptance:

- each capability is independently selectable and reportable;
- unsupported combinations fail explicitly;
- adding a codec or transport does not change H.264/UDP baseline behavior.

Blocked by: VT-06.

### VT-10 — Produce release-grade conformance evidence

**Outcome:** other project sessions can run one command and attach trustworthy evidence to WVP/Gateway work.

Work:

- scenario manifest and deterministic seed in reports;
- JUnit XML, JSON summary and concise Markdown report;
- redacted request/SIP trace references for failures;
- resource leak checks;
- three consecutive clean runs;
- CI matrix for supported Python versions;
- documented separation of Simulator Conformance and Vendor Compatibility.

Acceptance:

- report is sufficient to reproduce a failure from a fresh checkout;
- credentials and customer data do not appear;
- mandatory versus capability-gated tests are explicit;
- WVP and sipgo use the same runner and report shape.

Blocked by: VT-01 and the protocol slices required by the target release.

## 6. Dependency graph

```text
VT-01 Machine-readable contract ──────────────────────────────┐
                                                              ├─> VT-10 Reports
VT-02 Registration -> VT-03 Keepalive -> VT-04 Catalog -------┤
                                      ├-> VT-05 Live signaling -> VT-06 H264 RTP/PS -> VT-08 Playback
                                      └-> VT-07 RecordInfo ----------------------------^
VT-06 -> VT-09 TCP/H265/Audio
```

Ready frontier after `0.1.0`: VT-01 when OpenAPI is available, and VT-02 immediately. Work on WVP/Gateway should prioritize VT-04 before expecting useful device discovery and VT-06 before claiming live-video success.

## 7. Configuration policy

- Environment variables use the `VIDEO_TESTKIT_` prefix.
- Defaults bind to loopback and safe non-privileged ports.
- Tests allocate dynamic ports; examples may use fixed local ports.
- Secrets are optional only in explicit local-development mode.
- External Provider, callback and ZLM addresses require explicit configuration and startup validation.
- Configuration errors fail before a scenario starts.

## 8. Test strategy

### Public-seam tests

- Provider behavior: raw HTTP and JSON assertions;
- scenario control: TestKit control HTTP;
- SIP: real UDP/TCP packets;
- media: real RTP packets and ZLM observations;
- events: controlled callback sink.

### Implementation-local tests

Use only where the public seam cannot efficiently isolate deterministic codec, digest, XML or packetization logic. They supplement but do not replace public-seam acceptance.

### Flake policy

- no arbitrary retry of failed assertions;
- all eventual assertions poll observable state until a short deadline;
- record seed, ports and state transition history on failure;
- a test that passes only after rerun is a defect;
- release gate requires three consecutive clean runs.

## 9. Security and compliance

- TestKit control endpoints bind to loopback by default and are not production interfaces.
- Service token support remains available for non-local environments.
- Digest passwords, Authorization values and callback tokens are redacted.
- Packet fixtures and video samples must include source, license and checksum.
- Do not publish captured customer SIP traffic or actual device credentials.
- Public repository status does not grant third-party code or media redistribution rights.

## 10. Release and verification gate

Every completed slice must provide:

- a public-seam acceptance test;
- one meaningful failure-path test;
- updated capability and README behavior;
- updated verification baseline when evidence changes;
- passing Ruff, format, mypy, pytest and build;
- clean-wheel startup for release-impacting changes;
- no unbounded tasks, sockets or temporary files after tests.

Version `1.0.0` is not reached until the machine-readable contract runner, Catalog, H.264 live media, RecordInfo/playback and release-grade reports are complete. Real-vendor compatibility remains a separate product acceptance gate even after TestKit `1.0.0`.

## 11. Explicit non-goals

- full GB/T 28181 certification suite;
- production media server or Provider;
- SmartFire business database simulation;
- browser multi-window rendering and performance tests;
- AI video analysis;
- long-term storage of test results inside TestKit;
- replacing physical IPC/NVR acceptance.

## 12. Instructions for a fresh implementation session

1. Read `AGENTS.md`, `CONTEXT.md`, ADRs and this plan.
2. Read the GitHub issue in full, including blockers and comments.
3. Verify the current baseline before changing behavior.
4. Work on one ready tracer-bullet issue only.
5. Write the public-seam failing test first.
6. Implement the minimum behavior to pass it.
7. Run targeted tests, then the full required verification.
8. Update documentation and evidence without claiming untested vendor compatibility.
9. Do not start a blocked slice or silently widen scope.
