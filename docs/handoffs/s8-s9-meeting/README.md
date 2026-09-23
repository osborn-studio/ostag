# 第八站 OT-2 首个切片 / 第九站共审交接

2026-09-23。用户明确启动编号 08、09 工作包，并选择「OStag 会前材料相关性与重复识别的离线评测」。这覆盖旧 08 文档的暂停安排，但不把 08 中所有候选实验都视为已实施。

## 范围与源码

- 仓库：`osborn-studio/ostag`。
- 基线：`0bc3d88f`（获取后的 origin/main）；开发分支：`codex/s8-s9-meeting-offline`。
- 首个实现提交：`369ee98b`。
- 审计修订：`9e8caf9a`；标签依据补充：`22aafffb`。最终文件摘要见 [source manifest](evidence/source-manifest.json)。
- 使用独立 worktree；原 OStag、OSStack 和 Alpha 工作目录未改动。
- 既有 `meeting collect`、会议服务、权限、路由、发送、停止/恢复协议没有接入评测输出。
- OSStack 仅核对分支状态：Decision Advisor 本地分支仍为 `5a1222455c57502154199ee11fe90e83e6203558`，远端 main 为 `8b2419a45b17cfdc2f85c9c3416f92b11fcfa201`；未返回同名 development 远端分支。未在本轮重跑 OSStack 测试，也没有修改或发布它。

## 第八站已交付

- 独立、无网络的评测入口，见 [使用与契约](../../ostag/meeting-relevance-eval.md)。
- `suite/input/predictions/report` 四种 `/1` 格式，输入摘要与完整参考集摘要分开；预测绑定不含标签的精确输入。
- 14 个中文合成案例、28 条材料、14 对材料判断。标签是作者预期，不冒称独立人工标注。没有读取真实聊天或 meeting collect 产物。
- 规则基线只识别议题字面提及与同来源的标准化文本副本；无法确定时弃权，不删除材料、不填负责人/期限。
- 报告分开列出错误、弃权、未运行和参考不确定；拒绝跨分区已声明事件/来源/相同文本泄漏。
- [固定基线报告](evidence/baseline-report.json)；输入为仓库 `examples/meeting-relevance-eval.json`，可通过同一命令逐字节重放。

固定样例揭示的局限：development 相关性有 2 次假阳性、3 次弃权；重复判断在 development、holdout 各命中 1/3 个正例，均有 6/7 个案例弃权。不能将低覆盖下的精确率解读为效果胜出。

## 第九站

本轮按用户最新要求使用 Claude CLI **Opus 5.5**；旧 09 文档中的 Fable 5.1 是历史审计记录，不覆盖本轮模型选择。模型只接收代码、测试、文档和合成样例，禁用工具，不代替测试执行。

已完成三轮静态审查，实际模型回执均为 `claude-opus-5-5`、`is_error=false`，没有以其他模型替代。初审两个 P1 在二审关闭；二审新增的参考标签依据 P2 在三审关闭。没有剩余 P0/P1；非阻塞的语料扩充建议保留跟踪。

- [初审原文](evidence/round1-review.md) / [回执](evidence/round1-receipt.json)
- [二审原文](evidence/round2-review.md) / [回执](evidence/round2-receipt.json)
- [三审原文](evidence/round3-review.md) / [回执](evidence/round3-receipt.json)
- [逐条处置](REVIEW-DISPOSITIONS.md)

每轮提示词摘要与所审文件摘要单列保存；初审对应 `369ee98b`，不是最终实现。Claude 未运行测试，命令与结果由 Codex 执行核实。三轮回执费用合计约 $0.95327，仅为本次 Claude 审计费用，不是 Jev pilot 成本。

## 验证记录

- RED：新增模块尚不存在时，定向 pytest 以 `ModuleNotFoundError` 失败，退出 2。
- GREEN：新增模块初版 29 项通过；修订后同一命令 **49 项通过**。
- `uv run --locked ruff check ostag tests`、`uv run --locked python -m compileall -q ostag` 通过。
- 首次全仓 pytest 因仅安装 dev extras，缺少 httpx/FastAPI/PyYAML，发生 34 项收集错误；没有当作产品回归。
- 补齐 extras 后，Python 3.13 全仓为 5,114 通过、13 跳过、2 失败；这两项卡片生命周期清理断言在未修改的 `0bc3d88f` 同样失败。本轮未修改该模块或认定其根因。
- 按 CI 使用 **Python 3.11.15** 后，同两项通过；最终全仓为 **5,136 通过、13 跳过**（307.96 秒）。跳过项不计为通过，不能声称覆盖了未执行的环境门槛。
- 相关会议/意图/路由回归 389 项通过；全仓 Ruff、编译、密钥卫生检查通过；报告逐字节重放一致。
- [结构化验证回执](evidence/validation.json)记录命令、版本、退出码与失败对照。本机未运行完整容器发布门槛，不把本地 pytest 当生产上线验收。

## 未完成与下一步

这次完成的是第八站的离线工程基础，而不是 Jev 效果准入。下一步需要记录真实规则误判/人工改写的数量和用时、固定获准任务样本及独立标注，再决定是否值得开启真实 shadow。当前 holdout 只是已公开合成样例的分区，不是封存盲测。没有真实 Jev 调用、费用授权扩展、生产接线或部署。

OT-1、OT-3、OT-4，以及 OSStack ST-1～ST-5 保持后续跟踪；用户本轮选择没有将它们一起开工。S6 的行情、拆股和历史可用时间证据缺口仍未关闭。

回滚只需撤回新增评测模块、样例、测试与文档；无数据库迁移、控制状态或业务数据需要恢复。
