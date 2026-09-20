# 合成双源会议预览

> SYNTHETIC LOCAL PREVIEW · 模拟授权，仅使用本地 fixture。synthetic 标记不证明内容是虚构的；输入 grant 不是经过认证的生产签发。draft_ready / meeting_ready 仅表示本地必需分区技术完整；始终需要人工语义审阅。不认证会议质量、实时完整性、历史重建或人工验收；不是 M05 完成或上线授权。

状态：needs_input；meeting_ready=false；semantic_review_required=true

目标：【虚构演示】核实同步功能的合并、检查与上线证据，确认会议还缺什么输入。
用途：release-review
时间窗口：[2026-09-10T00:00:00Z, 2026-09-17T00:00:00Z)；时区：Asia/Shanghai
观察截止（fixture 元数据）：2026-09-18T08:00:00Z
范围哈希：`1bee593619c526a315860d719595f7e16b8e2bc6c6e8b6a1e16bc7d6df19e408`

## 预读

先阅读变化与原文说法，再核对证据时间和分区缺口。所有记录均来自本地合成输入。

## 变化

- PR 已合并（不等于上线）：虚构：同步功能 PR 已合并 完全虚构的本地演示记录。；事件 2026-09-15T10:00:00Z；观察 2026-09-18T08:00:00Z。[gh-api-merge](https://github.com/synthetic-example/demo-api/pull/12) [gh-api-check](https://github.com/synthetic-example/demo-api/pull/12/checks?check_run_id=501)
  仓库 101 PR #12；head `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`；合并 SHA `bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb`；检查：success_at_head；生产部署：unknown。
- PR 已合并（不等于上线）：虚构：页面功能 PR 已合并 完全虚构的本地演示记录。；事件 2026-09-15T10:00:00Z；观察 2026-09-18T08:00:00Z。[gh-web-merge](https://github.com/synthetic-example/demo-web/pull/12)
  仓库 102 PR #12；head `cccccccccccccccccccccccccccccccccccccccc`；合并 SHA `dddddddddddddddddddddddddddddddddddddddd`；检查：unknown；生产部署：unknown。
- 检查成功记录（需匹配 PR 精确 head，不等于部署）：虚构：精确 head 检查成功 完全虚构的本地演示记录。；事件 2026-09-15T10:00:00Z；观察 2026-09-18T08:00:00Z。[gh-api-check](https://github.com/synthetic-example/demo-api/pull/12/checks?check_run_id=501) [gh-api-merge](https://github.com/synthetic-example/demo-api/pull/12)
  check SHA `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`；head `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`；匹配结果：success_at_head。
- 议题记录（不代表决策）：虚构：上线确认清单 完全虚构的本地演示记录。；事件 2026-09-15T10:00:00Z；观察 2026-09-18T08:00:00Z。[gh-api-issue](https://github.com/synthetic-example/demo-api/issues/21)
- 议题评论（说法）：虚构：讨论确认方式 完全虚构的本地演示记录。；事件 2026-09-15T10:00:00Z；观察 2026-09-18T08:00:00Z。[gh-api-comment](https://github.com/synthetic-example/demo-api/issues/21#issuecomment-601)
- 提交记录（不等于上线）：虚构：同步实现提交 完全虚构的本地演示记录。；事件 2026-09-15T10:00:00Z；观察 2026-09-18T08:00:00Z。[gh-api-commit](https://github.com/synthetic-example/demo-api/commit/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa)
- 版本已发布（不等于部署）：虚构：发布版本条目 完全虚构的本地演示记录。；事件 2026-09-15T10:00:00Z；观察 2026-09-18T08:00:00Z。[gh-api-release](https://github.com/synthetic-example/demo-api/releases/tag/v0.0.1-synthetic)
- 聊天原文说法（未验证，不是决策）：虚构说法：同步功能已上线。；事件 2026-09-16T09:00:00Z；观察 2026-09-18T08:00:00Z。`chat-shipped`
- 聊天原文说法（未验证，不是决策）：虚构讨论：\#12 需要再确认。；事件 2026-09-16T10:00:00Z；观察 2026-09-18T08:00:00Z。`chat-ambiguous`

## 差异与待核实

- 聊天含“已上线”说法，相关 PR 已合并，但窗口内未见匹配生产部署证据；状态未知，不能证明说法为假或部署失败。 `chat-shipped` [gh-api-merge](https://github.com/synthetic-example/demo-api/pull/12) [gh-api-check](https://github.com/synthetic-example/demo-api/pull/12/checks?check_run_id=501)
  证据事件时间：chat-shipped 2026-09-16T09:00:00Z；gh-api-merge 2026-09-15T10:00:00Z；gh-api-check 2026-09-15T10:00:00Z
- 裸引用 #12 未绑定仓库；候选仓库 2 个，不自动关联。 `chat-ambiguous`

## 议程（最多三项）

1. 确认目标与待确认决策：以请求目标为讨论边界；所有建议均待人工确认。
2. 核对变化与上线证据：逐项区分合并、精确 head 检查、版本发布与环境部署；核实聊天说法和未绑定引用。 [gh-api-merge](https://github.com/synthetic-example/demo-api/pull/12) [gh-web-merge](https://github.com/synthetic-example/demo-web/pull/12) [gh-api-check](https://github.com/synthetic-example/demo-api/pull/12/checks?check_run_id=501) [gh-api-issue](https://github.com/synthetic-example/demo-api/issues/21) [gh-api-comment](https://github.com/synthetic-example/demo-api/issues/21#issuecomment-601) [gh-api-commit](https://github.com/synthetic-example/demo-api/commit/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa) [gh-api-release](https://github.com/synthetic-example/demo-api/releases/tag/v0.0.1-synthetic) `chat-shipped` `chat-ambiguous`
3. 确认缺失输入与后续问题：检查下列分区清单及技术限制，商议需要补充的材料。

## 建议问题与行动（待讨论，未批准、未分派）

- 建议确认：本次希望确认什么决策？现有输入是否足够？
- 建议核对：合并、检查和发布分别证明了什么？生产环境与 SHA 的证据在哪里？
- 建议补充：缺失分区与未绑定仓库引用；由参会者讨论是否行动及如何安排。
- 建议核实：这条说法指哪个环境、哪个 SHA？能否补充明确的部署记录？ `chat-shipped` [gh-api-merge](https://github.com/synthetic-example/demo-api/pull/12) [gh-api-check](https://github.com/synthetic-example/demo-api/pull/12/checks?check_run_id=501)

## 来源

SHA256 基于原始摘录；展示文本已转义并省略任意链接。
- [gh-api-merge](https://github.com/synthetic-example/demo-api/pull/12)；github:repo:101；事件 2026-09-15T10:00:00Z；观察 2026-09-18T08:00:00Z；SHA256 `bf3de3877080bc0a497bd6e9bd3d901887442388295044e69469d4b15247dc15`
- [gh-web-merge](https://github.com/synthetic-example/demo-web/pull/12)；github:repo:102；事件 2026-09-15T10:00:00Z；观察 2026-09-18T08:00:00Z；SHA256 `3e3b4b666a2237d403e58c020f13da15b7c71b462a6d1c7b049ffd214c5d65e5`
- [gh-api-check](https://github.com/synthetic-example/demo-api/pull/12/checks?check_run_id=501)；github:repo:101；事件 2026-09-15T10:00:00Z；观察 2026-09-18T08:00:00Z；SHA256 `a597a859bbdbd7cc110e96811b0929cf1d31c2707e999330d4bb43f092dac1f4`
- [gh-api-issue](https://github.com/synthetic-example/demo-api/issues/21)；github:repo:101；事件 2026-09-15T10:00:00Z；观察 2026-09-18T08:00:00Z；SHA256 `a55b281c900bcb6e9ba4dfd963757e6c273369ed93c70e7ab39eb0c658d74536`
- [gh-api-comment](https://github.com/synthetic-example/demo-api/issues/21#issuecomment-601)；github:repo:101；事件 2026-09-15T10:00:00Z；观察 2026-09-18T08:00:00Z；SHA256 `2b9f1b43cded5e65d756806247571f098d6a86aa3579966f58d81bbdfc350ca9`
- [gh-api-commit](https://github.com/synthetic-example/demo-api/commit/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa)；github:repo:101；事件 2026-09-15T10:00:00Z；观察 2026-09-18T08:00:00Z；SHA256 `e2e5de1d38037129b3b768be7603dbf052a51f5f3dd66443af19df538958703b`
- [gh-api-release](https://github.com/synthetic-example/demo-api/releases/tag/v0.0.1-synthetic)；github:repo:101；事件 2026-09-15T10:00:00Z；观察 2026-09-18T08:00:00Z；SHA256 `ac49b569fcc539117207b8f2097faf9191d7b93efc675fb51dcbcd6134e64919`
- `chat-shipped`；feishu:chat:synthetic_dev；事件 2026-09-16T09:00:00Z；观察 2026-09-18T08:00:00Z；SHA256 `f222840380812b16bdadb225235f7a8918cab9cd9fcf2e28aa5bbf00a4119f5a`
- `chat-ambiguous`；feishu:chat:synthetic_dev；事件 2026-09-16T10:00:00Z；观察 2026-09-18T08:00:00Z；SHA256 `6c78f8578cd61ba48cee01a39670b2176b92b0d528466642033fcca609691214`

## 覆盖缺口与完整分区清单

- `github:repo:101:pulls`：complete；必需；窗口内 1；窗口外排除 0；虚构窗口数据已提供。
- `github:repo:101:issues`：complete；必需；窗口内 1；窗口外排除 0；虚构窗口数据已提供。
- `github:repo:101:issue_comments`：complete；必需；窗口内 1；窗口外排除 0；虚构窗口数据已提供。
- `github:repo:101:commits`：complete；必需；窗口内 1；窗口外排除 0；虚构窗口数据已提供。
- `github:repo:101:releases`：complete；必需；窗口内 1；窗口外排除 0；虚构窗口数据已提供。
- `github:repo:101:checks`：complete；必需；窗口内 1；窗口外排除 0；虚构窗口数据已提供。
- `github:repo:101:deployments`：complete；必需；窗口内 0；窗口外排除 0；虚构窗口数据已提供。
- `github:repo:102:pulls`：complete；必需；窗口内 1；窗口外排除 0；虚构窗口数据已提供。
- `github:repo:102:issues`：complete；必需；窗口内 0；窗口外排除 0；虚构窗口数据已提供。
- `github:repo:102:issue_comments`：complete；必需；窗口内 0；窗口外排除 0；虚构窗口数据已提供。
- `github:repo:102:commits`：complete；必需；窗口内 0；窗口外排除 0；虚构窗口数据已提供。
- `github:repo:102:releases`：complete；必需；窗口内 0；窗口外排除 0；虚构窗口数据已提供。
- `github:repo:102:checks`：complete；必需；窗口内 0；窗口外排除 0；虚构窗口数据已提供。
- `github:repo:102:deployments`：complete；必需；窗口内 0；窗口外排除 0；虚构窗口数据已提供。
- `feishu:chat:synthetic_dev:messages`：complete；必需；窗口内 2；窗口外排除 0；虚构窗口数据已提供。
- `feishu:chat:synthetic_qa:messages`：failed；必需；窗口内 0；窗口外排除 0；虚构测试群采集失败；需补充输入。
可见缺口：feishu:chat:synthetic_qa:messages
