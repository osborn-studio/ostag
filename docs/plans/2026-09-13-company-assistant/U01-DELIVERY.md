# U01 私有投递与观测：实施记录

状态：核心库已通过本地验证；真实飞书适配器、平台投递和真人收到尚未认证。代码提交 `ce66df0f68dc22ab57eb8ce8f982b4e801ed48ef`，三文件内容摘要 `ac08e38f82556723e287e1d6be3d77791341e955f7a71fc4933abee144fc5ebe`。

## 已实现的契约

生成状态与投递状态分开。A04 的 draft_ready 和 pending outbox 只表示已有私有草稿。U01 单独记录 pending、started、unknown 和 provider_accepted；provider_accepted 不等于用户已读或收到。

同一 delivery ID 固定绑定 job、revision、请求者、私聊目标、完整文本与飞书 tenant/installation/app/bot/绑定代次。仅完全相同的未开始输入合并；不同版本、正文或身份不能覆盖原投递。发送开始标记先持久化，进程崩溃、超时或提交响应丢失不会解除它，也没有自动重发入口。失败诊断须绑定实际owner与发送令牌；观察也领取独立、持久化、单次使用且有上限的预算，未获得预算或令牌失效的操作不能改写他人的结果。

响应中的可信 message ID 可以作为精确查询候选，不能单凭 ID 宣告成功。查询结果须同时匹配完整正文、原目标、供应商身份和消息 ID，并排除修改、删除或多个结果。响应完全丢失且没有 ID 时，保持待核验，不搜索相同句子认领消息。第一次不确定记录与最终回执分开保存；保留有界观察记录和读取次数。

发送与读取均要求可信本地授权镜像；数据库等待后和适配器实际出网前检查持久化取消，再核对当前来源/grant/ACL/目标/绑定。在接受观察结果时再次检查。哈希和用户提供的 approved 标志不产生权限。任意回调、供应商异常只映射为固定诊断，不保存私有异常正文。

## 接线限制

DeliveryService 是显式注入 FeishuTransport 的库，没有默认网络实现或后台轮询。适配器必须在每次真实 IO 前调用 before_egress，验证实际返回的供应商身份，禁止重试、重定向、搜索及后台隐藏 IO；不能把请求中的绑定原样返回当作证明。当前 wire body 是受限的确定性文本契约，真实 HTTP 编码与卡片渲染由 A05 补齐并测试。当前完整 wire body 上限16KiB，超大草稿在发送前拒绝且不自动截断；A05需制定并验证显示或拆分策略。

运行期限制：最后一次出网前数据库检查若遇到锁竞争，可能保守留下unknown，即使实际尚未发送；收到message ID后若回执事务一直无法提交，该ID也可能未持久保存。这些状态不能自动重发，需在试点中观察并通过可信流程核验。持久取消在部分路径上的诊断仍可能归为transport_error，但不会因此放行发送。

运行在自有事件循环上；同步 SQLite/fsync 和可信同步回调不能被强行抢占。异步调用超时、取消或 stop 后，未实际退出的协程继续占用容量，晚到结果不能写成成功。新建更多实例不能作为绕过容量或恢复未知发送的手段。

## 数据库与回滚

默认构造仍保持 schema2，enable_manual 保持 schema3；仅显式 enable_delivery 升级为 schema4，当前实现可读1..4。旧七张表、费用账本、历史版本及 outbox 保持原样。旧二进制拒绝 schema4，没有就地降级。生产启用前按 DEPLOYMENT 完成排空、备份、固定版本和恢复演练；本轮没有操作生产数据库。

## 验收证据

| 验收项 | 本地证据 | 尚待验证 |
| --- | --- | --- |
| U01-AC1 | 稳定身份/版本/正文绑定；完全相同输入重放；多句柄仅一个发送 claim | 真实飞书适配器身份映射 |
| U01-AC2 | 丢响应不重发；可信 ID 精确查询；错误目标/发送者/版本/完整正文拒绝 | 真实 API 返回格式和精确查询语义 |
| U01-AC3 | 崩溃/重启、提交响应丢失、取消/超时、观察上限与原始不确定记录 | 真人收到、生产长期保留与容量 |

Fable首次审查发现旧费用入口仅接受schema2/3，升级4后会阻断worker；修复后新增升级数据库上继续生成并结算费用的完整回归。第二次审查发现未获得发送权的竞争者超时，可能改写赢家的不确定状态；修复操作与数据库的拥有权约束，并加入竞争失败后的错误记录隔离测试。父级独立门槛同步覆盖这两条路径；同一竞争者故障探针在保存的旧候选上复现失败，在修复候选上通过。

固定验证器：**122 项目标测试，2225 项全量通过，11 条既有警告，Ruff 通过**。另通过原八组独立故障门槛、新增冻结 schema3→4 数据保全及升级后费用/生成可运行、适配器等待期间持久化取消阻止发送/查询两组门槛，并新增失败竞争者不能改写赢家的独立探针，共11组。测试均为本地合成数据。

真实 Claude Fable 5.1 静态复审：approved，未解决阻断为0。审查绑定上述候选摘要；Fable没有执行测试，测试由固定验证器运行。

[最终审查回执](reviews/u01-fable-round-3.json) · [来源与未验证接口范围](reviews/u01-sources.json) · [机器执行记录](execution.json)

## 验证中发现并修正的问题

最初两次宿主全量运行各有4项密钥扫描测试失败：上一轮来源记录的 local_input_tokens_sdk_sha256 字段存储公开SDK文件摘要，字段名称被识别为凭据赋值。父级仅将其改名为 local_counting_sdk_sha256，摘要和来源事实不变；没有放宽扫描规则或测试。当时第二次验证已启动，未读到运行中的修正；初始loop因此结束为escalated，代码未合入。随后在独立文档提交中修正字段，逐字节恢复三文件真实Codex候选，再经真实Codex复核、完整验证和Fable审查；修正后的来源记录作为上下文另有摘要。

编码时旧的1毫秒生成超时用例曾出现失败，原断言保留，最终编码回归与宿主全量重新通过。旧连接/事务路径保持原实现，投递使用独立的非等待事务路径。不能据最终通过声称极短计时测试不受主机负载影响。

## Fable 各轮非阻塞意见（原文保留）

- Liveness (not safety) limitation: the synchronous `before_egress` recheck opens a nonwaiting BEGIN EXCLUSIVE; a concurrent SHARED read at that instant raises StoreBusy inside the transport, which is classified 'transport_error' and permanently fences a send that never happened. Spec permits conservative fencing, but a plain reader should not be able to poison a delivery; consider retrying only the pre-egress check asynchronously and documenting the residual risk.
- When a direct response carries a candidate message_id but the accept transaction cannot commit before deadline/stop/cancel, the candidate is rolled back and `_unknown` does not persist it; the delivery is then unknown with no exact-ID lookup ever possible. Consider passing the candidate into the best-effort unknown write (still no resend).
- Persisted user cancellation observed at the egress gate raises plain Conflict inside the transport and is recorded as first_failure 'transport_error' rather than a cancellation reason; misleading diagnostic only.
- test_actual_pinned_old_binary_rejects_schema4 depends on `git show` of a hard-coded baseline SHA being resolvable in the verifier checkout; fragile if history is rewritten/gc'd, but acceptable as an explicit legacy-binary gate.
- test_independent_processes_compete_for_one_opaque_claim uses a fixed child clock '2026-09-13T00:00:00Z'; it only passes because the Authority fixture clock precedes it (ClockError otherwise). Host runs confirm it passes today; note the coupling.
- Honest limitations correctly stated in module docstring: no HTTP/Feishu implementation, no uuid retention/lookup-by-uuid claims, a lost response with no ID is unrecoverable automatically, provider_accepted is never human receipt, transport Protocol is trusted code not a credential.
- No dependency changes; only the three permitted files touched; legacy _connection/_transaction paths and schema1->2 / enable_manual contracts appear byte-identical in behavior; READABLE_SCHEMA_MAX raised to 4 with exact DDL set comparison retained.
- I did not run tests or Ruff; this is a static review.
- Post-start Conflict (e.g. outbox cancelled between send and accept, or claim mismatch) is persisted as reason 'store_error' because 'conflict' is not in REASONS; the diagnostic is sanitized but misleading. Consider adding a fixed 'conflict'/'cancelled_intent' reason.
- If the requester cancels after the provider accepted (direct response in hand), _accept_delivery -> _delivery_current rejects on outbox_state='cancelled' and the candidate message_id plus full proof are discarded; delivery stays unknown with no candidate and later observe/check_delivery_egress also refuse. State never implies unsend (good), but a real receipt is lost; document or allow candidate/receipt recording for an already-started claim regardless of local cancellation.
- egress() calls check_delivery_egress with a non-waiting connection; a transient SQLITE_BUSY at that instant surfaces as 'transport_error' -> permanent unknown with no candidate and no IO. Conservative and safe, but a liveness cost worth documenting.
- Error privacy, chained-context suppression, fixed reason codes, digest binding, strict ProviderMessage parsing, single-use egress gate, token fencing across processes, bounded observations (32/64KiB), separate initial uncertainty vs first_failure vs receipt, and additive schema4 (constructor stays 2, enable_manual stays 3, exact-schema/trigger rejection) all read correctly.
- Test suite depends on `git show 5e2e79b1...` and on subprocess importability of `ostag` from the repo root; both passed in prior host runs but are environment-sensitive.
- Honest limitations acknowledged by the candidate and confirmed: no HTTP/Feishu adapter, no human-receipt evidence, lost response without ID is unrecoverable, SQLite work blocks the loop (owned-loop only).
- Custody repair verified statically at both layers: service only calls _unknown when this operation's own begin_delivery/begin_delivery_observation returned a claim (attempt/observation stay None on StoreBusy loops, timeout/stop/cancel during lock wait, lost COMMIT response, or failing status read), and store.delivery_unknown/_accept_delivery/check_delivery_egress require matching owner_id + opaque token for send or an unsettled owned (owner, token, candidate, ordinal) read record for observation. Loser diagnostics with stale/wrong/missing token or another reader's unit raise Conflict before any UPDATE; first_failure remains COALESCE-immutable and send_unverified is committed with the claim before IO.
- Send fence not relaxed: claim is a single UPDATE ... WHERE state='pending' under BEGIN EXCLUSIVE with random token; check_delivery_egress requires state='started' + token, so a post-note 'unknown' row can never re-enter egress; begin_delivery returns None (never a new token) on started/unknown/accepted rows across handles/processes.
- Non-blocking design weakness: the second, synchronous before_egress gate inside the transport uses the nonwaiting (timeout=0) transaction with no retry, so a benign concurrent reader/writer at that instant raises StoreBusy through the adapter and is recorded as a permanent 'transport_error'/unknown with no IO. Within the spec's 'once started, conservatively unknown' rule, but operationally lossy; the parent's real-contention gate could be sensitive to this if contention is not injected only into the loser.
- Diagnostic labelling: Conflict/StoreBusy raised by the in-transport egress closure (e.g. outbox cancelled after adapter await) is sanitized as 'transport_error', and non-REASONS service failures ('conflict') are recorded as 'store_error'. Fixed/sanitized and private-free, but the stored reason can misdescribe a local cancellation as transport IO.
- Minor: delivery_unknown returns early on provider_accepted without settling the observation unit (budget already spent, harmless); begin_* custody lost to a post-transaction op.check() failure leaves a started row/spent read unit with no diagnostic, matching the documented conservative boundary.
- Privacy/chaining: all DeliveryError raises occur outside except blocks; _delivery_transaction rethrows a freshly built StoreError after the finally so SQLite text/context is dropped; policy/clock/callback exceptions (incl. CancelledError and malformed DeliveryError args) collapse to fixed codes.
- Schema: enable_delivery builds 2->3->4 explicitly, constructor still stops at 2, enable_manual at 3; exact-DDL _inspect, DELETE/FULL/EXCLUSIVE preserved on the new nonwaiting path; _cost_schema accepts 2/3/4; old rows untouched. The pinned-old-binary test depends on git commit 5e2e79b... being reachable in the worktree history.
- Limitations honestly retained: no HTTP/Feishu adapter, no uuid-lookup claim, no ID recovery for an entirely lost response, provider_accepted never implies human receipt; tests are synthetic fakes and cannot certify platform delivery.

## 下一步

A05 接入私聊卡片、经过身份核对的真实飞书投递适配器和安全回调；A11 补齐默认关闭的开关与生命周期。之后按原任务依赖推进 Feishu/GitHub 会议材料采集，使用冻结评测集做语义验收，再执行阶段环境和部署门槛。本轮未发送外部消息、push、合并main或部署。


交付文档与全部审查回执写入后，4项密钥扫描回归再次通过；未修改扫描规则。[文档验证记录](reviews/u01-document-validation.json)。
