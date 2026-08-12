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
- playback stream start establishes device-side Dialog via real SIP INVITE (`s=Playback`), SDP offer/answer, and sends deterministic H.264 RTP/PS media to ZLMediaKit; `s=Playback` SDP session name cleanly isolates Playback scenarios from Live scenarios;
- playback error mapping validates `VIDEO_RECORD_NOT_FOUND` (404), `VIDEO_RECORD_MISMATCH` (409) for device/channel/time range mismatch, and `VIDEO_DEVICE_OFFLINE` (422);
- playback scenario controls via `/testkit/v1/devices/{id}/playback` (normal, rejection 486, delayed, no-ack, drop, none, wrong-ssrc) with observable Dialog diagnostics and media stats;
- playback media ZLM integration smoke proves stream-online transition to STREAMING, `rtp/{streamId}` media online, no-media/wrong-SSRC FAILED convergence, DELETE BYE teardown, and double-reset orphan RTP port cleanup;
- VT-09 optional capability pack (each independently selectable, H.264/UDP baseline unchanged):
  - H.265 media scenario: redistribution-safe synthetic H.265 fixture (1280×720, 25 fps, 1 second, libx265, SHA-256 recorded; VPS/SPS/PPS/SEI/IDR structure), PS muxing with HEVC Program Stream Map (`stream_type=0x24`, PES `0xE0`, CRC-32/MPEG-2 recomputed), SDP negotiation via `a=rtpmap:98 H265/90000`, scene control `codec: "H265"` on `/testkit/v1` with media-sent observability; unknown codec values fail explicitly with 400 (no silent fallback);
  - G.711A audio scenario: redistribution-safe synthetic audio fixture (8000 Hz, mono, 1 second, SHA-256 recorded), per-frame audio PES (`0xC0`, PTS-synchronized) with PSM `stream_type=0x90` declaration (H.264 and H.265 combinations), scene control `hasAudio: true`, independent of codec selection;
  - SIP over TCP signaling: device persistent TCP listener with Content-Length framing, Provider UAC INVITE/ACK/BYE transactions over the same TCP connection, scene control `transport: "TCP"`; Dialog established/terminated, rejection→FAILED and drop→timeout FAILED all verified over real TCP packets; UDP signaling baseline unchanged;
  - RTP over TCP media: GB28181 4-byte framing (`0x24 0x00` + big-endian length) per RTP packet, device connects to the media endpoint (`mediaTransport: "TCP"`), bounded failure convergence when no endpoint listens, ZLM `openRtpServer` `tcp_mode=1` wiring when media transport is TCP; unknown media transport fails explicitly with 400;
  - capability declaration stays within the fixed 14-item `CapabilityCode` enum (contract authority); the supported codec/audio/transport sets are declared via the contract-permitted `constraints` object on `LIVE_STREAM` and `DEVICE_RECORD_PLAYBACK` (`codecs`, `audioCodecs`, `signalingTransports`, `mediaTransports`), keeping declarations consistent with actually executable scenarios;
- VT-11 Provider event delivery and inventory reconciliation:
  - `providerEpoch` (UUID) generated per process start, exposed on `/info` and carried in every event payload (contract `x-required-from-contract`);
  - event delivery uses an independent `Authorization: Bearer <token>` (token never enters payload/logs); `401/403` responses are not blindly retried (stable observable `no retry` marker), `5xx`/connection failures use bounded exponential backoff reusing the same `eventId`, and retries are bounded by `events_max_attempts`;
  - Callback Sink (`/testkit/v1/events/sink/*` control surface + real `/sink/provider-events` HTTP endpoint): dynamically configured URL/token at runtime, scriptable responses (2xx/401/403/500/delay) via `/events/sink/script`, at-least-once deduplication by `providerInstanceCode + eventId`, revision-order observability within the same epoch + resource (late stale events flagged `_outOfOrder`, duplicates flagged `_duplicate`), and full reset cleanup of received events/scripts;
  - inventory reconciliation: `/devices` and `/devices/{id}/channels` return a `snapshotToken` (contract-required) that binds the current catalog fingerprint (global change sequence + per-device/channel revisions); continuing pages echo the same token, unknown/stale tokens after catalog changes or reset return `409 VIDEO_CATALOG_SNAPSHOT_EXPIRED` (retryable, whole round must restart), device-level and channel-level rounds are independent;
  - dropped-event scenario (CT-EVT-005): with the sink scripted 500, new events stay FAILED in the outbox (never falsely marked delivered), and a full inventory snapshot round still converges on the Provider directory as source of truth without MISSING conclusions;
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

Current automated suite: 171 tests collected from behavior modules. Without ZLM configuration, 171 pass and the 12 controlled ZLM integration tests skip. With `VIDEO_TESTKIT_ZLM_API_URL` and `VIDEO_TESTKIT_ZLM_API_SECRET`, all 12 ZLM tests pass against real UDP RTP/PS transport (TCP-media ZLM smoke additionally requires a ZLM configured with TCP passive mode). The suite also starts real HTTP, UDP and TCP listeners and exercises contract conformance, registration, Keepalive, Catalog, RecordInfo, live/playback signaling, media lifecycle, Provider event callback delivery and inventory reconciliation behavior.

Local verification on 2026-08-12: required quality gates and build passed (`171 passed, 12 skipped`); with ZLM variables enabled the full suite passed `183/183`.

## Interpretation

This baseline proves Simulator Conformance for deterministic UDP H.264 RTP/PS, deterministic device RecordInfo over real SIP MESSAGE, deterministic device playback media over SIP/RTP/PS, the VT-09 optional capability pack (H.265, G.711A audio, SIP over TCP, RTP over TCP), Provider event delivery through the callback sink with epoch-aware revisions and bounded retry semantics, inventory snapshotToken reconciliation, and the controlled ZLMediaKit environment. It does not prove GB28181 certification, physical-camera interoperability, real-vendor compatibility or production performance.

## Unimplemented boundaries

- signed Provider event callbacks (Bearer-only v1 callbacks are validated; cryptographic signature verification is not part of the contract);
- real-vendor compatibility matrix;
- H.265/audio/TCP-media arrival verified against a real ZLMediaKit TCP passive-mode receiver (UDP arrival is proven in the baseline; TCP-media smoke is available on request with the matching ZLM configuration).
