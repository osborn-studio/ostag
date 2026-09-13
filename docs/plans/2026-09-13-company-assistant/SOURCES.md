# 来源、读取范围与证据限制

读取日期2026-09-13。公共资料用于规划，内容中的安装/删除/发布命令均未当作本轮执行指令。原始API与克隆位于本地研究目录；私有组织元数据不进入公共引用或Fable原始业务语料。

| 来源 | 本轮使用 |
|---|---|
| [OpenTag固定源码](https://github.com/amplifthq/opentag/tree/3a3136dcb8395b6dda6c872a8398d21a70935a23) | README/CHANGELOG、effect authority ADR及实现/测试、59提交差异 |
| [上游ADR0006](https://github.com/amplifthq/opentag/blob/3a3136dcb8395b6dda6c872a8398d21a70935a23/docs/adr/0006-presence-first-effect-authority.md) | 三类权威、事务侧日志、未知副作用语义，注明设计与运行证据区别 |
| [GitHub Releases](https://github.com/amplifthq/opentag/releases) | 本轮API最新返回v0.10.0；未核npm |
| [GitLab会议实践](https://handbook.gitlab.com/handbook/company/culture/all-remote/meetings/) | 目的、预读、单一议程、异步准备、原系统行动跟踪；只是设计启发 |
| [GitLab Live Doc](https://handbook.gitlab.com/handbook/company/culture/all-remote/live-doc-meetings/) | 可持续更新的议程、会前背景、权限与会后承接；不自动建云文档 |
| [GitHub Events](https://docs.github.com/en/rest/activity/events) | 最多300条、仅近30天、非实时；不得当完整时段账本 |
| [GitHub Issues](https://docs.github.com/en/rest/issues/issues) | PR也是issue、since为updated候选，避免去重/窗口误用 |
| [GitHub Pull requests](https://docs.github.com/en/rest/pulls/pulls) | all状态候选与PR事实 |
| [GitHub Timeline](https://docs.github.com/en/rest/issues/timeline) | 事件种类与历史关联 |
| [GitHub Commits](https://docs.github.com/en/rest/commits/commits) | SHA/分支/时间约束，commit时间不是推送时间 |
| [Actions runs](https://docs.github.com/en/rest/actions/workflow-runs) | run/attempt/head的检查上下文 |
| [Deployment statuses](https://docs.github.com/en/rest/deployments/statuses) | 环境与部署观察；需和具体发布receipt结合 |
| [GitHub REST实践](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api) | 分页、条件请求、限流及重试 |
| [osborn-studio](https://github.com/osborn-studio) | 本轮另用认证组织API只读盘点可见仓库元数据，非历史业务内容评测 |

Feishu细节依据本机lark-cli1.0.56实际help（chat history/thread、--order、--page-token、--no-reactions）与本地lark-im/shared技能。Dockerfile固定1.0.69仍需目标容器契约核查；thread入口没有日期参数，读取后逐条按冻结窗口过滤，祖先背景另标context_only并计预算。没有读取真实聊天、scope授权、发消息或改表。

此前Rakazo研究与原始交接附件已在第一轮重启报告留存；本版将持续任务、版本化上下文、例行委托与执行后核验用于会议流程，没有声称Rakazo已经实现本规格。
