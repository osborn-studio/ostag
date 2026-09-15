# A05 首张私聊卡片与翻页：实施记录

状态：本地首次卡片发送和安全翻页链路已实现并验证；真实飞书平台、手机/桌面验收及 A11 统一运行期接线尚未完成。

代码 `849fa69e91189a6541815ba1ce7af7393c1ae496`；八文件候选摘要 `2142a64451e923d250b33fea3e28a9dbf65d3ccb6798943ddb23a235964de6a8`。

## 实现与使用入口

新增独立 card-v1 账本和 HTTP 传输。可信调用方经 CardDeliveryService 的 prepare/deliver/send 接口，从原始存储草稿生成并发送首张私聊卡片；FeishuAdapter 的显式 brief_card_service 和 deliver_brief_card 入口调用该链路，绝不降级到旧 send_card、reply 或 U01 文本发送。已有 A05 预览与 br_ 回调继续兼容。

卡片身份绑定 namespace、请求者、原 job/revision/outbox、原私聊、安装/租户/app/bot/代次、完整来源/草稿指纹和渲染版本。首次准备同时固定卡片 JSON、发送字节、按钮令牌与期限；相同原请求重放返回原记录。相同 revision ID 却换正文/来源/绑定会冲突，不能暗中替换卡片或借此重发。原 U01 文本的 ID、字节、状态机和数据库 schema 保持原契约。

新账本显式初始化、固定容量，验证自有 application_id/version/DDL；未知表、视图、触发器及未来版本被拒绝。发送记录不因过期被删除，不能靠清理数据重新获得发送机会。生产保留和隐私清理策略继续由 A10 实现，不能直接删除未知发送或回调依据。

## 发送与回调顺序

1. 首张卡片里的 bc_ 按钮先是未激活能力，不绑定伪造消息 ID。
2. 发送开始之前，持久化唯一 owner/claim 与原始 send_unverified 记录。丢失本地提交回执也不会再次发送；竞争失败者无权写失败诊断。
3. HTTP 请求使用原私聊 chat_id、interactive 类型和字符串化的完整卡片 JSON，固定官方域名。创建响应须属于这次原始请求，并匹配实际 sender、租户、私聊、消息类型及未编辑/删除标志。
4. 只有正确 owner/claim 的直接创建回执可以在同一事务内写入真实消息 ID，并激活该卡片的按钮。失败/超时/取消/回执丢失都不能单独激活按钮；未知状态不搜索、不自动重发，也不接受按钮回调自己声明的消息 ID 来“修复”状态。
5. 签名回调核对请求者、私聊、租户/app、原消息、当前原版本及完整指纹；Next、Previous、Refresh 共用单调递增的页面 epoch。一次点击提交后，该页其他旧按钮也失效，避免两个并发点击同时更新。
6. 新页和新令牌以事务固定；全部当前来源/grant/私聊/版本权限在等待后与释放私有响应前检查。旧令牌不因重启恢复。回调响应丢失时，界面可能保留已失效按钮；不会为了恢复界面而隐式重发卡片。

**待补齐：已确认卡片过期后的专用重新预览入口尚未实现。** 初始与后续翻页令牌共同使用首次 prepare 的期限，不会因刷新或重复 prepare 延期；较晚收到ACK时，剩余交互时间也较短。该问题须在 A11 接线前解决，不能宣称已有恢复能力。过期或丢失响应后的用户恢复体验需要显式授权的重新预览/后续交互接线，不能重新发送同一原始记录。原始卡片与服务器准备的替换卡片不代表客户端已显示更新。

## 凭据、权限与运行边界

CardTransport 复用明确注入的 CredentialSnapshot 契约；真实 credential、安装身份、实际 sender 映射和私聊归属由可信本地基础设施提供，不能复制请求参数当证明。每次 HTTP 出网之前以及接受回执前重新检查；连接等待之后、HTTP headers/body 写入之前也有授权门槛。

独立创建状态 **create_acknowledged** 仅表示精确对应的创建请求获得经过身份核对的回执。它不是 U01 的完整文本读回证明；没有虚构飞书 interactive 消息能够完整回读按钮 JSON。content_verified 与 human_received 保持 false。

服务有明确的并发容量、总时限、取消及 stop；晚到结果不能激活按钮，尚未退出的异步传输继续占用容量。同步 SQLite/fsync 和可信同步镜像不能被硬抢占；完整生命周期、回调执行位置、镜像更新和默认关闭的开关需要 A11 统一接线。本轮没有自动轮询或默认后台发送器。

渲染沿用 A05 核验提示、可见字符转义和可信来源按钮。现有标点可读性及极端页长限制未在这次旁路修改。每页保留核验提示，全部覆盖缺口可通过分页访问。

## 本地验收证据

- 固定目标测试 **82 通过**；全量 **2395 通过**，11 条既有警告；Ruff 与锁文件检查通过，无依赖变更。
- 原12组独立门槛，加上新卡片发送权故障探针，共13组通过。探针包含持久化 begin 成功但本地回执丢失，以及失败竞争者随后读状态出错；两者都不产生第二次发送或污染赢家。
- 真实 A04 worker 调用实际 A03 生成器、产生存储草稿，再经过卡片 HTTP 创建与签名下一页/刷新/上一页。测试中的可信连接器 seam 注入 SourceLink；没有真实抓取聊天记录。原文本投递和 outbox 比较不变。
- 并发/重启/失效按钮/原消息/错身份/摘要替换/过期回拨/撤权/资源上限及传输取消均有本地回归。HTTPX MockTransport 与模拟网络不等于线上验收。

开发中新增用例发现过“最后检查过期、事务回滚、重启时钟回拨后又可发送”的问题，最终候选通过该回归。另对页面极限进行了有效边界构造，保留超大卡片拒绝策略。

第一次固定验证的发送权探针没有触发预期的状态读取故障：实现已经改用内部读取，探针仍注入公开 status 入口。探针断言未放宽；第二次实际 Codex 编码调整状态读取路径，并新增竞争失败后必须重新获取状态、不得返回早先 pending 状态的回归。最终完整验证与 Fable 审查绑定第二轮候选，首轮未进入 Fable，也未合入。

真实 **Claude Fable 5.1** 对同一候选进行静态挑战复审并批准；未解决阻断0。测试由固定宿主验证器执行，Fable未运行测试。

[最终审查回执](reviews/a05-delivery-fable-round-1.json) · [来源与协议证据边界](reviews/a05-delivery-sources.json) · [执行记录](execution.json)

### 正式复审阻断记录

- 正式宿主复审没有提出阻断。

### Fable 非阻塞意见（原文）

- Plaintext live callback tokens are persisted in cards.card_json and cards.response_card (required for byte-exact re-prepare and exact response card), unlike the legacy digest-only CallbackLedger. Any reader of the ledger file (or CardLedger.frozen caller with scope) holds usable capabilities until epoch rotation/expiry; frozen() is documented trusted-only but not otherwise fenced.
- CardLedger.begin: if the final _clock_live (deadline/stop/rollback) fails after the policy passed, it COMMITS a permanent 'started' row without returning custody or performing IO. Fail-closed against resend, but a deadline expiring at that instant silently makes the card undeliverable forever with no diagnostic. test_expiry_during_begin_final_check_cannot_reopen_after_restart accepts either outcome.
- CardTransport._exchange discards gate_error and raises a fixed CardTransportError, including when before_egress raised asyncio.CancelledError (U01 feishu_transport re-raises the exact gate exception). Harmless for activation because the parent has already abandoned the task, but the child task ends as a plain exception rather than cancelled.
- Navigation tokens (initial and every replacement) inherit the prepare-time cards.expires; an ACK late in the TTL leaves little navigation time, and this slice provides no explicit re-issue path for an acknowledged card after expiry (spec says 'explicit fresh authorized preview flow'; only the legacy br_ controller exists).
- _navigate calls the loader/authorize mirrors and renders the page (two full render_private passes over the entire draft) while holding the SQLite write lock (BEGIN IMMEDIATE). With large previews (tests use 40x900-char gaps) this can hold the lock for hundreds of ms, causing concurrent deliveries/callbacks in other processes to spin/fail on _Busy. Same-service multi-thread callbacks fail closed via the non-blocking _lock; _last_time is not thread-safe (benign).
- Several _SCHEMA string lines exceed 100 columns; if the repo's Ruff config enables E501 with line-length <=100 the lint gate will fail (not in Ruff defaults).
- test_two_processes_prepare_send_exactly_one uses multiprocessing 'fork' inside pytest on macOS; Python 3.12 emits DeprecationWarning when threads exist. If the suite runs with filterwarnings=error this test will fail; otherwise fine.
- ledger.unknown after stop/deadline intentionally uses the raw service clock (bypassing the stop latch) so the unknown/first_failure marker is still durably written; highwater rollback checks still apply.
- Acknowledge rejects a correct provider response that arrives after cards.expires (conservative unknown, no resend). Documented behaviour, but operators should expect legitimate late ACKs to be recorded as unknown.
- Verification is static only; I did not run the test suite, Ruff, or any Fable/host gates.

## 下一步开发、验证和部署

1. **A06 纠错与采用**：纠错产生新 revision，保留父版本/修改人；旧卡操作不能确认新版；采用和撤回不等于业务批准。
2. **A10 撤权与保留**：把新卡片账本纳入源资料删除、保留期限、取消和 tombstone 策略。已经发送的卡片不能保证收回，删除私有正文不能让未知发送再次执行。
3. **A05 恢复入口与 A11 运行期接线**：先增加已确认卡片过期后的显式授权重新预览，保持原消息绑定且不重发原始记录；并处理锁内整页渲染耗时与卡片账本的文件权限。A11依赖 A05/A06/A10，统一默认关闭的配置、可信凭据/来源/授权镜像、自有 worker/loop、首张卡片调度及签名路由。覆盖全off、启动/停止、排空、异常隔离和历史功能回归。
4. **阶段环境验收**：使用明确获授权的测试私聊，核验真实 create 响应字段、当前 sender 和原 chat；验证手机/桌面下一页、刷新、长内容、过期与丢响应恢复体验。分别记录创建回执、客户端显示和真人确认，不相互替代。
5. **会议能力和上线**：继续按原 DAG 接入 Feishu 历史/线程与 GitHub 变化，运行冻结语义评测；满足 release_check、CI、备份/恢复和部署回滚门槛后再上线。当前仅本地集成分支提交，未 push、合并 main、发送真实消息或部署。
