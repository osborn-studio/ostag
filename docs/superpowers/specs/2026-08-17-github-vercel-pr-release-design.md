# OStag GitHub PR 审核与 Vercel Production 发布规格

**日期：** 2026-08-17

**状态：** 基于用户明确交付授权进入实施；下列假设如有偏差需先修订本规格

**范围：** 群内 @OStag 的 GitHub PR 通知，经过 OStag receipt 治理后提交 GitHub
`APPROVE` review，并把同一 PR head SHA 发布到指定 Vercel Production 项目

## 1. 目标

为没有足够 Vercel 项目权限的协作者提供一条最小授权发布路径：协作者在已授权群中
@OStag 并附上 GitHub PR URL，OStag 自动读取 PR、校验仓库/群/状态/checks、冻结
PR head SHA，并生成一张高风险 action receipt。拥有 OStag `write` 权限的成员依次执行
`approve` 和 `apply` 后，OStag 使用自己的受限 GitHub/Vercel 凭据：

1. 再次确认 PR 仍然 open、非 draft、head SHA 未变化且 checks 满足策略；
2. 针对冻结 SHA 向 GitHub 提交 `APPROVE` review；
3. 针对同一 SHA 创建 Vercel `target=production` deployment；
4. 等待部署到 `READY` 且 production alias 已分配后，向群返回审核和上线证据；
5. 把提议、批准、执行、失败或成功写入现有 run ledger。

成功标准不是“已发请求”，而是 receipt 进入 `applied`，GitHub 返回 `APPROVED`，且
Vercel 返回同一 deployment 已 `READY` 并分配 Production alias。

## 2. 假设与非目标

### 2.1 假设

- 群通知已经由现有 Feishu/Lark/Slack/DingTalk adapter 规范化为
  `NormalizedEvent`，可信 actor/chat 来自 adapter，不从文本推导。
- GitHub 认证继续使用宿主机已经认证的 `gh` CLI；不把 GitHub token 加入 OStag 配置。
- Vercel 使用只存于服务端环境变量的 Access Token，并显式绑定 Team 与 Project。
- 一个启用的 OStag 实例只服务一个 GitHub 仓库到一个 Vercel Project 的映射。
- 生产发布必须保留现有 `propose -> approve -> apply` 人工治理，不允许收到消息后直接写。

### 2.2 非目标

- 不自动 merge PR，不修改分支保护、ruleset 或 Vercel 项目成员权限。
- 不接受任意 GitHub/Vercel 项目；不支持群消息临时覆盖 Team、Project 或目标环境。
- 不把 Vercel token、完整第三方错误体或请求头写入聊天、ledger 或 audit log。
- 不在本期实现多仓库映射、回滚、取消部署或自动绕过失败 checks。
- 不声称完成真实生产发布；没有真实凭据和指定测试 PR 时只形成经过 mock 边界验证的
  production-candidate。

## 3. 技术栈与官方接口

- Python `>=3.11`；只使用标准库 `urllib`/`asyncio`，不增加运行时依赖。
- GitHub CLI `gh api` 调用
  `POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews`，请求包含冻结的
  `commit_id` 和 `event=APPROVE`。GitHub 要求 fine-grained token 或 GitHub App 具有
  `Pull requests: write`。
- Vercel REST API `POST /v13/deployments`，请求包含 `project`、
  `target=production` 与 `gitSource={type: github, org, repo, ref, sha}`；Team 资源通过
  `teamId` query parameter 绑定，认证为 Bearer Access Token。
- 创建后轮询 Vercel deployment，直到 `READY`/`ERROR` 或超时；只在
  `READY + target=production + aliasAssigned=true` 时视为上线。

官方来源：

- GitHub PR reviews：<https://docs.github.com/en/rest/pulls/reviews#create-a-review-for-a-pull-request>
- Vercel create deployment：<https://vercel.com/docs/rest-api/deployments/create-a-new-deployment>
- Vercel API authentication/team scope：<https://vercel.com/docs/rest-api>

## 4. 命令

```bash
# 安装/测试
uv sync --extra dev
uv run pytest -q tests/test_github_vercel_release.py tests/test_github_connector.py

# 全量验证
uv run pytest -q
uv run ruff check .
scripts/release_check.sh

# 本地 dry-run（不会触发第三方写）
python -m ostag.main --platform feishu --dry-run --event examples/feishu_event.json
```

## 5. 项目结构与接口

```text
ostag/connectors/github.py
  GitHubPullRequestConnector：读取并严格校验 PR/source/check 证据，提交 SHA-bound review

ostag/connectors/vercel.py
  VercelClient：固定访问 api.vercel.com，创建并轮询 Production deployment

ostag/workflows/github_vercel_release.py
  PR URL 解析、群/仓库 admission、receipt 构造、apply-time 再校验和组合执行

ostag/core/runner.py
  在通用 workflow/backend 前路由确定性的 PR 发布通知

ostag/config.py / .env.example
  默认关闭的 feature flag、群/仓库 allowlist、Vercel team/project/token 与超时

tests/test_github_vercel_release.py
  纯 fake 边界单元/集成测试；不访问真实 GitHub 或 Vercel
```

稳定输入契约：

```python
@dataclass(frozen=True)
class PullRequestSource:
    repository: str
    number: int
    url: str
    head_sha: str
    head_ref: str

async def prepare(event: NormalizedEvent, run_id: str) -> ActionReceipt | str | None:
    """返回冻结 receipt、确定性拒绝消息，或表示非本 workflow 的 None。"""
```

第三方响应一律视为不可信数据：限制大小，要求 JSON object，逐字段验证类型、状态、URL、
repository、PR number、SHA、deployment ID 和 deployment URL。

## 6. 行为与状态流

```text
群内 @OStag + GitHub PR URL
  -> feature enabled?
  -> group + platform:chat allowlisted?
  -> repository exact match?
  -> GitHub PR open + non-draft + same-repo head?
  -> checks policy satisfied?
  -> high-risk receipt (head SHA/ref frozen)
  -> write actor: approve <ref>
  -> write actor: apply <ref>
       -> re-read PR/checks and compare frozen SHA/ref
       -> GitHub APPROVE(commit_id=frozen SHA)
       -> Vercel Production deploy(gitSource.sha=frozen SHA)
       -> poll READY + production alias
       -> ledger evidence + group reply
```

错误遵循 fail-closed：任一 admission 或 apply-time 校验失败时，不执行后续写；GitHub review
成功但 Vercel 失败是可观察的部分完成，receipt 标记 `failed` 并在 ledger detail 中保留不含
秘密的阶段证据。Vercel 默认部署去重保持开启，不设置 `forceNew=1`。

## 7. 配置

```dotenv
OSTAG_GITHUB_VERCEL_RELEASE_ENABLED=false
OSTAG_GITHUB_VERCEL_RELEASE_REPOSITORY=owner/repo
OSTAG_GITHUB_VERCEL_RELEASE_CHANNELS=feishu:oc_xxx
OSTAG_GITHUB_VERCEL_RELEASE_BASE_BRANCH=main
OSTAG_GITHUB_VERCEL_REQUIRED_CHECKS=release-check
OSTAG_VERCEL_ACCESS_TOKEN=
OSTAG_VERCEL_TEAM_ID=team_xxx
OSTAG_VERCEL_PROJECT_ID=prj_xxx
OSTAG_VERCEL_PROJECT_NAME=my-project
OSTAG_VERCEL_DEPLOY_TIMEOUT_SECONDS=600
OSTAG_VERCEL_POLL_INTERVAL_SECONDS=2
```

2026-09-04 的 fail-closed 加固取消了 checks 可选开关；Production 发布始终要求
current-head checks 完整、非空、包含配置的可信检查且全部通过；目标 base branch 也会冻结并在
apply 时重验。

该 JSONL receipt/ledger 部署模型要求单写者；进程在 provider 响应确认前退出时，`applying`
视为不确定状态，必须先人工核对 GitHub/Vercel，禁止自动重放 Production 写入。

启用时所有必填项必须存在，channel 必须采用 `platform:chat_id`，repository 必须为规范化
`owner/name`，Vercel timeout/poll 必须在有界范围内。Vercel endpoint 不可由群文本或普通
环境变量覆盖，避免 SSRF。

## 8. 代码风格

沿用项目的 frozen dataclass、明确异常和 100 字符行宽。边界验证后，内部代码使用已验证
类型；不会用宽泛 dict 猜测业务状态。

```python
if evidence.head_sha != frozen.head_sha:
    raise SchemaError(
        "provider_sha_mismatch",
        "GitHub PR head SHA changed after approval",
        "Create a fresh release receipt for the new SHA.",
    )
```

## 9. 测试策略

- 小型测试：PR URL/allowlist/config/parser、严格 JSON schema、状态/check 判定。
- 中型测试：fake `gh` + fake Vercel transport 验证 exact argv/body/query、SHA 绑定、轮询、
  超时、失败和秘密不泄漏。
- Runner 集成：群 mention 自动产生 receipt；非 mention、私聊、非白名单群、错误仓库不会写；
  `approve -> apply` 只执行一次组合路径并留下 ledger evidence。
- 回归：现有 GitHub issue、provider evidence、receipt 与 runner 测试保持通过。
- 外部验证：只有在操作者提供专用测试 PR、Vercel Project 与明确授权后才执行一次真实
  Production smoke；否则记录为外部 blocker，不伪造上线结论。

## 10. 边界

### Always

- 默认关闭；严格群和仓库 allowlist；production target 固定在服务端。
- receipt 风险为 `high`，沿用 write 权限、quorum、four-eyes、TTL 和 apply flag。
- 提议时与执行时都校验 PR；review 和 deploy 绑定同一个冻结 SHA。
- 第三方输入限长并严格验证；错误信息和日志做秘密隔离。
- 执行聚焦测试、全量测试、Ruff、release check 和 staged diff 秘密扫描。

### Ask first

- 真实 GitHub review、真实 Vercel Production deployment、修改 Vercel/GitHub 权限或项目设置。
- 增加第二个仓库/项目映射、自动 merge、跳过 checks、自动 apply 或回滚能力。

### Never

- 提交 token/.env；从群文本读取 token/team/project/target；部署未冻结或变化后的 SHA。
- 在 checks 失败/等待、PR draft/closed/fork、receipt 未批准或 alias 未分配时声称上线。
- 让通用 LLM 决定是否执行 GitHub/Vercel 写入。

## 11. 验收标准

1. 启用且在白名单群中收到唯一、合法、允许仓库的 PR URL 时，生成一张包含准确
   repo/number/head SHA/ref 的高风险 receipt，proposal 阶段没有第三方写。
2. 未通过 OStag `approve` 与 `apply` 状态机时，GitHub review/Vercel deployment 调用数为 0。
3. apply 前 PR 状态、draft、fork、SHA/ref、checks 任一不满足时 fail-closed，Vercel 调用数为 0。
4. 成功路径先提交 SHA-bound GitHub `APPROVE`，再创建相同 SHA 的 Vercel Production
   deployment，并等待 `READY + aliasAssigned=true`。
5. 重复/并发动作由 receipt 状态机阻止；Vercel 不强制新建重复 deployment。
6. 所有回复、ledger、audit、测试输出不含 Vercel token。
7. 聚焦测试、全量 pytest、Ruff 和 release check 通过；没有凭据时明确报告未做真实发布。

## 12. 开放问题

- 是否需要在后续版本把“一仓库一项目”扩展为经过签名配置的多项目映射。
- 是否需要在 Vercel deployment `READY` 后增加项目自定义 health URL 验证与自动回滚。
- 是否允许未来把两步 `approve`/`apply` 合并为受更严格策略保护的单次卡片操作；本期不做。
