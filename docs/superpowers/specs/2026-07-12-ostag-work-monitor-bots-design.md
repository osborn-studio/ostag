# OStag Work Monitor Bots Design

日期：2026-07-12
状态：已批准需求的 OStag 原生重构版

## 1. 目标与归属

工作监控是 OStag 的原生能力，不是 osstack 内的第二个控制面。它让用户通过飞书或 Telegram 手机端，随时查看和安全控制分布在任意电脑、服务器或云环境中的 OStag/osstack 工作实例。

第一版必须提供：

- 跨实例统一任务投影与移动端纵向 Kanban；
- 阻塞、失败、需要批准和实例离线的 24 小时即时通知；
- `Asia/Shanghai` 每日 09:00 早报和 19:00 晚报；
- 私聊为主、显式白名单群聊为辅的查看与订阅；
- 查看、订阅、暂停、继续、重试、取消；所有改变执行状态的操作受权限和 receipt 治理，取消必须五分钟内二次确认；
- 飞书交互卡片与 Telegram InlineKeyboard 的等价交互；
- 只上传允许的任务元数据和脱敏摘要，绝不上传源码、diff 正文、完整日志、环境变量值、密钥或提示词全文。

## 2. 核心决策

采用“本地 reporter + 中央 OStag Python service + 多平台 OStag bot”的事件驱动架构。中央 service 以 Docker 常驻运行在腾讯云 CVM/Lighthouse（或同等支持持久容器和磁盘的主机），SQLite 与数据目录挂载持久卷。EdgeOne 只提供公网域名、HTTPS、WAF 和到中央 service 的反向代理，不承载 Python、SQLite、scheduler 或业务状态。实例只建立出站 HTTPS 连接，不开放公网端口；中央 service 维护跨实例只读投影和短期命令信箱，本地 runner/ledger 始终是执行事实源。

治理不另起炉灶：

- `OSTagRunner` 仍是消息与控制意图入口；
- `RunLedger` 记录监控事件、订阅、通知和控制结果；
- `permissions` 决定查询及控制授权；
- `ActionReceipt` 承担 pause/resume/retry/cancel 的提议、批准和执行状态；
- 中央 service 内唯一 `WorkMonitorScheduler` 复用 ambient 的窗口/抑制语义，承担定时摘要、关键事件入队和 notification outbox drain；不得在 reporter、EdgeOne 或多个 web worker 中重复运行；
- adapters 只负责平台事件规范化、发送与富交互渲染。

不采用中心轮询或上传整板快照：前者要求开发机公网可达，后者无法可靠表达顺序、幂等、订阅游标和审计。

## 3. 组件边界

### 3.1 Work monitor domain

新增 `ostag/work_monitor/`，只包含平台无关领域逻辑：

- `models.py`：版本化实例、任务、事件、订阅、命令和投递模型；
- `identity.py`：canonical Principal、私聊 owner/admin binding、群聊 enrollment/revoke；
- `redaction.py`：严格字段白名单、长度上限和敏感值替换；
- `store.py`：SQLite 事件、投影、outbox、订阅和命令存储；
- `projection.py`：按 `instance_id + sequence` 幂等、有序地折叠六阶段 Kanban；
- `service.py`：看板查询、订阅、关键事件分类和 receipt 创建；
- `reporter.py`：本地可靠队列、签名上报、心跳、命令领取及执行回执；
- `executor.py`：将领取的命令映射为本地 OStag/osstack pause/resume/retry/cancel，执行前验证目标实例、任务版本和预期状态；
- `scheduler.py`：北京时间双日报、实例健康判断、通知 outbox；
- `rendering.py`：平台无关的移动视图模型。
- `server.py`：FastAPI 生命周期、webhook/API routes、唯一 scheduler 与 outbox drain owner。

该模块不调用飞书或 Telegram SDK，也不自行实现 actor 权限、审批或审计。

### 3.2 Reporter 与中央 service

每个实例有稳定 `instance_id` 和可轮换独立密钥。reporter 持久保存 ledger inode/offset/event cursor，自动尾读 OStag `RunLedger` 的新增结构化 run、stage、receipt 和 terminal 事件并映射为 monitor event，重启后从确认位置续读。osstack 不直接写中央数据库：其 bridge 通过 `ostag monitor ingest --file/--stdin` 提交相同 JSON 契约到本地 spool，并保存 osstack task ID/version/status 到 OStag task reference，不要求移动或复制 osstack 状态库。

reporter 先脱敏并落入本地 SQLite outbox，再以 HMAC 签名批量上报。中央 service 校验实例、时间偏差、签名、schema、大小、幂等键和速率，持久化后返回确认 sequence。断网恢复后顺序补传；监控故障不得停止本地任务。

中央 FastAPI service 最终提供 `/v1/events`、board/task/instance 查询、订阅、`/v1/commands/poll|ack`、`/webhooks/feishu/card-action`、`/webhooks/telegram` 和 `/healthz`。组合顺序不可倒置：core server 先只注册平台无关 API/health/lifespan；Feishu 安全与 adapter 完成后才注册 card-action router；Telegram 安全与 adapter 完成后才注册 Telegram router，不创建占位 webhook。FastAPI lifespan 启动并监督唯一现有 `FeishuAdapter` WebSocket consumer：普通飞书消息固定走 WebSocket → `NormalizedEvent` → `OSTagRunner`，只有 card action 走 EdgeOne HTTPS endpoint。WS 崩溃时有界退避重启，关闭时取消并等待 WS、scheduler 和 outbox drain；生产只启一个 uvicorn worker，未来扩容前必须加入数据库租约和 WS 单消费者选主。

### 3.3 飞书与 Telegram adapters

扩展 adapter 能力，但保持文本回退：

- 飞书：普通消息仅由中央 service 监督的现有 WebSocket adapter 接收；出站使用 `interactive` card；card action 单独经 HTTPS 回调并转换为 `NormalizedEvent`；主动私聊必须调用 messages API 的 `receive_id_type=open_id`，群聊使用 `receive_id_type=chat_id`；
- Telegram：Bot API long polling/webhook 均可，第一版生产使用 webhook；看板以短消息分段并用 InlineKeyboard 提供查看、订阅和控制；callback query 转换为相同控制意图；
- 两端共享 `MobileBoardView`、命令解析、授权、receipt、订阅和审计，只各自实现 renderer/transporter。

`NormalizedEvent` 增加由 adapter 从签名验证后的平台原始事件推导的 `chat_type: private|group`（以及可信 destination ID）；权限判断不得从用户文本、卡片 value 或 Telegram callback data 推断私聊/群聊。

飞书 card-action endpoint 使用平台原生安全流程，不复用实例 reporter HMAC。固定处理顺序为：读取不变的 raw body/ciphertext 与 timestamp、nonce；用 encrypt key 派生的飞书平台算法做 constant-time 原始签名比较；检查 timestamp 时间窗和持久 replay key；仅在这些检查全部通过后解密；最后校验解密 payload 的 verification token 和严格 schema，再处理 URL verification challenge 或 card action。若未配置 encrypt key，则明确拒绝 encrypted payload。坏签名或篡改密文在解密前拒绝，decryptor、`OSTagRunner`、receipt 和成功审计均不得被调用。

## 4. 数据契约与状态

允许上云字段为：schema version、event/instance/sequence/time、task ID、标题、项目、阶段、进度、agent 标识、耗时、脱敏摘要、错误分类以及不含内容的证据元数据引用。严格拒绝未知字段。

Kanban 阶段固定为 `triage / todo / ready / running / blocked / done`。云端状态是投影，不得反向覆盖本地状态。实例内 sequence 单调递增；重复事件只应用一次，缺口暂存并触发数据不完整告警。

实例超过 2 分钟无心跳为 `degraded`，超过 10 分钟为 `offline`。控制命令包含 instance、task、expected version/status、action、requester、nonce、TTL 和 receipt ID，只能领取一次；过期、版本不符或跨实例命令必须拒绝并审计。

本地 `CommandExecutor` 是控制闭环的唯一执行者。它通过 OStag runner/task control port 或配置的 osstack CLI bridge 执行 pause/resume/retry/cancel；执行前重新读取本地任务的 instance、version 和 status，与命令的 expected 值逐项匹配。无对应能力、状态漂移、执行超时或非零退出都返回失败 ack，禁止用更新云端投影模拟成功。

实例注册和每次心跳都声明 `supported_actions`；卡片只启用实例当前声明的动作，服务端在 receipt/apply 时再次校验。权威基线 osstack 0.8.1 支持 `status`、`observe`、`kanban`、`cancel`、`loop.stop`，且 cockpit 已支持对 failed task 执行 retry/requeue，但缺少统一版本化 `RemoteCommand` contract；它仍不支持 task pause/resume。OStag 应适配已有 cockpit retry，只允许 failed task，读取重排/新 attempt 状态并验证结果，不得重复实现 retry。`kanban.move` 和 `daemon.resume` 严禁伪装 task pause/resume。第一阶段对缺失动作显示 `UNSUPPORTED`；上线完成前必须在 osstack upstream 实现真正 pause/resume 及统一版本化 remote control contract。cancel 发出后必须轮询本地权威状态直到 `cancelled` 终态或超时。

## 5. 命令、权限与 receipt

身份的 canonical key 是 `Principal(platform, actor_id)`，持久化、权限、receipt 和审计都使用完整二元组；禁止裸 actor ID 查权。飞书 `ou_*` 与 Telegram 数字/字符串 ID 永远位于不同命名空间，即使字符相同也不得互认或覆盖。首次 owner/admin 私聊必须通过部署时一次性 enrollment secret 或本机管理员 CLI 显式绑定到 `(platform, actor_id, destination_id, chat_type=private)`；绑定可列出和撤销。群聊必须由已绑定 owner/admin 显式 enroll `(platform, group_destination_id, scope)`，并可 revoke；平台 scoped permission 不跨平台继承。callback actor 只能来自平台签名验证后的原始 payload，不能来自按钮 value。

两端支持相同意图：

| 意图 | 文本入口 | 富交互 |
|---|---|---|
| 统一看板 | `看板`、`/board` | 刷新/筛选 |
| 任务详情 | `任务 <id>`、`/task <id>` | 任务按钮 |
| 订阅 | `订阅 <id>`、`/watch <id>` | 关注/取消关注 |
| 实例 | `实例`、`/instances` | 实例列表 |
| 控制 | 暂停/继续/重试/取消 | Feishu buttons / Telegram InlineKeyboard |

查询至少需要 `read`，订阅至少需要 `propose`，pause/resume/retry/cancel 至少需要 `write`。群聊只能查看和订阅，除非 channel policy 显式授权控制；cancel 始终只能由绑定管理员在私聊确认。

所有控制先创建 `ActionReceipt`，批准后才能进入命令 outbox。取消返回五分钟单次确认；按钮 payload 只放 opaque receipt/action ID，不携带可信权限或目标状态。服务端重新读取 receipt、actor、chat policy、任务版本和 TTL。

`ostag/core/schemas.py` 的 `MUTATION_TYPES`/`ActionReceipt` 扩展 work-control mutation 和 platform-scoped `Principal` serialization。迁移必须向后兼容：旧 JSONL ledger 中没有 principal platform 字段的 receipt 仍可读取，并按其已有 `platform`/channel 上下文归一化；无法唯一归一化时标记 legacy/unbound 并拒绝控制，不得把裸 actor ID 默认绑定到另一平台。新写入始终保存完整 principal。

## 6. 移动信息设计

以 390px 手机宽度为基线，不把六阶段压成横向六列。首屏顺序为：总体健康、运行中、阻塞、待批准、今日完成；“需要你处理”优先于普通进度。正文至少 14px、可点击区域至少 44px；长标题自然折行，无横向滚动。

飞书卡片使用高对比纵向模块和卡片动作；Telegram 使用短段落、状态 emoji 和 InlineKeyboard，一页最多呈现最重要的任务，其余分页。橙红只表示阻塞/危险，绿色只表示健康/完成。危险确认先写影响和安全退路，破坏性按钮放最后。两端都必须在 iOS/Android 真实客户端验证长中文、深色模式、刷新、确认和失败回执。

## 7. 通知与可靠性

- 即时：blocked、failed、approval_required、offline，全天发送；普通完成进入摘要；
- 定时：`Asia/Shanghai` 09:00 早报和 19:00 晚报，每个平台/目标/窗口用幂等键只生成一次；
- 中央 service 的唯一 scheduler 拥有 notification outbox drain；outbox 至少一次投递，event/delivery ID 抑制重复；429/5xx 指数退避加抖动，持续失败进入 dead letter，恢复后标记延迟补发；
- 订阅保存平台、chat/user、资源范围和投递游标；群聊必须在白名单；
- 中央 service 不可用时 reporter 继续排队，本地 OStag/osstack 不受影响。

## 8. 部署与秘密管理

生产控制面是腾讯云 CVM/Lighthouse 上常驻的 Docker 容器，运行 `uvicorn ostag.work_monitor.server:app`。`/var/lib/ostag` 作为宿主机持久卷挂入容器，保存 SQLite、ledger、outbox 和迁移版本；升级前备份并执行迁移，回滚同时恢复兼容镜像/数据备份。实例密钥、飞书 App Secret、Telegram Bot Token 只存主机权限受限 env/secret 文件，不进入仓库、事件、卡片或 ledger。每实例密钥可单独撤销轮换。当前 Telegram token 不存在，管理员必须通过 BotFather 创建后再注入；代码、测试或部署脚本不得编造 token，也不得把该外部步骤误报为已完成。

EdgeOne 配置源站反代、TLS、WAF、请求大小/速率限制和 `/healthz` origin probe；不配置 Pages Functions、KV、SQLite 或定时器。部署还包括 Docker healthcheck/restart policy、持久卷备份、飞书应用发布与事件/卡片回调、Telegram webhook secret token、实例注册及 reporter 常驻服务。`ostag doctor` 检查中央 health、持久卷可写/可恢复、单 scheduler owner、平台凭证/webhook 和实例心跳。

## 9. 测试与上线验收

单元测试覆盖契约、脱敏、签名、幂等/乱序、投影、通知规则、北京时间窗口、canonical Principal、绑定/撤销、平台 scoped 权限、receipt 和二次确认。集成测试覆盖 reporter→中央 service→投影→双平台 renderer，以及按钮/callback→receipt→实例 poll→ack→卡片更新。

故障与安全测试覆盖断网补传、重复/缺失/乱序、平台限流、中央 service 短暂故障、伪造签名、重放、跨平台同 ID 不互认、绑定撤销、callback actor 伪造、过期确认、群内越权、跨实例命令和敏感字段泄漏。

真实上线验收必须保存：中央容器镜像/version、持久卷重启恢复证据、EdgeOne 域名/WAF/origin health、脱敏后的 lark-cli 配置证据、Telegram webhook 状态、两个真实手机端的 09/19 测试摘要与关键事件、一次真实 pause/resume 和一次 cancel 二次确认闭环、实例离线与恢复、完整 `uv run pytest` 和 `scripts/release_check.sh` 结果。

## 10. 第一版边界

包含 Feishu + Telegram 双通道、跨实例监控、移动 Kanban、订阅、双日报、关键告警和四类受治理控制。

不包含源码/完整日志/产物云端浏览、从聊天创建任意开发任务、agent 调度策略编辑、发布/合并批准、多租户计费或用云端投影替代本地事实源。
