# 任务分解与依赖（会议与上游增强版）

> 实施已开始：本文保留原方案快照；当前 A01、M01 已通过本地验证，最新进度见 [执行记录](EXECUTION.md)。

本版40任务、120条验收，全部planned。旧任务ID保留，新增M01—M07/D07和U01—U04/D08；P00已有部分历史基线证据，不等于完整完成。文件≤5是候选范围，独立子系统继续拆小；tests不可因文件合并缩水。新命令均为实施后入口，缺文件须失败，不能跳过假通过。

通用完成：三个AC逐项有证据；目标测试非空通过；受影响回归/lint/严格输入负例通过；当前指定独立审计模型（Kimi K3）审同一最终diff无未处理阻断；原工作区不变。真实读取、写入和发布任务还须执行VALIDATION/MEETING-PREP/DEPLOYMENT对应平台门槛，单元命令不能替代真人记录。

开发PR目标为拟建`codex/ostag-company-integration`，沿用当前不限制base的pull_request CI；main每个窗口只接收一个release PR，D02可信锁/preflight先安装。发布后main正常合回integration保留未发布工作，不reset/force-push。共享config/store/renderer同一时间单writer。每2—3任务检查一次可演示行为与依赖回执。

顺序：P00→A1→A2+会议M→D07→B→C；U01进入A1可靠投递，U02在B写回前，U03仓库执行与U04反馈独立准备，D08单独放行。D07不依赖B/C/U03，会议只读不被仓库自动执行建设阻塞。P01/I01仍可选；不自动将规划backlog塞进osstack旧13任务。

file_status仅表示本轮基线是否存在，不代表每项任务都重新创建；file_reuse_from给出新文件的创建任务，后续任务复用并修改。共享写入顺序：store为A02→U01→A06→M06→U02→B03→B04；rendering为A05→A06→M05→U04；config由A11之后按会议M05→业务B02/B04→C02分批，其他接线按依赖在单writer窗口内进行。此为实施调度约束，不新增自动调度器。

## P00 基线与旧成果保护

阶段：prep；规模：S；依赖：无；检查点：准备。

候选文件：`docs/plans/2026-09-13-company-assistant/EVIDENCE.json`（existing）。

- [ ] P00-AC1：固定main/四工作树完整SHA和dirty清单；列M0/PR增量及保全方式；重查上游2689b07→3a3136d与组织可见范围，不将59提交直接批量合并。
- [ ] P00-AC2：locked Python3.11基线与依赖差异明确；现有13任务不变。
- [ ] P00-AC3：记录生产拓扑/容量/连接身份可见性，未知项不补成功。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q
```

## P01 osstack单任务执行准备

阶段：optional；规模：M；依赖：P00；检查点：协作。

候选文件：`scripts/engineering/runner_probe.py`（new）、`tests/test_engineering_runner_probe.py`（new）、`docs/plans/2026-09-13-company-assistant/COLLABORATION.md`（existing）。

- [ ] P01-AC1：先复用8b0dd3d9探针并复验installed0.19接口，不复制未审全分支。
- [ ] P01-AC2：单attempt隔离旧队列、超时/失败存证、模型错/空verifier拒绝。
- [ ] P01-AC3：自动loop不可用时受控Codex+Fable继续合成切片，不自行开启旧daemon。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_engineering_runner_probe.py
```

## A01 来源和最小授权契约

阶段：A；规模：M；依赖：P00；检查点：合约。

候选文件：`ostag/briefs/models.py`（new）、`ostag/briefs/grants.py`（new）、`tests/test_brief_contracts.py`（new）、`tests/test_brief_grants.py`（new）。

- [ ] A01-AC1：严格类型/未知字段/时间窗/作用域及A1出网校验。
- [ ] A01-AC2：PilotGrant与原身份权限取交集，当前revision/撤销可查。
- [ ] A01-AC3：来源ID可追溯，建议/承诺/事实分开。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_brief_contracts.py tests/test_brief_grants.py
```

## A02 简报状态和作业持久化

阶段：A；规模：M；依赖：A01；检查点：状态。

候选文件：`ostag/briefs/store.py`（new）、`tests/test_brief_store.py`（new）、`tests/test_brief_recovery.py`（new）。

- [ ] A02-AC1：只前向迁移briefs库；声明可读schema区间；不兼容只停brief域，Monitor user_version不变。
- [ ] A02-AC2：接收/入队与草稿/outbox分别原子；claim/fence防陈旧提交；心跳独立于阻塞模型且续租失败停止后续IO。
- [ ] A02-AC3：崩溃/数据库忙/重复投递/非法状态迁移有负例。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_brief_store.py tests/test_brief_recovery.py
```

## A03 有来源草稿生成

阶段：A；规模：M；依赖：A01；检查点：生成。

候选文件：`ostag/briefs/generation.py`（new）、`ostag/briefs/validation.py`（new）、`tests/test_brief_generation.py`（new）、`tests/test_brief_validation.py`（new）。

- [ ] A03-AC1：冻结实际模型ID、prompt、预算集和验证规则版本及config hash；schema坏响应拒绝。
- [ ] A03-AC2：不存在source_id/自造URL拒绝，partial不出完整性断言。
- [ ] A03-AC3：注入仅作为数据；无任意工具/写凭据；每任务成本上限入评测参数，每日上限仅入config审计，均执行限额且可取消。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_brief_generation.py tests/test_brief_validation.py
```

## A04 手工材料任务纵向切片

阶段：A；规模：M；依赖：A02、A03；检查点：A1。

候选文件：`ostag/briefs/service.py`（new）、`ostag/briefs/worker.py`（new）、`ostag/core/runner.py`（existing）、`tests/test_brief_service.py`（new）、`tests/test_brief_worker.py`（new）。

- [ ] A04-AC1：A1从受理到draft_ready/outbox可在真实backend契约跑通。
- [ ] A04-AC2：单worker不阻塞monitor循环，超限请求明确失败。
- [ ] A04-AC3：发前/保存前复查grant，失败可恢复但不盲重放。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_brief_service.py tests/test_brief_worker.py
```

## A05 飞书任务卡与安全回调

阶段：A；规模：M；依赖：A04、U01；检查点：交互。

候选文件：`ostag/briefs/rendering.py`（new）、`ostag/briefs/callbacks.py`（new）、`ostag/adapters/feishu.py`（existing）、`ostag/work_monitor/feishu_routes.py`（existing）、`tests/test_brief_cards.py`（new）。

- [ ] A05-AC1：原授权私聊预览，移动端/桌面均有任务/来源/下一步。
- [ ] A05-AC2：callback绑定actor/destination/revision/expiry，重复只生效一次。
- [ ] A05-AC3：投递不明不报已收到；群状态不泄漏私人正文。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_brief_cards.py
```

## A06 纠错和内容验收

阶段：A；规模：M；依赖：A05；检查点：交互。

候选文件：`ostag/briefs/service.py`（new）、`ostag/briefs/store.py`（new）、`ostag/briefs/rendering.py`（new）、`tests/test_brief_revisions.py`（new）。

复用前置创建：`ostag/briefs/service.py` ← A04；`ostag/briefs/store.py` ← A02；`ostag/briefs/rendering.py` ← A05。

- [ ] A06-AC1：纠错创建新revision，保留前版关系和修改人。
- [ ] A06-AC2：旧卡确认不得确认新版；采用简报不创建业务批准。
- [ ] A06-AC3：纠错可见但不覆盖源系统事实；用户能撤回采用。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_brief_revisions.py
```

## A07 授权历史与线程读取

阶段：A；规模：M；依赖：A01；检查点：A2。

候选文件：`ostag/connectors/brief_sources.py`（new）、`tests/test_brief_history.py`（new）、`tests/fixtures/briefs/history-pages.json`（new）。

- [ ] A07-AC1：固定chat/请求者身份/时间窗；分页去重和thread有总预算；服务端user凭据供给/受保护存储/刷新/轮换/撤销可验，不复制Mac钥匙串。
- [ ] A07-AC2：403/缺页/循环cursor/媒体占位/撤回显式gap，不能用5页auto_base当完整历史。
- [ ] A07-AC3：CLI超时取消后清理进程并限制输出；在目标1.0.69容器验证help/契约；真实身份与平台证据另在D04验收。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_brief_history.py
```

## A08 需求表快照核对

阶段：A；规模：M；依赖：A01；检查点：A2。

候选文件：`ostag/briefs/table_quality.py`（new）、`tests/test_brief_table_quality.py`（new）、`tests/fixtures/briefs/table-quality.json`（new）。

- [ ] A08-AC1：脱敏样本精确13具名+4空白、10无负责人；不把历史数写死。
- [ ] A08-AC2：跨表8差异按明确记录映射验证，歧义保留。
- [ ] A08-AC3：只读表连接器保留空白行和人工编辑；日期语义不明不判逾期；给B02提供同资源独立读回契约。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_brief_table_quality.py
```

## A09 历史简报组合与完整性

阶段：A；规模：M；依赖：A04、A07、A08、A10、A11；检查点：A2。

候选文件：`ostag/briefs/service.py`（new）、`ostag/briefs/validation.py`（new）、`tests/test_brief_history_journey.py`（new）、`ostag/config.py`（existing）、`tests/test_brief_flags.py`（new）。

复用前置创建：`ostag/briefs/service.py` ← A04；`ostag/briefs/validation.py` ← A03；`tests/test_brief_flags.py` ← A11。

- [ ] A09-AC1：A2端到端取数→确定性核对→模型组织→带gap简报。
- [ ] A09-AC2：来源集合/时间截止显示；缺关键资料降为待补充。
- [ ] A09-AC3：刷新创建新revision，不沿用旧hash/验收；整条历史链路中单源撤权使整版失效。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_brief_history_journey.py tests/test_brief_flags.py
```

## A10 撤权删除与保留

阶段：A；规模：M；依赖：A02、A04；检查点：隐私。

候选文件：`ostag/briefs/grants.py`（new）、`ostag/briefs/retention.py`（new）、`tests/test_brief_revocation.py`（new）、`tests/test_brief_retention.py`（new）。

复用前置创建：`ostag/briefs/grants.py` ← A01。

- [ ] A10-AC1：生成中撤权取消后续出网和投递，旧fence结果不可发布。
- [ ] A10-AC2：原文/摘录到期清理，tombstone阻止缓存和索引复活。
- [ ] A10-AC3：发送过的卡不能保证回收；hash-only不显示可重验。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_brief_revocation.py tests/test_brief_retention.py
```

## A11 单服务接线与回归隔离

新增部分完成：A11b 可信本地权限状态与 BriefService 一致授权检查已验证；完整业务生命周期仍待接线。见 [范围与后续实施](A11-AUTHORITY.md)。

当前部分完成：A11a 默认关闭的开关、状态 owner、控制代次和排空验证基础已本地验证；其余 A11 验收未完成。见 [具体范围与A11b](A11-STATUS.md)。

阶段：A；规模：M；依赖：A05、A06、A10；检查点：运行。

候选文件：`ostag/config.py`（existing）、`ostag/work_monitor/server.py`（existing）、`tests/test_brief_lifespan.py`（new）、`tests/test_brief_flags.py`（new）、`deploy/work-monitor/docker-compose.yml`（existing）。

- [ ] A11-AC1：全off不创建brief worker/业务DB，但含brief能力镜像仍写状态/排空ack；统一注册全部flag，未实现模块拒绝开启；MEETINGS一并注册且仅M05就绪后可启用。
- [ ] A11-AC2：on只有一owner；运行期控制文件停接单/认领/新IO，boot默认off；独立在途计数与排空ack可核验。
- [ ] A11-AC3：延迟/失败/brief schema不兼容不破坏Monitor/relay/日报；brief降级单列；同digest只读维护模式禁止业务IO；状态目录不可写仅brief-status降级，Monitor正常、preflight拒绝。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_brief_lifespan.py tests/test_brief_flags.py
```

## A12 真实模型语义评测

阶段：A；规模：M；依赖：A06、A11；检查点：评测。

候选文件：`scripts/engineering/brief_eval.py`（new）、`examples/brief-eval/dev-cases.json`（new）、`examples/brief-eval/README.md`（new）、`tests/test_brief_eval_contract.py`（new）。

- [ ] A12-AC1：24情景定义18开发+6独立保留变体；当前只验A1的16情景子集，含至少4保留变体，不宣称A2通过。
- [ ] A12-AC2：A1关键语义case实际模型重复3次，统计所有失败/弃权与成本；参数版本不同单列。
- [ ] A12-AC3：关键断言逐项有依据/待确认，采用率和修订时间独立记录。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_brief_eval_contract.py
```

## D01 隔离staging与真实链路

阶段：release；规模：M；依赖：A11、A12；检查点：stage。

候选文件：`deploy/work-monitor/docker-compose.brief-staging.yml`（new）、`scripts/brief_staging_smoke.sh`（new）、`tests/test_brief_staging_config.py`（new）、`release/brief-live-template.md`（new）。

- [ ] D01-AC1：独立test app/凭据/群/数据卷/端口，不消费生产bot。
- [ ] D01-AC2：A1 profile目标linux/amd64镜像+真实模型+两身份/转发卡/手机桌面验证，不声称历史或跨源ACL通过。
- [ ] D01-AC3：staging独立boot/restart/cancel/revoke流程，留真实平台证据。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_brief_staging_config.py
```

## D02 发布锁和多库恢复

阶段：release；规模：M；依赖：P00、A02、A11；检查点：运维。

候选文件：`scripts/deploy_work_monitor_release.sh`（existing）、`deploy/work-monitor/ostag-github-actions-ssh`（existing）、`scripts/backup_work_monitor.sh`（existing）、`tests/test_deploy_brief_safety.py`（new）、`docs/ostag/deployment.md`（existing）。

- [ ] D02-AC1：核验server并补全所有deploy/rollback/控制写共用锁；受信brief-control show/set原子控制；preflight强制fresh drain ack/零lease/零在途，not_ready不切镜像不rollback；CI密钥身份调用on及伪造operator拒绝，仅独立操作者入口可激活。
- [ ] D02-AC2：briefs/附件备份恢复及schema兼容；全off镜像preflight通过、含brief却缺状态拒绝、legacy例外精确限定；不覆盖Monitor新数据。
- [ ] D02-AC3：可信脚本安装/hash同步后才merge本批；失败回滚强制所有明确brief功能flag及admission off，含Compose覆盖和同digest只读维护演练。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_deploy_brief_safety.py
```

## D03 A1发布与首位用户验收

阶段：release；规模：S；依赖：D01、D02；检查点：A1发布。

候选文件：`release/brief-a1-evidence.md`（new）、`release/brief-a1-manifest.json`（new）。

- [ ] D03-AC1：候选及合并tree CI/容器/模型/A1 stage证据齐全；全off上线后激活前在合并tree重跑16情景、关键语义case各3次；排空覆盖merge至deploy且仅重跑当前main。
- [ ] D03-AC2：先全部off上线，再仅owner A1，核对image/receipt/health/真实草稿。
- [ ] D03-AC3：关闭A1并恢复前镜像演练可维护旧业务，新数据保留。

最低自动验证（实施后运行）：

```bash
scripts/release_check.sh && scripts/ci_release_gates.sh
```

## D04 A2历史试点与批次A退出

阶段：release；规模：S；依赖：A09、D03；检查点：A2发布。

候选文件：`release/brief-a2-evidence.md`（new）、`release/brief-a2-manifest.json`（new）。

- [ ] D04-AC1：含A09候选在A2 profile重跑D01三项stage门槛；目标1.0.69+服务器user身份全生命周期/历史ACL；全24情景重验且关键语义case各3次。
- [ ] D04-AC2：至少7天且10个不同真实委托，统计接受/拒绝/人修/缺口。
- [ ] D04-AC3：扩到具名成员前双人可见性实测；未达标保持试点。

最低自动验证（实施后运行）：

```bash
scripts/release_check.sh && scripts/ci_release_gates.sh
```

## B01 确认行动到冻结变更

阶段：B；规模：M；依赖：A06；检查点：B合约。

候选文件：`ostag/briefs/actions.py`（new）、`ostag/core/receipts.py`（existing）、`tests/test_brief_action_proposals.py`（new）。

- [ ] B01-AC1：单资源/字段/原值/新值/身份/版本冻结，严守旧wire兼容。
- [ ] B01-AC2：内容验收不构成动作授权，重复提议关联唯一receipt。
- [ ] B01-AC3：修改目标或payload使旧批准失效。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_brief_action_proposals.py
```

## B02 单目标执行与独立读回

阶段：B；规模：M；依赖：B01、A08、A11、U02；检查点：B执行。

候选文件：`ostag/briefs/actions.py`（new）、`ostag/connectors/apply.py`（existing）、`tests/test_brief_action_execution.py`（new）、`ostag/config.py`（existing）、`tests/test_brief_flags.py`（new）。

复用前置创建：`ostag/briefs/actions.py` ← B01；`tests/test_brief_flags.py` ← A11。

- [ ] B02-AC1：apply重查身份/当前对象和授权，未批准零写。
- [ ] B02-AC2：若无条件写，不承诺single-writer覆盖多人编辑；选可证明安全路径。
- [ ] B02-AC3：provider成功仍需独立readback，不同资源/版本拒绝完成。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_brief_action_execution.py tests/test_brief_flags.py
```

## B03 跨库与外部不确定对账

阶段：B；规模：M；依赖：B02；检查点：B恢复。

候选文件：`ostag/briefs/reconciliation.py`（new）、`ostag/briefs/store.py`（new）、`tests/test_brief_action_reconciliation.py`（new）。

复用前置创建：`ostag/briefs/store.py` ← A02。

- [ ] B03-AC1：receipt创建/外部写/本地回写各崩溃窗口可定位。
- [ ] B03-AC2：响应丢失、重复worker、人工并发不盲重放、不伪造成功。
- [ ] B03-AC3：无法唯一证明结果保留待核验，人工对账有身份与理由。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_brief_action_reconciliation.py
```

## B04 一条例行简报

阶段：B；规模：M；依赖：A11、A09、M05；检查点：B例行。

候选文件：`ostag/briefs/routines.py`（new）、`ostag/briefs/store.py`（new）、`tests/test_brief_routines.py`（new）、`ostag/config.py`（existing）、`tests/test_brief_flags.py`（new）。

复用前置创建：`ostag/briefs/store.py` ← A02；`tests/test_brief_flags.py` ← A11。

- [ ] B04-AC1：模板/资料/受众/时区/频率/预算可见，例行执行不能再建例行；支持会议模板，未就绪模板拒绝启用，时间窗随每次实例冻结。
- [ ] B04-AC2：每次执行复核grant，重启补偿有窗口且不追发全部旧实例。
- [ ] B04-AC3：只在变化/失败/需决定通知，暂停后未投递作废。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_brief_routines.py tests/test_brief_flags.py
```

## D05 B真实写回验收与发布

阶段：release；规模：S；依赖：B03、B04、D04、D07；检查点：B发布。

候选文件：`release/brief-b-evidence.md`（new）、`release/brief-b-manifest.json`（new）。

- [ ] D05-AC1：B profile含B03/B04候选重跑D01三项stage门槛及B专项；采用A格式输入，先测试表/任务再获具体原表动作授权。
- [ ] D05-AC2：真实重复请求/响应丢失/并发编辑/撤权验收零错写及重复。
- [ ] D05-AC3：独立B开关、部署/读回/降级回只读，外部效果不随镜像撤销。

最低自动验证（实施后运行）：

```bash
scripts/release_check.sh && scripts/ci_release_gates.sh
```

## C01 销售文字与客户匹配

阶段：C；规模：M；依赖：A01；检查点：C。

候选文件：`ostag/briefs/sales.py`（new）、`tests/test_sales_brief.py`（new）、`tests/fixtures/briefs/sales-cases.json`（new）。

- [ ] C01-AC1：客户歧义必须确认，金额/日期/阶段不猜。
- [ ] C01-AC2：销售私聊不默认进管理报告或另一销售上下文。
- [ ] C01-AC3：只用指定原表，不创建伴随表或改权限。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_sales_brief.py
```

## C02 复用业务闭环

阶段：C；规模：M；依赖：C01、B03、A11；检查点：C。

候选文件：`ostag/briefs/sales.py`（new）、`tests/test_sales_brief_journey.py`（new）、`examples/brief-eval/sales-cases.json`（new）、`ostag/config.py`（existing）、`tests/test_brief_flags.py`（new）。

复用前置创建：`ostag/briefs/sales.py` ← C01；`tests/test_brief_flags.py` ← A11。

- [ ] C02-AC1：复用grant/task/revision/receipt/readback，不另建一套核心。
- [ ] C02-AC2：文字到草稿到确认执行完整链路，错误客户负例阻断。
- [ ] C02-AC3：记忆更正与业务更正区分，后续提醒需独立授权。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_sales_brief_journey.py tests/test_brief_flags.py
```

## D06 销售试点与发布交接

阶段：release；规模：S；依赖：C02、D05；检查点：C发布。

候选文件：`release/brief-c-evidence.md`（new）、`release/brief-c-manifest.json`（new）。

- [ ] D06-AC1：C profile含C02候选重跑D01三项stage门槛及C专项；指定sales用户/表/字段/模型出网，测试表和真实表分开。
- [ ] D06-AC2：至少7天且10真实跟进，人工纠错负担与有效记录数达到商定目标。
- [ ] D06-AC3：留操作/撤权/恢复手册，其他能力开关状态明确。

最低自动验证（实施后运行）：

```bash
scripts/release_check.sh && scripts/ci_release_gates.sh
```

## I01 PR证据独立保全和评测

阶段：optional；规模：S；依赖：P00；检查点：独立线。

候选文件：`docs/research/pr-evidence-integration.md`（new）。

- [ ] I01-AC1：保留f62b68a5及差异，集成单独评审不整体合M0。
- [ ] I01-AC2：先真实GitHub只读控制证据，再补受控diff/模型样本，分开验收。
- [ ] I01-AC3：不自动APPROVE/MERGE/DEPLOY，不作为A交付前置。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_github_review_evidence.py tests/test_pr_review_evaluation.py
```

## M01 会议范围与项目映射

阶段：meeting；规模：M；依赖：A01；检查点：会议契约。

候选文件：`ostag/briefs/meeting_models.py`（new）、`ostag/briefs/project_mapping.py`（new）、`tests/test_meeting_contracts.py`（new）。

- [ ] M01-AC1：冻结目的/时区/半开窗口/群repo ID与分支/受众，采集后封存实际观察截止；受理前做最少调用/时间预检并固定状态表。
- [ ] M01-AC2：租户与请求者权限交集；群/仓库/人员映射具名版本，转名/转移/新增仓库不自动继承。
- [ ] M01-AC3：严格拒绝歧义相对日期与跨租户引用，提供可修改范围确认卡契约；冻结会议评测规格/评分规则/独立holdout保管协议。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_meeting_contracts.py
```

## M02 GitHub组织时间窗取数

阶段：meeting；规模：M；依赖：M01、A07；检查点：GH取数。

候选文件：`ostag/connectors/github_history.py`（new）、`tests/test_github_history.py`（new）、`tests/fixtures/meetings/github-history.json`（new）。

- [ ] M02-AC1：按冻结清单逐repo有界采集PR/Issue/评论/timeline/default-branch commits，保留event_at/observed_at，updated_at只筛候选。
- [ ] M02-AC2：分页完整性/循环cursor/1000搜索上限/窗口后更新/旧作者日期/权限撤销/半开边界有反例，不以Events页当全量；每repo每类/每群子预算，required分区partial不可ready。
- [ ] M02-AC3：read-only transport限字节/页数/重试/总deadline并kill-reap；身份缓存隔离/ETag校验/限流对Monitor无饥饿；cursor/消耗/deadline持久，恢复不重置；120秒取数软截止保留60秒生成，硬deadline仍180秒。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_github_history.py
```

## M03 合并检查发布证据分类

阶段：meeting；规模：M；依赖：M02；检查点：GH事实。

候选文件：`ostag/briefs/github_change_facts.py`（new）、`ostag/connectors/github_history.py`（new）、`tests/test_meeting_release_facts.py`（new）。

复用前置创建：`ostag/connectors/github_history.py` ← M02。

- [ ] M03-AC1：PR合并、default-branch commit、release published、Actions成功与production部署各自事实，不互相冒充；首次无旧head非PR合入时间unknown；旧head须来自同repo/分支/principal有效TTL快照。
- [ ] M03-AC2：复用ProviderChangeEvidence完整current-head验证；过去事件与当前观察分开，缺环境/receipt则部署unknown。
- [ ] M03-AC3：rerun/checks过期/不同SHA或repo/回滚/删除记录/同一PR多事件去重有明确断言。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_meeting_release_facts.py
```

## M04 跨来源冲突与议题提取

阶段：meeting；规模：M；依赖：M03、A09；检查点：事实整合。

候选文件：`ostag/briefs/meeting_facts.py`（new）、`ostag/briefs/validation.py`（new）、`tests/test_meeting_facts.py`（new）。

复用前置创建：`ostag/briefs/validation.py` ← A03。

- [ ] M04-AC1：显式链接和已批准项目映射优先；语义相关仅候选，Feishu身份与GitHub登录不按同名自动合并。
- [ ] M04-AC2：聊天承诺/PR事实/发布证据分列，矛盾按时间展示；无证据不推导无进展/人员绩效。
- [ ] M04-AC3：关键资料缺失阻止meeting_ready，数字确定性核对、关键论断人工语义验证；源失败置顶，按待决/阻塞/冲突而非bot活跃量选题。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_meeting_facts.py
```

## M05 会议准备端到端功能

阶段：meeting；规模：M；依赖：M04、A11、U01；检查点：首个会议包。

候选文件：`ostag/briefs/meetings.py`（new）、`ostag/briefs/rendering.py`（new）、`ostag/config.py`（existing）、`tests/test_meeting_journey.py`（new）、`tests/test_brief_flags.py`（new）。

复用前置创建：`ostag/briefs/rendering.py` ← A05；`tests/test_brief_flags.py` ← A11。

- [ ] M05-AC1：复用BriefService/job/revision/grant/outbox，生成会前阅读/重要变化/冲突风险/至多3重点决策议题与时间预算/建议行动。
- [ ] M05-AC2：OSTAG_BRIEF_MEETINGS_ENABLED默认off且未实现拒绝开启；私聊状态+可导出的本地MD，原A1/A2和其他业务回归通过。
- [ ] M05-AC3：先确认目的和范围再取资料；没有日历邀请/群分享/表写，采取建议不构成批准；shareable_sources默认空，来源权限与受众交集逐项检查。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_meeting_journey.py tests/test_brief_flags.py
```

## M06 会前刷新与版本差异

阶段：meeting；规模：M；依赖：M05、A10；检查点：持续准备。

候选文件：`ostag/briefs/meetings.py`（new）、`ostag/briefs/store.py`（new）、`tests/test_meeting_refresh.py`（new）。

复用前置创建：`ostag/briefs/meetings.py` ← M05；`ostag/briefs/store.py` ← A02。

- [ ] M06-AC1：冻结窗口不变时刷新增加observation revision；扩大窗口或范围是新request/grant，不偷偷采集。
- [ ] M06-AC2：新旧议题/事实差异可追溯，保留人工更正与冲突；删源撤权使整版不可继续发布。
- [ ] M06-AC3：关键资料无法刷新则needs_input/旧版明确stale；只合并未开始投递的旧状态，不覆盖已发送/不确定结果。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_meeting_refresh.py
```

## M07 会议专项评测与可用性

阶段：meeting；规模：M；依赖：M06；检查点：会议评测。

候选文件：`scripts/engineering/meeting_eval.py`（new）、`examples/meeting-eval/README.md`（new）、`examples/meeting-eval/dev-cases.json`（new）、`tests/test_meeting_eval_contract.py`（new）。

- [ ] M07-AC1：MC01—MC24会议专项18开发+6私有保留，与旧Q01—Q24分开，关键语义case各3次并保留所有失败。
- [ ] M07-AC2：时间/关联/权限/合并发布区分全部精确通过，议题支持与决策价值由owner盲评。
- [ ] M07-AC3：3个人工基线+7天10份不同真实会议委托指标定义齐全；尚未真人评测只报not_run。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_meeting_eval_contract.py
```

## D07 会议功能真实试点与发布

阶段：release；规模：M；依赖：M07、D04、D02；检查点：会议发布。

候选文件：`release/meeting-evidence.md`（new）、`release/meeting-manifest.json`（new）、`scripts/meeting_staging_smoke.sh`（new）、`tests/test_meeting_staging_config.py`（new）。

- [ ] D07-AC1：会议profile重跑D01三项stage门槛，真实Feishu+GH user只读身份、两人权限、至少2repo，扩到组织清单需覆盖证明。
- [ ] D07-AC2：同候选完整MC24及受影响Q套件/CI/容器通过，全off上线后准确合并tree重验，再由operator单人激活。
- [ ] D07-AC3：7天10不同真实会议委托与人工基线对比；会议flag独立关闭、原域继续，撤权/drain/恢复均留证据。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_meeting_staging_config.py
```

## U01 移植投递观测与结果不确定处理

阶段：upstream；规模：M；依赖：A02；检查点：上游投递。

候选文件：`ostag/briefs/delivery.py`（new）、`ostag/briefs/store.py`（new）、`tests/test_brief_delivery_observation.py`（new）。

复用前置创建：`ostag/briefs/store.py` ← A02。

- [ ] U01-AC1：移植上游观测语义到Feishu专属outbox，稳定delivery ID与revision绑定；只合并证明尚未开始的投递。
- [ ] U01-AC2：响应丢失仅在可唯一验证provider ID与内容时确认，不能搜索一句相同文本就认领；未知保持待核验不重发。
- [ ] U01-AC3：持久输入后及时受理、重启不丢，接收/开始/生成/收到分开；观测保留有界，原始不确定证据不可覆盖。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_brief_delivery_observation.py
```

## U02 外部操作日志与执行授权适配

阶段：upstream；规模：M；依赖：B01、A02；检查点：上游操作。

候选文件：`ostag/core/effects.py`（new）、`ostag/core/effect_journal.py`（new）、`ostag/core/approvals.py`（existing）、`tests/test_effect_journal.py`（new）、`ostag/briefs/store.py`（new）。

复用前置创建：`ostag/briefs/store.py` ← A02。

- [ ] U02-AC1：复用ActionReceipt作为唯一批准权威，闭合操作kind+目标/payload/身份/attempt/fence/expiry摘要；会议读取不走外部写许可。
- [ ] U02-AC2：B中央事务侧journal与动作意图同briefs.sqlite3原子，IO前持久begin；与JSONL批准分开对账，not-started不等于provider absent。
- [ ] U02-AC3：记录证据而非模型指定成功状态，未知副作用不因新attempt/人工备注自动重试；新持久路径有备份和领域兼容约束；合并测试文件保留原authority/故障反例，D05先验证B路径。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_effect_journal.py
```

## U03 仓库批次证明与能力门槛

阶段：upstream；规模：M；依赖：U02；检查点：上游仓库。

候选文件：`ostag/backends/executor_runtime.py`（existing）、`ostag/core/workspace_attestation.py`（new）、`tests/test_workspace_attestation.py`（new）、`tests/test_repository_write_conflicts.py`（new）。

- [ ] U03-AC1：交付repo identity/base SHA/worktree realpath/attempt/fence/变更摘要证明契约，受控适配器验证审批前与执行前重核。
- [ ] U03-AC2：缺证明、workspace切换、越界路径/符号链接、互相冲突ACP写操作拒绝；实际CLI不具备管控能力则不宣称支持。
- [ ] U03-AC3：当前普通CLI没有受管cwd/ACP证据，真实路径缺conformance必须unsupported；不宣称完整ACP或自动push/PR/merge。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_workspace_attestation.py tests/test_repository_write_conflicts.py
```

## U04 审批反馈和状态时间语义

阶段：upstream；规模：M；依赖：A11；检查点：上游反馈。

候选文件：`ostag/work_monitor/presence.py`（existing）、`ostag/briefs/rendering.py`（new）、`tests/test_agent_presence.py`（existing）、`tests/test_brief_feedback.py`（new）。

复用前置创建：`ostag/briefs/rendering.py` ← A05。

- [ ] U04-AC1：已批准/待执行/执行中/结果不明独立呈现，批准反馈不等worker；旧卡控件不能复活已终结动作。
- [ ] U04-AC2：保留既有六态Presence只读模型，仅新增反馈/时序投影与守卫；heartbeat不伪称租约/完成证明。
- [ ] U04-AC3：接收/排队/执行/投递时间单列，clock skew有界、busy不被queued覆盖，相关日志脱敏/有界保留。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_agent_presence.py tests/test_brief_feedback.py
```

## D08 上游适配批次验收

阶段：release；规模：M；依赖：U01、U02、U03、U04、D05；检查点：上游发布。

候选文件：`release/upstream-adaptation-evidence.md`（new）、`release/upstream-adaptation-manifest.json`（new）、`tests/test_upstream_adaptation_journey.py`（new）、`docs/upgrade/2026-09-13-opentag-sync.md`（new）。

- [ ] D08-AC1：固定上游59commit分类与本地映射，保留MIT/Apache来源说明，所有源码/测试/未实测差异逐项有回执。
- [ ] D08-AC2：复用D01隔离stage检验受管仓库写与业务effects路径；证明旧CLI不越权升级、未知副作用不盲重放。
- [ ] D08-AC3：独立release走排空/锁/备份/准确SHA/全off/操作者激活，新journal兼容和当前digest维护恢复演练通过。

最低自动验证（实施后运行）：

```bash
uv run --locked pytest -q tests/test_upstream_adaptation_journey.py
```
