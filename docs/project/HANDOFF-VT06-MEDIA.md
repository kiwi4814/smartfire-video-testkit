# Handoff: VT-06（H.264 RTP/PS 到 ZLMediaKit）中途交接

> 交接时间：2026-08-12。由上一模型转交下一模型继续开发与测试。
> 上一模型在 ZLM 媒体识别环节受阻（见 §5 调试记录），其余实现已完成并通过无 ZLM 依赖的全部测试。
> **Resolved after handoff:** the remaining intermittent malformed-PS path was caused by a missing Program Stream Map. The final mux declares PES `0xE0` as H.264 (`stream_type=0x1B`) with a valid MPEG-2 CRC, so ZLM no longer relies on codec guessing. See the VT-06 local Issue and Verification Baseline for final evidence.

## 1. 交接状态概览

| 项 | 状态 |
|---|---|
| VT-06 Issue | `.scratch/smartfire-video-testkit/issues/06-...md`，Status: `in-progress` |
| 本地索引 | `.scratch/smartfire-video-testkit/index.md`（VT-06 in-progress，frontier 指向 VT-06） |
| Git | **未提交**：VT-06 全部改动在工作区（见 §6） |
| 测试 | `104 passed, 7 skipped`（7 skipped = ZLM 集成冒烟，未配环境变量） |
| 静态检查 | `mypy src` 全绿、`ruff check`/`format` 全绿（最后执行于交接前） |
| 环境 | ZLM 容器 `zlm-testkit` 运行中（OrbStack）；连接信息见 `docs/project/ZLM-INTEGRATION.md` |

## 2. 任务（VT-06 验收标准）

1. [x] fixture 记录来源、许可、checksum、codec、分辨率、duration —— 完成：`src/video_testkit/media/fixture.py` + `testkit-1s-720p.h264`（ffmpeg testsrc 合成，30KB，25 帧，SHA-256 已记录）
2. [x] UDP H.264/PS 基线的 sequence/timestamp/marker/SSRC 可重复 —— 完成：`src/video_testkit/media/rtp_ps.py`（单元测试 `tests/test_media_rtp_ps.py` 7 项全过）
3. [ ] **ZLM stream-online 后 Provider 才可报告 STREAMING** —— 代码已实现（ZLM 模式 STARTING→STREAMING），**被 §5 的媒体识别问题阻塞**（ZLM 收不到可识别流 → wait_stream_online 超时 → FAILED）
4. [x] no-media / wrong SSRC / packet loss / 中途停止场景 —— 设备侧已实现（`LiveScenario.media_mode`），no-media/wrong-ssrc 的 FAILED 路径在集成测试中已验证通过
5. [x] Stop/reset 释放 task、socket、stream、端口 —— 已实现（BYE 停推流、close_rtp_server 幂等、reset 清理）
6. [ ] teardown/finally 强制清理 orphan stream/RTP port、重复 reset 幂等 —— 已实现，**同样受 §5 阻塞**（正常推流场景无法完成）

## 3. 已完成实现（工作区，未提交）

- `src/video_testkit/media/`（新）：`fixture.py`（元数据+checksum 校验）、`rtp_ps.py`（PS mux + RTP 打包）
- `src/video_testkit/zlm_client.py`（新）：ZLM HTTP API（openRtpServer/closeRtpServer/isMediaOnline/getMediaList）
- `config.py`：ZLM 集成配置（`zlm_api_url` 空=关闭、`zlm_api_secret`、`zlm_rtp_host`、`zlm_rtp_port_range`、`zlm_media_server_id`、`zlm_stream_online_timeout`）+ 设备媒体参数（`gb_media_fps`/`gb_media_mtu`）
- `sip/registrar.py`：`invite_device` 支持 `sdp_media` 参数（offer 指向 ZLM RTP 端口）
- `sip/simulator.py`：设备 UAS 媒体推流（`_media_loop`，ACK 后启动、BYE/reset 取消）、`LiveScenario` 媒体场景字段、Dialog 诊断加媒体统计
- `service.py`：ZLM 模式 `_establish_live_stream`（openRtpServer→INVITE→wait_stream_online→STREAMING/FAILED，finally 关端口）、teardown 关 ZLM
- `testkit_api.py`：reset 强制清理 ZLM 端口、`LiveBody` 媒体场景字段
- `tests/conftest.py`：`zlm_server`/`zlm_client` fixture（session，配 `VIDEO_TESTKIT_ZLM_API_URL` 才启用，否则 skip）
- `tests/test_live_media_zlm.py`（新）：7 个集成冒烟（2 个失败路径已过，5 个正常推流场景被 §5 阻塞）
- `tests/test_media_rtp_ps.py`（新）：7 个单元测试全过

## 4. 运行 ZLM 集成测试

```bash
# 需要 ZLM 环境变量（本机已部署）
VIDEO_TESTKIT_ZLM_API_URL=http://127.0.0.1:23332 \
VIDEO_TESTKIT_ZLM_API_SECRET=e504ea5f353ee1357fa6e7e28b3b6541 \
uv run pytest tests/test_live_media_zlm.py -v

# 无环境变量时：该文件整体 skip（CI 安全）
uv run pytest tests/test_live_media_zlm.py
```

## 5. 阻塞问题：ZLM 不识别自生成 PS 流（重点调试记录）

**现象**：设备模拟器向 ZLM 的 `openRtpServer` 端口推流后，ZLM 日志 `judged to be PS` 但**不出流**（isMediaOnline=false，getMediaList 无 `rtp/{stream_id}`），Provider 等 online 超时 → FAILED。

**已排除**：
- RTP 传输链路 ✓（ZLM 收到包：`允许RTP推流，ssrc: 05F5E120`）
- openRtpServer 端口参数 ✓（ZLM 要求显式 `port`，已修复）
- isMediaOnline 查询参数 ✓（需 `vhost=__defaultVhost__`，已修复）
- SSRC 溢出 ✓（GB28181 y= 10 位十进制，改 `int(ssrc, 10) & 0xFFFFFFFF`）
- PES payload 内 Annex-B 起始码干扰 ✓（已去掉起始码，NAL 裸数据 + PES 长度定界，符合 GB28181）
- PS pack/system header ✓（已对齐 ffmpeg `mpeg2program`/`vob` 输出格式）

**当前状态（probe-v3 实验，最后执行的版本）**：
- 打包器（`rtp_ps.py`）：ffmpeg 格式 pack header + system header（含 0xE0 video 条目）+ ffmpeg 完整 PES 头（`80 81 09` + PTS + 4 字节 extension）+ **无起始码** NAL payload
- ZLM 日志：`允许RTP推流` → `judged to be PS` → **无 `Got track`、无解析错误、无流**
- 早期版本（带起始码 payload）：ZLM 报 `解析 ps 异常: bytes=947, Assertion failed: (0 == pkt->codecid), mpeg-packet.c:201`

**重要发现（对照实验）**：把 ffmpeg 生成的 `ref.mpg`（`ffmpeg -i testkit-1s-720p.h264 -c copy -f vob /tmp/ref.mpg`）**原始 2048 字节块**直接按 RTP 发送 → ZLM 日志出现 `Got track: H264`（识别轨道）但不出流（ffmpeg 的块把一帧切成多个 PES，IDR 不完整）。**这说明 ZLM 的 PS 解析对"每帧一个完整 PES + 无起始码 NAL"仍有未知要求**，下一步应对比 ffmpeg 块内 PES 与我们的 PES 的字节级差异。

**下一步建议**（按顺序）：
1. **对比字节**：用 ffmpeg 生成 ref.mpg（已有），逐块解析其 pack/PES 头与 `rtp_ps.py` 输出对比（重点：pack header 的 SCR 值、PES 头字节、PES_packet_length 语义、帧边界处理）。可参考 ZLM 源码 `3rdpart/media-server/libmpeg/source/mpeg-ps.c` 与 `mpeg-packet.c`（GitHub: ZLMediaKit/ZLMediaKit）
2. **尝试 pack header SCR 递增**：当前每帧 pack header 是固定字节（SCR 不变）。真实 GB28181 设备每帧更新 SCR。ffmpeg 输出也每块更新。可先把 SCR 编码按帧递增再测
3. **参考 wvp-pro 的 PSMuxer**（Java，GitHub: 648540858/wvp-GB28181-pro）—— 业界与 ZLM 互通验证过的 PS 封装实现，直接对照其 pack/PES 构造
4. 修复后跑 §4 的集成测试直至 7/7 通过，然后按 AGENTS.md 完成验证矩阵、更新基线、提交

**调试技巧**：
- ZLM 日志：`docker logs zlm-testkit --since 1m | grep -iE "probe-|解析|track"`
- ZLM API 实测（无 Python 依赖）：`curl "http://127.0.0.1:23332/index/api/openRtpServer?secret=...&stream_id=probe&port=21001&tcp_mode=0"`
- 手动推流实验脚本：在 `eval`/Python 中直接构造 UDP 发送（本交接不附代码，可参考 `tests/test_live_media_zlm.py` 与 zlm_client 用法）

## 6. Git 工作区（全部未提交，交接时状态）

```
 M src/video_testkit/app.py            # zlm_client 装配
 M src/video_testkit/config.py         # ZLM + 媒体配置
 M src/video_testkit/live_client.py    # establish 支持 sdp_media
 M src/video_testkit/service.py        # ZLM 模式状态机 + teardown
 M src/video_testkit/sip/registrar.py  # invite_device sdp_media
 M src/video_testkit/sip/simulator.py  # 设备推流 + 媒体场景
 M src/video_testkit/testkit_api.py    # reset 清理 + LiveBody
 M tests/conftest.py                   # zlm_server fixture
?? src/video_testkit/media/            # fixture + rtp_ps（新）
?? src/video_testkit/zlm_client.py     # ZLM API 客户端（新）
?? tests/test_live_media_zlm.py        # 集成冒烟（新）
?? tests/test_media_rtp_ps.py          # 单元测试（新）
```

## 7. 环境速查（详见 `docs/project/ZLM-INTEGRATION.md`）

- ZLM 容器：`zlm-testkit`（`zlmediakit/zlmediakit:master`），HTTP API `http://127.0.0.1:23332`，secret `e504ea5f353ee1357fa6e7e28b3b6541`
- RTP 端口：`21000` 固定 + `21001-21036` 范围（UDP）
- 配置落盘：`.zlm/config.ini`（gitignore）
- 本次调试额外用到的端口：21005-21011（openRtpServer 实验，均已 closeRtpServer 释放）

## 8. 完成后收尾（AGENTS.md 流程）

1. `uv sync --locked && uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest && uv build`（含 ZLM env 时跑集成冒烟）
2. 更新 `VERIFICATION-BASELINE.md`（测试计数、新行为、移除 "RTP/PS media generation and ZLM observation" 未实现项）
3. 更新 VT-06 Issue 证据 + `done`，更新 `index.md`（frontier → VT-08，注意 VT-08 依赖 VT-06/VT-07 + CONTRACT-04）
4. 更新 `README.md` 已实现列表
5. 一个聚焦 commit（参考过往风格：`feat: ... (VT-06)`）
