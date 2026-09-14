# OStag 开发执行记录：A01

本记录与 81195d9b 的已评审方案分开保存，规划文档中的 planned 是当时快照。当前 A01 已完成本地验证并合入 integration；会议功能整体尚未完成。

## 本轮执行范围

用户要求使用 osstack 编码。当前仅执行 A01：会议/简报公用的来源与最小授权契约；真实连接器、简报生成、持久化、界面及部署依后续任务推进。

## 已验证基线

远端 main：`b73dc1d9709a4924b7928b8d7ebe9ef171720ff4`；方案：`81195d9bfc2bb4bacb8f47595e7d41310e4a7166`；Python 3.11.15，`uv sync --all-extras --locked --python 3.11`；1121 tests passed / 11 warnings / 26.48s；Ruff passed。

| 工作区 | 固定 HEAD | 开始时状态 |
| --- | --- | --- |
| OStag | `5098f9141864d413718d776c9ae783951ea4b5c1` | clean |
| OStag-console | `d2a09920604b860fda80db043685aec88b3f330f` | clean |
| OStag-company-m0 | `8b0dd3d92804a0b09660ec2d32f12e9ecca42768` | clean |
| OStag-pr-evidence | `f62b68a58725c99b8f35a0e55dfc85208612d68c` | clean |
| OStag-deploy-ingress | `1fb5a9028e4475645cf81108412389f0dd38c927` | clean |
| OStag-upgrade-plan | `81195d9bfc2bb4bacb8f47595e7d41310e4a7166` | clean |
| OStag-company-integration | `242feea80461a33f5df6b3bf66ac6021faa02164` | clean |

## osstack 调度边界

使用 0.19.0 CLI 的单轮、独立 git worktree 和外部固定 execute/verify 命令。DiscoveryPolicy 默认 blocked，仅 `ostag.a01` 可执行；官方授权函数预检为 1 可执行、13 blocked。旧 13 文件内容哈希固定，不移动、不删除。未使用 auto-agent、daemon 或无验证器的 noop 成功。

Codex builder 指定原用户配置中的 gpt-6-astra/high，采用 workspace-write；核查的是工作区与变更范围，未宣称全面主机/凭据隔离。Fable reviewer 使用真实 `claude-fable-5-1`、关闭工具/MCP/hooks，核验 modelUsage 和结果 JSON；审批绑定变更文件内容摘要，CLI exit 0 不等于通过。

## 运行前检查中的异常与处置

`osstack loop run --help` 不显示帮助而执行 run；已有任务均已有历史完成状态，本次未出现新任务执行时间，只刷新旧状态文件元数据。原代码及任务文件未变化，输出与刷新后的状态已留档。正确帮助命令是 `osstack loop help`。enqueue 的未知 `--task-id` 也被当作 prompt 文本；在启动前已纠正新任务 ID/prompt 并用原生授权函数重新核验。

首次基线命令早于依赖安装完成，因 pytest 尚未安装退出；等待 sync 完成后重跑取得上述完整通过结果。未将失败当作验证通过。

复验旧 M0 的 osstack adapter 负向探针：接口指纹匹配、模拟 dispatch_verified=true，预期 exit 1 / automatic_writes_allowed=false。探针只访问自己创建的临时 canary；它不证明真实 CLI 的完整沙箱边界。

## 尚未执行

release_check、容器测试、飞书真实读写、生产 health/SSH、容量与连接身份现场核查、生产部署。P00 中这部分可见性记录为 unknown/not_run，不作为生产放行证据。


## 本轮已交付

代码提交 `c740aa250567e45546df3c97af6b9ceeb253b05e`，位于 `codex/ostag-company-integration`。5 个产品/测试文件的最终内容摘要为 `dec4ee344ec3eb9a0c7a704a1dae1860f6cf19b9ac69111ca79b110392fb1c2f`。

| 验收项 | 实现与证据 |
| --- | --- |
| A01-AC1 | TimeWindow、SourceRef、SourceSet、BriefClaim 严格解析；规范 UTC 半开窗口；IANA 大小写和数据库异常修复；出网须显式授权 |
| A01-AC2 | PilotGrant 与平台身份/核心权限取交集；每次 authorize 从可信 lookup 重读，拒绝旧版本、撤销、过期和越界请求 |
| A01-AC3 | validate_claims 只接受冻结来源集中的 ID；事实、来源所述承诺、行动建议分开，不产生批准或执行状态 |

两个独立 osstack 编码阶段均先出现 RED 再通过目标测试。初版127项；时区负例新增64项。最终固定验证器：**191项目标测试通过，1312项全量测试通过，11条既有警告，Ruff通过**。全量耗时 21.41 秒。

编码 CLI 内的全量回归因沙箱缓存/文件写入限制曾失败，原日志保留；没有把它算作通过。外部固定验证器在相同代码上完成上述全量回归。合成契约 smoke 验证了来源绑定、建议不等于批准、伪造来源拒绝和撤销后再次调用拒绝；它没有调用模型生成会议材料，也不是平台验收。

## Fable 挑战、修复与复审

真实 `claude-fable-5-1` 初审通过，并提出时区大小写跨平台差异、tzdata I/O 异常泄漏两点。Codex 在 macOS 复现后，通过第二个 osstack任务增加缓存的精确时区注册表和 OSError 转换；没有扫描每条来源时重复遍历时区库。tzdata升级需要重启进程。

第二阶段两次 Fable 回复未给出合格 JSON，而是输出了拟执行的命令文本，因此验证器判失败、osstack未合入。没有执行那些文本，也没有把 CLI exit 0 当作审查通过。失败后原临时 worktree 被 osstack 清理；外部候选快照完整保留，第三个 loop 用摘要核对后恢复完全相同的2文件内容，再跑测试和带 JSON Schema 的纯静态 Fable审查。最终批准、blockers为空，代码才合入integration。

初审中的模型文字提到工具尝试；实际调用禁用了工具，记录num_turns=1，没有模型测试执行证据。所有测试结论均来自固定验证器的实际日志，Fable只作为静态评审。最终2个改动文件由复审覆盖，另外3个文件与初审代码逐字节一致；机器记录保存覆盖关系和摘要。

[初审回执](reviews/a01-fable-initial.json) · [时区复审回执](reviews/a01-fable-timezone.json) · [机器执行记录](execution.json)

完整本地日志、固定执行/验证脚本、候选快照与失败记录：

- [A01执行与基线](/Users/osborn/.codex/visualizations/2026/09/13/01a09aef-9838-72c3-b5fa-9e65457ce215/ostag-restart/coding-a01)
- [时区编码与失败审查记录](/Users/osborn/.codex/visualizations/2026/09/13/01a09aef-9838-72c3-b5fa-9e65457ce215/ostag-restart/coding-a01-timezone)
- [精确候选恢复与成功复审](/Users/osborn/.codex/visualizations/2026/09/13/01a09aef-9838-72c3-b5fa-9e65457ce215/ostag-restart/coding-a01-timezone-review)

本轮新建的3个原生任务文件归档到本地 tasks/archive/2026-09-13-company-assistant；旧13文件未改动。不要使用裸 `osstack loop run` 启动旧队列。之后为已满足依赖的单切片生成专属 capability、默认拒绝的 discovery policy、固定非空验证器和独立状态路径；不依赖 --cycles 1 作为单任务隔离。

## 接续开发

下一检查点按 M01、A02、A03 顺序推进：固定会议项目/群/仓库与时间范围；实现独立 briefs 状态库及恢复；接通有来源的草稿生成。随后 A04 才形成可演示的任务纵向链路。M01的范围预检、A04的来源授权绑定必须继续检查每个 SourceRef 的 grant_id/revision/resource_id 与作业及当前grant一致，不能因为 SourceSet 接受多份来源就略过逐来源授权；多源撤销使整版失效。

规划 TASKS.md/backlog.json 保留为已评审的计划快照，不自动改作 osstack队列；实际进度以本文件与execution.json为准。A11/容器验收还须确认目标镜像的时区注册表非空、包含试点配置所选时区；本轮不把主机测试等同于容器验收。通用无人值守runner准入仍未完成；本轮证明的是用户授权下的受控本地开发闭环。真实连接、部署和发布继续执行原VALIDATION/DEPLOYMENT中的门槛。


## M01 实施中：首次评审与修复

已先冻结 MEETING-EVALUATION.md（f471737）：18开发用例与6保留用例的责任、评分和门槛明确；私人保留输入尚未创建或运行。M01首版139项目标测试、1451项全量测试、Ruff通过，Fable真实静态评审要求修改3项：执行时限从受理而非首次claim计算；finished布尔值可放行超时结果；时区构造异常未统一转SchemaError。首版未合入integration。

原生osstack只将截断的评审摘要传给第二轮builder，故保留代码后主动终止该轮。又发现原生runner在execute失败后仍启动verify，已停止该次验证及其未完成review；没有接受结果。原loop记录escalated。修复后的固定harness直接读取完整Fable回执，并要求最新builder成功回执与候选摘要匹配才允许verify。新修复任务从首次被评审候选逐字节恢复，使用首次claim作为时限起点，并重新走Codex→测试→Fable；当前尚未宣称M01完成。

[首次Fable阻塞回执](reviews/m01-fable-initial-blockers.json) · [首次loop与中断记录](/Users/osborn/.codex/visualizations/2026/09/13/01a09aef-9838-72c3-b5fa-9e65457ce215/ostag-restart/coding-m01)
