# Verification Baseline

## Baseline identity

- TestKit version: `0.1.0`
- Provider Contract: `1.0.0-draft.1`
- Verified on: 2026-08-10
- Python used for the current environment: 3.13
- Declared minimum Python: 3.11

## Verified behavior

- Fake Provider health, info, capabilities, device/channel paging, Catalog operation, live stream, device-record query and playback stream interfaces;
- request ID and success/error envelopes;
- service-token behavior when enabled;
- idempotency reuse and conflict behavior;
- deterministic reset, IPC and four-channel NVR scenarios;
- real UDP REGISTER, 401 Digest MD5 with `qop=auth`, authenticated REGISTER and 200 response;
- wrong-password and no-response timeout paths;
- wheel and source-distribution build;
- clean wheel installation and installed CLI startup;
- installed `/health/live=UP` and `/health/ready=READY` responses.

## Verification commands

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
uv build
```

Current automated suite: 52 tests collected from eight behavior modules. Tests start real HTTP and UDP listeners on dynamically selected local ports.

## Interpretation

This baseline proves Simulator Conformance for the implemented slice. It does not prove GB28181 certification, physical-camera interoperability, RTP/PS correctness, ZLMediaKit interoperability, H.264/H.265 playback or production performance.

## Unimplemented boundaries

- REGISTER refresh and unregister;
- Keepalive MESSAGE;
- Catalog MESSAGE request/response and partial/multi-message behavior;
- TCP SIP transport;
- INVITE/ACK/BYE as a simulated device;
- RTP/PS media generation and ZLM observation;
- device RecordInfo and playback media;
- external Provider black-box execution and machine-readable OpenAPI validation;
- signed Provider event callbacks;
- real-vendor compatibility matrix.
