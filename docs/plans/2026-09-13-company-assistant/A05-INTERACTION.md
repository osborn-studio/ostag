# A05 私聊预览、安全回调与飞书 HTTP 传输

本页保留 A05 第一阶段基础实现的历史记录。首次卡片发送和翻页已在后续完成本地验证，见 [最新卡片交付记录](A05-CARD-DELIVERY.md)；完整服务器接线及真实客户端验收仍待完成。

代码提交 `cee76573207385099e5b93e33c1fb142174a88eb`；七文件候选摘要 `e96465bb7d7e3eed22f34010bde19d5d3cba1b0e10d648bc0c3ff338ee4c1b17`。

## 交付范围

- 从 A04 已存储且经过结构验证的草稿构造私聊预览，核对原请求者、job、revision、来源集合和可信链接。完整草稿仍保存在原 revision，U01 冻结文本及其投递 ID 不改变。
- 有界分页，保留覆盖缺口数量和人工核验提示；逐页展示全部缺口、主张与来源。普通正文采用可见的字符转义来阻止链接、Markdown、HTML、提及和控制字符被激活；只有可信 SourceLink 按钮可跳转。这会牺牲部分标点可读性，仍需真实客户端体验评审。
- 独立 SQLite 回调账本，显式初始化，固定容量与持久化时间水位；随机一次性令牌绑定 namespace、请求者、安装/租户/app/代次、原私聊、原消息、job/revision、用途、动作、页码和不超过300秒有效期。到期边界拒绝，重启与并发只允许一次消费。
- 回调仅做预览/刷新。签名验证沿用 FeishuCardSecurity；br_ 分支不会落入旧控制指令。消费前及渲染后重新验证全部当前来源、授权、私聊和版本；晚到失败不会恢复已消费令牌。群内状态不包含私人正文、来源或回执。
- 新 FeishuTransport 通过 HTTPX 实现 U01 文本创建和精确 message ID 查询。固定官方域名，显式凭据/安装/发送者/私聊归属镜像，无环境凭据发现、自动重试、重定向、搜索或公开降级。发送与查询都在实际 HTTP 边界再次检查，完整响应受字节和分块上限约束。
- 只有匹配完整正文、原 chat、实际 sender 类型/id/id_type/租户、未编辑未删除标志以及当前安装代次的回执可以成为平台接收证明。缺字段不默认安全；provider_accepted 从不表示真人已读或收到。

## 可组合接口及边界

`PrivatePreview.from_outbox` 加载原草稿；`BriefCallbackController.prepare_preview` 返回经过当前权限检查的单页；`issue` 为明确已知的原私聊消息创建回调令牌。`FeishuAdapter(brief_controller=...)` 提供本地准备接口；`create_feishu_card_router(..., briefs=...)` 显式接入已经通过安全依赖验证的回调。默认没有 controller，也不会隐式发送。

`ostag.briefs.feishu_transport.FeishuTransport(resolve=...)` 注入 U01 DeliveryService。resolver 必须由可信基础设施提供不可变凭据快照、独立安装身份、实际 sender 映射和私聊归属，并在旋转/撤权后立即反映变化。它不是从请求里复制绑定的证明机制；本轮没有读取真实凭据或验证线上镜像。

回调的 load/authorize/binding 镜像必须是有界、同步、非阻塞、非重入的可信本地读。authorize 对所有来源/grant/当前版本/原私聊再次检查，不能把哈希或 approved 字段当权限。同步 SQLite/fsync 不能被硬抢占，完整运行期容量与线程/自有事件循环在 A11 接线。

旧 U01 文本里若含可能激活的 URL/提及/标记，本适配器拒绝发送并保持原文；不能删链接、改正文后沿用同一 delivery ID。过滤范围比“危险链接”更宽：@、尖括号、反引号、星号、格式控制字符（包括部分emoji的ZWJ）以及 word.word 形式都可能拒绝；Node.js、i.e.、St.Louis 和包含可信 URL 的正常草稿也可能因此不能作为文本发送。这是已知可用性限制，后续需在防激活测试约束下收窄过滤。真正的自动卡片发送需要新的、版本明确且不可变的卡片 outbox；本轮不会用旧 send_card 或 reply 绕过一次发送与不确定状态机制。

## 验证证据

固定宿主验证：**88 项目标测试、2313 项全量通过、11 条既有警告，Ruff 通过**。锁文件检查通过，依赖未改。12组父级故障门槛通过，包括真实 A04 存储草稿到 A05 预览、渲染期间撤销来源权限不得释放内容、失败后令牌保持已消费。

测试覆盖真实签名回调路由、两个进程争用、重启重放、错 actor/chat/tenant/app/message/revision/代次、独占到期、回拨时钟、容量/锁等待、恶意正文/来源、资源限制、响应字段缺失、HTTP库出网门槛及 U01 全链路。HTTP测试使用 MockTransport 或模拟网络，没有真实发消息。

真实 **Claude Fable 5.1** 宿主静态审查已批准，同一候选摘要、未解决阻断0。Fable不执行测试；测试来自固定宿主验证器。编码沙箱里另一次 Claude 尝试返回 Not logged in、用量0，不计为审查证据。原始输出保留在本地运行工件。

[最终 Fable 回执](reviews/a05-core-fable-round-1.json) · [源码依据与平台待验证项](reviews/a05-sources.json) · [机器执行记录](execution.json)

### 正式复审提出并经后续候选处理的阻断

- 本次正式宿主复审没有提出阻断。

### Fable 非阻塞意见（原文）

- feishu_transport._UNSAFE is far stricter than the module docstring implies: it rejects any '@', '<', '>', '`', '*', any Cf-category char (e.g. ZWJ in emoji sequences) and any 'word.word' token (e.g. 'i.e.', 'Node.js', 'St.Louis') anywhere in the frozen revision JSON. This is fail-closed and permitted by the brief, but it will make a meaningful share of legitimately A03-validated drafts undeliverable over the U01 TEXT path. Parent docs should state this breadth explicitly; consider aligning with A03's _LINK plus '<at' detection in follow-up.
- rendering.render_private fails closed (RenderError 'preview page exceeds bound') rather than exceeding 16 KiB in the worst case of 128-char job_id AND revision_id consisting mostly of '_'/'-' (each defanged to ~18 ASCII-escaped bytes) combined with a full ~11000-byte page. Correct behaviour, but callers should expect that edge.
- CallbackLedger.lookup takes BEGIN IMMEDIATE (write lock) solely to persist the observed highwater; every lookup therefore contends with claims/issues under the 50 ms budget. Acceptable per the documented fail-closed contention policy, but it lowers throughput of read-only replays.
- The callback capability key binds job/revision ids but not a digest of the rendered draft; correctness relies on the U01 invariant that a stored revision is immutable for its revision_id. _current() re-loading and comparing the full PrivatePreview mitigates in-process, but note the assumption.
- Tests rely on environment assumptions the verifier will settle: multiprocessing 'fork' context on macOS, tests-dir imports (test_brief_validation/test_brief_service helpers such as evidence()/Authority/SCOPE and the coverage gap text 'missing page'), IdentityStore.bind_private being re-callable on the same monitor.db across multiple signed_app() constructions, and pytest-asyncio/anyio auto mode for unmarked async tests.
- Ruff: a handful of lines exceed 100 chars (e.g. callbacks.py controller attribute assignment, feishu_transport.py before_egress/token check, one transport-test parametrize entry) and test import lists are not isort-ordered (BriefCallbackController, CallbackLedger, CallbackError; render_private before render_group_status). Existing repo code already has >100-char lines, so this is likely not enforced, but flagging in case I001/E501 are enabled.
- Scope confirmation: no auto-send card lifecycle, versioned card outbox, or device/platform acceptance is claimed; uuid is intentionally omitted from the create body; original U01 wire bytes are transmitted unchanged; adapter helper only prepares a local card and never calls send_card/reply.

## 下一实施步骤

1. A05 剩余：冻结新的卡片投递格式、页码与回调令牌生命周期；卡片 ID 不复用旧文本 ID，发送开始持久化后不得因超时自动重发。先定义首次投递、刷新和下一页的原消息绑定，避免尚未收到平台 message ID 就伪造回调绑定。
2. 接线测试：使用同一原请求，从 A04 草稿到卡片 outbox、实际 HTTP 编码、平台 ID 记录、签名回调再到下一页；覆盖首次响应丢失、消息 ID 未知、撤权、回调过期、重启与并发，群内不得出现私有降级内容。
3. A11：显式配置默认关闭的功能开关、可信镜像和运行期所有者；禁止请求级新建 DeliveryService 绕过容量。启动/停止、故障隔离、迁移备份/恢复和回滚按原 DEPLOYMENT 执行。
4. D01：在获授权测试私聊中认证真实 create/get、实际 sender 和内容返回格式，验证桌面/手机卡片布局、长文本分页及来源跳转。单独记录平台接收、真人收到和内容正确性，不能以合成测试代替。
5. 继续原任务依赖上的 Feishu/GitHub 会议资料采集、冻结评测集语义验收，完成阶段环境测试再部署。本轮未 push、合并 main、发送外部消息或部署。


## A05 首次卡片发送与翻页本地完成

代码 `849fa69e91189a6541815ba1ce7af7393c1ae496`。新增独立 card-v1 outbox、唯一发送权、直接创建回执激活、原消息绑定及 epoch 翻页；相同原始卡片不重发，旧文本记录不改写。82 项目标、2395 项全量及13组父级门槛通过，真实 Fable5.1批准。

[实施契约与剩余验收](A05-CARD-DELIVERY.md)。实际手机/桌面、平台身份和 A11 生命周期尚未认证；依原 DAG 下一步 A06，然后补齐 A10/A11。
