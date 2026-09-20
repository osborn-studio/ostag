# 监控操作重试与并发恢复

状态：本地实现已通过固定验证和新 Opus 会话的独立审查。集成代码 `7de75d4b2f2c0b7392abfca3b2942b14866a9563`，摘要 `5f72711f1d0e7aa6947647bba4a767c865af8d06739dde2a6412e0067ac8f434`。尚未生产部署。

## 用户行为

用户对同一已批准操作重复点击或重试时，应复用原命令，保持命令标识、操作者、任务版本、发出时间和到期时间不变。仍须检查当前权限、会话、任务状态和确认有效期。已完成或失败的操作不能被迟到请求写回“执行中”。

修复前，较晚请求如果读到 `applying` 会抛出状态错误，尽管数据库保留了同一个命令。原测试同时存在“顺序重试报错”和“并发重试成功”的冲突约定；本次统一为同一有效操作的幂等重试。修改对应的一处旧断言，并保留、加强单命令及冻结内容断言。

## 开发与独立挑战

使用原生 osstack 隔离工作树，真实 Claude Opus 5 编码。首轮候选通过 64 项目标测试，但独立检查复现了两个未覆盖问题，因此没有集成：

- 命令已保存、回执追加失败后，`ready_to_apply` 恢复路径接受了操作者绑定被改变的命令。
- 一个线程保留旧的 `ready_to_apply` 读取结果；另一个线程完成并确认操作后，旧线程追加 `applying`，覆盖了已完成状态。

后续候选必须同时通过这些确定性检查、完整旧测试及新回归，再提交给无工具的新 Opus 会话独立审查。首轮“目标测试通过”不能替代上述证据。

租约测试原来在推进虚拟时钟后固定等待 60 毫秒，容易受机器负载影响。修复只改变同步方式：观察真实心跳写入的租约续期，设置等待超时并清理异步任务，继续检查竞争方被拒绝、实际发送一次、游标只增加一次。生产调度器不在本次修改范围。

## 验证和上线边界

最终记录须包含代码摘要、实际模型回执、独立审查回执、macOS 全量结果，以及与 A10 组合后的 Linux 全量结果。Linux 为本地隔离 ARM64 容器，不能当作 GitHub Ubuntu x86_64 CI 或腾讯生产环境认证。

本地修复不直接触发生产部署。后续仍须完成会议功能运行时接线、真实平台验收、仓库发布检查、CI 和部署后的健康验证；执行入口以项目现有 deployment.md 和 GitHub workflow 为准。


## 最终证据

- 独立监控分支目标 66 项，全量 2515 项通过；全量尚不包含 A10。组合验证见 A10-RETENTION.md 和 execution.json。
- 两组额外确定性验证、Ruff 和锁文件检查通过。
- [最终独立复审](reviews/monitor-opus-round-1.json)、[实际编码回执](reviews/monitor-opus-builders.json)。编码主体模型为 claude-opus-5；CLI 回执保留辅助模型计量，未将它隐去。
- 正常重复提交和回执丢失恢复均校验命令归属，写入 applying 前在与 ACK 共用的数据库写锁下核对最新状态。

## 独立复审补充意见

- Residual (unavoidable) timing dependency in the lease test: scheduler.drain_once awaits the send with wait_for(timeout=lease_seconds*0.8)=0.12s, while the heartbeat interval is lease_seconds/3=0.05s. The barrier removes the old 10ms sleep margin and widens it to ~70ms, and it fails fast with a clear message via delivery.done()/result(), but full determinism is impossible without touching production scheduler code or raising lease_seconds, both of which are forbidden. Acceptable as written.
- Unrelated pre-existing defect worth reporting (not fixed here, correctly out of scope): store.claim_pending_commands selects by command status only, so a command reserved by an apply whose ledger publication was lost is pollable while the receipt is still 'ready_to_apply'; acknowledge_control then does receipt.with_state('applied'/'failed') directly, driving the receipt from ready_to_apply to a terminal state without ever passing through 'applying'. The candidate's _owned_command correctly fails closed on the subsequent replay (command_replay), so it does not widen the hole, but the ack path itself does not assert the receipt is in 'applying'.
- _owned_command validates the required bindings but ignores unknown extra keys in the stored command payload, so a tampered row carrying additional fields is still adopted on replay. No new exposure, since such a row is already pollable by the instance independently of replay, and every field the service relies on (command_id, instance/task, action, expected_version, expected_lifecycle, requested_by, issued_at, expires_at) is pinned.
- Replay of an 'applying' receipt more than 5 minutes after the last state change raises stale_receipt from the preserved ensure_fresh_confirmation gate rather than returning the idempotent receipt. This is fail-closed and matches the instruction to preserve existing authority/freshness checks, and it never creates a second command; flagging only so the behavior is not mistaken for a regression.
- Static-only risk I cannot rule out: adapter-level tests not supplied in this review (feishu/telegram route or copilot tests that call apply_control twice) could still assert the old sequential-replay invalid_transition. Only the parent's full-suite run can confirm; the parent already re-scoped the one known conflicting controls assertion.
- Scope is clean: service.py, the new tests/test_monitor_apply_replay.py, the single re-scoped controls assertion, and the lease test plus one module-level async helper and the contextlib import. No scheduler/schema/production changes, no test removal or xfail, receipts.py transition table untouched.


补充边界：租约测试观察真实续约，但仍受生产发送路径约0.12秒的测试配置超时约束，未宣称负载无关的完全确定性。复审发现的旧回执发布/命令ACK顺序问题列为运行时接线前的后续核查；本轮已验证的重试归属和终态不回退修复不依赖该事项完成。Linux目标66项及2组恢复检查通过，见[Linux目标证据](reviews/monitor-linux-targets.receipt.json)。
