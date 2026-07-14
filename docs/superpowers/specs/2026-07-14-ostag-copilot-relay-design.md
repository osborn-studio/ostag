# OStag Copilot Relay 设计规格

日期：2026-07-14
状态：交互式设计已批准，待用户复核书面规格

## 1. 产品目标

OStag Copilot Relay 将飞书变成用户的远程开发协作控制台。用户仍是需要回答问题、补充要求和作出决策的 Copilot；OStag 负责发现由 osstack 管理的开发会话，把 AI 的关键问题送到飞书，再把用户的回答精确送回原 AI session，使开发、测试和验证可以继续。

第一版必须支持：

- 查看所有已登记 osstack 实例、任务和可恢复 AI session；
- 在飞书私聊中选择并保持一个明确可见的“当前任务”；
- 将 AI 的关键决策请求即时推送到飞书，将普通进度聚合为阶段摘要；
- 按需获取最近一轮经过安全过滤的 AI 完整回复；
- 通过引用回复或当前任务发送答案、补充要求和方向调整；
- 从飞书提出新任务，经机器、仓库、agent 和验收条件预览后确认创建；
- 在问题不影响其他工作时继续安全的独立步骤，在方向性分叉处进入 `awaiting_decision`；
- 对每条消息展示真实的送达、接受、应用、拒绝或不确定状态；
- 断网、进程重启和平台重投后不丢消息、不重复执行、不串会话。

第一版只接管由 osstack 启动和登记的 Codex CLI、Claude Code 等会话。它不接管任意手工终端会话或 Codex Desktop 聊天，不提供远程 Shell。

## 2. 与现有 Work Monitor 的关系

现有 OStag Work Monitor 仍负责跨实例状态投影、移动看板、健康、订阅、通知以及 pause/resume/retry/cancel 的受治理控制。Copilot Relay 建立在同一个中央 OStag、SQLite、reporter、身份和审计基础上，但使用独立领域模块和消息协议。

本规格有意扩展 Work Monitor 第一版“不从聊天创建任意开发任务”的边界：仅允许已绑定 owner/admin 在飞书私聊中创建经过结构化预览和一次确认的 osstack 任务。它不允许聊天文本直接成为 Shell 命令，也不改变高风险操作的 receipt 治理。

职责保持清晰：

- OStag 是路由、治理、可靠信箱和移动交互控制面；
- osstack 是任务编排和 agent session 生命周期管理者；
- 本地 AI session 与本地 osstack 状态是执行事实源；
- 云端投影或消息送达不能被解释为本地执行成功。

## 3. 已选架构

采用事件驱动的持久会话中继：

```text
用户 / 飞书私聊
  <-> 中央 OStag Copilot Relay
        - SessionRegistry
        - DecisionService
        - RelayMailbox
        - FeishuCopilotService
        - PolicyService
  <-> 现有签名 HTTPS reporter / command poll
  <-> Mac mini 或其他开发机
        - OsstackRelayAgent
        - AgentSessionAdapter
        - LocalContextVault
        - DurableSpool
  <-> osstack task/run
  <-> 原 Codex CLI / Claude Code session
```

实例只建立出站 HTTPS 连接，不向公网开放端口。中央服务继续部署在现有持久化 OStag FastAPI 容器中，EdgeOne 只承担 TLS、WAF 和反向代理。所有待发送事件和待领取消息先持久化，再通过 ACK 移动游标。

不采用以下方案：

- 实时 WebSocket 远程终端：断线恢复、去重、多机路由和安全边界更脆弱；
- 中央 OStag 重新调用一个新 AI：会丢失原 session 的工具状态、隐含上下文和未提交工作。

## 4. 组件边界

新增 `ostag/copilot_relay/`，保持平台无关：

- `models.py`：严格、版本化的 session、decision、instruction、snapshot 和 receipt 契约；
- `store.py`：session registry、双向 mailbox、去重、游标、状态与审计索引；
- `sessions.py`：session 登记、能力发现、版本推进和当前事实校验；
- `decisions.py`：决策请求、答案、并行安全工作声明、过期与冲突处理；
- `service.py`：当前任务、任务选择、方向调整、新任务预览和确认；
- `redaction.py`：上云字段白名单、密钥扫描、长度限制和回复快照过滤；
- `reporter.py`：本地事件采集、持久 spool、批量上报和中央消息领取；
- `session_adapter.py`：统一 agent session 接口；
- `rendering.py`：平台无关的决策卡、任务选择、回执和摘要视图；
- `server.py`：在现有 composition root 中注册 relay API 和唯一 mailbox owner。

飞书 adapter 只负责可信身份规范化、卡片渲染、引用关系解析与投递。身份、权限、目标 session 和动作状态都必须从服务端读取，不能相信按钮 value 或聊天文本里的标识。

## 5. Agent session 接口

每个 osstack agent adapter 必须实现稳定接口：

```text
register(task, run, session) -> SessionDescriptor
status(session_id) -> SessionStatus
send(session_id, expected_version, message) -> Accepted | Rejected
resume(session_id, expected_version) -> Resumed | Unsupported | Rejected
snapshot(session_id, turn_id, policy) -> SanitizedAssistantSnapshot
create_task(TaskLaunchSpec) -> TaskReference
```

`SessionDescriptor` 至少包含 `instance_id`、`task_id`、`run_id`、`session_id`、agent 类型、workspace/repository 的非敏感引用、当前状态、单调 `session_version`、最近 turn、支持能力和更新时间。

如果 agent 不支持恢复，不得宣称已恢复。OStag 返回“需要新建续接会话”，先展示准备注入的新会话摘要和影响，获得用户确认后才启动。

## 6. 消息协议

禁止使用一个无类型的万能 message。第一版固定支持：

- `decision.request`：AI 提问，包含问题、选项、推荐、是否阻塞、可继续的安全工作和期限；
- `decision.answer`：用户对指定 decision 的答案；
- `instruction.send`：对当前任务补充要求或改变方向；
- `assistant.snapshot.request` / `assistant.snapshot.response`：按需获取最近一轮过滤后的 assistant 回复；
- `task.create.proposal` / `task.create.confirm`：新任务的预览与确认；
- `delivery.accepted` / `delivery.applied` / `delivery.rejected` / `delivery.indeterminate`：真实回执。

每个 envelope 必须包含：

```text
schema_version
message_id
instance_id + task_id + run_id + session_id + turn_id
session_version
sequence
created_at + expires_at
sender Principal
message type + strict payload
correlation_id / causation_id
```

飞书消息只携带 opaque `relay_id` 或 `action_id`。中央服务重新加载 envelope、目标 session、actor、destination policy、TTL 和当前版本。

## 7. 飞书交互

### 7.1 当前任务

owner/admin 私聊维护一个当前任务。所有未引用的普通协作消息默认投递给它，但 OStag 必须在回复和任务卡顶部持续显示目标任务、实例和状态。若没有当前任务、任务已终止或目标不唯一，必须展示任务选择卡，不能自动猜测。

引用一条决策消息时，引用绑定优先于当前任务，答案返回该消息对应的原 session/turn。

### 7.2 推送策略

采用混合模式：

- 即时推送：需要决策、需要补充信息、阻塞、方向冲突和恢复失败；
- 阶段摘要：计划完成、实现完成、测试完成、部署完成和终态；
- 按需内容：用户点击或发送“查看原回复”，返回最近一轮安全快照；
- 普通过程事件继续进入现有看板，不逐条刷屏。

### 7.3 决策卡

决策卡显示任务、机器、问题、有限选项、AI 推荐、阻塞影响、可继续工作和过期时间。用户可以选择推荐方案、其他方案或输入自定义答案。

回答后卡片按真实状态更新：

```text
queued -> delivered -> accepted -> applied
                          |          |
                          +-> rejected
                          +-> expired
                          +-> indeterminate
```

“已发送”不能显示成“AI 已采纳”，“accepted”不能显示成“代码已应用”。

### 7.4 改变方向与创建任务

补充要求和方向调整是普通协作指令，可直接投递给当前 session，但必须携带 `expected_session_version`。若与正在执行步骤冲突，本地 adapter 进入 `direction_change_pending`，在安全停点返回影响分析后再应用。

新任务必须先展示 `TaskLaunchSpec`：目标、机器、仓库、workspace、建议 agent、验收条件和敏感操作声明。用户确认一次后由 osstack 创建，并自动设为当前任务。

## 8. 人在回路策略

采用“可并行则继续，不可逆则等待”：

- 与答案无关的测试、分析或其他独立步骤可以继续；
- 不同答案会导致方向分叉时，主任务进入 `awaiting_decision`；
- adapter 必须持久化恢复点和当前 session version；
- OStag 不会因超时擅自替用户选择，只发送有抑制的提醒；
- 用户可明确回复“按推荐方案继续”；
- 回答成功应用后才从 `awaiting_decision` 回到 `running`。

## 9. 权限与治理

复用现有 canonical `Principal(platform, actor_id)`、binding、grant、permissions、ActionReceipt 和审计链。

- 已绑定 owner/admin 私聊可查看、选择任务、回答问题和发送普通指导；
- 群聊默认只能查看和订阅，不能创建任务或改变方向；
- 普通指导直接投递并生成回执；
- 新任务需要一次结构化确认；
- 取消任务、删除成果、生产部署、合并代码和修改权限等高风险操作必须走独立 ActionReceipt 和二次确认；
- OStag 永远不把自然语言、代码块或卡片内容当作 Shell 命令执行。

## 10. 数据边界与隐私

允许进入中央控制面的内容：任务和会话标识、结构化问题与选项、推荐方案、阶段摘要、脱敏 assistant 快照、用户答案与指令、状态回执及不含正文的证据引用。

默认只留在开发机：源码、完整 diff、环境变量、密钥、完整终端历史、工具原始输出、系统提示、未请求的完整对话上下文和 agent 内部推理。

“查看原回复”只请求最近一轮 assistant response，经字段白名单、密钥检测、危险路径过滤和长度限制后返回。它不是上传整个 session。

## 11. 可靠性与冲突处理

- 本地与中央均先持久化再发送，ACK 后移动游标；
- 至少一次传输，按 `message_id` 和目标 session version 业务去重；
- 重复点击只接受第一个有效答案；
- 旧 turn、过期 decision 或版本不符的 instruction 被明确拒绝；
- 开发机离线时显示排队状态，恢复后补传；尚未送达的普通 instruction 可以撤回；
- 中央 OStag 不可用时本地任务继续安全工作，事件进入本地 spool；
- session 已退出时尝试权威 adapter resume，失败则进入确认后的新会话续接；
- 无法判断目标时要求选择，不做内容相似度猜测；
- 副作用结果无法确认时进入 `indeterminate` 并从本地事实源重新对账。

## 12. 交付阶段

### 阶段一：可靠中继

- session 注册、发现和当前任务；
- 决策请求、飞书回答、原 session 恢复和应用回执；
- 普通方向调整与最近 AI 回复快照；
- osstack 管理的 Codex CLI、Claude Code adapters。

### 阶段二：任务创建与协作增强

- 新任务预览、确认和 osstack 创建；
- 机器、仓库和 agent 推荐；
- 安全并行工作、方向变更影响分析和安全停点；
- 决策提醒、任务订阅和阶段摘要。

### 阶段三：生产强化

- 多机器故障处理、死信重放和审计查询；
- session 恢复失败后的新会话续接；
- 权限撤销、密钥轮换和灾难恢复；
- 飞书真实手机、长中文和深色模式验收。

## 13. 测试与上线门禁

自动化测试必须覆盖：

- 消息乱序、重复、断网补传和两端重启；
- 多任务并行时 session/turn 精确隔离；
- 旧回答、版本冲突、重复点击、TTL 和撤回；
- 离线设备排队与恢复；
- session 可恢复、不可恢复和恢复后状态漂移；
- 普通指导、新任务确认和高风险 receipt 的权限边界；
- 飞书伪造 actor、篡改 action ID、群聊越权；
- 源码、diff、环境变量、密钥和工具日志不会进入中央 payload。

真实验收必须证明：

1. Mac mini 上 osstack 启动开发任务并登记可恢复 session；
2. AI 提问后进入 `awaiting_decision`，手机飞书收到决策卡；
3. 用户查看最近 AI 回复并作答；
4. 原 AI session 从原上下文继续修改和测试；
5. 飞书依次收到 accepted、applied 和阶段完成回执；
6. 用户改变方向，AI 在安全停点返回影响摘要并继续；
7. 用户从飞书确认创建另一个 osstack 任务；
8. 断网和重启后重复闭环，不丢失、不重复、不串线。

上线门禁为：全量自动测试通过、osstack Loop 验证通过、生产 SQLite 备份恢复通过、真实飞书闭环至少两次通过。新 Relay 与旧监控查询路径可以并存；只有 Relay 门禁通过后，才把新的决策入口设为默认交互。

## 14. 明确不包含

- 任意远程终端或 Shell；
- 接管手工启动且未登记的 AI 会话；
- Codex Desktop 外部续聊；
- 上传源码、完整 diff、完整日志或整个 AI session；
- 让中央 OStag 代替原 AI session 继续工作；
- 根据自然语言相似度猜测目标任务；
- 绕过 osstack 或 ActionReceipt 直接执行高风险操作。
