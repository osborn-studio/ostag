# OStag

[English](README.md) · [简体中文](README.zh-CN.md) · [文档导航](docs/README.md)

OStag 是可自行部署的人与智能体协作控制平台。它将工作群消息和 CLI 请求转换为有权限边界、可审批、可审计的任务，记录产物并支持持久化跟进；同时提供远程工作看板，以及结合 GitHub 变化和指定飞书聊天的本地会议准备能力。

## 能力与使用条件

| 能力 | 已有实现 | 配置与限制 |
|---|---|---|
| 群聊与 CLI 任务 | 事件适配、工作流路由、智能体后端、回复和运行账本 | 各平台和执行器需独立配置；默认 mock、dry-run |
| 受控操作 | 冻结提案、审批和执行回执、人员与频道权限、预算 | 受控写入需显式开启；各连接器支持范围不同 |
| 需求与跟进 | 飞书多维表格／钉钉表格工作流、持久化续办、仅生成提案的环境监测 | 需要连接器、权限和频道配置 |
| 远程工作看板 | 持久化任务看板、飞书／Telegram 投递、受回执控制的操作、可选权限范围报表 | 需要服务端、机器人和部署配置 |
| 本地会议准备 | 一个 GitHub 仓库与最多两个指定飞书群，按时间窗生成带引用和缺口的 Markdown／JSON | 使用本机已授权 CLI；不调用模型、不发送消息 |
| Brief／会议服务 | 已实现持久化、授权检查、版本和卡片处理、DeepSeek 传输通道 | 默认关闭；仍需可信生产接线与真实业务验收 |
| GitHub → Vercel 发布 | 绑定 PR 提交 SHA 的审核与生产发布工作流 | 独立选配功能，与 OStag 自身的腾讯云发布流程不同 |

代码实现、部署成功和业务验收是三个不同阶段。仅打开功能开关不能提供身份验证入口或授权。完整账本主机迁移与恢复、生产 Brief 功能启用、真实业务试点仍未完成。启用新服务能力前，请阅读[实施与验收计划](docs/ostag/delivery-implementation-plan-2026-09-21.md)和[主机实施指南](docs/ostag/host-rollout.md)。

## 快速开始

需要 Python 3.11+、Git 和 [uv](https://docs.astral.sh/uv/)。在仓库根目录运行：

```bash
git clone https://github.com/osborn-studio/ostag.git
cd ostag
uv sync --locked --extra dev
uv run python -m ostag.main --platform feishu --dry-run --event examples/feishu_event.json
```

新检出仓库默认使用 mock 后端，不向平台发送回复；可能生成本地审计文件和账本。需要自定义配置时，先阅读 `.env.example` 再创建自己的 `.env`，不要提交凭据。已有环境变量可能改变后端等运行行为。

通过同一条任务处理流程提交本地请求：

```bash
uv run ostag run --platform feishu --chat oc_demo --text "请总结项目下一步" --json
```

回复返回本地；平台投递需要配置实时适配器。参见 [CLI 参考](docs/cli_reference.md)和[平台接入导航](docs/README.md#platforms-and-integrations)。

## 本地准备会议

分别使用自己的账号完成 `gh` 和 `lark-cli` 授权。选择已结束、不超过 31 天的 UTC 时间窗，以及有权访问的明确群聊：

```bash
uv run ostag meeting collect \
  --repo osborn-studio/ostag \
  --start 2026-09-13T00:00:00Z --end 2026-09-20T00:00:00Z \
  --timezone Asia/Shanghai --goal '项目周会' \
  --chat-id oc_REPLACE_WITH_YOUR_CHAT_ID \
  --format markdown
```

请替换示例日期和群 ID；如需第二个群，再传一次 `--chat-id`，总数最多两个。结果输出到本地标准输出，不会自动保存或发送。资料缺失或采集不完整时，会标记缺口并返回退出码 3，不会声称获得完整历史。不传 `--chat-id` 可生成仅含 GitHub 的草稿，同时注明飞书资料缺口。目前按单仓库采集，不是整个组织的变化聚合。详情见[会议采集指南](docs/ostag/meeting-collect.md)。

## 模型与执行器

以下是独立的配置入口：

- **任务执行：** `OSTAG_BACKEND=mock|codex_cli|claude_cli|kimi_cli|openai_api`。需安装和配置所选执行器（`openai_api` 使用 `uv sync --locked --extra openai` 安装依赖）；Kimi 还需要功能门控和一致性检查。参见[执行器说明](docs/ostag/executor_conformance.md)。
- **Brief／会议生成：** DeepSeek 使用固定地址 `https://api.deepseek.com`、受保护的 Key 文件和显式可信服务接线；它不是 `OSTAG_BACKEND` 的 `deepseek` 选项。通过 `uv sync --locked --extra deepseek` 安装依赖，按照 [DeepSeek 配置指南](docs/ostag/deepseek.md)隐藏输入 Key，并可主动运行会产生 API 费用的合成连接测试。没有自动回退到 OpenAI。
- **开发与审核：** Codex、Claude CLI、osstack 是开发工具；它们的凭据不等于生产业务模型的凭据。

## 常用命令

```bash
uv run ostag runs list --json
uv run ostag status --run run_x --json
uv run ostag ledger export --run run_x --json
uv run ostag context export --run run_x --json
uv run ostag receipts list --pending --json
uv run ostag followups list --json
uv run ostag connector list --json
uv run ostag executor list --json
```

配置受控写入后，交互顺序为：提案 → `approve <ref>` → `apply <ref>`。审批绑定被冻结的内容；执行仍需通过人员权限、频道策略等适用门控。参见[回执协议](docs/action_receipt_protocol.md)、[受控写入](docs/governed_writes.md)和[频道策略](docs/channel_policy.md)。

## 架构

```text
平台事件／CLI 请求
  → 适配器 → 统一事件 → 任务运行器
  → 身份、策略与限定范围的上下文
  → 工作流／执行器 → 回复与产物
  → 回执、运行账本与审计

工作看板：签名事件 → 持久化看板 → 受控操作
本地会议采集：已授权 CLI → 证据与缺口 → 私有草稿
Brief 服务：显式可信接线 → 受保护的任务执行与私有投递
```

适配器负责平台通信，核心层负责治理与账本，连接器负责外部操作，工作流协调业务行为。MFS 检索与频道记忆提供可选的限定范围上下文。详见[架构说明](docs/ostag/architecture.md)和[文档导航](docs/README.md)。

## 开发与验证

完整发布检查还需要 ripgrep、Pandoc、SQLite CLI、GitHub CLI，以及带 Compose 的 Docker。会议集成检查需要 Pandoc。

```bash
uv sync --all-extras --locked
uv run ruff check ostag tests
uv run pytest
scripts/release_check.sh
```

安装 osstack 后，可独立运行仓库知识整理检查：

```bash
osstack neat check --repo . --all --fail-on warning
```

规则检查没有告警，不代表文档中的功能描述已经过实际运行或业务验收。详见[文档维护说明](docs/README.md#documentation-maintenance)。

## 部署与恢复

OStag 的自动部署流程以腾讯云轻量应用服务器为目标，在主分支 CI 通过后执行。Brief 服务的生产接线与完整账本主机迁移仍是尚未完成的独立门槛。本地安装服务端时需包含 `server` 扩展依赖（`uv sync --locked --extra server`）。请阅读[部署指南](docs/ostag/deployment.md)、[工作看板](docs/ostag/work_monitor.md)和[安全说明](docs/ostag/security.md)。主机已安装的发布脚本与配置哈希需要单独核验。

备份工具只将一个 SQLite 数据库复制并校验到**新的、未使用的路径**；即使传入 `--force`，也拒绝覆盖已有目标。它不是原地恢复或多库恢复控制器。健康检查、模型连通性检查不能代替迁移、回滚或业务验收证据。

## 来源与许可证

OStag 源自 `zilliztech/mfs` 的 open-tag skill，并参考、适配了 OpenTag 的相关设计。详见[分支来源](FORK_ORIGIN.md)、[上游研究](docs/research/)和[产品策略](docs/claude_tag_strategy.md)。这是独立项目，不是 Zilliz 官方产品。采用 [Apache-2.0](LICENSE) 许可证。
