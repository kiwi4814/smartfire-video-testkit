# ZLMediaKit 集成环境（VT-06 依赖）

> 本文是 TestKit 后续会话的 **ZLM 环境事实源**：容器、端口、Secret、验证命令与接入约定。
> 变更环境后必须同步更新本文件，避免后续 Agent 使用过期信息。

## 状态

- **已部署并验证**：2026-08-12
- 运行方式：Docker（OrbStack），容器 `zlm-testkit`
- 镜像：`zlmediakit/zlmediakit:master`（官方 latest 标签不存在，网络曾抖动）
- 用途：VT-06（H.264 RTP/PS 媒体到达验证）的受控 ZLM；`openRtpServer`/`closeRtpServer`/`getMediaList` 已实测可用

## 连接信息

| 项 | 值 |
|---|---|
| HTTP API / HTTP-FLV | `http://127.0.0.1:23332` |
| API secret | `e504ea5f353ee1357fa6e7e28b3b6541` |
| RTSP | `rtsp://127.0.0.1:15554` |
| RTMP | `rtmp://127.0.0.1:11935` |
| RTP 接收（UDP） | `127.0.0.1:21000`（固定主端口）+ `21001-21036`（随机范围） |
| mediaServerId | `testkit-zlm-01` |

端口均为本机检测空闲后选取的冷门端口；容器映射：

```text
23332/tcp   HTTP API + HTTP-FLV 拉流
15554/tcp   RTSP
11935/tcp   RTMP
21000-21036/udp  RTP 接收（GB28181 推流）
```

## 配置与启动

config.ini 位于 **`.zlm/config.ini`**（已 gitignore，含 secret 不提交）。
容器由 `--restart unless-stopped` 守护；重启方式：

```bash
docker restart zlm-testkit        # 重启
docker rm -f zlm-testkit && docker run -d --name zlm-testkit --restart unless-stopped \
  -p 23332:23332 -p 11935:11935 -p 15554:15554 \
  -p 21000-21036:21000-21036/udp \
  -v "$PWD/.zlm/config.ini:/opt/media/conf/config.ini" \
  zlmediakit/zlmediakit:master    # 重建（须在仓库根目录）
```

config.ini 关键项：`[api] secret`、`[http] port=23332`、`[rtmp] port=11935`、
`[rtsp] port=15554`、`[rtp_proxy] port=21000 / port_range=21001-21036`、
`[general] mediaServerId=testkit-zlm-01`。

## 验证命令

```bash
SECRET=e504ea5f353ee1357fa6e7e28b3b6541
BASE=http://127.0.0.1:23332

# API 鉴权与存活
curl -s "$BASE/index/api/getMediaList?secret=$SECRET"          # {"code":0}

# RTP 端口动态开启/关闭（VT-06 用法）
curl -s "$BASE/index/api/openRtpServer?secret=$SECRET&port=21001&stream_id=probe&tcp_mode=0"   # {"code":0,"port":21001}
curl -s "$BASE/index/api/closeRtpServer?secret=$SECRET&stream_id=probe"                         # {"code":0,"hit":1}
```

注意：`openRtpServer` 只开启端口，`getMediaList` 要收到实际 RTP 媒体后才出现对应流
（`app/stream = rtp/{stream_id}`）——这正是 VT-06 "stream-online 证明媒体到达"的判据。

## TestKit 接入约定（VT-06 实现时落进 `config.py`）

环境变量（`VIDEO_TESTKIT_` 前缀）建议：

| 变量 | 本环境值 |
|---|---|
| `VIDEO_TESTKIT_ZLM_API_URL` | `http://127.0.0.1:23332` |
| `VIDEO_TESTKIT_ZLM_API_SECRET` | `e504ea5f353ee1357fa6e7e28b3b6541` |
| `VIDEO_TESTKIT_ZLM_RTP_HOST` | `127.0.0.1` |
| `VIDEO_TESTKIT_ZLM_RTP_PORT_RANGE` | `21001-21036` |
| `VIDEO_TESTKIT_ZLM_MEDIA_SERVER_ID` | `testkit-zlm-01` |
| `VIDEO_TESTKIT_MEDIA_BASE_URL` | `http://127.0.0.1:23332`（sources URL 基础） |

- 单元测试**不依赖** ZLM；媒体到达/清理冒烟仅在 `ZLM_API_URL` 显式配置时启用。
- secret 为本地开发生成值；任何共享/生产环境必须轮换。
