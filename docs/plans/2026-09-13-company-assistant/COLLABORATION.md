# Codex、独立审计模型与 osstack 协作流程

> **历史记录提示：** 下文保留规划或各切片当时的状态，不代表当前阻断、模型分工或生产状态。最新已核实进展统一查看 [当前状态与证据](CURRENT-STATUS.md)；不要据旧记录重复认证、采集或修复已经完成的问题。

## 当前角色

2026-09-20 用户最新指定：Codex 编码，实际 Claude CLI `claude-opus-5` 独立审计。
必须保存 CLI 原始输出、modelUsage、最终候选摘要及测试回执；不能将历史
Kimi/Fable 结果改名，也不能仅凭 CLI 退出 0 宣称批准。以下原轮次记录保留原模型身份。


按用户最新要求“use codex for coding and kimi k3 for auditing and verfication”，Codex负责实现，Kimi K3负责独立审计和核查验证证据，osstack负责隔离执行与验收循环。本轮实际编码CLI请求gpt-6-astra；实际Kimi Code CLI请求kimi-code/k3，配置提供方managed:kimi-code、模型k3。记录请求标识与实际工具回执，不推测未返回的服务端模型身份，也不把替代模型称作Fable或Opus。

每个切片只有一名代码writer；迁移、配置、runner等共享入口不能并行修改。reviewer只读准确候选及必要上下文，提出具体反例；Codex以代码和可重复测试回应。本轮Kimi没有执行测试：固定验证器执行macOS/Linux回归，Kimi审查源码、断言和证据。完整范围与真实平台验收必须区分。

## 执行循环

冻结需求和验收条件 → Codex实现最小切片 → 固定目标/全量/独立检查 → Kimi审计准确最终候选 → 修复实际问题并重验 → 绑定源码摘要、测试和批准回执 → 本地集成。代码变化后不能沿用旧摘要的通过记录；仅修复审计读取或输出解析时，允许在逐字节确认同一候选后复用确定性测试证据，并保留工具故障历史。

评审结论包含candidate_sha256、verdict、blockers和notes。CLI退出0不代表批准。若JSON带说明文字或代码围栏，必须从原始输出中无歧义提取、校验结构、核对摘要，并保存原始输出；不能把一句“通过”改写成批准。读取工具截断时补齐准确材料，核查实际读取记录。A10材料覆盖44/44；A05恢复的46份材料在同会话补读清单后，通过实际返回内容的逐行等值检查。两次独立审计均批准且阻断项为0，见[A10回执](reviews/a10-kimi-k3-round-1.json)和[A05回执](reviews/a05-recovery-kimi-k3.json)。不能只凭Read工具成功状态认定完整读取，还须检查单行静默截断。

A11a 状态基础与 A11b 权限接线分别完成 29/29、27/27 份材料的逐行完整读取核对，两轮 Kimi 均批准、零阻断。A11b 一条非阻断建议由父验证器核对既有平台白名单并复现拒绝，原始审计回执保持不变，见 [核对记录](reviews/a11-authority-review-note.json)。

## 固定验证与证据

builder不能修改独立验收脚本、保留评测集或验收条件来过关。验证器自身需要修正时，由独立维护方说明原断言的问题并保存前后证据；A10中按Kimi挑战，将ACK测试从“删除全部按钮行”改为“旧按钮不能再导航私有内容”，保留有效对照和真实SQLite等待。

记录任务、基线、准确代码摘要、变更文件、命令/退出码、非空测试数量、环境、模型调用、审查和限制。所有失败保留，osstack清理工作树前先保存不可变代码快照。真实业务原文与凭据不进Git。模型审计、技术测试、真实平台回执、真人采用分别记录。

## osstack与仓库

本轮使用osstack0.19.0原生隔离循环和自定义execute/verify命令。策略只允许本次任务的能力；原13个历史任务仍在队列中，由默认blocked策略隔离，不能把40项规划直接批量入队。原生queue不替代本项目的依赖检查；每次只放行依赖已验收的切片，保留明确范围与输出状态。不要把cycles=1理解为只执行一个任务。

本地集成分支为codex/ostag-company-integration。osstack可以自动提交并快进该分支；这不等于GitHub main合并或生产发布。原OStag工作目录保持不动。开发PR、release PR、CI和生产部署继续遵循现有流程；squash发布后正常合回main，保留尚未发布工作，不reset或force-push。

运行前检查模型可用性、非空builder/verifier、改动范围、只读审查、准确摘要和失败拒绝。升级osstack或CLI时先看实际帮助/源码，不假设不存在的参数。本轮Kimi会话恢复需沿用已绑定agent，不能同时再次传agent-file；长文件同时按行数与字符数拆分。

## 历史与后续

2026-09-13的Fable规划讨论以及早期Fable/Opus代码审查保留其真实范围，见[规划审查](REVIEW.md)、[会议共创](MEETING-REVIEW.md)和[执行记录](EXECUTION.md)。额度耗尽、超时、格式错误和未完成读取均不计批准。最新模型选择替代此前固定Fable的要求。

用户已有开发授权按会话持续使用。A10与A05显式恢复已完成本地集成；下一步A11运行时接线，随后完成会议采集和真实平台验收。尚未push、main合并或部署，完整会议产品未宣告完成。


## P01 单次执行诊断

先复用 `8b0dd3d92804a0b09660ec2d32f12e9ecca42768` 的
`scripts/engineering/runner_probe.py` 与原测试，保留其安装源码摘要校验。
`--negative-control --surface osstack_codex_adapter` 只通过生成的假 CLI
验证实际安装的 0.19.0 适配器分派，预期退出 1；它既不调用模型，也不证明
CLI 沙箱行为。工作目录与 Git worktree 均不是操作系统安全边界。

新增固定情景诊断：

```bash
python scripts/engineering/runner_loop_probe.py \
  --osstack /opt/homebrew/bin/osstack \
  --output /absolute/new-diagnostic-directory \
  --scenario success
```

输出目录必须不存在，其父目录需预先存在。每次只使用新建的合成工作目录，
本项目原队列、旧 daemon、凭据和业务数据不会被复制或使用。0.19.0 原生
`loop enqueue` 创建一个任务；精确能力白名单拒绝优先级更高的旧任务哨兵。
执行固定命令、非空验证，限制为一轮一次尝试。除 `success` 外还必须运行
`builder_failure`、`verifier_failure`、`wrong_model`、`empty_verifier`、`timeout`。
验证 `completed_tasks` 只有指定任务、迭代数为 1、结果与情景匹配，检查非空
验证记录和旧任务 blocked 记录，而不是只看退出码。失败输出、状态与回执留在
独立目录，不覆盖已有结果。超时终止该诊断自己的进程组，包含忽略 TERM 的子进程。

这里的模型回执都是明示的合成测试数据。实际开发中，父验证器必须从受信 CLI
调用的原始输出读取模型元数据，并将它与请求模型、准确候选摘要、非空且通过的
测试证据绑定；不能把 builder 自写的 JSON 视为模型身份或审批。
`validate_verification` 只检查这些字段，不能验证证据来源或授权自动写入。
原生 loop 不可用时保留错误和候选，在受控 Codex 实现、固定测试、实际指定模型
独立审计的流程下继续；不得自行启动旧 daemon，也不得把诊断通过当成发布批准。

诊断子进程只接收固定 PATH／语言和新建目录下的 OSSTACK_HOME、XDG 配置与缓存、
临时目录、GitHub 配置目录。宿主凭据、NODE_OPTIONS 和原 osstack 环境设置不继承；
入队与执行都显式传入同一个 `--workspace`。这仍不是文件系统沙箱：回执不声称
审计过宿主所有仓库的变化，只记录实际队列哨兵摘要与原生调度结果。
