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
- `POST/GET /device-record-queries`（按小时确定性生成录像目录，`recordType` 仅支持 `ALL`/`TIME`）
- `POST/GET/DELETE /playback-streams`（`recordKey` 优先，`VIDEO_RECORD_MISMATCH` 校验）
- 统一 envelope `{requestId, data}` / `{requestId, error}`、稳定错误码、`Idempotency-Key`（缺失 400、复用冲突 409）
- Provider 事件 outbox（CATALOG_CHANGED 等）+ 可选回调投递（httpx，有界重试）

**已实现（GB28181 Device Simulator + Fake Registrar，`/testkit/v1`）**

- 内置场景：1 台 4 通道 NVR + 1 台 IPC（`reset` 可确定性复位）
- `POST /devices/{id}/register` 触发真实 UDP REGISTER：`401 Digest(MD5, qop=auth)` → Authorization → `200 OK`
- 有界超时；状态与最后错误可查（`GET /devices/{id}/status`）
- 内置 Fake SIP Registrar（UDP）：请求日志与注册表可通过控制面查看；Provider 侧可向设备发起 Catalog 查询并聚合多消息响应
- 设备常驻 UDP 监听：接收 Provider 的 Catalog 查询 MESSAGE，按可编排场景响应（normal/multi/duplicate/delayed/missing/malformed/out-of-order/timeout，`POST/GET /devices/{id}/catalog`）
- 设备侧实时流 UAS：INVITE/SDP/200/ACK/BYE Dialog 生命周期，按场景应答并通过 UDP 发送确定性 H.264 RTP/PS（normal/no-media/wrong-ssrc/lossy/stop-after）；Dialog、SSRC、媒体目标与发送统计经 `/testkit/v1` 脱敏诊断观察
- 设备在线状态注入（`POST /devices/{id}/status`）、就绪状态注入（`POST /ready`）

**未实现（后续切片）**：设备真实 RecordInfo 查询、H.265/TCP/音频媒体、
Provider 事件回调的签名认证、多实例/持久化、OpenAPI 正式发布。

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

- RecordInfo：设备侧录像目录查询的 SIP 实现，替代内存确定性生成
- Playback：复用 RTP/PS 媒体能力发送确定性设备录像
- 可选媒体：TCP、H.265 和音频能力场景
