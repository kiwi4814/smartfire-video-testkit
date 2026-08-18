# smartfire-video-testkit

SmartFire 视频测试套件：**Fake Video Provider** + **GB28181 Device Simulator** 的第一条纵向切片。

共同契约见 `smartfire-repo/docs/project/SMARTFIRE-VIDEO-PROVIDER-CONTRACT-BASELINE.md`（`1.0.0-draft.1`）。
本仓库不修改 `smartfire-repo`、`smartfire-device-simulator`、`smartfire-sipgo`，也不依赖 WVP / Gateway；ZLM 仅用于显式启用的 VT-06 集成冒烟。

项目开发入口：

- [完整实施计划](docs/project/SMARTFIRE-VIDEO-TESTKIT-IMPLEMENTATION-PLAN.md)
- [当前验证基线](docs/project/VERIFICATION-BASELINE.md)
- [ZLM 集成环境（VT-06 依赖，端口/Secret/验证命令）](docs/project/ZLM-INTEGRATION.md)
- [统一术语](CONTEXT.md)
- [架构决策](docs/adr/)
- [AI 开发规则](AGENTS.md)

## 本切片边界

**已实现（Provider，`/provider/v1`）**

- `GET /health/live`、`GET /health/ready`、`GET /info`、`GET /capabilities`
- `GET /devices`、`GET /devices/{id}`、`GET /devices/{id}/channels`、`GET /devices/{id}/status`（分页/过滤/稳定排序）
- `POST /devices/{id}/catalog-syncs` + `GET /catalog-syncs/{operationId}`（异步操作机；通过真实 SIP Catalog 查询发现目录，SUCCEEDED/PARTIAL 含 discoveredCount，非破坏性 reconcile）
- `POST/GET/DELETE /live-streams`（复用返回 200、新建返回 201、DELETE 幂等 204；未配置 ZLM 时保持 Fake Provider 即时 `STREAMING`，配置 ZLM 时先返回 `STARTING`，经真实 SIP INVITE/ACK 和 RTP/PS 到达 ZLM 后才收敛为 `STREAMING`）
- `POST/GET /device-record-queries`（异步操作机；通过真实 SIP RecordInfo 查询设备录像目录，SUCCEEDED/PARTIAL/FAILED，空窗口成功返回空 items，recordKey 稳定不透明且绑定左闭右开区间）
- `POST/GET/DELETE /playback-streams`（复用 200、新建 201、DELETE 幂等 204；经 `s=Playback` SIP INVITE/ACK 和 RTP/PS 推流到达 ZLM 收敛为 `STREAMING`；mismatch / not_found 错误校验）
- 统一 envelope `{requestId, data}` / `{requestId, error}`、稳定错误码、`Idempotency-Key`（缺失 400、复用冲突 409）
- Provider 事件 outbox（CATALOG_CHANGED 等）+ 可选回调投递（httpx，有界重试）

**已实现（GB28181 Device Simulator + Fake Registrar，`/testkit/v1`）**

- 内置场景：1 台 4 通道 NVR + 1 台 IPC（`reset` 可确定性复位）
- `POST /devices/{id}/register` 触发真实 UDP REGISTER：`401 Digest(MD5, qop=auth)` → Authorization → `200 OK`
- 有界超时；状态与最后错误可查（`GET /devices/{id}/status`）
- 内置 Fake SIP Registrar（UDP）：请求日志与注册表可通过控制面查看；Provider 侧可向设备发起 Catalog 查询并聚合多消息响应
- 设备常驻 UDP 监听：接收 Provider 的 Catalog 查询 MESSAGE，按可编排场景响应（normal/multi/duplicate/delayed/missing/malformed/out-of-order/timeout，`POST/GET /devices/{id}/catalog`）
- 设备侧录像目录（RecordInfo）：响应 Provider 的 RecordInfo 查询 MESSAGE，按可编排场景返回录像 XML（normal/multi/duplicate/delayed/missing/out-of-order/timeout/empty，`POST/GET /devices/{id}/recordinfo`，支持 `timeOffsetSeconds` 控制设备本地时间偏移）
- 设备侧实时流与回放流 UAS：INVITE(`s=Play`/`s=Playback`)/SDP/200/ACK/BYE Dialog 生命周期，按独立场景应答（`POST/GET /devices/{id}/live` 与 `POST/GET /devices/{id}/playback`）并通过 UDP 发送确定性 H.264 RTP/PS（normal/no-media/wrong-ssrc/lossy/stop-after）；Dialog、SSRC、媒体目标与发送统计经 `/testkit/v1` 脱敏诊断观察
- 设备在线状态注入（`POST /devices/{id}/status`）、就绪状态注入（`POST /ready`）
- **VT-09 可选能力包**（每项独立编排，H.264/UDP 基线不变；无效取值一律 400，不静默降级）：
  - H.265：`codec: "H265"` 编排 H.265 fixture（libx265 合成，PSM `stream_type=0x24`）推流，SDP 以 `H265/90000` 协商
  - 音频：`hasAudio: true` 在视频 PS 中附带 G.711A 音频 PES（`0xC0`，PSM `stream_type=0x90`），可与 H.264/H.265 组合
  - SIP over TCP：`transport: "TCP"` 后 Provider 经真实 TCP（Content-Length 分帧）发送 INVITE/ACK/BYE
  - RTP over TCP：`mediaTransport: "TCP"` 后设备主动连接媒体端点并以 GB28181 4 字节长度头推流（ZLM 侧 `tcp_mode=1`）
- **Provider 事件投递与对账（VT-11）**：
  - 进程级 `providerEpoch`（UUID）经 `/info` 与事件 payload 暴露；事件回调独立 Bearer token（不进入 payload/日志），`401/403` 不盲目重试、`5xx` 有界退避重试并复用同一 `eventId`
  - Callback Sink（`/testkit/v1/events/sink/*`）：运行态配置投递 URL/token、脚本化 2xx/401/403/500/延迟响应、按 `providerInstanceCode + eventId` 幂等去重、同 epoch/resource 内 revision 乱序可观察、reset 全量清理
  - inventory 对账：`/provider/v1/devices` 与通道分页返回 `snapshotToken`（绑定目录指纹），续页回传同一 token，目录变化/未知 token → `409 VIDEO_CATALOG_SNAPSHOT_EXPIRED`（retryable，整轮重启）；漏事件后全量快照对账以 Provider 目录为事实源收敛
- **发布级 Conformance 报告（VT-10）**：`video-testkit conformance` 输出 JSON + JUnit XML + 简洁 Markdown 三份报告（contract/checksum、Provider、implementation、scenario、seed 标识；mandatory 与 capability-gated 分开统计；失败带脱敏 HTTP 证据引用——token/敏感头绝不落盘；结论固定声明 Simulator Conformance、不推断 Vendor Compatibility）；`--runs N` 支持发布 Gate（N≥3 次连续干净运行零失败）

**未实现（后续切片）**：Provider 事件回调的签名认证、多实例/持久化、OpenAPI 正式发布。

## 快速开始

前置：Python ≥ 3.11（本机 3.12），建议使用 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync        # 生成 .venv 与 uv.lock（含 dev 依赖组）
uv run video-testkit --port 8000
```

不使用 uv 的 pip/venv 备选：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
video-testkit --port 8000
```

启动后：

- Provider：`http://127.0.0.1:8000/provider/v1/`
- 控制面：`http://127.0.0.1:8000/testkit/v1/`
- 内置 Registrar：UDP `127.0.0.1:15060`（仅环回）

## 外部 WVP Provider 验收

本仓库没有 `justfile`；TestKit 的开发、单元测试和外部控制面均使用 `uv`。内置 Fake Provider 的 `/provider/v1` 测试与外部 WVP Provider 验收是不同路径，前者不能证明 WVP 的 SIP、PostgreSQL 或 ZLMediaKit 行为。

外部 WVP 验收前置条件：

1. 单独启动 WVP Provider、TestKit 和受控 ZLMediaKit；确保 TestKit 控制面、WVP HTTP/SIP 和 ZLM RTP 端口没有上一轮残留进程占用。
2. 将 `VIDEO_TESTKIT_GB_REGISTRAR_ADDR` 指向 WVP SIP 地址，并使 `VIDEO_TESTKIT_GB_PASSWORD` 与 WVP 的 `SMARTFIRE_WVP_SIP_PASSWORD` 一致；密码只从各自本地未跟踪环境读取。
3. 通过 `POST /testkit/v1/devices/{deviceId}/register` 触发 REGISTER，轮询状态确认注册成功。
4. 通过 `POST /testkit/v1/devices/{deviceId}/keepalive/start` 启动 Keepalive，确认 WVP 设备状态为 `ONLINE`。
5. 设备在线后再触发 WVP Catalog sync；随后才运行 VT-05、VT-06 或 `video-testkit conformance`。

`POST /testkit/v1/reset` 只清理 TestKit 模拟器、Dialog、编排场景和 callback sink，不会清理 WVP PostgreSQL 技术投影、WVP Redis、Flyway 历史或 ZLM 媒体。WVP 的 `just verify-infrastructure` clean gate 必须在 TestKit REGISTER 之前执行；如果验收已经写入 `wvp_device`，应使用隔离数据库或经过授权的 WVP 技术投影清理，不要把 reset 当成数据库清理。

外部 Contract Runner 只验证被测 Provider 的公共 HTTP seam，不会替 Provider 完成 REGISTER、Keepalive 或 Catalog。每轮结束后停止 TestKit/WVP，并用 ZLM `getMediaList` 确认没有残留流；手工调用 Provider WebHook 只能证明 WebHook seam 的处理逻辑，不能证明 ZLM 自动回调已配置。

## 环境变量（前缀 `VIDEO_TESTKIT_`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `8000` | HTTP 监听 |
| `AUTH_TOKEN` | 空 | 设置后启用 `Bearer` 认证（生产必须设置；空=显式关闭开发认证） |
| `REGISTRAR_ENABLED` / `REGISTRAR_HOST` / `REGISTRAR_PORT` | `true` / `127.0.0.1` / `15060` | 内置 Fake Registrar |
| `GB_REGISTRAR_ADDR` | 空 | 设备注册目标，留空使用内置 Registrar |
| `GB_PASSWORD` / `GB_REALM` / `GB_EXPIRES` / `GB_REGISTER_TIMEOUT` | `12345678` / `3402000000` / `3600` / `3.0` | Digest 参数与有界超时 |
| `EVENTS_CALLBACK_URL` / `EVENTS_MAX_ATTEMPTS` / `EVENTS_RETRY_BASE_DELAY` | 空 / `3` / `0.1` | 事件回调与重试 |
| `MEDIA_BASE_URL` | `http://127.0.0.1:8080` | Fake 媒体引用基础地址 |
| `ZLM_API_URL` / `ZLM_API_SECRET` | 空 / 空 | 留空关闭 ZLM 集成；配置后 live stream 需等待 ZLM stream-online |
| `ZLM_RTP_HOST` / `ZLM_RTP_PORT_RANGE` / `ZLM_MEDIA_SERVER_ID` | `127.0.0.1` / `21001-21036` / `testkit-zlm-01` | ZLM RTP 接收地址、端口范围与媒体服务器标识 |
| `GB_MEDIA_FPS` / `GB_MEDIA_MTU` | `25` / `1200` | 确定性 H.264 RTP/PS 帧率与 RTP payload 上限 |
| `GB_RECORDINFO_QUERY_TIMEOUT` / `GB_RECORDINFO_SETTLE_WINDOW` | `2.0` / `0.4` | Provider 侧 RecordInfo 查询总超时与响应聚合收尾窗口（秒） |

启动校验：地址格式、端口范围、注册目标自洽、token 长度；Registrar UDP 绑定失败即启动失败。

## 控制示例

```bash
# 复位到内置场景
curl -X POST http://127.0.0.1:8000/testkit/v1/reset

# 触发 NVR 的 SIP 注册（后台执行），随后轮询状态
curl -X POST http://127.0.0.1:8000/testkit/v1/devices/34020000001320000001/register
curl http://127.0.0.1:8000/testkit/v1/devices/34020000001320000001/status

# 查看 Registrar 收到的请求（应看到 401 挑战与鉴权 REGISTER）
curl http://127.0.0.1:8000/testkit/v1/sip/registrar/requests

# 编排 NVR 目录为分页（每条 2 通道）后，发起 catalog-sync 走真实 SIP 发现
curl -X POST -H 'Content-Type: application/json' \
  -d '{"mode":"multi","pageSize":2}' \
  http://127.0.0.1:8000/testkit/v1/devices/34020000001320000001/catalog
curl -X POST -H "Idempotency-Key: $(uuidgen)" \
  http://127.0.0.1:8000/provider/v1/devices/34020000001320000001/catalog-syncs
curl http://127.0.0.1:8000/testkit/v1/devices/34020000001320000001/catalog

# 编排 NVR 录像目录为分页（每条 2 条）后，发起 device-record-query 走真实 SIP RecordInfo
curl -X POST -H 'Content-Type: application/json' \
  -d '{"mode":"multi","pageSize":2}' \
  http://127.0.0.1:8000/testkit/v1/devices/34020000001320000001/recordinfo
curl -X POST -H "Idempotency-Key: $(uuidgen)" -H 'Content-Type: application/json' \
  -d '{"externalDeviceId":"34020000001320000001","externalChannelId":"34020000001310000001","startTime":"2026-08-01T00:00:00.000Z","endTime":"2026-08-01T02:30:00.000Z","recordType":"ALL"}' \
  http://127.0.0.1:8000/provider/v1/device-record-queries
curl http://127.0.0.1:8000/testkit/v1/devices/34020000001320000001/recordinfo
```

Provider 契约示例（写操作必须带 `Idempotency-Key`）：

```bash
BASE=http://127.0.0.1:8000/provider/v1

curl $BASE/health/ready
curl "$BASE/devices?page=1&pageSize=100"
curl "$BASE/devices/34020000001320000001/channels"

curl -X POST -H "Idempotency-Key: $(uuidgen)" \
  -H 'Content-Type: application/json' \
  -d '{"externalDeviceId":"34020000001320000001","externalChannelId":"34020000001310000001","streamProfile":"AUTO"}' \
  $BASE/live-streams
```

## 开发校验

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest
uv build
```

## 后续工作

- Playback：复用 RTP/PS 媒体能力发送确定性设备录像
- 可选媒体：TCP、H.265 和音频能力场景
