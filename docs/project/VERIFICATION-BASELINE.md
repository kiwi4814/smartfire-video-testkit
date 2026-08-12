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
- device RecordInfo over real SIP MESSAGE (MANSCDP+xml, RecordInfo CmdType): Provider submits device-record-query, queries the device through its persistent UDP listener, aggregates multi-message responses by stable half-open time-range identity (no duplicates, out-of-order does not change result identity);
- RecordInfo scenario controls via /testkit/v1 (single/multi-message pagination, duplicate, delayed, missing, out-of-order, timeout, empty, malformed) with response progress counters, revision and `timeOffsetSeconds`; UTC/device-time offset is controllable and reproducible in reported items;
- device-record-query ACCEPTED/RUNNING then SUCCEEDED with stable opaque recordKeys bound to device/channel and correct half-open ranges (empty window returns successful empty items, PARTIAL preserves valid items with explicit status, timeout/malformed converge FAILED); repeated queries with the same Idempotency-Key reuse the same query without a second device query; recordKeys stay stable across repeated queries with different keys; channels are isolated;
- live-stream start returns `STREAMING` synchronously when ZLM integration is disabled; when enabled, a new stream returns `STARTING`, establishes the device-side Dialog through real SIP INVITE/ACK, and only transitions to `STREAMING` after the expected ZLM `rtp/{streamId}` is online;
- device-side UAS signaling scenarios via `/testkit/v1` (normal, rejection 486, delayed, no-ack, drop) with redacted Dialog diagnostics (call-id truncated, SSRC/media port/target observable only on the control interface);
- deterministic redistribution-safe H.264 fixture (1280×720, 25 fps, 1 second, SHA-256 recorded), MPEG-2 PS muxing with an H.264 Program Stream Map (`stream_type=0x1B`, PES `0xE0`) and RTP/UDP packetization with reproducible sequence/timestamp/marker/SSRC;
- controlled ZLM integration smoke through real RTP packets: normal media, negotiated SSRC enforcement, no-media, wrong-SSRC, deterministic post-warmup frame loss, Stop cleanup and double-reset orphan cleanup; ZLM identifies H.264 directly from the PSM without its malformed-PS codec-guess fallback;
- rejection and INVITE timeout converge the Provider live stream to FAILED (stable, observable via GET); no-ack leaves the device Dialog in a stable FAILED state with ackReceived=false;
- repeated DELETE on a live stream is idempotent 204 and tears down the device Dialog via BYE; failed scenarios, reset and process teardown leave no Dialog, socket, RTP port or ZLM stream behind;
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

Current automated suite: 128 tests collected from sixteen behavior modules. Without ZLM configuration, 121 pass and the 7 controlled ZLM integration tests skip. With `VIDEO_TESTKIT_ZLM_API_URL` and `VIDEO_TESTKIT_ZLM_API_SECRET`, all 7 ZLM tests pass against real UDP RTP/PS transport. The suite also starts real HTTP and UDP listeners and exercises contract conformance, registration, Keepalive, Catalog, RecordInfo, live signaling and media lifecycle behavior.

Local verification on 2026-08-12: required quality gates and build passed (`121 passed, 7 skipped`); with ZLM variables enabled the full suite passed `128/128`.

## Interpretation

This baseline proves Simulator Conformance for deterministic UDP H.264 RTP/PS, deterministic device RecordInfo over real SIP MESSAGE and the controlled ZLMediaKit environment. It does not prove GB28181 certification, physical-camera interoperability, H.265/audio/TCP media, device playback media or production performance.

## Unimplemented boundaries

- TCP SIP transport;
- optional TCP/H.265/audio media scenarios;
- device playback media (real RTP/PS playback; the Playback Stream mock interface already exists);
- signed Provider event callbacks;
- real-vendor compatibility matrix.
