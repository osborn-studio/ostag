# OStag 多设备实时 Copilot 看板设计

**日期：** 2026-07-16
**状态：** 已通过对话评审，等待书面规格复核
**范围：** 同一飞书 OStag 身份聚合 MacBook Pro/Codex 与 Mac mini/Claude Code，并支持实时查询、精确路由与移动端决策

## 1. 背景与问题

现有 OStag 飞书看板已经能渲染单个 osstack 会话，但它展示的是中心数据库中已有记录的快照，而不是用户两台设备上的实时开发现场。

2026-07-15 的生产诊断显示：

- 中心端只有一个有效实例 `ostag-local`，显示名为 `Mac mini`；
- 该实例只有一个 `codex-cli` 会话，且会话状态和摘要已经陈旧；
- MacBook Pro 上存在运行中的 OStag Reporter 与 Copilot Relay Worker；
- Mac mini 上的 Claude Code 没有以独立设备和 Agent 进入中心端；
- 两台设备缺少独立、稳定的设备身份，导致看板无法准确表达“哪台设备、哪个 Agent、哪个会话正在工作”。

因此，本项目不是单纯重画卡片，而是补齐设备采集、身份绑定、实时事件、双向指令和恢复语义，再以设备优先的 UI 呈现结果。

## 2. 目标

1. 同一飞书 OStag 身份始终显示其已绑定的 MacBook Pro 和 Mac mini，包括离线设备。
2. 自动发现每台设备上的 osstack Agent 与会话，首期覆盖 Codex 和 Claude Code。
3. 状态变化在 10 秒内进入 OStag 中心端；等待决策、失败和完成事件立即主动推送。
4. 飞书回复精确路由到指定设备、Agent、会话和 turn，禁止跨会话串线。
5. 设备断网、进程重启和中心服务重启后，事件不丢失、不重复，绑定和待决策可恢复。
6. 默认只上报完成看板所需的非敏感元数据、摘要和决策，不采集源代码、密钥或完整终端内容。
7. 先在用户自己的两台设备灰度，真实验收通过后再扩展到员工设备。

## 3. 非目标

- 不把飞书做成完整终端或远程桌面。
- 不上传完整 workspace、代码仓库内容或无筛选的 Agent transcript。
- 不在首期支持任意第三方 Agent；首期只保证 Codex 与 Claude Code，接口为后续扩展保留。
- 不允许中心服务器主动连接位于 NAT 后的 Mac；所有连接由设备向外建立。
- 不在本项目中重做 osstack 的任务编排和执行引擎。

## 4. 核心产品决策

### 4.1 设备优先的信息层级

主看板按设备分组。每台设备卡展示：

- 设备显示名和在线状态；
- 当前 Agent 类型；
- 当前会话标题、状态和最近进展；
- 最后更新时间；
- 是否等待用户决策；
- 进入设备详情或处理决策的主操作。

设备详情页按 Agent 和会话展示时间流。设备离线后卡片继续存在，保留最后在线时间、最后摘要和未处理决策。

### 4.2 实时与推送策略

- Device Gateway 每 5 秒发送心跳。
- Agent 状态和回复发生变化时立即上报，不等待下一次心跳。
- 30 秒没有心跳时标记“连接不稳定”。
- 2 分钟没有心跳时标记“离线”。
- 等待决策、任务失败、任务完成立即发送飞书卡片。
- 普通编码、测试和部署进度只更新实时看板，避免刷屏。
- 设备恢复连接时静默恢复在线；如果仍有未处理决策，则重新提示一次。

### 4.3 指令路由策略

- 从设备详情点击“继续这个会话”后，当前飞书上下文固定到该设备和会话。
- 包含明确设备名或 Agent 名的自然语言指令按唯一匹配目标路由。
- 只有一个等待决策会话时，“继续”等短回复可以自动路由。
- 同时存在多个候选会话时不猜测，返回设备/Agent/会话选择卡。
- 决策卡必须展示“本次回复将发送到：设备 · Agent · 会话”。

## 5. 总体架构

采用“每台设备一个 Device Gateway”的架构。

```text
MacBook Pro
  ├─ osstack daemon
  ├─ Codex adapter
  └─ OStag Device Gateway ─┐
                           ├─ outbound HTTPS ─> OStag Central ─> Feishu OStag
Mac mini                   │                    ├─ device registry
  ├─ osstack daemon        │                    ├─ event/session store
  ├─ Claude Code adapter   │                    ├─ command queue
  └─ OStag Device Gateway ─┘                    └─ board/push renderer
```

### 5.1 Device Gateway

每台 Mac 只运行一个 Gateway，职责为：

- 持有稳定且不可重复的设备 ID；
- 完成一次性用户配对和设备密钥注册；
- 连接本机 osstack daemon；
- 自动发现支持的 Agent runtime 和会话；
- 将本地事件规范化、排序并写入持久 spool；
- 将事件和心跳向中心端上报；
- 长轮询中心命令队列，将指令交给准确的本地会话；
- 收到中心确认后推进本地游标；
- 断网重连后按序补传。

Gateway 通过 adapter 边界接入 Codex 和 Claude Code。adapter 只负责把 Agent 原生状态映射为统一契约，不负责用户权限和跨设备路由。

### 5.2 OStag Central

中心端职责为：

- 验证设备身份和签名；
- 维护用户、设备、Agent runtime 和会话的归属关系；
- 按设备事件序号幂等摄取事件；
- 聚合在线状态、当前会话、最近摘要和待决策；
- 为飞书生成设备优先看板；
- 将用户回复写入精确绑定的命令队列；
- 记录投递、执行与确认回执；
- 在关键事件上主动推送飞书卡片。

### 5.3 Feishu OStag

飞书是用户入口，不是数据源。它只展示中心端的授权投影，并把用户操作转换为 opaque callback 或文本指令。飞书负载不得携带可信设备 ID、用户 ID 或权限范围；所有目标都由服务端 callback 映射解析。

## 6. 身份、设备与配对

### 6.1 稳定设备身份

每台设备首次启动时生成随机 `device_id` 和设备密钥，并存入 macOS Keychain。设备显示名可以修改，但 `device_id` 不随主机名、网络、Agent 升级或重启变化。

禁止两台设备共用 `ostag-local`。迁移后建议显示名：

- `MacBook Pro`
- `Mac mini`

### 6.2 一次性配对

1. 用户在飞书私聊 OStag 请求“添加设备”。
2. OStag 返回短时、单次使用的配对码。
3. 用户在对应 Mac 执行 Gateway 配对命令。
4. 中心端把 `device_id` 绑定到已验证的飞书 principal，并签发设备凭证。
5. 以后该设备自动恢复连接，无需重新配对。

设备解绑或密钥轮换后，旧凭证立即失效。普通员工只能看到自己绑定的设备；全局负责人按现有授权模型查看团队设备。

## 7. 统一实时数据模型

### 7.1 DeviceRegistration

- `device_id`
- `owner_principal`
- `display_name`
- `credential_fingerprint`
- `gateway_version`
- `capabilities`
- `last_seen_at`
- `connection_state`
- `revoked_at`

### 7.2 AgentRuntime

- `device_id`
- `runtime_id`：设备内稳定、重启后可重新关联
- `agent_type`：首期为 `codex` 或 `claude-code`
- `agent_version`
- `capabilities`
- `state`
- `last_seen_at`

唯一键为 `(device_id, runtime_id)`。

### 7.3 AgentSession

- `device_id`
- `runtime_id`
- `session_id`
- `task_id`
- `run_id`
- `title`
- `state`
- `latest_summary`
- `session_version`
- `last_turn_id`
- `updated_at`

唯一键为 `(device_id, runtime_id, session_id)`。同名会话不得合并。

### 7.4 DeviceEvent

- `event_id`
- `device_id`
- `device_sequence`
- `occurred_at`
- `kind`
- 可选的 `runtime_id`、`session_id`、`turn_id`、`session_version`
- 经过大小限制和敏感信息过滤的 payload

中心端以 `(device_id, device_sequence)` 保证顺序，以 `event_id` 保证全局幂等。序号缺口进入 pending 队列，补齐后按序折叠。

### 7.5 RoutedCommand

每条飞书指令绑定：

```text
principal + device_id + runtime_id + session_id + turn_id + session_version
```

Gateway 只能拉取自身 `device_id` 的命令。Agent adapter 在执行前再次验证 runtime、session 和 version；任何目标变化都返回 stale receipt，不把指令发给当前碰巧活跃的其他会话。

## 8. 事件与命令协议

沿用当前 OStag Reporter/Copilot Relay 的向外连接模型，扩展而不另建第二套传输：

- 心跳：5 秒；
- 事件批量上报：事件发生后立即尝试，失败写 spool；
- 命令长轮询：连接可用时持续等待，避免中心主动访问 Mac；
- ack：中心持久化成功后才确认事件；Gateway 本地 adapter 确认执行后才确认命令；
- 重试：指数退避并加抖动；心跳恢复后优先补传历史事件，再发送新快照；
- 快照：每个会话定期发送有版本号的受限快照，用于修复极端情况下的状态漂移；
- 时间：服务端使用接收时间判断连接健康，事件发生时间仅用于展示和会话排序。

10 秒实时目标按“Agent 状态发生变化到中心端授权投影可查询”计量，不把飞书客户端自身的网络渲染延迟计算在内。

## 9. UI Flow

### 9.1 主看板

顶部展示在线设备、运行中、等待决策、异常和今日完成数量。设备卡按以下优先级排序：

1. 等待决策；
2. 失败或连接不稳定；
3. 运行中；
4. 空闲；
5. 离线。

卡片主操作根据状态变化：等待决策时为“处理决策”，其他状态为“查看设备”。

### 9.2 设备详情

设备详情展示 Gateway 状态、Agent runtime、当前会话和最近事件时间流。用户可点击“继续这个会话”建立当前飞书路由上下文，也可在多会话时显式切换。

### 9.3 决策卡

决策卡必须包含目标设备、Agent 和会话标题；推荐项有明确标识。选择后返回简短确认 toast，并更新原卡。用户可以直接回复补充要求，但仍受精确会话绑定约束。

### 9.4 主动推送

等待决策、失败和完成单独推送。普通进度仅改变看板，不产生新消息。完成卡展示结果摘要、验证证据和设备/Agent 来源。

### 9.5 卡片生命周期

只读看板按钮有效 24 小时，每次操作后换发新 callback。过期卡片返回带“一键获取最新看板”的恢复卡，而不是只有错误 toast。

## 10. 故障处理与恢复

### 10.1 设备断联

- 30 秒未上报：`degraded`；
- 2 分钟未上报：`offline`；
- 保留最后摘要、未决事项和最后在线时间；
- 恢复后补传事件并重新计算投影；
- 如果恢复后仍有未处理决策，最多补发一次提醒。

### 10.2 事件乱序和重复

重复事件幂等接受但不重复投影。序号缺口进入 pending；超过保留窗口仍未补齐时请求 Gateway 发送版本化快照，不能跳过缺口后静默继续。

### 10.3 命令投递

命令状态至少包括 `queued`、`leased`、`delivered`、`applied`、`stale`、`failed`。租约过期可重新投递；同一个 `command_id` 在 Agent adapter 侧幂等执行。目标会话版本变化时只能返回 `stale`，不得自动改投其他会话。

### 10.4 服务重启

设备注册、事件游标、会话投影、命令队列、callback 和待决策全部持久化。服务启动时不得把旧在线状态直接视为在线，必须等待新心跳或显示上次在线时间。

## 11. 安全与隐私

- 设备凭证按设备隔离，支持吊销和轮换。
- 所有事件和命令经过身份、设备归属和会话范围校验。
- callback 只携带 opaque ID，且绑定 principal、飞书目标和期限。
- 默认不上报完整路径、环境变量、密钥、源代码、终端 transcript 或模型原始上下文。
- 摘要和错误信息经过长度限制、控制字符过滤和敏感字段脱敏。
- 本地 spool 加权限保护；设备解绑时停止发送并清理未投递命令租约。
- 服务端审计记录动作类型、设备、会话和结果，不记录敏感指令正文。

## 12. 迁移策略

1. 保留当前 `ostag-local` 作为迁移来源，不直接复用为新设备身份。
2. 在 MacBook Pro 安装升级后的 Gateway，完成一次性配对，注册为新设备 `MacBook Pro`。
3. 验证 Codex 会话实时上报后，将旧 `ostag-local` 标记为已迁移并撤销。
4. 在 Mac mini 安装同版本 Gateway，独立配对为 `Mac mini`，接入 Claude Code adapter。
5. 同时运行两台设备，执行双会话和双决策串线测试。
6. 灰度稳定后，清理旧实例的可操作 callback；历史审计记录保留。

迁移期间看板可以暂时显示“旧实例（迁移中）”，但旧实例不得接收新的飞书写指令。

## 13. 测试与验收

### 13.1 自动化测试

- 设备 ID 生成、持久化、配对、吊销和轮换；
- 两个设备使用相同旧配置时被拒绝或引导迁移；
- Agent 自动发现、启动、退出和重启关联；
- 事件重复、乱序、缺口、断网补传和快照修复；
- 两设备、两 Agent、同名会话仍准确隔离；
- 命令租约、重试、幂等、stale version 和跨设备拒绝；
- 10 秒状态新鲜度指标；
- 主看板排序、离线保留、决策目标标签和过期卡恢复；
- 飞书 callback 身份绑定、24 小时 TTL 和移动端协议形状；
- 中心服务与 Gateway 重启恢复。

### 13.2 真实环境验收

必须同时满足：

1. MacBook Pro/Codex 与 Mac mini/Claude Code 出现在同一用户看板；
2. 任一状态变化 10 秒内进入中心投影；
3. 两端同时等待决策时，回复各自进入正确会话，零串线；
4. 任一设备断网、重启和恢复后，事件不丢失、不重复；
5. OStag 服务重启后，绑定、当前会话和待决策仍存在；
6. 飞书手机端完成总览、设备详情、决策回复、刷新和过期恢复；
7. 生产健康检查、数据库完整性、容器重启次数和外网入口均正常。

## 14. 发布与回滚

发布顺序：

1. 部署兼容旧 Gateway 的中心端 schema 和只读聚合逻辑；
2. 灰度 MacBook Pro Gateway 与 Codex adapter；
3. 灰度 Mac mini Gateway 与 Claude Code adapter；
4. 开启双设备主看板；
5. 开启精确写指令和主动推送；
6. 完成真实验收后撤销旧 `ostag-local`。

每阶段都通过 feature flag 控制。若出现串线、事件丢失、错误率显著上升或无法恢复的状态漂移，立即关闭写指令与新看板 flag，恢复现有单实例只读看板；设备注册和事件数据保留用于诊断，不执行破坏性回滚。

## 15. 成功定义

用户在飞书私聊 OStag 查询状态时，能在一张设备优先看板中准确看到 MacBook Pro/Codex 与 Mac mini/Claude Code 的在线状态、当前会话、最新回复和等待决策；关键变化 10 秒内可见，飞书回复始终进入明确标注的目标会话，并且断网、重启和迁移不会造成状态丢失或跨会话串线。

## 16. 实施锁定项

真实设备接入验证后，以下细节作为兼容性约束锁定：

- v2 事件消息 ID 必须包含签名设备身份命名空间；不能与旧
  `ostag-local` 的稳定消息 ID 共用全局空间。
- 签名设备使用独立 `device-relay.sqlite3`，旧实例 spool 仅作为迁移来源，
  不能继续双写。
- Gateway 必须绑定 OStag 专用 osstack daemon/socket；运行时安装同时包含
  CLI 构建产物和 `@osstack/core/dist`，否则 runtime inventory 不可信。
- macOS LaunchAgent 运行器必须复制到 Application Support，以避开受保护源码
  目录的 TCC 访问差异；路径和环境值必须按 shell 规则转义。
- 私钥只保存在 Keychain。Keychain 返回兼容编码时可在内存中解码，但不得把
  私钥回写到环境文件、plist、日志或证据。
- Agent 探测失败不能阻断设备心跳；看板连接状态始终依据中心收到心跳的时间，
  30 秒内在线、30 秒至 2 分钟连接不稳、2 分钟后离线。
- `ostag-local` 只在签名替代设备完成真实写入灰度后迁移为只读；迁移保留全部
  历史会话、消息、回执和审计数据。

生产操作、验收和回滚命令统一见 `docs/operations/copilot-relay.md`。
