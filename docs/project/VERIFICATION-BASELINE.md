# Verification Baseline

## Baseline identity

- TestKit version: `0.1.0`
- Provider Contract: `1.0.0-draft.1`
- Verified on: 2026-08-12
- Python used for the current environment: 3.13 (full suite also green on 3.11)
- Declared minimum Python: 3.11

## Verified behavior

- Fake Provider health, info, capabilities, device/channel paging, Catalog operation, live stream, device-record query and playback stream interfaces;
- request ID and success/error envelopes;
- service-token behavior when enabled;
- idempotency reuse and conflict behavior;
- deterministic reset, IPC and four-channel NVR scenarios;
- real UDP REGISTER, 401 Digest MD5 with `qop=auth`, authenticated REGISTER and 200 response;
- wrong-password and no-response timeout paths;
- REGISTER refresh before expiry without duplicate Protocol Source Identity (bounded short expiry);
- `Expires: 0` unregister forming observable offline state (Simulator, Registrar registry and Provider status);
- Provider/TestKit restart (reset) followed by deterministic re-registration with the same identity;
- stale nonce challenge auto-retry, wrong realm rejection, duplicate and delayed UDP responses;
- Keepalive MESSAGE driving ONLINE/OFFLINE convergence (valid XML, ordered SN, MANSCDP+xml);
- normal/paused/dropped/malformed Keepalive scenarios controllable via /testkit/v1 control interface;
- Catalog discovery over real SIP MESSAGE (MANSCDP+xml): Provider submits catalog-sync, queries the device through its persistent UDP listener, aggregates multi-message responses by stable Device/Channel ID;
- Catalog scenario controls via /testkit/v1 (single/multi-message pagination, duplicate, delayed, missing, malformed, out-of-order, timeout) with response progress counters and revision;
- catalog-sync SUCCEEDED for complete catalogs (IPC 1 channel, NVR 4 channels with stable GB IDs), PARTIAL preserving valid items with deterministic discoveredCount for missing responses, FAILED for timeout/malformed;
- repeated Catalog idempotent (no duplicate resources); non-destructive reconcile keeps previously discovered channels when a response omits items (missing channel reappears with the same Protocol Source Identity);
- live-stream start returns STREAMING synchronously while a background real SIP INVITE establishes the device-side Dialog (SDP offer/answer, 200 OK, ACK → ESTABLISHED; BYE → TERMINATED);
- device-side UAS signaling scenarios via /testkit/v1 (normal, rejection 486, delayed, no-ack, drop) with redacted Dialog diagnostics (call-id truncated, SSRC/media port/target observable only on the control interface);
- rejection and INVITE timeout converge the Provider live stream to FAILED (stable, observable via GET); no-ack leaves the device Dialog in a stable FAILED state with ackReceived=false;
- repeated DELETE on a live stream is idempotent 204 and tears down the device Dialog via BYE; failed scenarios and reset leave no Dialog, socket or port behind;
- machine-readable Provider Contract Bundle validation (version `1.0.0-draft.1`, SHA-256 integrity);
- black-box Provider Conformance Runner (`video-testkit conformance` CLI subcommand) targeting arbitrary Base URL/token;
- automated response envelope and payload Draft 2020-12 JSON Schema assertions against `openapi.yaml`;
- Machine-readable JSON summary and JUnit XML conformance reports with `operationId`, `requestId`, expected and actual error details;
- wheel and source-distribution build;
- clean wheel installation, installed CLI entrypoint `video-testkit` and `conformance` subcommand execution;
- installed `/health/live=UP` and `/health/ready=READY` responses.

## Verification commands

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
uv build
```

Current automated suite: 97 tests collected from thirteen behavior modules. Tests start real HTTP and UDP listeners on dynamically selected local ports, exercise the contract conformance runner, the registration lifecycle via UDP proxies, the Keepalive online/offline convergence via real SIP MESSAGE, Catalog discovery/pagination/partial/timeout scenarios via real SIP Catalog MESSAGE traffic, and live-stream signaling (INVITE/SDP/ACK/BYE Dialog lifecycle) over real SIP UDP.

GitHub Actions CI is green on push to `main` for Python 3.11/3.12/3.13 (run 31467232430, head `c5890e0`), covering lint, format, mypy, pytest and build. The preceding CI failure was a `tests/conftest.py` collection-time `ImportError` (`TypeVar` imported from `collections.abc`); fixed in `c5890e0` alongside Provider Contract alignment (camelCase aliases, idempotent stream stop).

## Interpretation

This baseline proves Simulator Conformance for the implemented slice. It does not prove GB28181 certification, physical-camera interoperability, RTP/PS correctness, ZLMediaKit interoperability, H.264/H.265 playback or production performance.

## Unimplemented boundaries

- TCP SIP transport;
- RTP/PS media generation and ZLM observation (VT-06);
- device RecordInfo and playback media;
- signed Provider event callbacks;
- real-vendor compatibility matrix.
