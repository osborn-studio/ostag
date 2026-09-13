# OpenTag增量对照与OStag适配

本轮从公开GitHub API和固定源码检视增量；没有运行OpenTag测试或服务器。最新源码不等于已发布、已验证或适合原样迁移。

## 基线与范围

- OStag main：`b73dc1d9709a4924b7928b8d7ebe9ef171720ff4`。
- 之前学习基线：`2689b07c50095974e224468fd1fb0f89d8c3c621`，来自docs/upgrade/2026-09-04-opentag-sync.md。
- 本轮OpenTag main：`3a3136dcb8395b6dda6c872a8398d21a70935a23`；compare返回ahead、59 commits、199 files，59条提交均已取得。固定比对[GitHub compare](https://github.com/amplifthq/opentag/compare/2689b07c50095974e224468fd1fb0f89d8c3c621...3a3136dcb8395b6dda6c872a8398d21a70935a23)。
- 本轮GitHub Releases最新返回v0.10.0（2026-08-17）；源码CHANGELOG还写有v0.11.0和Unreleased Agent Presence reset。没有查npm dist-tag，不将源码版本或repo pushed_at当成已发布证明。
- 当前源码README为Slack teammate，GitHub作为target；仓库metadata描述仍列旧多平台表述。采用固定代码/CHANGELOG解释当前支持面，不采用滞后description。

## 已有能力，不重复开发

OStag在9月4日已移植完整非空current-head checks与六态只读Agent Presence；completion/access/escalation/workstreams/路由解释也有既有实现。它们要回归验证，但不再计为本轮新增。旧文档延迟accepted-progress attribution是因为缺可信持久artifact与Run谱系；本轮不以会议摘要或活跃度替代该证明。

## 新增能力的适配任务

| 新变化与证据 | OStag实现决定 | 任务/验收边界 |
|---|---|---|
| ADR0006 closed effect authority；de1ef6da0204、66b9fd0e126a | 区分允许做、怎样执行、证据如何采信；复用现有ActionReceipt批准权威 | U02，不能再设第二套approval布尔值 |
| 事务侧持久日志；3bf7ccd58cb7，effect-journal.ts | IO前持久记录，permit丢失与not-started/absent/ambiguous区分，原不确定证据保留 | U02+B03；外部写首次落B，会议读取无需写许可 |
| 不确定投影观测；c523a0c3181c，provider-delivery/reconciliation.ts | Feishu只在唯一可证明时确认已发送，尚未开始的状态可合并，未知不盲重发 | U01，进入A05前置，原Slack协议不能直接当飞书API |
| attempt目录绑定与无证明写入拒绝；5b0ffd2529f8、1d3b5e180b67、9033783d282f | 受管仓库路径绑定身份/目录/attempt/fence/变更摘要；缺证明不给物质性写许可 | U03，普通CLI不自动获得ACP或受管能力 |
| ACP冲突写操作拒绝；bf8d013b118d | 严格操作分类与越界/冲突负例，不能由agent自报“普通编辑”绕过 | U03；未接入受管路径只报unsupported |
| 审批即时反馈、运行/队列计时与busy状态；87490127d118、c685418cbcbe、e37012e581d3 | 批准事实与执行/投递事实分别反馈；保留OStag多设备只读Presence语义 | U04，不把旧心跳改称有效lease |
| offline ingress及有界保留；edb4e6a6a948、49ba399abb36等 | 复用A02/U01持久接收与排空；资料、状态与观测按领域保留，不丢未决证据 | A02/A10/U01/U04；先映射再清理 |
| verified OCI、registry清理与Railway适配；ba88ef341d38、65b831184422、23eb909f0f9b | 核查并复用已有镜像SHA/摘要/受限SSH；只补适合腾讯路径的证明/清理缺口 | D02/D08，不迁Railway、不添加不需要的registry凭据 |

ADR0006本身说明垂直替代仍受验收门槛约束；本轮还读到实际effects模块、effect-journal、ACP executor与审批反馈测试，证明有源码实现，不能证明其生产可靠性。OStag改写为Python/既有生命周期适配，不整包复制TypeScript服务。

## 明确保留的OStag差异

当前OpenTag主线采用Slack-only、单paired Runner、PostgreSQL控制面、fresh-only数据库，删除Lark/Feishu等和workstreams/自动continuation；这些是上游产品收缩，不是OStag缺失的新能力。本轮明确保留飞书、多设备、现有数据库和工作流。删除历史迁移或拒绝旧数据库会破坏现有部署，不能因“同步最新”而执行。旧版Linear只读backlog思路可借鉴分页/权限隔离，不在本轮新添Linear产品。

OStag来源文件FORK_ORIGIN记录zilliztech/mfs Apache-2.0；amplifthq/opentag是重要学习/选择移植对象，当前MIT。保持两者来源说明，逐文件记录port/adapt/existing/not_applicable/deferred与固定提交；代码移植保留对应版权/许可声明，不覆盖原项目LICENSE。

U02固定首个实现边界：ActionReceipt JSONL仍是唯一批准权威；B在中央服务执行的material effect采用briefs.sqlite3内effect_attempts/观测记录，和该领域动作意图同库提交，不新增第三个业务数据库或第二份approval状态。core journal接口依赖注入领域存储，不能从core反向导入会议模块。投递outbox不等于物质性操作journal：二者复用存储/租约工具，但去重、重试与对账规则分别定义，不统一成一个ambiguous字段就算移植。跨JSONL批准与SQLite journal仍须关联ID对账。D08必须声明新增journal所在卷/模式/可读schema区间、备份及只读维护；不能用A1只备brief库的回执放行U批。U03交付的是批次/目录证明契约、冲突验证器和运行时能力门槛；当前普通CLI无受管cwd/ACP证明，不把它包装成完整ACP执行产品。以受控适配器/测试工作树先证明门槛，真实仓库路径没有conformance则标unsupported且不能在D08声称该路径已移植；这不阻塞D07，也不借本任务自动开放GitHub发布。

## 提交审计和后续维护

59条提交按上述语义族审查，机器清单包含完整SHA、标题和文件状态，见[增量清单](upstream-delta.json)。删除/迁移/数据库合并类属于上游结构变化；保留其不丢队列、输入保留与证据一致性要求，OStag无需同名表重构。D08验收时每条提交须有最终映射结果，不能把一个“同步上游”总勾当全部测试通过。

每次开发前P00重查main与上游SHA；新变化另列，不自动升级依赖/安装CLI或移植未经审核差异。本轮只生成方案，不建立持续监控任务。U02中央业务journal由D05随B阶段先完成平台验证与放行；D08重新验证共享U能力，并仅为其新增仓库适配/反馈范围放行，不能把D05所需授权推迟到依赖D05的D08制造循环。U03真实受管路径未证明即unsupported，不能由D08总勾掩盖。D07会议只读可先完成。
