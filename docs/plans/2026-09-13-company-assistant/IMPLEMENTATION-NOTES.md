# 实施衔接记录（当前代码检查所得，后续任务未完成）

1. A04：M01 的 content_hash 标识一次受理快照，包含 intake_at；不能充当跨次请求的幂等键。入口事件必须有独立、按 namespace/platform/requester 隔离的幂等键，重放先找原任务，不要重建 intake_at 后误当新任务。
2. A04：后续每次读取、模型出网、提交、投递都重新核对当前授权/映射/ACL；这不等于重建已确认范围。原 FrozenScope 不变，当前政策变化导致 needs_input/重新确认，不能借重试换 intake_at 或 first_claimed_at。
3. A04：ExecutionTiming 必须使用 A02 持久化的首次 claim 时间及相同冻结时长。M01 can_commit 对 failed/cancelled 只允许无私有内容的状态记录；真实存储提交仍检查当前 fence。硬截止处 lease 也已到期时，由可信恢复/维护路径记录终态，不让旧尝试写草稿。
4. M04/M05：A01 SourceSet 当前最多500条；M01 的两个 provider 配额分别最多500，合计上限可能1000。必须显式调整版本化契约或保留可见部分状态；1MiB模型输入还要计算整个请求，不能静默截断后称完整。
5. A03/A04：现有 Backend.run 仅返回字符串，不能证明实际模型ID、用量或只读工具边界；需要窄的可信生成接口。模型只给 source_id，链接由可信映射生成。引用存在不等于自然语言语义被证实，真实模型/人工核验仍是独立门槛。
6. A03：费用账本应在独立brief库前向迁移，预留与结算原子、跨重启保留，未知费用不按0退款。该实施需要在原A03候选4文件之外复用store.py（第5文件），并重跑A02回归；实施时须明确记录，当前尚未完成。
7. A11/发布：store抛出的领域兼容性异常尚需lifespan按brief域降级接线；主机timezone/pytest通过不能替代目标镜像tzdata、真实Feishu、排空、发布和7天试点评估。

8. A04：纯ExecutionTiming可以描述needs_input范围；服务必须先检查受理结果为accepted再入队，不把dataclass构造成功当受理许可。A02为通用brief存储，不信任payload中的accepted/authorized布尔值。
