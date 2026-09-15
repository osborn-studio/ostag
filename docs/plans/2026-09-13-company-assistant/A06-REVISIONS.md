# A06 纠错、采用与撤回：实施记录

状态：本地领域/服务 API 已完成并验证。飞书纠错、采用和撤回按钮的签名接线、真实客户端验收属于后续 A11；本轮没有宣称线上按钮可用。

代码 `8ef7aa72f75de24178ce922b10143df44a0a6708`；两文件候选摘要 `ec22aa74799ab1881b9ef4e0f7680631dfaa0eb1b4743bdbfc90e1f68dc83f7b`。原计划的 A06 验收目标不变；候选实现路径调整为独立 `ostag/briefs/revisions.py` 和 `tests/test_brief_revisions.py`，避免修改只允许每个生成任务一个原始 revision 的旧存储契约。

## 行为与接口

显式初始化 RevisionLedger，再构造 RevisionService，注入可信 namespace、load、authorize、clock。load 只能返回从原始存储 outbox 构造的 PrivatePreview；authorize 每次检查所有来源、grant、请求者、私聊、安装代次和取消状态。输入请求不能自己证明权限。

- register(scope, job_id)：登记原版本的完整冻结快照；同一作用域和任务的精确重复登记幂等。
- read / render：读取完整历史与当前状态，或生成可交给受信传输层的不可变分页 JSON；返回前重新授权。
- correct：必须携带 expected_revision_id、request_id 和 text；可指定原始 section/item_id。生成新 revision，保留父版本、当前认证用户、时间和原始人工文本。新纠错取消当前采用状态，旧历史保留。
- adopt / withdraw：必须携带精确版本和 request_id。采用仅表示选择这一版内容；business_approved 恒为 false，不创建业务审批、任务、发送或外部写入。撤回要求仍有当前访问权且操作当前已采用版本；撤权后保守拒绝并不释放私有内容。

所有变更通过单个排他 SQLite 事务串行化。同一父版本的并发纠错只有一个成为下一版，另一请求得到冲突。相同请求 ID 的不同操作或文本冲突；精确重放返回历史 event，并另外返回 current 状态。读取旧“采用成功”事件不能把已经撤回或因纠错失效的采用恢复。

例如：原卡绑定 revision R0，纠错生成 R1 后，用 R0 发起新的采用会冲突；采用 R1 后撤回，再重放原采用请求，只能看到历史回执及当前“未采用”。**A11 必须从已验证签名及服务器保存的卡片绑定恢复 R0，不能把客户端 latest 或任意 revision 字段直接当授权。** 本轮测试证明的是这一领域接口约束，未完成真实采用按钮的端到端接线。

## 人工意见与原始证据

人工纠错是明确署名的注释，可以针对整篇、章节或原始条目。它不会覆盖 SourceSet、原始 claims、coverage、URL、摘录或来源事实。原始生成 revision/outbox、U01 文本字节和 A05 已发送卡片仍保留原契约；新版本不暗中更新或重发旧卡。

渲染先保留原始草稿、所有覆盖缺口与可信来源链接，再按顺序展示完整人工纠错及父版本/编辑者/时间信息。每页均说明人工意见未经核验、采用不等于业务批准。所有非可信文本沿用可见字符转义，只有冻结的可信 SourceLink 可点击。长标识和纠错分段展示，不静默截断。

## 存储与失败语义

独立 schema v1 账本检查 application_id、DDL、版本、元数据和历史一致性；未知或未来 schema 拒绝处理，不自动迁移旧业务库。DELETE/FULL、50ms 锁等待；同步 fsync 和镜像回调无法强制抢占。数据读取、完整渲染和 SQL 操作完成后，再做当前根快照/授权/时钟检查，随后提交或返回私有数据。

根快照包含请求者、任务、原 revision/outbox、来源、草稿、链接、私聊和安装身份；复用相同 ID 却改变正文或代次也会被拒绝。回拨时钟、取消、权限变化及数据库异常不会被当成成功。错误为固定诊断，正文、路径与下层异常不写进公开异常上下文；返回对象隐藏私有 repr。

单条纠错上限8KiB，根快照1MiB，纠错链至多32版，任务事件至多256，总数据行至多10000（默认1000，可配置更小），分页至多4096、单页16KiB。每次采用为后续撤回保留任务事件槽，全局准入还保守地按已登记根版本预留空间，其他任务不能占用撤回容量。这会降低可用容量；没有通过无限追加或自动清理绕开硬上限。

SQLite 官方说明：非WAL模式下 EXCLUSIVE 事务阻止其他连接读取；本实现以此避免提交前再等待外部读者。DELETE/FULL 不能作为断电后最后一次提交必然保留的保证，官方建议需要此保证时使用 EXTRA 并依赖正确的底层文件系统。本轮验证了本地重开与注入故障，未认证宿主断电/磁盘故障；生产前须统一复核各账本的同步策略与恢复门槛。[事务文档](https://www.sqlite.org/lang_transaction.html) · [同步设置](https://www.sqlite.org/pragma.html#pragma_synchronous)。

账本依赖可信本地文件权限，摘要不提供防篡改保证；当前权限镜像与 SQLite 也不构成跨系统原子事务。若 COMMIT 成功但回执丢失，调用方不能从异常推断失败，也不能换请求 ID 重试；在仍有访问权时，用同一请求 ID 和精确参数恢复历史结果。A10 负责原快照、人工意见和历史的保留/删除，本轮不自动清理。

## 开发与验证证据

- 真实 Codex 在 osstack 原生隔离工作树编码，首先运行缺失模块的 RED 测试，再完成实现和故障回归。
- 固定目标测试 **103 通过**；全量 **2498 通过**，11 条警告；Ruff 和锁文件检查通过，无依赖变更。
- 原13组独立门槛及新增撤回容量探针，共 **14组通过**。新增探针包括满额后的重启撤回、另一任务注册竞争全局剩余容量。
- 真实本地 A04 worker 生成存储草稿后，经过登记→纠错→分页→采用→撤回→重启重放；原数据库导出、outbox 和 SourceSet 保持一致。测试中的模型和外部传输为受控替身，没有真实平台或语义验收。
- 其他测试覆盖跨用户/namespace、来源/私聊/代次变化、渲染后撤权、并发连接、重复请求、时间回拨、未来事件损坏、静默忽略写入、COMMIT歧义、未知schema、长文本和可点击注入。

开发中独立探针确认过“成功采用后，事件满额导致无法撤回”，后续实际候选修复了每任务和全局预留。同一开发过程还通过回归修复未来事件被较晚时钟掩盖、静默忽略根记录写入、长合法标识导致渲染超限的问题。最终验收以固定验证器的完整结果和候选摘要为准，早期通过的部分用例不代替最终结果。

真实 **Claude Fable 5.1** 进行了 1 轮正式静态挑战复审，最终批准，未解决阻断0。测试由固定宿主验证器运行，Fable 没有运行或模拟测试。

[最终 Fable 回执](reviews/a06-fable-round-1.json) · [本地契约来源摘要](reviews/a06-sources.json) · [执行记录](execution.json)

### 正式复审阻断历史

- 正式 Fable 宿主复审未提出阻断。

### Fable 非阻塞意见（原文）

- Verified paths (static): exact expected_revision_id required for correct/adopt/withdraw (no 'latest' default); replay is detected by (scope,job,request) BEFORE the head check and compares (operation, expected, text, section, item_id) exactly, returning the immutable stored event plus separately recomputed current state, so a replayed adopt after withdraw or after a new correction never re-adopts; UNIQUE(scope,job,parent) backs the CAS for one child per parent; new correction resets adopted_revision_id in _history; withdraw requires adopted==expected==head; RevisionState.business_approved is a constant False property. Final _fresh(scope,job,raw) + clock fence run after all SQL/rendering and before COMMIT; failure rolls back via connection close.
- Reads and renders are writes: every operation (including read/render and replays) takes BEGIN EXCLUSIVE, requires clock >= highwater and updates revision_meta.highwater. Consequences: the ledger must be writable to read; any backward clock skew (or a slightly slower clock on a second instance sharing the file) blocks reads with RevisionClockError; and with the 50 ms busy timeout, a concurrent reader fails with RevisionStorageError whenever another operation (which holds the EXCLUSIVE lock through loader/authorize callbacks, full 1 MiB root revalidation in _snapshot, and full-page rendering) exceeds 50 ms. This is consistent with 'distinct operations exactly serialize' but is a real availability limit; A11 should expect and surface busy failures as retryable.
- The two-connection tests (test_two_connections_serialize_head_and_deduplicate_event, test_distinct_adoptions_serialize_and_same_withdraw_deduplicates) only catch RevisionConflict; if the winning transaction (with synchronous=FULL fsyncs and quick_check) exceeds 50 ms on a slow disk, the loser raises RevisionStorageError and the test fails. Likely fine on an internal APFS SSD tmp_path, but flaky on slower storage.
- test_lock_wait_then_revocation_is_seen_before_commit does not actually demonstrate a lock wait: the blocker is rolled back immediately after authorize fires (which happens in the pre-lock _fresh), so the worker usually never blocks on BEGIN EXCLUSIVE. It does prove the final precommit re-authorization sees revocation; the test name overclaims.
- revision_roots declares `job TEXT NOT NULL UNIQUE` in addition to PRIMARY KEY(scope,job). This makes job IDs globally unique across namespaces/principals in the ledger: if BriefStore job IDs could ever collide across scopes, a second scope's legitimate register would fail with RevisionStorageError (IntegrityError) rather than succeed or be Denied, an existence oracle. With server-random job IDs this is negligible, but the constraint is undocumented and unnecessary given loader-based ownership.
- _verify runs PRAGMA quick_check(1) plus full schema comparison on every operation (three to four times per call, inside and outside the transaction). Cost is O(file size); with up to MAX_ROWS roots of up to MAX_ROOT_BYTES each this can grow large. Row bounds are enforced; byte bounds are not.
- Capacity reserve is conservative across multiple roots: correct/adopt reserve `roots` global rows, so with several registered roots and a tight capacity a root may never be able to adopt even when free rows exist. Documented as conservative; just be aware when choosing capacity.
- Rendering differs from rendering.py in one respect: an oversized trusted SourceLink button (e.g. a 2000-char URL made of astral characters, ~12 bytes each under ensure_ascii) is not visibly omitted; the whole render raises RevisionLimit. Only trusted roots can trigger this, so it is fail-closed rather than unsafe.
- Unexpected Python errors (e.g. TypeError from a missing keyword argument, RenderError from defang) are mapped to RevisionStorageError by _public. Hygienic, but the diagnostic is misleading for caller bugs; test_explicit_initialize_and_mutation_revision_are_required relies on this by catching the RevisionError base.
- Unverifiable assumptions from imported test helpers (preview(text=...), preview(Coverage(...)), evidence/output/validate, Authority/BriefWorker, FeishuBinding.to_dict/bool-generation rejection, RequesterContext.__post_init__) are consistent with the quoted contracts but were not checked against their source. Ruff line-length config is unknown; several lines are ~103 chars, similar to existing rendering.py lines.
- Doc contradiction to note for A11: the module says neither callback may do IO under the ledger lock, yet the loader is expected to be 'normally constructed from the A04 outbox' and is invoked inside the EXCLUSIVE transaction at precommit. Composition must supply a pre-materialized/local mirror or accept that BriefStore reads occur under this ledger's lock.

## 复审意见的落实与证据边界

- read/render/重放也要求可写账本，并推进时钟高水位；跨实例时钟偏差和50ms竞争超时会导致读取失败。A11须提供统一可信时钟、专用执行线程、明确的重试体验；变更发生提交歧义时仅可复用原请求ID，不能自动换ID。
- 每次操作重复进行schema/quick_check并重验证根快照，开销随文件大小增长。现有行数与单项大小限制不等于已完成总字节预算或负载认证；多用户上线前须测量并设计字节预算。
- 原job ID在该账本中全局唯一。应使用原BriefStore生成的随机ID；多个来源库合并时需要明确命名空间映射，不能假设相同字符串可跨scope独立登记。
- A06渲染中的极端可信URL可能令整次渲染拒绝，而旧A05渲染器会显示省略提示。这是可用性差异，完整客户端验收前须明确产品行为。
- 名称包含lock wait的撤权测试只证明最终授权能看到撤权，不能宣称已证明线程真正等待过锁。并发测试在慢磁盘上可能因忙错误失败；本轮环境已通过，尚未覆盖慢存储压测。
- load在最终事务中再次调用；A11必须提供预先物化、当前且有界的本地镜像，不能在回调里现查BriefStore或远程系统。原始outbox桥接测试不等于已经提供生产镜像基础设施。
- Fable未读取所有测试辅助实现，其关于辅助函数与行长的意见是静态审查边界。固定宿主验证器已经实际运行完整用例和仓库Ruff并通过，不能把这些推测写成实际失败。

## 下一步开发、测试、验收、部署

1. **A10 撤权与保留**：统一生成、原摘录、A06人工意见/采用历史、U01和卡片/回调账本的删除与保留策略。测试生成中撤权阻止后续出网和投递、旧结果不再发布、清理后缓存/索引不复活；未知发送和已发送记录不能因删除正文而再次发送。
2. **A05 过期恢复与性能**：增加已确认过期卡片的显式授权重新预览，保持原消息绑定和发送墓碑；测量锁内渲染耗时。A06分页为有界单次构造，但同步执行及多用户排队仍需实际负载验证。
3. **A11 单服务接线**：在A10和A05恢复完成后接入默认关闭开关、可信权限/来源/凭据镜像、独占worker/loop生命周期、签名按钮、纠错/采用/撤回入口与首次投递。测试全off、旧功能隔离、启停/排空、重复/旧卡/错用户、重启以及撤权后的响应。
4. **资料与会议路径**：沿冻结 DAG 推进A07 Feishu历史/线程、A08表质量、A09历史简报组合及会议功能；GitHub变化和聊天资料均保留时间范围、来源及缺口。跑冻结语义评测，不用单元测试代替会议材料质量验收。来源刷新不是人工注释：A09 改变 SourceSet 时需要新的授权根快照及独立生成版本关系，不能复用 A06 冻结根摘要。
5. **阶段环境验收**：使用明确授权的测试私聊，核验真实发送响应、签名按钮、手机/桌面分页、旧卡冲突、采用/撤回和过期恢复。分别记录创建回执、客户端展示、内容核验和真人采用。
6. **上线实施**：按部署文档完成release_check、CI、备份/恢复与回滚演练，再通过既有GitHub Actions→Tencent Lighthouse流程发布并核验healthz、版本回执与关键私聊旅程。本轮仅本地集成提交，未push、合并main、发送真实消息或部署。
