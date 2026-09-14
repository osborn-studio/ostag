# 实施衔接记录（当前代码检查所得，后续任务未完成）

1. A04：M01 的 content_hash 标识一次受理快照，包含 intake_at；不能充当跨次请求的幂等键。入口事件必须有独立、按 namespace/platform/requester 隔离的幂等键，重放先找原任务，不要重建 intake_at 后误当新任务。
2. A04：后续每次读取、模型出网、提交、投递都重新核对当前授权/映射/ACL；这不等于重建已确认范围。原 FrozenScope 不变，当前政策变化导致 needs_input/重新确认，不能借重试换 intake_at 或 first_claimed_at。
3. A04：ExecutionTiming 必须使用 A02 持久化的首次 claim 时间及相同冻结时长。M01 can_commit 对 failed/cancelled 只允许无私有内容的状态记录；真实存储提交仍检查当前 fence。硬截止处 lease 也已到期时，由可信恢复/维护路径记录终态，不让旧尝试写草稿。
4. M04/M05：A01 SourceSet 当前最多500条；M01 的两个 provider 配额分别最多500，合计上限可能1000。必须显式调整版本化契约或保留可见部分状态；1MiB模型输入还要计算整个请求，不能静默截断后称完整。
5. A03/A04：现有 Backend.run 仅返回字符串，不能证明实际模型ID、用量或只读工具边界；需要窄的可信生成接口。模型只给 source_id，链接由可信映射生成。引用存在不等于自然语言语义被证实，真实模型/人工核验仍是独立门槛。
6. A03：费用账本应在独立brief库前向迁移，预留与结算原子、跨重启保留，未知费用不按0退款。该实施需要在原A03候选4文件之外复用store.py（第5文件），并重跑A02回归；A03已实现并完成schema1→2迁移验证，A04进一步显式升级schema3；不据此声称生产升级完成。
7. A11/发布：store抛出的领域兼容性异常尚需lifespan按brief域降级接线；主机timezone/pytest通过不能替代目标镜像tzdata、真实Feishu、排空、发布和7天试点评估。

8. A04：纯ExecutionTiming可以描述needs_input范围；服务必须先检查受理结果为accepted再入队，不把dataclass构造成功当受理许可。A02为通用brief存储，不信任payload中的accepted/authorized布尔值。


## A02 存储接线和时限

最终A02使用DELETE+BEGIN EXCLUSIVE+FULL，所有fenced方法传可信clock callable，锁后及提交前检查；不要使用旧评审里的now参数或BEGIN IMMEDIATE。A04把store.first_claim_at原样映射为M01 ExecutionTiming.first_claimed_at，恢复不重置。未来入队时间须来自可信入口时钟，不能由请求体提供未来now导致排队阻塞。LeaseGuard独立心跳必须由worker实际接入；先完成生成语义校验再调用guard.complete。

DDL/版本不符会在业务写入前拒绝；SQLite打开hot journal/WAL可能执行内部恢复或建立sidecar，这不是字节级无副作用保证。A11在打开DB前须核对brief与Monitor路径隔离；本轮只证明合成数据库的schema拒绝和已测字节不变，不冒充在线生产库零写现场验证。

成本表由A03前向迁移；保存实际schema1合成样本（queued、active、ready、revision/outbox）作独立升级门槛。新schema上线后的镜像回滚继续遵守DEPLOYMENT中可读区间和brief域降级，不能覆盖Monitor新数据。


## A03 → A04/A05 实际接口义务

A03已通过真实Fable最终复审。当前生成入口是async generate，返回draft_candidate仅表示结构/来源引用检查通过并带semantic_review_required，不代表批准或语义真实性。进展/冲突可为fact/commitment；待决事项/行动建议保持proposal。整个source/request/config/model/usage信息进入冻结provenance，实际actor/grant/resource/ACL校验仍由可信checkpoint提供，不能从模型JSON或SourceSet哈希推导权限。

最后的授权检查之后仅允许非阻塞时钟/取消检查；所有已知阻塞DB工作必须在它之前。checkpoint自身的guard/lease读取也必须早于其最后权限检查。同步SQLite会占用执行它的event loop：A04用独立worker线程/事件循环，线程安全地触发取消，保留独立LeaseGuard心跳；不能把生成协程直接挂到Monitor共享loop后宣称不会阻塞。

费用单位为整数micro-USD，参数中的token/定价界限是保守预约上界，adapter必须证明含provider framing/system overhead的完整计费输入满足它，否则拒绝调用。实际超额会保守停止该job及其namespace/UTC日的新调用；换config不能清空花费。账本schema2可读1..2并自动前向迁移，不支持降级旧DB数据。

start_cost拒绝后可能留有reserved记录；A04可用cost_record(scope, job_id, call_id)寻找并在明确未开始时调用内部release_cost。不得退款started/unknown；不能因返回结果未带reservation_id便当作没有额度占用。维护方法的WorkerContext仅为可信内部能力，不能接收端点输入；关联实际provider调用/usage是服务的职责。晚到settlement可能改写diagnostic，故作业失败原因另行持久保存；捕获StoreBusy/ClockError/Conflict，未确认的外部结果保守记为unknown。timeout会置协作Event，用户取消应另有持久意图，不能仅凭Event判定。

_LINK有意只做常见转义形式过滤，仍有如<name>、socket.io等误报和未列TLD的漏报；A05必须把所有模型文字保持纯文本、转义并关闭Markdown/HTML/autolink，只有可信SourceLink可生成可点击出处。该要求和真实模型语义评测属于后续放行门槛，不能用本轮单测代替。


## A04 本地接线完成后的约束

A04 service/worker/runner已组合A03生成器，逐条来源和当前grant/ACL/收集身份/目的地复核、事件先查后受理、原时间/config固定、单worker/单通知槽、独立heartbeat和原子private outbox均有本地测试。默认BriefStore schema2、enable_manual schema3，可读1..3；不支持旧二进制读取schema3，实际多库隔离/降级仍归A11/D02。

Future回调不得在worker控制锁或stop调用路径内执行。当前独立通知线程保留一个工作/结果槽直到回调结束；回调阻塞不会扩充线程/任务队列，stop不等待任意回调。默认A02 LeaseGuard sticky loss不改，A04显式retry_busy只重试SQLite StoreBusy；原确认lease/原deadline及单调时钟共同限界，不能续接过期owner/fence。首次等待仅可用已提交claim快照设界且服从stop，没有快照时首次busy拒绝；维护收尾的有界重试不执行provider IO或授予草稿权限。A03只收到窄GenerationStore数据库包装，不重发generate；费用证据的一次普通结算可以在lease失效后发生，进一步等待不得无界。未知费用仍需可信核对，不能盲重放。

A04-AC1的生产只读模型adapter仍未实现/认证。后续必须证明实际模型ID、usage与完整provider framing计费输入上界；真实D01与18/6语义评测另取证。当前注入的可信SourceSnapshot lookup是实际ACL mirror契约，不是已实现Feishu授权服务；precommit回调只能有界地读可信本地mirror，不能重入DB/guard。M01 FrozenScope/ExecutionTiming接线仍属于未来会议job；manual worker明确拒绝meeting payload。U01尚未投递，draft_ready/outbox不代表用户已收到。


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
