# OStag documentation / 文档导航

[English overview](../README.md) · [中文概览](../README.zh-CN.md)

Start with a guide for the task you want to perform. Product guides describe
behavior and prerequisites; plans and research describe intent or dated evidence.
A completed code change is not proof of production activation or business acceptance.

按使用目的选择文档。使用指南说明行为与前提，计划和研究记录设计或特定时间的证据；代码完成不等于生产启用或业务验收通过。

## Start here / 入门

| Need / 目的 | Guide / 文档 |
|---|---|
| Understand the system / 理解系统 | [Architecture / 架构](ostag/architecture.md) |
| Run and inspect tasks / 运行与查询任务 | [CLI reference](cli_reference.md) |
| Prepare a meeting locally / 本地会议准备 | [Local collection](ostag/meeting-collect.md), [synthetic preview](ostag/meeting-preview.md) |
| Configure the business model / 配置业务模型 | [DeepSeek profile and private key entry](ostag/deepseek.md) |
| Operate the task board / 使用任务看板 | [Work monitor](ostag/work_monitor.md), [scoped operational reports](ostag/operational_report.md) |
| Deploy and recover / 部署与恢复 | [Deployment](ostag/deployment.md), [host rollout](ostag/host-rollout.md) |
| Assess remaining work / 了解待完成工作 | [Delivery and acceptance plan](ostag/delivery-implementation-plan-2026-09-21.md) |

## Platforms and integrations

平台接入和执行器需分别配置；请查阅各连接器的能力声明，不要假定各平台功能完全一致。

- [Feishu / 飞书](ostag/feishu_setup.md), [Lark](ostag/lark_setup.md),
  [DingTalk / 钉钉](ostag/dingtalk_setup.md), [Slack](ostag/slack_setup.md),
  [Telegram](ostag/telegram_setup.md).
- [Connector capability matrix / 连接器能力](connector_capabilities.md),
  [GitHub](github_connector.md), [GitLab](gitlab_connector.md).
- [Executor conformance / 执行器检查](ostag/executor_conformance.md),
  [osstack execution board / 执行看板](ostag/osstack_execution_board.md).
- [Copilot relay operations / Relay 操作指南](operations/copilot-relay.md),
  [relay runbook / 运维手册](runbooks/copilot-relay.md).

## Governance and safety / 治理与安全

| Topic / 主题 | Reference / 参考 |
|---|---|
| Approval and writes / 审批与写入 | [Receipts](action_receipt_protocol.md), [governed writes](governed_writes.md), [action proposals](ostag/action-proposals.md) |
| Scope and budgets / 范围与预算 | [Channel policy](channel_policy.md), [cost budget](cost_budget.md) |
| Audit and follow-ups / 审计与跟进 | [Run ledger](run_ledger.md), [audit export](audit_export.md), [async runs](async_runs.md) |
| Ambient monitoring / 环境监测 | [Ambient V2](ambient_v2.md) |
| Security / 安全 | [Operational security](ostag/security.md), [governance design](security_governance.md) |

## Briefs, meetings and recovery / 简报、会议与恢复

These documents mix implemented contracts and explicit remaining gates. Read each
status section before using a design as a production procedure.

以下文档包含已实现契约和明确的待完成项，执行前必须确认其中的状态和前提。

- [Meeting preparation design](ostag/meeting-preparation.md),
  [PR closure evidence](ostag/meeting-pr-closures.md),
  [semantic evaluation](ostag/semantic-evaluation.md).
- [Ledger integration and rollout](ostag/ledger-integration-rollout.md),
  [persistent maintenance seal](ostag/maintenance-seal.md),
  [container stop evidence](ostag/container-stop-evidence.md).
- [Effect journal](ostag/effect-journal.md),
  [effect retention design](ostag/effect-retention-design.md).
- [Acceptance criteria](ostag/acceptance_criteria.md),
  [live validation](ostag/live_validation.md).

## Plans, research and history / 计划、研究与历史

- [Company assistant planning](plans/2026-09-13-company-assistant/): specifications,
  implementation notes, reviews and dated release evidence / 规格、实现记录、审核与阶段证据。
- [Design specifications](superpowers/specs/) and [implementation plans](superpowers/plans/):
  historical decisions and proposed changes / 设计决策与实施计划。
- [Upstream research](research/), [fork provenance](../FORK_ORIGIN.md),
  [migration report](fork_migration_report.md), [product strategy](claude_tag_strategy.md).
- [Release evidence and templates](../release/): distinguish completed receipts
  from empty templates or synthetic fixtures / 区分真实回执、空模板与合成样例。

Original files remain at their existing paths so historical links stay valid.
Do not interpret an old “next”, “pending”, or “done” as today's deployment state;
check current code, CI, immutable release receipts and the relevant acceptance evidence.

原文保留在原路径，避免破坏历史引用。旧文中的“下一步”“待办”“完成”不代表当前生产状态，应核对代码、CI、不可变发布回执和对应验收证据。

## Documentation maintenance

Use English `README.md` and Chinese `README.zh-CN.md` as matched entry points;
keep commands, feature status and safety boundaries consistent in both. Put
operational detail in the linked guide instead of duplicating it in the READMEs.
Date observed host facts, and keep session logs, local credentials and transient
reports outside repository instructions.

中英文 README 保持命令、功能状态与安全边界一致；具体操作集中在专题指南中。主机观察注明日期，会话日志、凭据与临时报告放在仓库指令文件之外。

```bash
osstack neat check --repo . --all --fail-on warning
```

Neat's mechanical coverage is not a semantic or production acceptance verdict.
Inspect its completeness and coverage fields. If safe patch planning is supported
by your installed version and repository configuration, preview `neat fix` before
applying it; manual content edits still need source and link verification.

本次整理以规则扫描、源代码核对和相对链接检查为依据；规则检查没有告警，并不证明所有历史文档已完成语义审核。
