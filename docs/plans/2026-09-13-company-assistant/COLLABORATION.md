# Codex、Claude Fable 5.1 与 osstack 共创流程

## 角色与证据

每个切片固定一个builder、一个独立reviewer；默认Codex实现，Claude Fable 5.1挑战和审查，也可以互换。各自独立worktree，迁移/配置/runner等公共入口同一时段只允许一名writer。用户没有指定具体Codex调用ID时，以本任务实际模型及工具返回身份记录，不用推测名字冒称GPT-6 Astra。

Fable必须显式请求`claude-fable-5-1`、不设fallback，检查返回modelUsage.canonicalModel；Haiku等CLI辅助用量单独记。模型错、不可用或无验证身份时暂停该模型评审，继续与其无关的确定性准备，不能换模型后声称Fable已审。

循环：冻结需求/AC和输入版本→builder实现最小切片→目标测试→Fable独立看最终完整diff及必要上下文→Codex用代码/复现回应→有变更则更新候选→同一最终候选通过检查→才形成PR。开发任务PR目标为拟建`codex/ostag-company-integration`，走现有不限制base的pull_request CI；main每窗口只接收一个release PR，并且D02可信锁/preflight先安装核验。squash发布后将新main正常合回integration并跑CI，保留未发布工作，不reset/force-push；共享config/flag文件同一时段单writer，其他任务基于已合入版本重放自身变更。此处是方案，本轮不创建integration分支或PR。Fable会自行提出反例而不是只做文字复述；最终事实由测试/原系统/用户验收核对。

评审结构：verdict（approved/changes_requested/blocked）、reviewed_tree_hash、实际模型、findings（严重度、触发条件、文件/行、影响、修订和验证）。退出0只是CLI成功；缺schema、拒绝、模型不符、tree不符一律不是通过。模型长文中出现“通过”不能替代结构化结果。

## 固定验证器与防自改

协议/授权/取消/恢复的确定性验收由当前批准版本固定；语义保留集由operator/reviewer独立保管。builder不读保留集，也不能改外部verifier后过关。普通功能任务不可修改固定gate目录或信任hash；gate维护单独切片、独立评审并重新发布外部只读副本。

工件：task_id/attempt_id、依赖回执、plan hash、base/tree/commit、变更文件、运行命令/退出码/非空测试数、环境/模型、review结果、live evidence、operator决定与风险。真实原文/凭据不进Git；必要摘录先脱敏。失败也保留，不自动删失败worktree前丢证据。

## osstack的真实前置

本轮只做`osstack --version`与`osstack loop doctor --workspace ... --json`：0.19.0，13个旧任务，daemon未检查。P01不是部署或真实业务的自动许可。

优先复验已有M0分支runner_probe，禁止将40项规划JSON直接塞进tasks。原生queue不执行本backlog depends_on。每次由已验收guard验证依赖和范围，单attempt生成进入独立任务工作目录，确认实际discover只见该attempt；`--cycles 1`不等于只处理一个任务。

原13任务留在原工作区。新执行checkout若继承旧tasks，复制到仓库外只读保全并在专用checkout隔离，执行层必须检查队列扫描实际结果，禁止泛化清理原tasks。task_id每次重试递增attempt，状态文件和失败工件独立。queue无执行字符串/凭据，命令参数来自固定可信launcher。

运行前证明：builder/verifier非空；指定模型无fallback；writer无越界写；reviewer只读；非法schema/失败/假成功/旧队列/抢占/超时反例拒绝；合回仅专用integration分支，不能main或生产checkout。osstack可能自动commit/ff-only合回，不能把本地合回等同GitHub合并/部署。

新版本必须重查实际CLI帮助和installed源码，不使用想象中的`--depends-on`、`--model`或`import backlog`。通过skill wrapper的doctor可以替代原生doctor，但不改变上述真实限制。

如果P01尚未准备好，A的受控实现可以在本任务执行，仍保持独立Fable评审和固定验收；在完成首个A1纵向切片检查点复盘P01，不把通用loop工程变成无限前置，也不暗自宣布loop已接管。生产发布始终走现有受治理流程。

## 本轮咨询与后续代码评审的区别

本轮Fable工具关闭，读取的是Codex提供的代码事实与完整规划文本，做方案挑战，没有自行跑仓库或服务器。因此只能报告“规划已讨论/修订”，不能报告“实现通过Fable代码审查”。真实实施时还需对准确diff、代码与测试做新一轮review。

用户对已有开发范围的授权按会话持续使用；需要新增资料范围或真实外部写时说明具体对象与理由，不问笼统“可不可以继续”。本轮只要求详细方案，不启动旧历史授权里的全部自动开发/部署。

## 本轮会议与上游共创

新增审查集中在时间窗历史正确性、组织权限交集、合并/部署区分、会议决策价值和上游已有/新增差异。Fable只收到脱敏规划与源码事实，不发送12私有仓库的原始业务内容或真实聊天。新增讨论记录见MEETING-REVIEW.md；历史四轮记录保持不变。
