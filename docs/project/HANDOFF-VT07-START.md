# Handoff: 下一个对话从 VT-07 开始

> 交接时间：2026-08-12。上一对话已完成 VT-06 并通过全部验证；下一对话直接开始 **VT-07（Return deterministic device RecordInfo results）**。
> 读此文件前先读：`AGENTS.md`（执行路由）、`docs/project/SMARTFIRE-VIDEO-TESTKIT-IMPLEMENTATION-PLAN.md`（VT-07 节）、`CONTEXT.md`、`.scratch/smartfire-video-testkit/index.md`。

## 1. 项目状态（已验证）

| 项 | 状态 |
|---|---|
| VT-01..VT-06 | **全部 done**，各有聚焦提交（最新 `bbbf4d9`） |
| 全局 frontier | **VT-07**（`planned`，本地 blocker VT-06 done + 跨项目 CONTRACT-04 done，均确认解除） |
| Git 工作区 | **干净**（无未提交改动） |
| 验证矩阵（VT-06 收尾时实测） | 带 ZLM env：`111 passed`；无 ZLM：`104 passed, 7 skipped`；ruff lint/format、mypy（34 files）、`uv build` 全绿 |
| Python | 3.13 为当前 venv；3.11 也已验证过全绿 |

## 2. 下一步任务：VT-07

- Issue：`.scratch/smartfire-video-testkit/issues/07-return-deterministic-device-recordinfo-results.md`（Status 当前 `blocked`，需改为 `in-progress` 后开工）
- 目标：通过真实 RecordInfo MESSAGE/XML 返回空、完整、多消息、重复、乱序、PARTIAL、timeout 的设备录像目录；`/testkit/v1` 安排夹具，真实 SIP MESSAGE 是协议 seam，`/provider/v1` Device Record Query HTTP 是结果 seam
- 验收 6 项：空窗口成功返回空 items；recordKey 不透明稳定且左闭右开区间正确；多消息聚合无重复/乱序不改变身份；PARTIAL/timeout 保留有效项；UTC/device-time offset 可控可复现；相同 Idempotency-Key 不创建第二个查询
- **Out of scope**：Playback 媒体、平台录像、Provider RecordInfo 实现

## 3. 现有代码关联（VT-07 实现起点）

- **Provider 侧已存在 mock 实现**：`service.py` `submit_record_query` / `_generate_records` / `get_record_query`（按小时确定性生成录像目录，`recordType` 仅 `ALL`/`TIME`），HTTP 路由在 `provider_api.py`。**VT-07 要把它从"直接生成"改为"走真实 RecordInfo SIP MESSAGE 与设备交互"**
- **设备侧可复用模式**（VT-04/05/06 已建好）：
  - `sip/catalog.py`：MANSCDP+xml 查询/响应编解码 + `CatalogClient` —— **RecordInfo XML 编解码可照此模式新建 `sip/recordinfo.py`**
  - `sip/simulator.py`：设备常驻 UDP 监听 + 场景编排（`LiveScenario`/`CatalogScenario` 模式）→ 新增 RecordInfo 应答场景（单/多消息/重复/乱序/PARTIAL/timeout）
  - `sip/registrar.py`：`query_catalog` 会话聚合模式 → 新增 `query_recordinfo` 或泛化
  - `service.py`：`_complete_catalog_sync` 的"真实 SIP 查询 + 非破坏性 reconcile"模式 → RecordInfo 查询同理
- **recordType 语义**：CONTRACT-04 已发布（Record Query 异步操作机、左闭右开、PARTIAL、幂等），实现时以现有 `service._generate_records` 的确定性输出作为设备侧夹具事实源对齐

## 4. 环境速查

- ZLM 容器 `zlm-testkit` 常驻（OrbStack）；API `http://127.0.0.1:23332`，secret `e504ea5f353ee1357fa6e7e28b3b6541`，RTP UDP `21000-21036`；详见 `docs/project/ZLM-INTEGRATION.md`
- VT-07 不需要 ZLM（RecordInfo 是 SIP MESSAGE 信令，无媒体）；ZLM 只在 VT-06/VT-08 集成冒烟用

## 5. 验证命令与注意事项

```bash
uv sync --locked
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest                      # 无 ZLM：104 passed, 7 skipped
# 带 ZLM 集成冒烟（VT-07 不强制，但回归时要跑）
VIDEO_TESTKIT_ZLM_API_URL=http://127.0.0.1:23332 \
VIDEO_TESTKIT_ZLM_API_SECRET=e504ea5f353ee1357fa6e7e28b3b6541 \
uv run pytest
uv build
```

注意：
- `tests/conftest.py` 的 `_base_settings()` **已显式 `zlm_api_url=""`**，普通 fixture 不会被环境变量污染；只有 `zlm_settings()` 专用 fixture 启用 ZLM
- VT-07 测试应走公共 seam（`/testkit/v1` 编排 + 真实 SIP UDP + `/provider/v1` 结果），不要调用私有方法；动态端口、有界轮询、reset 清理（沿用 `tests/conftest.py` 的 `ServerHandle`/`wait_until_value` 模式）
- 完成 VT-07 后：Issue 勾选验收 + 追加证据 + `done`，更新 `index.md`（frontier → VT-08）与 `VERIFICATION-BASELINE.md`，一个聚焦 commit（风格：`feat: ... (VT-07)`）

## 6. 本交接的清理动作

- 已删除过时的 `docs/project/HANDOFF-VT06-MEDIA.md`（VT-06 已完成）
- 本文件为下一对话的入口说明；VT-07 完成后删除或替换
