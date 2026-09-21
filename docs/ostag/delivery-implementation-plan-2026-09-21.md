# OStag 开发、验证与发布实施方案（2026-09-21）

状态：已获 Claude CLI Opus 5 独立挑战并据此修订；审查 session `fb6ab999-fff3-4a1e-a75e-44c9ce155198`，原结论为 revise_before_execution，不标作整份方案已批准。范围保持 31 项任务、9 组技术债、120 条验收标准；附录逐条保留。此文件是工程方案，不是部署成功或功能验收证明。

## 当前事实与推进方式修正

PR #15 已由 gilberthello 正式批准，于 2026-09-21T12:04:22Z 合并为 `23d3c4da59e5bf30722df4ac5433d225588b5bf6`；主分支 CI 35597429664 和生产部署 35598142948 成功，最新读取 healthz 返回 ok。部署成功不表示 A/B/C/会议功能真实验收完成。服务器安装的部署脚本仍是 de656d47992b53e2a8546bf4a156396f7a542618f74cc2037c68fc4d9d3da4dc；新的互斥/维护封闭机制尚未安装。服务器发布回执和业务开关没有在本轮重新读取，不能推测其状态。

本地 U02 分支含日志、协调锁、托管 ledger、容器停止观察等已审计开发提交；持久维护封闭修复已提交为 `a96b5f5b`，未合并或上线。实际 Claude Opus 5 第三轮对新增修复及受影响边界给出 approved、无阻断（session `d97d4617-ae04-47f4-83db-cac1c52d224e`）；本机 113 passed/13 skipped，Linux ARM64 126 passed。它仍不是 U02 整体验收。

此前过多时间集中在 U02 的迁移安全基础，未充分利用不依赖 U02 的只读业务路径。现在按依赖分成三条工作线：A1 真实只读价值、会议准备、外部写入与恢复。单仓库同一时刻只有一个写入者；独立评审和测试可并行。依赖图只能解除实现的排队，不能跳过冻结的验收依赖。

## Graph Engineering 与 Loop Engineering 分工

Graph 固定模板 `code-delivery-v1` 提供 workspace → implement → freeze → 独立 review/tests → join → 有限 repair。它发布 artifact_only，不直接批准 PR、合并或部署。40 个节点分别生成原生 GraphSpec，保留任务/仓库/基线/验收引用、两轮预算、单仓库单 writer 和明确截止时间。`dependency-inventory.json` 是跨任务依赖清单，不冒充原生模板可执行的任意 DAG；待执行节点由已验证的依赖状态选取。当前 40 份规格绑定生成时的开发基线 `0c9fbcbb`，保留为离线设计证据；真正派发前按工作线重新选择 main 或明确集成候选、重算哈希并校验，不沿用过期基线或把 U02 未发布改动混进只读 A1 候选。

当前默认 daemon 的 graph-host 注册绑定 osstack-internal-beta 和 DeepSeek，不能拿来声称 OStag 已获 Graph 执行授权。保留该配置。OStag 需独立 host 注册，固定仓库、任务、验收命令、Codex writer 和 Claude Opus 5 reviewer，并验证当前授权与实际子进程停止凭证；GraphSpec 离线验证不等于 graph run 成功。未满足时，原生 Loop 继续执行已授权开发与验证，Graph 状态标记为 execution_not_enrolled，不能伪造 ready。

Loop 是当下的执行闭环：每个有界工作包使用精确 capability 白名单，默认阻止历史队列其他任务；执行命令、针对性测试、独立审计、失败反馈、状态及 proof 全部保存到仓库外 runtime。只在 verifier 成功后归档该工作包，不把它等同于父任务完整验收。不得通过 run --help 查询帮助（本机该调用会执行）；使用 osstack help / loop help。

外部前置节点 A01/A02/A04/A05/A06/A10/M01/U01 来自原冻结计划，但不在剩余 40 个节点中。重新核实其当前提交、测试和发布证据；不将 core_local_only 自动当作已验收。

## 阶段 0：先校准执行入口与发布门槛

本轮已实际执行：40 个原生 GraphSpec 全部校验通过；依赖图无环；40 项验证入口盘点得到 10 项现有测试可收集、16 项指定测试文件缺失、9 组技术债未登记独立 verifier、5 项非 pytest 命令待单独核实。现有入口合计成功收集 519 个测试（collect-only，未冒充运行通过）。缺文件证明该验收入口未建立，不直接证明全部功能不存在。Graph inventory 已逐项标注 verifier_status、execution_not_enrolled 和 acceptance_ready=false。

首先核对八个外部前置成果当前 SHA、代码、实际测试与发布证据；将主机安装包 D02 作为发布关键路径，与 A12/D01 的开发准备并行。P00、P01 和不依赖其他节点的 TD06–TD09 核对可以先做；不是所有工作都被外部前置节点阻塞。只有完成证据核对的节点才能被选为完整验收任务，提前开发的候选不宣告验收通过。

接下来三个有界工作包：

1. WP1 校准：完成前置成果核对、验证入口缺失清单和技术债 verifier 注册；通过允许的只读接口取得当前服务器回执/镜像/业务开关。接口无法提供的内容明确为 unknown，不能用 healthz 补足。
2. WP2 发布路径：将 U02 当前增量固定为明确开发提交，释放写入位置；单独准备 D02 主机安装/恢复包与目标架构验证，不将未完成 seal 迁移绑进 A1 普通镜像发布。安装包需满足 forced wrapper 能力，不能自行扩大远程执行权限。
3. WP3 只读价值：A12 的 24 情景定义（18 开发、6 独立保留，当前只验 A1 的 16 情景子集及至少 4 保留变体），关键 case 三次真实模型执行；D01 独立 app/群/凭据/卷/端口和目标镜像验证；I01 独立只读取证。按权限可生成本地会议准备 Markdown，但不能将本地文件算作 D07 真实产品验收。

系统盘阻塞已解除：原默认 `~/.osstack` 已迁至工作盘 `/Volumes/AIMATE_WORK/Developer/osstack-data/stable-home`，原路径保留兼容符号链接；完整文件/数据库校验与备份读回通过，daemon 健康，维护封闭工作包已通过原生 Loop 并归档（task_3aea9ca2）。未降低 5 GiB 阈值或修改角色白名单。之前另建控制域的尝试已弃用，不再把它记录成当前执行入口。Graph 的 OStag 独立执行注册仍未完成。

## A12 当前实施增量（独立于 U02）

从已部署 main `23d3c4da` 建立 `codex/ostag-semantic-evaluation`，不含 U02 未发布代码。新增 `ostag.briefs.evaluation` 离线评测证据统计入口和冻结标准指定的 `tests/test_brief_eval_contract.py`。入口验证 24 情景定义的结构、18/6 划分、A1 的 16 情景和至少 4 保留变体；当前测试材料全部为合成数据，不是独立真实评测集。

Claude CLI 显式 `claude-opus-5` 的设计挑战指出整组缺失、失败模型归组和重复索引语义问题。现用预先声明的模型/参数组、每次执行唯一标识和逐次结果解决；模型身份仍需实际传输证据。审查材料和 native Loop 证据保存在仓库外 runtime 的 `a12-evaluation`，不得把离线统计或审查结果当作模型业务质量。

输出分别列出成功数、全部尝试数、失败/弃权/未完成、逐条待判定、未知成本和 A2 排除项。`evidence_complete` 表示证据已记录与判定，不表示语义全部正确；`acceptance_evaluated=false` 固定表明未验收。使用说明见 [semantic-evaluation.md](semantic-evaluation.md)。

下一步按顺序：独立冻结实际 24 情景输入和断言；用预期生产 transport 运行 A1 子集与关键情景三次重复，保全每次启动和原始回执；独立判定和统计；收集用户采纳率/修订时间；结合 D01/D02 推进 D03。当前增量只关闭 A12 缺失验证入口的一部分，A12 和 TD05 均不宣告完成。

## 优先级与有界工作包

| 工作线 | 顺序 | 本轮后紧接的实际动作 | 完成证据 |
|---|---|---|---|
| A1 只读交付 | A06+A11 → A12；A11+A12 → D01；P00+A02+A11 → D02；D01+D02 → D03 | 核对已部署 A11 的范围；建立真实模型语义评测集和隔离 staging；走完整只读简报链路 | 同版本语义报告、隔离测试、真实来源引用、用户确认；D02 未闭合前不可宣告 D03 发布 |
| A2 历史来源 | A04+A07+A08+A10+A11 → A09；A09+D03 → D04 | 复用已授权的中转站群历史，检查时间窗、分页、权限和脱敏证据；合并需求表快照 | 日期覆盖与缺失说明、来源引用、历史试点验收；不重新申请已有授权 |
| 会议准备 | M02 → M03 → M04 → M05 → M06 → M07 → D07 | GitHub 组织时间窗/提交/PR/发布证据分类；与飞书议题冲突合并；会前刷新与差异 | 会议页面端到端、引用可追溯、权限隔离、真实会议试点；M04 等待 A09、D07 等待 D04/D02 |
| 外部写入 | U02 → B02 → B03；B04 等待 A09/M05；D05 等待 B03/B04/D04/D07 | 收敛当前封闭机制；完成受管范围与恢复历史见证；再做单目标写入、独立读回和不确定对账 | 无盲重试、真实 provider 条件写语义或明确安全替代、故障后保持未知隔离、D05 实际链路 |
| 销售与上游 | C01+B03+A11 → C02；C02+D05 → D06；U01+U02+U03+U04+D05 → D08；I01 依赖 P00 | 复用已证实的闭环，逐项比对上游行为；保全 PR 原始证据 | 各自业务案例、兼容回归、真实试点；不批量照搬上游提交 |

U02 的 umask 与路径校验修复已审计完成；剩余明确边界：受管全服务停止与启动封闭；B01 历史 recovery prefix 的可信迁移见证；原始 JSONL 内容退休；受控备份/导出登记与反回滚；跨库退休回执；B02/B03/D05 集成。只读 A1 的开发测试不得被这些写入专属依赖长期串行阻塞。

技术债挂在对应交付门上：TD01→A11，TD02→D02，TD03→A07/D04/D07，TD04→D01/D03/D07，TD05→A12/M07。TD06–TD09 先核对 PR15 合并后的实际代码、CI 与各自三条验收证据；不能仅凭集成状态改成 done。

## 真实日历约束

原标准中 D03 没有七天窗口；D04 要求至少七天及十个不同真实委托，D07 依赖 D04 且要求七天及十个不同真实会议委托。因此按冻结依赖串行验收，D05 在 D03 之后至少仍有十四天观察窗口；D06 再要求七天及十个真实跟进，累计至少二十一天，另外还要加工程、配置、审批和业务量等待时间。不能用模拟、重复同一委托或压缩自然时间替代。没有确定试点开始日期前，不承诺完成日期。先准备指标、受试者选择和基线，不擅自发送招募信息。

Claude 初稿推断 D05 至少二十一天和“零节点可启动”，与冻结标准不完全一致，已按原标准纠正；这些不是新验收要求。

## 每个工作包的执行与退出标准

1. 固定源提交及工作树增量，读取对应三条验收标准，记录可复现问题与允许修改的范围。不能用旧源码测试冒充新候选。
2. Codex 修改实现；逻辑故障先提供针对性反例，补必要回归。低影响文档不制造镜像测试。
3. 在目标环境运行针对性测试；涉及 Linux flock、容器、部署与恢复必须有 Linux 实验，Mac 跳过不等于通过。记录导入路径、文件哈希、退出码；清理自己创建的资源，不动其他 Docker 上下文。
4. 每次审查必须注明目标验收条款及当前不能闭合的部分；文档或纯测试增补不单独循环审计。每组条款一次冻结审查、一次修复复审；仍失败则先归并根因和调整工作包，禁止原样重投。本次维护第三轮仅处理已复现的 umask 和新增路径/重启边界，已通过；非阻断建议归档，不启动第四轮重复审查。冻结候选，Claude CLI 显式 `--model claude-opus-5`、只读工具边界，审查代码/用例/故障证据。保留实际 session/modelUsage/候选哈希；Claude 静态审查不能冒充它亲自执行测试。
5. 把发现分为可复现阻断、非阻断改进和后续产品门。对有依据的分歧给出反例或协议说明，不机械添加新要求，也不改 verifier 绕过未解决阻断。修复后只重跑受影响测试和必要复审；两轮后仍未收敛时做根因归并，而非重复完整旧审计。
6. 通过后提交有界改动，保留父任务未完成项；集成同一候选后执行完整 release_check 和必要 Linux/镜像验证。发布和业务验收状态分别更新。

正式发布的部署/容器/恢复证据必须包含目标 linux/amd64 当前候选的记录。当前本地 Colima 为 Linux ARM64，只算补充回归；不能替代最终目标镜像/CI。证据分别记录 executed_by（Codex/CI、命令、退出码）、reviewed_by（实际 Opus 模型、session、候选哈希）、approved_by（GitHub 审批者）、deployed 与 live_accepted。

## PR、部署与真实验收

准备新 PR 时基于当前 main；PR15 已合并，不继续向已关闭 PR 堆积本地 U02 提交。先解决集成冲突，核对 diff，再跑 scripts/release_check.sh；PR 描述包含行为变化、风险、测试和部署前置条件。Claude 审计不是 GitHub 正式审批；必须有满足仓库规则的当前提交审批，随后 squash 合并。

等待合并提交的 OS Tag CI 成功以及对应 OStag Production Deploy 成功，核验该 SHA 的不可变服务器回执、镜像与 healthz；再进行实际业务探针。只读健康检查不证明写回与会议产品已验收。任何需要现场授权/用户真实使用的门必须有真实证据，不填造成功。

维护协议变更不得随着普通镜像更新就当作完成安装。先准备可审查的 host 安装与恢复包、固定脚本哈希、当前部署清单和受控备份，通过已安装的 forced SSH wrapper 支持的操作实施；不绕过 wrapper 去执行任意 shell。现有主机不可当 fresh host 写 generation zero。未解决安装/迁移前，不上线要求新 seal state 的 launcher。写入未知时保持隔离，不能回退旧镜像导致重放；已验证的只读版本可按原发布回执回退。

## 进度衡量与防止重复

每项分别记录 implementation / tests / independent_review / integrated / deployed / live_accepted；done 必须覆盖原全部标准。当前工作包通过数不累加成新功能数。每轮记录唯一新证据、剩余依赖和下一可执行节点；没有源码/风险变化不重复旧审查。保留所有原始 120 条标准和失败证据，状态变化有依据链接。

验收测试命令表来自冻结 ledger，是执行入口候选；运行前必须确认命令、依赖、目标数据域与当前文件存在，不把旧命令字符串视为已验证测试覆盖。Graph 注册同样只能采用重新验证的命令。

## 原冻结任务清单

| ID | 名称 | 账本状态（待按最新发布证据校准） | 依赖 |
|---|---|---|---|
| P00 | 基线与旧成果保护 | partial | 无 |
| P01 | osstack单任务执行准备 | partial | P00 |
| A07 | 授权历史与线程读取 | partial | A01 |
| A08 | 需求表快照核对 | partial | A01 |
| A09 | 历史简报组合与完整性 | pending | A04, A07, A08, A10, A11 |
| A11 | 单服务接线与回归隔离 | partial | A05, A06, A10 |
| A12 | 真实模型语义评测 | pending | A06, A11 |
| D01 | 隔离staging与真实链路 | pending | A11, A12 |
| D02 | 发布锁和多库恢复 | partial | P00, A02, A11 |
| D03 | A1发布与首位用户验收 | pending | D01, D02 |
| D04 | A2历史试点与批次A退出 | pending | A09, D03 |
| B01 | 确认行动到冻结变更 | integrated_pending_release | A06 |
| B02 | 单目标执行与独立读回 | pending | B01, A08, A11, U02 |
| B03 | 跨库与外部不确定对账 | pending | B02 |
| B04 | 一条例行简报 | pending | A11, A09, M05 |
| D05 | B真实写回验收与发布 | pending | B03, B04, D04, D07 |
| C01 | 销售文字与客户匹配 | partial | A01 |
| C02 | 复用业务闭环 | pending | C01, B03, A11 |
| D06 | 销售试点与发布交接 | pending | C02, D05 |
| I01 | PR证据独立保全和评测 | pending | P00 |
| M02 | GitHub组织时间窗取数 | partial | M01, A07 |
| M03 | 合并检查发布证据分类 | partial | M02 |
| M04 | 跨来源冲突与议题提取 | partial | M03, A09 |
| M05 | 会议准备端到端功能 | partial | M04, A11, U01 |
| M06 | 会前刷新与版本差异 | pending | M05, A10 |
| M07 | 会议专项评测与可用性 | pending | M06 |
| D07 | 会议功能真实试点与发布 | pending | M07, D04, D02 |
| U02 | 外部操作日志与执行授权适配 | in_progress | B01, A02 |
| U03 | 仓库批次证明与能力门槛 | pending | U02 |
| U04 | 审批反馈和状态时间语义 | pending | A11 |
| D08 | 上游适配批次验收 | pending | U01, U02, U03, U04, D05 |
| TD01 | 生产运行时组合 | partial | A11 |
| TD02 | 部署互斥与恢复保障 | pending | D02 |
| TD03 | 服务器凭据与 CLI 契约 | pending | A07, D04, D07 |
| TD04 | 飞书真实交互组合验收 | pending | D01, D03, D07 |
| TD05 | 语义质量与测试稳定性 | partial | A12, M07 |
| TD06 | Monitor 命令与 ledger 发布间隙 | integrated_pending_release | 无 |
| TD07 | 旧合成预览文本展示损失 | integrated_pending_release | 无 |
| TD08 | 重复过期进度文档 | integrated_pending_release | 无 |
| TD09 | CI 重复整套发布检查 | integrated_pending_release | 无 |

## 原验收标准（逐条保留）

### P00 — 基线与旧成果保护

- 固定main/四工作树完整SHA和dirty清单；列M0/PR增量及保全方式；重查上游2689b07→3a3136d与组织可见范围，不将59提交直接批量合并
- locked Python3.11基线与依赖差异明确；现有13任务不变
- 记录生产拓扑/容量/连接身份可见性，未知项不补成功

验证入口：`uv run --locked pytest -q`

### P01 — osstack单任务执行准备

- 先复用8b0dd3d9探针并复验installed0.19接口，不复制未审全分支
- 单attempt隔离旧队列、超时/失败存证、模型错/空verifier拒绝
- 自动loop不可用时受控Codex+Fable继续合成切片，不自行开启旧daemon

验证入口：`uv run --locked pytest -q tests/test_engineering_runner_probe.py`

### A07 — 授权历史与线程读取

- 固定chat/请求者身份/时间窗；分页去重和thread有总预算；服务端user凭据供给/受保护存储/刷新/轮换/撤销可验，不复制Mac钥匙串
- 403/缺页/循环cursor/媒体占位/撤回显式gap，不能用5页auto_base当完整历史
- CLI超时取消后清理进程并限制输出；在目标1.0.69容器验证help/契约；真实身份与平台证据另在D04验收

验证入口：`uv run --locked pytest -q tests/test_brief_history.py`

### A08 — 需求表快照核对

- 脱敏样本精确13具名+4空白、10无负责人；不把历史数写死
- 跨表8差异按明确记录映射验证，歧义保留
- 只读表连接器保留空白行和人工编辑；日期语义不明不判逾期；给B02提供同资源独立读回契约

验证入口：`uv run --locked pytest -q tests/test_brief_table_quality.py`

### A09 — 历史简报组合与完整性

- A2端到端取数→确定性核对→模型组织→带gap简报
- 来源集合/时间截止显示；缺关键资料降为待补充
- 刷新创建新revision，不沿用旧hash/验收；整条历史链路中单源撤权使整版失效

验证入口：`uv run --locked pytest -q tests/test_brief_history_journey.py tests/test_brief_flags.py`

### A11 — 单服务接线与回归隔离

- 全off不创建brief worker/业务DB，但含brief能力镜像仍写状态/排空ack；统一注册全部flag，未实现模块拒绝开启；MEETINGS一并注册且仅M05就绪后可启用
- on只有一owner；运行期控制文件停接单/认领/新IO，boot默认off；独立在途计数与排空ack可核验
- 延迟/失败/brief schema不兼容不破坏Monitor/relay/日报；brief降级单列；同digest只读维护模式禁止业务IO；状态目录不可写仅brief-status降级，Monitor正常、preflight拒绝

验证入口：`uv run --locked pytest -q tests/test_brief_lifespan.py tests/test_brief_flags.py`

### A12 — 真实模型语义评测

- 24情景定义18开发+6独立保留变体；当前只验A1的16情景子集，含至少4保留变体，不宣称A2通过
- A1关键语义case实际模型重复3次，统计所有失败/弃权与成本；参数版本不同单列
- 关键断言逐项有依据/待确认，采用率和修订时间独立记录

验证入口：`uv run --locked pytest -q tests/test_brief_eval_contract.py`

### D01 — 隔离staging与真实链路

- 独立test app/凭据/群/数据卷/端口，不消费生产bot
- A1 profile目标linux/amd64镜像+真实模型+两身份/转发卡/手机桌面验证，不声称历史或跨源ACL通过
- staging独立boot/restart/cancel/revoke流程，留真实平台证据

验证入口：`uv run --locked pytest -q tests/test_brief_staging_config.py`

### D02 — 发布锁和多库恢复

- 核验server并补全所有deploy/rollback/控制写共用锁；受信brief-control show/set原子控制；preflight强制fresh drain ack/零lease/零在途，not_ready不切镜像不rollback；CI密钥身份调用on及伪造operator拒绝，仅独立操作者入口可激活
- briefs/附件备份恢复及schema兼容；全off镜像preflight通过、含brief却缺状态拒绝、legacy例外精确限定；不覆盖Monitor新数据
- 可信脚本安装/hash同步后才merge本批；失败回滚强制所有明确brief功能flag及admission off，含Compose覆盖和同digest只读维护演练

验证入口：`uv run --locked pytest -q tests/test_deploy_brief_safety.py`

### D03 — A1发布与首位用户验收

- 候选及合并tree CI/容器/模型/A1 stage证据齐全；全off上线后激活前在合并tree重跑16情景、关键语义case各3次；排空覆盖merge至deploy且仅重跑当前main
- 先全部off上线，再仅owner A1，核对image/receipt/health/真实草稿
- 关闭A1并恢复前镜像演练可维护旧业务，新数据保留

验证入口：`scripts/release_check.sh && scripts/ci_release_gates.sh`

### D04 — A2历史试点与批次A退出

- 含A09候选在A2 profile重跑D01三项stage门槛；目标1.0.69+服务器user身份全生命周期/历史ACL；全24情景重验且关键语义case各3次
- 至少7天且10个不同真实委托，统计接受/拒绝/人修/缺口
- 扩到具名成员前双人可见性实测；未达标保持试点

验证入口：`scripts/release_check.sh && scripts/ci_release_gates.sh`

### B01 — 确认行动到冻结变更

- 单资源/字段/原值/新值/身份/版本冻结，严守旧wire兼容
- 内容验收不构成动作授权，重复提议关联唯一receipt
- 修改目标或payload使旧批准失效

验证入口：`uv run --locked pytest -q tests/test_brief_action_proposals.py`

### B02 — 单目标执行与独立读回

- apply重查身份/当前对象和授权，未批准零写
- 若无条件写，不承诺single-writer覆盖多人编辑；选可证明安全路径
- provider成功仍需独立readback，不同资源/版本拒绝完成

验证入口：`uv run --locked pytest -q tests/test_brief_action_execution.py tests/test_brief_flags.py`

### B03 — 跨库与外部不确定对账

- receipt创建/外部写/本地回写各崩溃窗口可定位
- 响应丢失、重复worker、人工并发不盲重放、不伪造成功
- 无法唯一证明结果保留待核验，人工对账有身份与理由

验证入口：`uv run --locked pytest -q tests/test_brief_action_reconciliation.py`

### B04 — 一条例行简报

- 模板/资料/受众/时区/频率/预算可见，例行执行不能再建例行；支持会议模板，未就绪模板拒绝启用，时间窗随每次实例冻结
- 每次执行复核grant，重启补偿有窗口且不追发全部旧实例
- 只在变化/失败/需决定通知，暂停后未投递作废

验证入口：`uv run --locked pytest -q tests/test_brief_routines.py tests/test_brief_flags.py`

### D05 — B真实写回验收与发布

- B profile含B03/B04候选重跑D01三项stage门槛及B专项；采用A格式输入，先测试表/任务再获具体原表动作授权
- 真实重复请求/响应丢失/并发编辑/撤权验收零错写及重复
- 独立B开关、部署/读回/降级回只读，外部效果不随镜像撤销

验证入口：`scripts/release_check.sh && scripts/ci_release_gates.sh`

### C01 — 销售文字与客户匹配

- 客户歧义必须确认，金额/日期/阶段不猜
- 销售私聊不默认进管理报告或另一销售上下文
- 只用指定原表，不创建伴随表或改权限

验证入口：`uv run --locked pytest -q tests/test_sales_brief.py`

### C02 — 复用业务闭环

- 复用grant/task/revision/receipt/readback，不另建一套核心
- 文字到草稿到确认执行完整链路，错误客户负例阻断
- 记忆更正与业务更正区分，后续提醒需独立授权

验证入口：`uv run --locked pytest -q tests/test_sales_brief_journey.py tests/test_brief_flags.py`

### D06 — 销售试点与发布交接

- C profile含C02候选重跑D01三项stage门槛及C专项；指定sales用户/表/字段/模型出网，测试表和真实表分开
- 至少7天且10真实跟进，人工纠错负担与有效记录数达到商定目标
- 留操作/撤权/恢复手册，其他能力开关状态明确

验证入口：`scripts/release_check.sh && scripts/ci_release_gates.sh`

### I01 — PR证据独立保全和评测

- 保留f62b68a5及差异，集成单独评审不整体合M0
- 先真实GitHub只读控制证据，再补受控diff/模型样本，分开验收
- 不自动APPROVE/MERGE/DEPLOY，不作为A交付前置

验证入口：`uv run --locked pytest -q tests/test_github_review_evidence.py tests/test_pr_review_evaluation.py`

### M02 — GitHub组织时间窗取数

- 按冻结清单逐repo有界采集PR/Issue/评论/timeline/default-branch commits，保留event_at/observed_at，updated_at只筛候选
- 分页完整性/循环cursor/1000搜索上限/窗口后更新/旧作者日期/权限撤销/半开边界有反例，不以Events页当全量；每repo每类/每群子预算，required分区partial不可ready
- read-only transport限字节/页数/重试/总deadline并kill-reap；身份缓存隔离/ETag校验/限流对Monitor无饥饿；cursor/消耗/deadline持久，恢复不重置；120秒取数软截止保留60秒生成，硬deadline仍180秒

验证入口：`uv run --locked pytest -q tests/test_github_history.py`

### M03 — 合并检查发布证据分类

- PR合并、default-branch commit、release published、Actions成功与production部署各自事实，不互相冒充；首次无旧head非PR合入时间unknown；旧head须来自同repo/分支/principal有效TTL快照
- 复用ProviderChangeEvidence完整current-head验证；过去事件与当前观察分开，缺环境/receipt则部署unknown
- rerun/checks过期/不同SHA或repo/回滚/删除记录/同一PR多事件去重有明确断言

验证入口：`uv run --locked pytest -q tests/test_meeting_release_facts.py`

### M04 — 跨来源冲突与议题提取

- 显式链接和已批准项目映射优先；语义相关仅候选，Feishu身份与GitHub登录不按同名自动合并
- 聊天承诺/PR事实/发布证据分列，矛盾按时间展示；无证据不推导无进展/人员绩效
- 关键资料缺失阻止meeting_ready，数字确定性核对、关键论断人工语义验证；源失败置顶，按待决/阻塞/冲突而非bot活跃量选题

验证入口：`uv run --locked pytest -q tests/test_meeting_facts.py`

### M05 — 会议准备端到端功能

- 复用BriefService/job/revision/grant/outbox，生成会前阅读/重要变化/冲突风险/至多3重点决策议题与时间预算/建议行动
- OSTAG_BRIEF_MEETINGS_ENABLED默认off且未实现拒绝开启；私聊状态+可导出的本地MD，原A1/A2和其他业务回归通过
- 先确认目的和范围再取资料；没有日历邀请/群分享/表写，采取建议不构成批准；shareable_sources默认空，来源权限与受众交集逐项检查

验证入口：`uv run --locked pytest -q tests/test_meeting_journey.py tests/test_brief_flags.py`

### M06 — 会前刷新与版本差异

- 冻结窗口不变时刷新增加observation revision；扩大窗口或范围是新request/grant，不偷偷采集
- 新旧议题/事实差异可追溯，保留人工更正与冲突；删源撤权使整版不可继续发布
- 关键资料无法刷新则needs_input/旧版明确stale；只合并未开始投递的旧状态，不覆盖已发送/不确定结果

验证入口：`uv run --locked pytest -q tests/test_meeting_refresh.py`

### M07 — 会议专项评测与可用性

- MC01—MC24会议专项18开发+6私有保留，与旧Q01—Q24分开，关键语义case各3次并保留所有失败
- 时间/关联/权限/合并发布区分全部精确通过，议题支持与决策价值由owner盲评
- 3个人工基线+7天10份不同真实会议委托指标定义齐全；尚未真人评测只报not_run

验证入口：`uv run --locked pytest -q tests/test_meeting_eval_contract.py`

### D07 — 会议功能真实试点与发布

- 会议profile重跑D01三项stage门槛，真实Feishu+GH user只读身份、两人权限、至少2repo，扩到组织清单需覆盖证明
- 同候选完整MC24及受影响Q套件/CI/容器通过，全off上线后准确合并tree重验，再由operator单人激活
- 7天10不同真实会议委托与人工基线对比；会议flag独立关闭、原域继续，撤权/drain/恢复均留证据

验证入口：`uv run --locked pytest -q tests/test_meeting_staging_config.py`

### U02 — 外部操作日志与执行授权适配

- 复用ActionReceipt作为唯一批准权威，闭合操作kind+目标/payload/身份/attempt/fence/expiry摘要；会议读取不走外部写许可
- B中央事务侧journal与动作意图同briefs.sqlite3原子，IO前持久begin；与JSONL批准分开对账，not-started不等于provider absent
- 记录证据而非模型指定成功状态，未知副作用不因新attempt/人工备注自动重试；新持久路径有备份和领域兼容约束；合并测试文件保留原authority/故障反例，D05先验证B路径

验证入口：`uv run --locked pytest -q tests/test_effect_journal.py`

### U03 — 仓库批次证明与能力门槛

- 交付repo identity/base SHA/worktree realpath/attempt/fence/变更摘要证明契约，受控适配器验证审批前与执行前重核
- 缺证明、workspace切换、越界路径/符号链接、互相冲突ACP写操作拒绝；实际CLI不具备管控能力则不宣称支持
- 当前普通CLI没有受管cwd/ACP证据，真实路径缺conformance必须unsupported；不宣称完整ACP或自动push/PR/merge

验证入口：`uv run --locked pytest -q tests/test_workspace_attestation.py tests/test_repository_write_conflicts.py`

### U04 — 审批反馈和状态时间语义

- 已批准/待执行/执行中/结果不明独立呈现，批准反馈不等worker；旧卡控件不能复活已终结动作
- 保留既有六态Presence只读模型，仅新增反馈/时序投影与守卫；heartbeat不伪称租约/完成证明
- 接收/排队/执行/投递时间单列，clock skew有界、busy不被queued覆盖，相关日志脱敏/有界保留

验证入口：`uv run --locked pytest -q tests/test_agent_presence.py tests/test_brief_feedback.py`

### D08 — 上游适配批次验收

- 固定上游59commit分类与本地映射，保留MIT/Apache来源说明，所有源码/测试/未实测差异逐项有回执
- 复用D01隔离stage检验受管仓库写与业务effects路径；证明旧CLI不越权升级、未知副作用不盲重放
- 独立release走排空/锁/备份/准确SHA/全off/操作者激活，新journal兼容和当前digest维护恢复演练通过

验证入口：`uv run --locked pytest -q tests/test_upstream_adaptation_journey.py`

### TD01 — 生产运行时组合

- 单生产 owner 与相同生命周期绑定
- 持久启动核对及可信权限更新
- 生产接线、降级、恢复和真实验收

验证入口：`未登记，执行前补齐`

### TD02 — 部署互斥与恢复保障

- 共用主机锁覆盖发布和回滚
- 新鲜且准确的排空核验、保护目录、可信安装
- 备份恢复与全关回滚真实演练

验证入口：`未登记，执行前补齐`

### TD03 — 服务器凭据与 CLI 契约

- 目标镜像固定 CLI 契约
- 只读身份供给刷新轮换撤销
- 不复制开发机会话，真实验证

验证入口：`未登记，执行前补齐`

### TD04 — 飞书真实交互组合验收

- 首卡、回调、签名、纠错采纳真实验证
- 两身份隔离
- 手机桌面验收

验证入口：`未登记，执行前补齐`

### TD05 — 语义质量与测试稳定性

- 真实模型与保留集证据
- 异常和平台边界复现验证
- 成本与真实用户效果

验证入口：`未登记，执行前补齐`

### TD06 — Monitor 命令与 ledger 发布间隙

- 复现 poll/ACK 先于 applying 发布
- 明确持久命令和账本状态恢复契约
- 不重复执行的故障探针

验证入口：`未登记，执行前补齐`

### TD07 — 旧合成预览文本展示损失

- URL相邻中文、文件名引号标识符保真
- 回归现有预览
- 真实已生成材料验证

验证入口：`未登记，执行前补齐`

### TD08 — 重复过期进度文档

- 一个当前状态索引引用证据
- 冻结计划和历史快照与动态进展区分
- 不得再把已授权/已采集/已合并记成阻塞

验证入口：`未登记，执行前补齐`

### TD09 — CI 重复整套发布检查

- 每个 CI job 只调用一次完整 release check
- 保留部署、连接器、凭据门禁校验及独立 preflight 完整性
- 真实 Linux CI 与故障传播检查证明

验证入口：`未登记，执行前补齐`
