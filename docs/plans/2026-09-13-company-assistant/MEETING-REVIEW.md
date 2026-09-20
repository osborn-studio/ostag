# 会议与上游增量：Claude Fable 5.1共创

本轮是在此前四轮实施方案之后的新一轮功能扩展，使用真实Claude CLI显式请求claude-fable-5-1，无fallback。工具/MCP/hooks关闭，不访问真实聊天或私有业务正文。模型只能审提供的源码事实和规格，不能声称独立实测GitHub/飞书/生产。

## 第一轮挑战与Codex回应

原文见[第一轮JSON](reviews/meeting-fable-round1.json)。已采纳：每仓库每类/每群子预算、partial阻断关键源ready、PR merged_at锚与首次无head基线的unknown、shareable来源交集、部署环境/receipt区分、预算/cursor/deadline持久化、源失败置顶与决策价值排序。

没有机械接受的意见：

- GraphQL一次请求内first50同样可能截断；没有证据要求换httpx并把repo上限20任意降8。沿用有界gh读取，借鉴本地已有增量输出/kill-reap模式；先2repo实测，再证明冻结清单覆盖。
- “D07缺M05依赖”是遗漏了传递依赖；实际D07→M07→M06→M05，验证器核对完整闭包。
- visible_since并非已知API必返回字段，缺失必须unknown；preview成功不证明production不存在，仍是unknown。
- partial来源中已有可证合并事件无需全隐藏，但只能标已覆盖部分；关键分区partial不可meeting_ready。
- 投递outbox不等于material-effect journal。U02首个journal放在briefs.sqlite3领域表，ActionReceipt JSONL仍唯一批准权威，不新增第三数据库。
- U03保留明确范围的批次证明和能力门槛；普通CLI无受管cwd/ACP，真实路径不合规必须unsupported，不把测试fixture当完整ACP产品。

M01先冻结会议评测规范与独立保留集保管协议，M07评分不随表现修改标准。保留既有Presence六态，只增加审批反馈、时间投影与守卫。

## 最终复核

[第二轮JSON](reviews/meeting-fable-round2.json)结论为`approved_for_planning`；Fable接受了Codex对GraphQL强制改造、传递依赖、partial事实与journal边界的回应，并撤回“D07缺M05依赖”判断。实际模型身份与调用结果见[元数据](reviews/meeting-metadata.json)。

最后实施门槛已由Codex写入：M01最少调用/时间预检；120秒取数软截止+60秒生成保留且180秒硬截止不变；旧head须同repo/分支/principal有效快照；Search仅线索；统一needs_input/draft_ready/failed与meeting_ready映射。全局预算和持久恢复继续严格。

没有采纳“把未来文件标existing”的措辞：existing/new仍准确描述本轮基线，新增file_reuse_from明确创建任务；验证器按拓扑检查复用依赖。共享store/rendering/config明确单writer顺序。

Codex另补了观察截止在采集后封存、无日期thread API不得越grant读取、U02的store迁移触达与测试覆盖，以及放行顺序：中央journal随B在D05实测放行，D08是后续仓库适配批次，不能要求D05等待依赖D05的D08。U03没有真实路径conformance就必须unsupported。

**审查边界**：共两轮真实Fable共创；第二轮审核的是其input hash对应规格，最后上述落字/调度澄清由Codex完成并验证，未声称Fable再读了最终每个字节。没有产品代码审查、真实会议质量评测或生产批准。本轮最终文档hash与检查结果另记EVIDENCE。
