# 实施衔接记录（当前代码检查所得，后续任务未完成）

1. A04：M01 的 content_hash 标识一次受理快照，包含 intake_at；不能充当跨次请求的幂等键。入口事件必须有独立、按 namespace/platform/requester 隔离的幂等键，重放先找原任务，不要重建 intake_at 后误当新任务。
2. A04：后续每次读取、模型出网、提交、投递都重新核对当前授权/映射/ACL；这不等于重建已确认范围。原 FrozenScope 不变，当前政策变化导致 needs_input/重新确认，不能借重试换 intake_at 或 first_claimed_at。
3. A04：ExecutionTiming 必须使用 A02 持久化的首次 claim 时间及相同冻结时长。M01 can_commit 对 failed/cancelled 只允许无私有内容的状态记录；真实存储提交仍检查当前 fence。硬截止处 lease 也已到期时，由可信恢复/维护路径记录终态，不让旧尝试写草稿。
4. M04/M05：A01 SourceSet 当前最多500条；M01 的两个 provider 配额分别最多500，合计上限可能1000。必须显式调整版本化契约或保留可见部分状态；1MiB模型输入还要计算整个请求，不能静默截断后称完整。
5. A03/A04：现有 Backend.run 仅返回字符串，不能证明实际模型ID、用量或只读工具边界；需要窄的可信生成接口。模型只给 source_id，链接由可信映射生成。引用存在不等于自然语言语义被证实，真实模型/人工核验仍是独立门槛。
6. A03：费用账本应在独立brief库前向迁移，预留与结算原子、跨重启保留，未知费用不按0退款。该实施需要在原A03候选4文件之外复用store.py（第5文件），并重跑A02回归；实施时须明确记录，当前尚未完成。
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
