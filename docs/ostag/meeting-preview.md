# 本地合成双源会议预览

这是 **SYNTHETIC LOCAL PREVIEW**：读取一个明确标记为合成的 JSON fixture，
生成中文会议草稿。不是 M05 完成、生产授权或实时会议服务。
`synthetic: true` 只是一项输入声明，不能证明文本真的虚构。请只使用合成数据。

## 快速运行

在已具备 OStag 依赖的 Python 环境、仓库根目录运行：

```sh
python -m ostag.main meeting preview --input examples/meeting-preview.json --format markdown
python -m ostag.main meeting preview --input examples/meeting-preview.json --format json
ostag meeting preview --input examples/meeting-preview.json --format markdown
```

默认格式是 Markdown。直接重定向 stdout 即可保存，不需要 `--output`：

```sh
ostag meeting preview --input examples/meeting-preview.json --format markdown > /tmp/meeting-pack.md
```

即使在 `meeting` 前提供 `--dry-run`、`--live`、`--config` 等顶层选项，
也只执行本地预览；这些选项不触发配置读取、数据库访问或适配器启动。

示例故意包含一个失败的必需测试群分区，因此会**输出可读的部分草稿并返回 3**。
在 shell / CI 中，退出码 3 不表示没有输出。不会省略已覆盖事实，也不会隐藏缺口。

| 退出码 | 含义 |
| --- | --- |
| 0 | `draft_ready`：本地必需分区技术完整，没有范围预算或历史重建限制 |
| 3 | `needs_input`：必需分区不完整或既有范围预检要求补充输入；仍输出部分草稿 |
| 2 | 输入或参数无效；stdout 为空，stderr 为有界通用错误，不回显输入或 traceback |

`meeting_ready` 与本地技术状态对应，**不表示可以直接开会或业务已获批准**。
`semantic_review_required` 始终为 `true`。可选分区不完整也会在缺口中完整列出，
但不阻止本地 `draft_ready`。即使每个分区都声明 `complete`，也不证明数据真实完整、
会议质量合格、实现了历史重建或获得人工验收。空分区可以 complete，但不证明没有变化。

## 示例会看到什么

- API PR #12 创建于窗口之前，在窗口内合并，窗口后更新：按合并事件纳入。
- 同一 PR 的精确 head SHA 检查成功；发布版本不等于生产部署，部署状态仍为 `unknown`。
- 开发群显式关联 API PR #12 的“已上线”说法，产生带证据 ID 和事件时间的待核实差异。
  这不是证明说法错误，也不是证明部署失败。
- 两个仓库各有 #12，裸引用 `#12` 不会依据群名、显示名或相同编号自动关联。
- 必需的 `feishu:chat:synthetic_qa:messages` 分区失败，事实仍展示，缺口仍可见。

为验证确定性，可以向 `github` 添加以下**合成**记录，再运行同一个命令。
API PR 的 `production_deployment` 将变成 `success`，相应待核实差异消失；
测试群缺口不会因此消失，状态仍为 `needs_input`。

```json
{
  "source_id": "gh-deploy",
  "repo_id": "101",
  "kind": "deployment_success",
  "event_at": "2026-09-16T08:00:00Z",
  "observed_at": "2026-09-18T08:00:00Z",
  "title": "虚构生产部署",
  "excerpt": "完全虚构的成功记录",
  "deployment_id": 701,
  "sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "environment": "production",
  "ref": "main"
}
```

改为其他仓库、不同 SHA、不同 ref 或 `staging`，都不能满足该 PR 的生产部署证据。
成功仅是 fixture 记录的含义，不声明这是观察截止时仍运行的版本。

## 输入 schema（version 1）

唯一 bundle 必须恰好包含以下字段，额外字段、缺失字段、任意层级重复 JSON key、
非标准 JSON 数值、错误类型均拒绝。布尔值不能代替整数。
文件须为普通文件，最大 **1 MiB**；符号链接、FIFO、目录及其他非普通文件拒绝。
`github`、`feishu`、`partitions` 三个数组条数合计最多 **500**，不截断。

| 字段 | 精确要求 |
| --- | --- |
| `synthetic` | 必须是 JSON 布尔值 `true`；不是字符串或数字 |
| `version` | 必须是整数 `1` |
| `request` | 现有 `MeetingRequest.from_dict` wire schema |
| `mapping` | 现有 `ProjectMapping.from_dict` wire schema |
| `grant` | 现有 `PilotGrant.from_dict` wire schema |
| `observed_at` | 显式 UTC ISO 时间，fixture 的观察截止，不读取当前时钟 |
| `github` | 下述精确事件 schema 的数组，可为空 |
| `feishu` | 下述消息 schema 的数组，可为空 |
| `partitions` | 每个请求分区恰好一项的完整清单 |

契约字段以 [完整示例](../../examples/meeting-preview.json) 为可复制模板。
使用现有 `TimeWindow` 验证半开区间 `[start, end)` 及 IANA 显示时区；
时间戳必须是 `Z` 或 `+00:00` 的显式 UTC 时间，窗口不能越过 fixture 观察截止。
所有记录必须满足 `event_at <= observed_at <= bundle.observed_at`。

固定 fixture 策略为 namespace `synthetic-preview`、请求人
`{"platform":"feishu","actor_id":"synthetic-reviewer"}`、预算 profile
`synthetic-preview` revision `1`，最多两个仓库，其余预算沿用现有 `BudgetProfile` 默认值。
scope 预检超出预算或请求历史 as-of 时，输出限制并标记 needs_input。
`request` 仍需声明 mapping/grant 的 ID 和 revision、私有 audience、时间范围、仓库、群聊、
GitHub 类别及非空的必需分区列表；`shareable_sources` 必须为空。
只支持下表事件；没有 reviews/review_comments 事件格式，也不声称合成 complete 清单是采集证明。

在**这个模拟中**，bundle mapping / grant 提供当前策略和资源元数据 lookup，
并用固定的合成 `Settings` 调用 `freeze_meeting_scope`，复用 namespace、revision、
请求人、资源、窗口、权限交集检查。grant 必须在 fixture 观察时间有效且未撤销，
`model_egress_allowed` 必须为 `true` 以满足现有 generate 契约；**实际上没有模型调用**。
grant 不是认证过的生产签发，lookup 不是在线权限查询，scope hash 也不是授权凭证。

### GitHub 精确事件 schema

每项公共字段必需：`source_id`、`repo_id`、`kind`、`event_at`、`observed_at`、
`title`（非空，最多 400 字符）、`excerpt`（可空，最多 3000 字符）。
`source_id` 在两种来源中全局唯一，1–80 个 ASCII 字母、数字、`_`、`-`，首字符为字母或数字。
grant ID 也须满足此安全标识符限制。`repo_id` 是映射中选定仓库的稳定数字字符串。

| kind | 分区类别 | 额外必需字段 |
| --- | --- | --- |
| `pr_merged` | `pulls` | `pr_number`, `head_sha`, `merge_sha`, `ref`, `created_at`, `updated_at` |
| `issue` | `issues` | `issue_number` |
| `issue_comment` | `issue_comments` | `issue_number`, `comment_id` |
| `commit` | `commits` | `sha`, `ref` |
| `release_published` | `releases` | `tag`, `sha`, `ref` |
| `check_success` | `checks` | `pr_number`, `head_sha`, `check_sha`, `check_run_id`, `ref` |
| `deployment_success` | `deployments` | `deployment_id`, `sha`, `environment`, `ref` |

数字标识符是 1 到 2^63−1 的整数；SHA 是完整 40 或 64 位小写十六进制字符串。
`ref` 表示关联的目标分支，`environment` 必须显式提供，二者为非空、最多 256 字符的字符串。
`tag` 为 1–128 个 ASCII 字母、数字、点、`_`、`-`，首字符为字母或数字。
不接受任意 `url` 或未列出的字段。同一仓库同一 PR 的合并记录只能有一条。

PR 的 `event_at` 是合并时间，须满足 `created_at <= event_at <= updated_at <= observed_at`。
入窗只看 `event_at`，不看创建或更新时间。所有记录先检查 scope，再排除窗口外记录，
每个分区明确报告排除数；越权资源即使在窗口外也使整个输入失败。

精确 head 检查必须同时匹配仓库、PR 号、ref，且
`check_sha == check.head_sha == pr.head_sha`；不匹配时为 `unknown`。
生产部署需同时匹配仓库、ref、`sha == pr.merge_sha` 和大小写精确的 `environment: production`。
只有窗口内记录参与关联；提交、PR 合并、检查和 release 都不会被推断成生产部署。

### 飞书消息

必需字段为 `source_id`、`chat_id`、`text`（非空，最多 4000 字符）、`event_at`、`observed_at`。
可选字段只有 `references`：精确的 `{"repo_id":"101","pr_number":12}` 数组，不能重复，
仓库必须在冻结范围内。指向窗口内未出现的 PR 会明确列为未能核实的引用。
群聊必须在冻结范围内。不会根据显示名、群名或正文 URL 关联。

聊天始终作为**未验证原文说法**展示。窄规则仅检测字符串 `已上线` 加显式 PR 引用，
并对缺少匹配生产部署的合并 PR 提出问题；含否定、引号的文本也可能触发，需人工判断。
这是确定性字面规则，不是通用 NLP。裸 `#正整数`（最多 19 位）且没有相应结构引用时，
列出全部已选仓库作为候选，连单仓库情况也不擅自补充关联。
精确规则为 `(?<![A-Za-z0-9_])#([1-9][0-9]{0,18})(?![0-9])`，先移除链接形文本再检测；
支持 `看#12需要再确认`，不匹配 ASCII 单词内的 `abc#12`、前导零或超长编号。

### 分区清单

每项恰好含 `partition_id`、`status`、`reason`。`status` 仅支持
`complete` / `partial` / `failed` / `denied`；原因非空且最多 256 字符。
分区 ID 为 `github:repo:<repo_id>:<category>` 或 `feishu:chat:<chat_id>:messages`。
必须覆盖 `request.repo_ids × request.source_categories` 及每个请求群聊，各恰好一次。
缺项、重复、额外项均拒绝。failed/denied 分区不能同时携带记录；已有部分记录时使用 partial。
必需的不完整分区导致 needs_input，但保留其他已覆盖的事实。

## 输出与安全边界

JSON 稳定核心字段：`synthetic`、`status`、`meeting_ready`、`semantic_review_required`、
`window`、`observed_at`、`scope_hash`、`sources`、`facts`、`conflicts`、`ambiguities`、
`agenda`、`gaps`、`coverage`。附带 goal、purpose、模拟授权声明、caveat、citations 和技术限制。
事实、差异、歧义、议程理由中的 `source_ids` 均解析到 `sources` 中的现有 `SourceRef`。
纯请求目标和通用讨论建议的来源数组为空，不假装它们是采集事实；缺口由分区清单支持。

`SourceRef.content_sha256` 对原始 UTF-8 摘录计算：GitHub 为 `title + "\n" + excerpt`，
飞书为原始 `text`。展示用 `SourceRef.excerpt` 已去除终端控制/双向控制字符、转义 HTML / Markdown，
并将带 `://` 的 scheme、`javascript:` / `data:` / `vbscript:` / `file:` / `mailto:`、
www、协议相对 URL 和域名形文本替换为 `（链接已省略）`。
哈希不是展示文本的哈希，也不是外部真实性证明；未保存第二份原始明文。
转义后超过既有 SourceRef 4000 字符上限会拒绝输入，不会截断。
JSON 也是经过清理的**展示投影**，不是原始文本导出；goal、reason、text、excerpt 等字符串
可能含 Markdown 反斜线和 HTML 实体。消费者应按此展示约定使用，不能用展示字符串复算原文哈希。
域名形文本只匹配 ASCII 标签，末段须为至少两个英文字母；不会将中文当作域名字符。
`升级到2.0后测试通过`、`耗时1.5秒`、`核实v2.0上线证据` 和普通 `Fix:` 前缀保留，
相邻中文中的裸 `#12` 仍参与歧义检测。`config.yaml` 等与域名同形的文件名仍会替换，
但保留周围中文（如 `修改config.yaml后再确认` 显示为 `修改（链接已省略）后再确认`）。
通过 schema 校验的 `tag` 仅作 HTML / Markdown 转义，保留 `v0.0.1-synthetic` 等点号标识符。
`ref` / `environment` 仅对严格的 ASCII 路径片段免除链接过滤：字母数字、`_`、`-`，
或带可选字母/`_`/`-` 前缀的两到三段数字版本号及可选 `-`/`_` 后缀；片段用单斜线分隔。
因此 `release/1.2`、`staging-v1.2` 可保留；其他值仍作完整链接过滤、控制字符清理与转义，
不会因属于标识符字段就信任 `rogue.invalid`、URL 或 HTML。

只有冻结 owner/repo 加类型化编号、SHA、tag 形成的 GitHub 引用 URL 能进入 citations；
飞书仅显示 source ID，不从消息拼接链接。来源正文不参与工具调用或执行。
引用仅供合成 fixture 演示，未联网验证可达性或对应资源存在。
特别是 `/deployments/<id>` 是此预览的合成定位形式，不是经确认的标准 GitHub 网页地址。
建议均标为待讨论，不生成已批准决策、不指派负责人、不编造业务期限。

此命令不读取生产配置、凭据或业务数据库，不启动 worker、不执行 subprocess、
不联网、不调用模型、不发消息、不注册公共路由、不新增依赖或持久化层。
未来的真实采集、认证签发、授权复查、语义评估和人工验收需要独立设计；本增量不实现 A11。
