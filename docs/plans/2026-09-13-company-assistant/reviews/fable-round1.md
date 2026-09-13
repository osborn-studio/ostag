> 以下为Fable原文，包含随后被纠正或替换的建议；不单独作为最终实施指令。决策以REVIEW.md和当前方案为准。

以下是基于你给出的材料做的纯推理审查。我没有运行任何命令，也没有独立验证 main、worktree、pytest 或服务器状态，所有"实查"结论均以你的描述为准。

## 总体判断

架构方向可接受：不动 Monitor schema、单 owner、单 worker、outbox 与 job 同库事务、acceptance 与批准分离，这些都是对的。但方案有六处是"写在纸上成立、跑起来会漏"的点，必须在开工前修订。以下按你点名的维度列出，每条给失败模式和最小改法。

## 必须修订的问题

**1. "同库事务"覆盖不到 ActionReceipt，跨库不原子。** briefs.sqlite3 里的 outbox 与核心库里的 receipt 是两个 SQLite 文件，接受简报后"建 receipt、提交 outbox"必然分两步。崩在中间，恢复时要么看到 receipt 无投递而重发，要么有投递无 receipt 而无审批凭据。最小改法：outbox 行先于 receipt 创建，幂等键固定为 brief_id + revision + target；receipt 记录 delivery_id；恢复逻辑遇到"receipt 存在而 outbox 缺失"或反之一律进 reconcile 状态，禁止自动补发。身份和 receipt 只通过现有 Python API 读取，不做跨库 SQL join，并在启动时校验核心库 user_version 与 briefs 期望值的兼容矩阵。

**2. lease 时长与 LLM 调用时长的关系没定义。** 单 worker 并发 1，一次生成可能数分钟，lease 若按固定 TTL 到期，会出现同一 job 被崩溃后的新进程重跑，而旧进程仍在等模型返回，两边都写 revision。最小改法：lease 心跳续期，且每次写 DB 和每次出网投递前都校验 fence token；generating 阶段以 DB checkpoint 保存快照引用，重启后有效快照内直接从 generate 续跑，不重新 collect。

**3. 合并 main 自动部署等于随时 kill 在跑任务。** 你已经指出合 main 会触发 deploy，但没把"重启打断 collecting/generating"写进 production 影响。第一批需要明确：部署前查 briefs 库无 active job 才放行，或接受打断并由 lease 过期后进 failed 且通知用户，二选一写死，不能靠运气。同时 compose 持久卷必须显式包含 briefs 文件路径，release_check 增加"路径在卷上且可写"检查，否则第一次重建容器就丢库。

**4. 权限模型的真实缺口在"bot 能看到的比用户能看到的多"。** A2 读历史走的是应用凭据，用户被移出群后 bot 仍可读。只回原授权私聊不解决这个问题，因为泄露发生在内容层。最小改法：每个 source 记录 acting principal 和作用域会话，采集时和投递前各校验一次该用户当前仍是该会话成员；卡片回调必须校验操作者等于 brief owner 且 revision 匹配，转发出去的卡片被他人点击一律拒绝。首批不做批量接受按钮。

**5. 撤权中途的三个盲区。** 其一，撤权后 worker 内存里的快照和已经发出的 prompt 无法收回，方案应明文承认 provider 侧不可撤回，并把快照 TTL 压到小时级加定期清理。其二，多源简报撤销任一源应使整个 revision 失效并进 needs_input，不自动再生成，避免用剩余源二次出网。其三，飞书消息被撤回后 reader 会 404，reconcile 必须把该 source 标 stale，含 stale 源的 revision 不可投递。

**6. 事实核验把"数字"交给模型是最大风险。** Base 计数和统计准确性必须由确定性代码产出，模型只填模板槽位。加一道生成后校验：正文中出现的任何数字、URL、人名必须命中计算字段或 source 集合，否则整条降级为待确认。holdout 6 例只向 builder 回报通过数，不回传样本，否则 Codex 调 prompt 时会隐性过拟合。revision 记录实际 model id，API 不可用直接 failed，不允许静默换模型。

**7. 恢复镜像不能事后再造。** "briefs 新 schema 以后用兼容恢复镜像"意味着事故时目标镜像不存在。最小改法：每次发布同时构建并在 CI 用 fixture 库验证 N-1 兼容镜像，且两个 SQLite 在同一个备份步骤内快照，避免 receipt 与 brief 时间不一致。这次发布把容器 smoke 至少在 flag-off 路径改为必跑。

**8. B 的写回前提与"不增隐藏列"冲突。** 无 CAS 时靠 append 加 readback，但没有任何标识列就无法在 reconcile 时识别自己写的行，重复写将不可判定。B 虽不在首批，前提要现在定：只支持已有可见"来源/备注"列或 OStag 自有日志表的目标，其余目标不进 B 范围。

**9. 验收指标缺基线。** "人修分钟"和"延迟/成本"没有对照就无法判断价值。pilot 前由用户本人手工完成 3 份同类简报并计时作为基线，10 个真实委托的通过标准写成相对基线的比例。

## 保留意见与建议保持的边界

飞书 API 限流由 monitor 与 history reader 共用同一应用凭据，A2 的分页拉取可能让 monitor 饿死；给 reader 单独的页数和时间预算，超预算进 needs_input 带部分结果，不静默截断。staging 需要 pilot 用户 allowlist，避免 staging bot 私聊生产用户。osstack guard 未就绪时的"受控本会话实施"要设时间盒，否则 guard 会永远排在后面。

结论：架构骨架保留，但第 1、2、3、6、7 五条不改就不应进入 A1 开发；4、5、8、9 可在 A1 期间并行补齐，A2 前必须完成。