# OStag 开发执行记录

最新：Opus5编码的监控重试修复已通过独立Opus复审并完成本地集成；[A10候选](A10-RETENTION.md)已通过测试，但Claude额度429没有提供审查结论，独立Codex代理正在只读复审。完整会议运行时与生产部署尚未完成。见[监控记录](MONITOR-REPLAY.md)。

## 本轮执行范围

用户要求使用 osstack 编码。首次切片执行 A01：会议/简报公用的来源与最小授权契约；真实连接器、简报生成、持久化、界面及部署依后续任务推进。

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


## A01 已交付

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

本轮已按 M01、A02、A03 顺序完成：固定会议项目/群/仓库与时间范围；实现独立 briefs 状态库及恢复；完成有来源的草稿生成基础。下一检查点 A04 将形成可演示的任务纵向链路。M01的范围预检、A04的来源授权绑定必须继续检查每个 SourceRef 的 grant_id/revision/resource_id 与作业及当前grant一致，不能因为 SourceSet 接受多份来源就略过逐来源授权；多源撤销使整版失效。

规划 TASKS.md/backlog.json 保留为已评审的计划快照，不自动改作 osstack队列；实际进度以本文件与execution.json为准。A11/容器验收还须确认目标镜像的时区注册表非空、包含试点配置所选时区；本轮不把主机测试等同于容器验收。通用无人值守runner准入仍未完成；本轮证明的是用户授权下的受控本地开发闭环。真实连接、部署和发布继续执行原VALIDATION/DEPLOYMENT中的门槛。


## M01 首次评审与修复经过（已关闭）

已先冻结 MEETING-EVALUATION.md（f471737）：18开发用例与6保留用例的责任、评分和门槛明确；私人保留输入尚未创建或运行。M01首版139项目标测试、1451项全量测试、Ruff通过，Fable真实静态评审要求修改3项：执行时限从受理而非首次claim计算；finished布尔值可放行超时结果；时区构造异常未统一转SchemaError。首版未合入integration。

原生osstack只将截断的评审摘要传给第二轮builder，故保留代码后主动终止该轮。又发现原生runner在execute失败后仍启动verify，已停止该次验证及其未完成review；没有接受结果。原loop记录escalated。修复后的固定harness直接读取完整Fable回执，并要求最新builder成功回执与候选摘要匹配才允许verify。新修复任务从首次被评审候选逐字节恢复，使用首次claim作为时限起点，并重新走Codex→测试→Fable；这一轮当时尚未宣称M01完成；最终复审与完成证据见下一节。

[首次Fable阻塞回执](reviews/m01-fable-initial-blockers.json) · [首次loop与中断记录](/Users/osborn/.codex/visualizations/2026/09/13/01a09aef-9838-72c3-b5fa-9e65457ce215/ostag-restart/coding-m01)


## M01 已完成本地验证

代码提交 `07081c3891882b5cbfed0985a2cc6007d7f9568f`，4文件最终摘要 `2e0fa8cc35f0b12f854f41fcc3266a4a43f6edbef058bf192f01ae49a3caff4a`，已合入integration。167项目标测试、1479项全量测试通过，11条既有警告，Ruff通过；固定全量验证耗时21.44秒。真实 `claude-fable-5-1` 复审批准，3个阻塞问题逐项关闭；批准绑定同一候选摘要。

- M01-AC1：固定目的/时区/半开窗口、群与仓库ID/分支/受众、全局预算及完整分区清单。预检仅估计最少调用，不等于采集完整。ExecutionTiming从可信首次claim开始，恢复不能换起点；采集封存与结果判断绑定该时限，超时只允许无私有内容的失败状态。
- M01-AC2：每次受理重新解析映射、grant、核心propose权限、模型出网和逐资源请求者ACL；仓库ID、全名、选择分支与默认分支变更拒绝。新增仓库不自动扩大范围，人员只用明确映射，不猜显示名。
- M01-AC3：严格日期/DST与跨租户负例；确认契约不构成授权或批准。评测规格及18/6保管/评分协议已冻结；私人holdout输入尚未创建或运行，不计成24场景验收。

独立合成探针复验：12:00受理、12:04:10认领，对应12:07:10硬截止；截止前可判为草稿就绪，等号处返回failed/deadline_exceeded，旧fence不可提交；tzdata OSError转换为SchemaError。没有调用真实资料、生成模型或发送消息。

复审非阻塞意见中的测试导入和100列格式，已由同一候选的pytest/Ruff实际验证。业务接线仍须在入队前拒绝needs_input，并使用存储中原始first claim重建ExecutionTiming；纯数据构造不证明当前授权。

[最终Fable回执](reviews/m01-fable-round-1.json) · [合成探针](/Users/osborn/.codex/visualizations/2026/09/13/01a09aef-9838-72c3-b5fa-9e65457ce215/ostag-restart/coding-m01-repair/parent-smoke.json) · [完整修复记录](/Users/osborn/.codex/visualizations/2026/09/13/01a09aef-9838-72c3-b5fa-9e65457ce215/ostag-restart/coding-m01-repair)

M01从原候选清单拆出test_meeting_preflight.py，便于权限/预算案例维护；没有修改A01产品文件或依赖。两个本轮M01任务文件均归档，旧13任务保持哈希不变。M01完成时接续A02→A03；两者的最终结果见后文，下一步A04接通手工材料作业纵向链路。跨切片约束另见[实施衔接记录](IMPLEMENTATION-NOTES.md)。


## A02 中间候选与新增阻塞（已关闭，过程保留）

原生loop将 `ecf38249cf88d994b986cbb75182979eac4812b3` 合入本地integration；110项目标、1589项全量、Ruff与Fable第二轮均通过。Fable初审要求修复锁前取时间；主审另发现persistent trigger可静默跳过outbox，两者已修复。

但主审随后用真实SQLite读事务复现新的超时提交：DELETE journal下，BEGIN IMMEDIATE后时钟仍有效，COMMIT却被读锁拖到deadline之后才成功。原验证进程已加载旧harness，未运行后来新增的门槛；Fable第二轮冻结输入也没有这个新发现。因此原生success不算A02完成。证据保留在coding-a02/parent-commit-reader-probe.json与parent-deadline-regressions-manual.stdout；追加独立osstack修复，完整3文件重审及读/写锁双门槛通过后才记录完成。


## A02 已完成本地验证

最终代码 `5b2abd5fd94f1cf1b051f330b1a543193b53a0ee`，3文件摘要 `7a9b75cc5eb151252bfd8831064ee5d295d2be875ed47e52da488f4cd825369f`。142项目标、1621项全量通过，11条既有警告，Ruff通过；全量22.02秒。真实Fable最终静态审查approved、blockers为空，覆盖完整3文件，而非只看增量。

- A02-AC1：独立briefs领域库，application_id/schema1及可读区间明确；严格完整DDL检查，拒绝不兼容schema和未支持日志模式；Monitor schema未改。
- A02-AC2：受理/入队原子、私有目的地固定；global单租约、30秒租期/10秒心跳、fence与首次claim的原始deadline跨恢复保持。草稿/revision/outbox/终态同事务完成，取消使待投递意图失效。
- A02-AC3：队列上限20、至多3次attempt、重复投递/并发完成/锁忙/崩溃/旧fence/非法状态有负例。独占事务将读写锁等待移到时钟检查之前；提交前再查原租约/时限，续租不能用自己的临时延长掩盖事务中原租约过期。

父级真实时钟门槛中的writer与reader_commit均返回LeaseLost、保持generating且无outbox；此前读锁场景会超时成功。修复保留DELETE日志与FULL同步；逻辑接收点是独占锁内的最后新鲜检查，不能承诺任意调度/fsync停顿下物理COMMIT或函数返回早于deadline。没有引入WAL迁移。

并发建库测试又发现多次schema读取可能混合两个提交快照，已用一次只读事务预检修复。Fable非阻塞指出reader recovery参数案例的COMMIT trace会过早释放读锁；该分支另有writer竞争和事务中到期案例覆盖，原样记录，不把这一个案例夸称完整读锁恢复证据。真实reader完成失败的无mock测试和父级门槛均通过。

builder沙箱全量曾因本地.venv路径/缓存权限失败；外部固定验证器在同一候选完成上述1621项，不混用结果。

[最终Fable回执](reviews/a02-fable-round-1.json) · [读写锁门槛](/Users/osborn/.codex/visualizations/2026/09/13/01a09aef-9838-72c3-b5fa-9e65457ce215/ostag-restart/coding-a02-repair/parent-deadline-regressions-1789353931739637000.stdout) · [完整A02修复记录](/Users/osborn/.codex/visualizations/2026/09/13/01a09aef-9838-72c3-b5fa-9e65457ce215/ostag-restart/coding-a02-repair)

A02两个任务已归档，旧13任务未变。A02完成时接续A03；当时尚无生成模型接口、来源采集、worker服务接线或实际消息投递。A03结果见后文。


## A03 编码与独立门槛（过程已关闭）

两轮Codex builder已完成5文件候选，新增generation/validation及2测试文件，并扩展store成本账本。首轮114项目标/1735全量通过；父级发现普通文本链接过滤误报、授权检查之后仍存在阻塞DB读取。首轮已修复文本误报，但后一个问题由独立gate确认失败，进入第二轮修复。

第二轮130项目标/1751全量通过；reader/writer deadline、真实schema1样本迁移、普通文本与显式恶意链接门槛通过。父级授权gate仍报一个错误；复查发现测试把transport进入后的撤销也算成调用前撤销。已修正gate的时间标记并新增after-IO控制，对两个冻结候选反向验证：首轮旧缺陷代码仍失败，第二轮修复代码5项全过。修正的是验证器，未修改第二轮产品代码。

原生loop在第二轮gate误报后记escalated并清理临时worktree，完整5文件快照/真实Codex回执保留。新受控osstack任务将逐摘要恢复第二轮候选 `2b908d96f7b911b7af98d94767f8d84c21db9fc9a5b79ed813ea396ff7540521`，重跑所有门槛后才调用真实Fable静态评审。首次恢复execute只是确定性文件复制，不伪称一次模型编码。

[验证器纠正的旧RED/新GREEN审计](/Users/osborn/.codex/visualizations/2026/09/13/01a09aef-9838-72c3-b5fa-9e65457ce215/ostag-restart/coding-a03/parent-gate-correction-audit.json) · [原始A03执行记录](/Users/osborn/.codex/visualizations/2026/09/13/01a09aef-9838-72c3-b5fa-9e65457ce215/ostag-restart/coding-a03)


## A03 已完成本地验证

代码 `e884cfc072aecb128114ab1ffb0ffddf0de10c57`，5文件摘要 `bdb2f398aa911634b5843aed0b087fd3c4e8d55caa79c9a14e8c9a7a9ee7e1ef`。148项A03目标、1769项全量通过，11条既有警告，Ruff通过；全量24.62秒。真实 `claude-fable-5-1` 静态评审批准，blockers为空。恢复阶段是确定性复制；原osstack两轮真实Codex builder完成候选后，Fable提出标题/比较式误报阻塞，又由新loop中的一轮真实Codex修复。最终审批绑定修复后的完整5文件内容。

- A03-AC1：冻结实际模型身份、prompt内容/摘要/版本、任务预算/定价界限、验证版本、source/request/config/evaluation摘要与可信usage。每日额度只改变config审计，不改变评测比较键；缺身份/用量、模型漂移或坏JSON拒绝。
- A03-AC2：严格section/claim结构和已存在来源ID；未知URL/工具/批准字段拒绝，事实、来源所述承诺、待决建议分开。trusted coverage与可见gap由采集端提供；所有草稿均保留semantic_review_required，不能把引用存在说成内容真实。普通冒号和store.py可作为纯文本，可信SourceLink才提供候选出处链接。
- A03-AC3：窄async transport加必须注入的当前授权checkpoint；任务/日额度和最多3次累计调用由schema2账本跨重启、恢复、配置变更保持。调用前预约最大费用，唯一call ID只能开始一次；未知结果保留预约，可信费用幂等结算，冲突/超额可见。跨UTC日的预约不能在新一天启动，晚到结算仍属原桶。

最终门槛覆盖实际schema1合成数据的前向迁移、原jobs/leases/revisions/outbox保留、不兼容未来schema拒绝；A02真实读写锁deadline门槛继续通过。旧A02代码打开schema2也按预期拒绝并保持测试DB字节不变；这只证明局部代码兼容规则，尚未完成A11运行时降级或D02生产备份恢复。

授权顺序修复使所有已知阻塞DB工作位于最后授权检查之前，再做不访问DB的时间/取消检查。最终控制案例：健康调用1次并产出候选；入场期间撤销0次调用；授权后无额外DB读取，测试不会再触发该处等待期间撤销；调用后撤销1次但拒绝候选。固定gate在原缺陷快照上仍失败，纠正验证器误判没有改变产品代码或放宽真实撤销要求。

仍须由A04提供已审计的真实只读adapter、逐SourceRef当前grant/resource/ACL/purpose/freshness绑定、heartbeat/process custody与最后原子完成。adapter必须证明整个请求及provider framing/system overhead符合所选输入token/价格上界，否则拒绝IO；合成transport不证明实际提供商隔离。取消不等于证明远端停止，未知费用是额度占用而非已确认账单。A05渲染须保持纯文本并禁用Markdown/HTML/autolink，只让可信出处映射成为链接；regex不会证明全部自然语言无误。使用真实会议材料的模型生成评测/人审、Feishu/GitHub连接、容器与发布门槛仍未运行。真实Fable调用用于代码静态审查，不是会议草稿质量评测；本轮合成单测不计为18/6会议评测通过。

[Fable首审与阻塞](reviews/a03-fable-round-1.json) · [最终Fable回执](reviews/a03-fable-round-2.json) · [机器执行记录](execution.json)

Fable首审补出的标题/比较式误报已由实际RED→GREEN修复：原来的URI正则能在profile/metadata单词内部匹配file:/data:，HTML正则也把数值比较看成标签。修复锚定scheme边界并要求URI正文、识别tag-like opener，新增18项覆盖验证器及生成器的回归；原有授权/账本设计在首审中获确认，最终仍完整复审。

首审的非阻塞意见作为A04接线要求保存：试点对任何实际费用超出预约的情况，保守停止该任务及其namespace/UTC日的新调用；这是明确的v1策略。账本维护接口是可信内部能力，WorkerContext不是端点凭证，服务须校验调用和账单归属。晚到费用结算可能把unknown的旧diagnostic替换为settled，A04应另存不可变的作业失败原因；Conflict/ClockError等异常也须在brief服务域内处理。当前测试包含真实并发预约和SQLite排他事务，但不夸称所有生产并发/恢复场景已验收。

最终Fable回执中的“1751-test”是旧候选输入计数，且评审明确未执行测试；最终148/1769来自固定验证器的实际日志。原A02两份测试文件的摘要未变，schema断言引用CURRENT_SCHEMA_VERSION，不需要改成硬编码2；当前配置的Ruff也实际通过。

最终复审新增的非阻塞接线义务：start被拒绝时可能保留未开始的reserved额度，A04需按job/call_id读取并仅在证明确未开始时释放；started/unknown不得当零成本退款。timeout设置的取消Event是协作停止信号，不等于用户手动取消。SQLite同步调用会阻塞所在event loop，A04应使用独立worker线程/事件循环并以线程安全方式发出停止信号，避免拖住Monitor。账本/时钟异常必须由brief服务域处理并保留作业原因。

本轮代码未推送或部署。当前分支为codex/ostag-company-integration；原OStag工作区保持5098f914且clean。本轮所有新原生任务已本地归档，旧13任务哈希保持不变。下一检查点A04把已验证基础组合为手工材料作业纵向链路；真实历史会议材料接入仍按M02—M07及发布门槛推进。


## A04 首轮审查后的修复中状态

osstack两轮编码得到6文件冻结候选41662d3a1d5c021c549c8d28a9224beee9417309131e1f82e7e3f6a7a1c138db；69目标/1838全量及五组独立检查通过。真实Fable静态评审提出两项SQLite短暂竞争导致永久停机/不必要未知花费的阻塞；父级另复现Future锁内回调死锁。原生loop以escalated结束，未合入产品代码；外部快照完整保留。接续受控osstack修复轮解决三项阻塞和相关竞态，再重新完整验证。

[真实Fable首审](reviews/a04-fable-initial-blocked.json)。此处记录为进行中，不能作为A04或生产验收完成证据。


A04修复候选5e12cb690983df2c91e84decc4952da54452b4e3500f76711dd1f8486ec6a580已通过99目标/1868全量和七组独立检查。Fable复审用满64000输出token（全部为thinking），返回会话额度限制，没有评审结论，不能算批准；原生loop以escalated结束且未合入代码。待显示的额度恢复时间后，对精确快照重跑固定验证并用medium effort、完整6文件及精确依赖/fixture上下文重审；不降低测试门槛、不更换Fable模型，也不把恢复快照算作新模型编码。


## A04 本地链路最终状态

本地代码提交 `95b686450415218d111c8bcc655136ec2dd2d068`；6文件摘要 `31a961af55044e1d7a9b05df5e24cf6c8f7f4a9a00668e670790ad23fedba06f`。原候选5文件外增加store.py，是为持久事件绑定、独立失败原因和锁后/提交前当前授权所必需；没有扩展到生产适配器、UI、投递或server生命周期。

`/brief <严格JSON>` 经过可信NormalizedEvent身份、逐条SourceRef当前授权/ACL/集合身份/内容摘要/有效期和私聊目的地核对后入队。重复事件在建立新intake之前查询持久绑定；同事件改材料冲突；原时间和config固定。群入口只返回任务状态/ID，材料不进入通用backend/MFS/输入审计。无显式可信transport或config时拒绝处理。

独立worker线程/事件循环执行A03真实generate契约，独立LeaseGuard心跳；intake和Future结果发布都有限额。结果发布使用单个通知线程/单槽，回调不占worker控制锁；回调未结束不扩大任务容量，停止不等待任意回调。草稿只有在最后当前授权和lease/fence/截止检查通过后，才与私有outbox一起原子保存。完成状态只表示本地草稿可供语义审核，不等于消息已收到、事实已核实或业务批准。

Store默认构造保持schema1→2，BriefService显式enable_manual升级3；当前可读1..3。新增intake/failure表保留原jobs/cost/revision/outbox；父级用升级前实际schema1、schema2合成样本核对原行与重开，拒绝高于可读范围的版本。没有执行生产DB升级或回滚演练，A11/D02仍须核对多库路径、镜像和恢复兼容。

最终固定验证：**119项目标、1888项全量通过，11条既有警告，Ruff通过**；全量43.1秒。七组独立门槛通过：原读写锁截止、schema1保留、schema2→3保留、来源最终有效期、保存锁等待期间撤权回滚、Future回调/停止、SQLite空闲与生成竞争（包括响应到达时仍持锁）。全部是合成材料和真实本地SQLite；不计入冻结18/6会议语义评测。

### 真实Fable与独立故障挑战

原loop两轮Codex得到69目标/1838全量通过的候选，但真实Fable以两项阻塞要求修改：短暂SQLite竞争导致已付费调用被误判lease_lost/unknown，空闲worker的可重试错误导致永久quarantine。父级在冻结候选上独立复现两项；另复现Future在锁内完成、回调重入run_once后死锁并拖住stop。该loop以escalated结束，产品代码未合入，候选快照保留。

受控修复loop先按摘要恢复候选，再由实际Codex修复。第一轮89目标/1858全量通过，父级补出的“响应已到、用量保存仍遇锁”案例仍失败；第二轮增加仅数据库操作的有界重试，保留响应和真实usage，不重发模型。费用证据可在lease失效后做一次普通结算尝试；进一步等待仍受确认过的lease/原截止及单调时钟限制。未知费用保守阻止新调用，检查先于消耗无关任务的恢复次数。

A04的retry_busy是显式启用，A02默认首次错误锁定loss保持不变。实际过期、owner/fence失效、撤权、无效/倒退时钟均不能靠重试放行。所有数据库等待发生在最后当前授权之前；重试提交时重新授权。临时空闲错误返回可重试状态，每次重试重新读当前DB/时钟；不会凭过去一次检查继续。当前策略可能保留未确认费用，需要可信账单核对，不能把客户端取消说成远端已停。

medium复审还发现两个此前测试未覆盖的阻塞：首次lease确认之前的StoreBusy可能无界等待；生成前和收尾的直接DB调用仍可能把短暂锁竞争变成任务失败或永久quarantine。实际Codex继续修复：只用已提交claim快照建立等待边界，没有快照时首次busy拒绝；停止信号参与退出；生成前DB调用经过guard，失败收尾只做短时间、有界、幂等的维护重试。数据库不可用时不据此推断费用未知；下次处理必须先重新核查持久费用账本。没有放松expiry/fence/撤权检查或重发模型。还按Fable非阻塞建议增加完整provenance大小的付费前检查，并区分保存前撤权的失败原因。新增针对性测试和七组父级门槛均在最终快照重跑。

最终真实`claude-fable-5-1`静态复审批准、blockers为空，绑定最终完整6文件摘要。模型没有执行测试；测试数据来自固定验证器。编码沙箱内两次CLI尝试返回未登录/空modelUsage，未产生模型审查。另一次高强度正式调用用满64000 thinking/output token并返回会话额度限制，没有结论；显示的恢复时间后，以medium effort、完整6文件和精确依赖/fixture上下文重审同一快照，得到上述两个新阻塞后修复并再次审查最终代码，未降低测试门槛。该无结论调用也不算批准。完整实际Codex迭代数为5，快照复制不算模型编码。

独立freshness探针曾因service新增必传stopped参数先遇TypeError。补上调用参数后，对旧冻结候选仍得到“应拒绝却允许”的RED；修复后正常允许/过期拒绝。保留旧probe及反向验证，未改阈值使坏代码通过。

[真实Fable首审阻塞](reviews/a04-fable-initial-blocked.json) · [无结论的输出限制记录](reviews/a04-fable-output-limit.json) · [最终Fable复审](reviews/a04-fable-round-2.json) · [机器执行记录](execution.json)

### 验收边界和接续

A04-AC1仅本地generate契约链路已验证。**当前没有实现生产只读provider adapter，真实后端验收未关闭。** 必须补齐可信实际模型ID/usage、包含provider framing/system overhead的输入计费上界、工具/凭据隔离证明；不能用Backend.run的字符串输出填补。下一步先补此A04剩余项；U01依赖A02已满足，也可接续私有outbox的投递观测。之后按原DAG推进A05/A06/A10/A11及A07/M02—M07。D01真实模型、双身份、手机/桌面和重启/撤权场景另取证。

A04-AC2/AC3的本地边界已有上述代码与故障测试；运行期配置、统一lifespan、真实ACL mirror更新、容器、Feishu/GitHub实际接入、语义审核和发布门槛仍未执行。trusted policy callbacks须有界、线程安全、在本地可信mirror查当前状态；precommit不得重入store/guard或做无界网络调用。Python无法强制终止任意同步回调，这仍需D01/A11运行期认证。Fable明确保留的限制：Future回调永久阻塞会一直占住通知槽，使后续run_once返回busy，虽stop仍有界，但恢复服务需要处理该回调或重启；lease已失效后的唯一结算尝试若再次遇锁，收到的usage可能无法持久化并保守转为unknown，后续须可信账单核对。部分授权/取消竞争的状态标签仍较粗，只能作为诊断，不能当作授权或投递证明。

本轮没有push、main合并或部署。原OStag保持5098f914且clean，旧13任务内容哈希未改变；本轮三个新原生任务均仅本地归档；最后一次首次execute为精确快照恢复，不是新模型编码。


## A04 HTTP适配器环境中断记录

首轮真实Codex曾得到162项新增测试通过，但在沙箱尝试旧全量测试时，系统盘耗尽导致stdout写入崩溃。osstack返回1，没有最终候选摘要、Fable评审或代码合入，隔离工作树被清理。已从完整命令日志恢复新增测试全文和适配器初版；后续patch内容未全部保存，故不称为最终代码的精确恢复，也不继承旧测试通过结论。真实Codex将重新构建并跑所有固定门槛。

本轮临时pytest目录已迁至外置盘保留，后续进程TMPDIR也指向外置盘；运行中源码快照另保留在外置盘。依赖缓存被其他uv进程占用，未强制清理。原项目和既有成果未改。详见[环境中断证据](reviews/a04-provider-environment-failure.json)。


## A04 HTTP适配器授权诊断复审

恢复loop先得到167目标/2055全量通过。真实Fable要求保留授权/lease/日界线失败的领域原因；第二轮184目标/2072全量通过，但父级HTTP探针发现任意可信回调异常被原样重抛，暴露私有文字。该loop以escalated结束，没有代码合入；完整最终候选已按摘要保存。后续按同一快照修复异常映射，仅保留安全、固定的领域诊断，不能把“可信回调”当成“异常文字不含敏感信息”。[Fable首审](reviews/a04-provider-initial-blocked.json)。


## A04 生产 HTTP 适配器本地实现

代码提交 `95af6f1355b1526b392e9a68e9d31bca4945d48f`；五文件摘要 `a11fdd60e4f2836e37f6e7254a006d1938fd43738994100d837513e46ddc7974`。独立的 `OpenAIReadOnlyTransport` 已实现，复用 A03 generate 和 A04 service/worker；没有复用只返回字符串的 Backend.run。固定 OpenAI 官方 HTTPS endpoint、一个模型快照与 standard 费率档，实际 HTTP 请求禁止工具/会话/历史响应、自动重试、重定向和环境代理。仅显式传入 API key；真实启动配置与统一 lifespan 仍属后续接线。

每次私有出网前调用同一可信授权入口：先数据库/lease检查，再当前来源/grant/ACL，最后非阻塞时钟与取消检查。先发送完整输入的计数请求，包括 provider 格式开销；再检查额度并重新授权，才发生成请求。两阶段共享原时限，输入计数通过不代表之后仍有权限。实际模型身份和 token 用量只取自供应商响应 envelope，模型正文不能提供或覆盖；无效正文不能抹掉可信用量。

首个已核对接口档为 `gpt-4.1-2025-04-14`，标准公开单价输入2、缓存输入0.5、输出8 microUSD/token，microUSD按合计向上取整。费用是供应商报告的实际token乘版本化公开费率，不是供应商账单；真实账号、费率更新、计数端点计费和语义质量仍须 D01 验证。固定快照用于收敛首个适配器的可测契约，不代表已选定最终会议产品的最佳模型。

首轮编码因系统盘ENOSPC中断，未合入代码且没有Fable结论；新增测试全文和适配器初版从实际命令日志恢复，后续修改由真实Codex重建。恢复loop的真实Fable首审发现授权/lease失败被误记为transport_error；第二轮改成透传回调异常后，父级HTTP探针抓到私有文字泄漏。最终修复依据保存完整摘要的候选继续，通过安全的领域异常映射保留原因，任意回调/网络异常仍脱敏。没有降低原HTTP隐私探针阈值。最终loop首次执行被“恢复测试函数AST必须完全相同”的机械校验拦住：父级核对后只登记两项Fable建议修订（扩大计时余量但仍要求总时限超限失败；增加持久化诊断断言），其余函数继续锁定。后续代码还保留旧A03检查点拒绝语义，仅对新per-egress检查区分lease_lost，并重跑既有回归。最终验证不继承中断前的测试结论；运行中源码快照与测试临时目录已置于外置盘。

最终固定验证：**215项目标测试、2103项全量通过，11条既有警告，Ruff通过**。原七组锁竞争、撤权、迁移、回调和截止故障门槛再次通过；新增父级HTTPX独立门槛覆盖正常计数/生成、计数间撤权、重定向、预检超限、伪造模型和工具输出六个场景，均验证请求数量/授权边界/资源关闭。既有锁定包完全不变，uv lock离线一致性校验通过。真实 Fable5.1最终静态评审批准、blockers为空；测试由确定性验证器执行，Fable没有执行或冒充测试。所有HTTP边界测试为本地合成响应，不能计作真实模型/Feishu/GitHub或冻结18/6语义评测。

边界：本轮未改变账本。编码阶段曾有一个旧超时用例在组合运行时未及时进入模拟调用，单独复查及最后固定全量运行通过；该失败记录保留在原builder日志，不据此声称计时测试绝不会受负载影响。A03在进入transport之前已标记成本预约started，因此计数/预检失败仍可能保守留下unknown费用，触发worker暂停并等待可信核账；不能当作免费或自动退款。网络超时/取消不证明远端停止计费；晚到结果不能产生草稿。真实生产权限、凭据与目标网络联通尚未验证，未push、main合并或部署。

Fable保留的非阻塞限制：极窄截止竞争可能将timeout记为transport_error，仍拒绝晚到草稿；二次取消可能让资源关闭在后台完成，不能证明远端已停止；内部注入的HTTP transport按每次调用关闭、不可作为可复用生产实例；计时测试仍可能受主机负载影响。这些不冒充生产运行期认证，后续A11/D01需继续观察。

下一开发切片为U01：私有outbox投递观测与结果不确定时的处理；之后再按DAG推进界面、生命周期及Feishu/GitHub会议材料采集。

[最终Fable审查](reviews/a04-provider-fable-round-1.json) · [官方接口与费率来源](reviews/a04-provider-sources.json) · [机器执行记录](execution.json)


## U01 私有投递核心本地完成

代码 `ce66df0f68dc22ab57eb8ce8f982b4e801ed48ef`。新增独立投递状态、持久发送 claim、精确 message ID 与完整内容验证、不可覆盖的不确定记录、有界观察和显式 schema4 迁移。默认 schema2/手动 schema3 行为保持兼容。

固定验证：122 项目标、2225 项全量通过，11 条既有警告，Ruff及11组父级门槛通过。真实 Fable5.1 对同一候选批准、blockers为空。

这是核心库与合成传输验证。完全丢失响应且无可信 message ID 时仍需人工核验；provider_accepted 不等于真人收到。真实飞书HTTP适配器、卡片、运行期接线与部署验收继续归A05/A11/D01。下一任务为A05。

[契约、验收与Fable意见](U01-DELIVERY.md) · [最终审查](reviews/u01-fable-round-3.json)


交付文档与全部审查回执写入后，4项密钥扫描回归再次通过；未修改扫描规则。[文档验证记录](reviews/u01-document-validation.json)。


## A05 基础实现本地验证

私聊分页预览、独立持久化一次性回调、显式适配器/路由注入及 U01 HTTPX 文本传输已实现。88 项目标、2313 项全量、Ruff和12组父级门槛通过；真实 Fable5.1 对同一候选批准。代码 `cee76573207385099e5b93e33c1fb142174a88eb`。

A05尚未整体完成：自动首次卡片发送、版本化卡片outbox、运行期接线和真实移动/桌面验收仍待完成。含可能激活链接的旧文本会拒绝发送，不改写原正文。详见 [A05实施与下一步](A05-INTERACTION.md)。


## A05 首次卡片发送与翻页本地完成

代码 `849fa69e91189a6541815ba1ce7af7393c1ae496`。新增独立 card-v1 outbox、唯一发送权、直接创建回执激活、原消息绑定及 epoch 翻页；相同原始卡片不重发，旧文本记录不改写。82 项目标、2395 项全量及13组父级门槛通过，真实 Fable5.1批准。

[实施契约与剩余验收](A05-CARD-DELIVERY.md)。实际手机/桌面、平台身份和 A11 生命周期尚未认证；依原 DAG 下一步 A06，然后补齐 A10/A11。


## A06 纠错与内容采用本地完成

代码 `8ef7aa72f75de24178ce922b10143df44a0a6708`；独立不可变纠错链、精确版本采用/撤回及历史回执与当前状态分离。103项目标、2498项全量和14组独立门槛通过，真实Fable5.1批准。见 [A06实施与后续方案](A06-REVISIONS.md)。飞书签名按钮接线仍属A11，下一步A10。


## A10候选：复审待恢复

A10撤权/保留及Linux兼容候选已保存，本地117项目标、2615项全量、16组独立检查通过。实际Fable5.1额度耗尽，尚无审查结论，候选未集成、未部署。见[A10实施与恢复方案](A10-RETENTION.md)。下一步先恢复A10真实复审，再推进A05过期预览与A11接线。
