# 部署、激活、运营与恢复

最新：A11b 可信权限状态已接入 BriefService，整次授权使用同一版本并拒绝并发撤权；Codex 编码、Kimi K3 审计批准，macOS/Linux 全量各 3219 项通过。见 [权限接线与下一步](A11-AUTHORITY.md)。A11业务生命周期、真实权限更新链、完整会议功能与生产部署仍待完成。

本文件是未来操作方案，本轮没有执行其中的服务器或GitHub写动作。OStag自身部署是GitHub Actions→腾讯Lighthouse；业务GitHub→Vercel流程是另一件事。Merge main会在OS Tag CI成功后自动触发生产部署，因此发布意图在merge之前核对。

## 拓扑与最小资源

开发：Mac mini专用worktree，模拟资料/网络，无生产凭据。Staging：先盘点现有云容量和隔离路径，独立Compose project、端口、test app/账号/群、数据卷、任务队列与预算；不能第二次登录生产bot WebSocket来试验。不预设旧0.5核/1GiB云探针足以跑本次真实模型和历史读取，也不自动采购。

生产：沿用现有单worker work-monitor服务的lifespan；BriefService并发1，不新起公共控制平面。新库固定到已挂载`/var/lib/ostag/briefs.sqlite3`，摘录/产物放`/var/lib/ostag/brief-artifacts`，权限限定服务用户。至少验证UID10001可写、目录不落容器临时层、卷内剩余空间、备份包含新路径、旧Monitor事件不被长模型调用阻塞。

API模型执行由批准provider配置驱动，优先复用已有backend；不把开发用Claude/Codex OAuth拷到生产，也不默认服务器已有可用模型身份。镜像内所需SDK、TLS/代理、DNS、超时、模型ID必须在stage实际调用证明。

## 开关与配置（A11a 已注册前八项，其余待接线）

六个功能开关及 STATUS_DIR、CONTROL_PATH 已由 A11a 注册，任何功能开启仍明确拒绝。STORE_PATH、ARTIFACT_DIR、GRANTS_FILE、MODEL_CONFIG 仍为后续接线方案。现有 Compose 尚未新增受保护目录挂载；配置文件校验函数也尚未安装为部署 preflight。

| 配置 | 默认/作用 |
|---|---|
| OSTAG_BRIEF_ENABLED | false；开启整个简报领域 |
| OSTAG_BRIEF_MEETINGS_ENABLED | false；M会议准备，M05实现并在D07单独放行 |
| OSTAG_BRIEF_HISTORY_ENABLED | false；单独开放A2历史读取 |
| OSTAG_BRIEF_ACTIONS_ENABLED | false；B外部业务写 |
| OSTAG_BRIEF_ROUTINES_ENABLED | false；B例行任务 |
| OSTAG_BRIEF_SALES_ENABLED | false；C销售模板 |
| OSTAG_BRIEF_STATUS_DIR | `/run/ostag-brief-status`；独立主机bind mount，服务可写、preflight主机可读，与业务数据卷/控制目录分离 |
| OSTAG_BRIEF_CONTROL_PATH | `/run/ostag-brief-control/control.json`；运行期接单控制，与状态目录隔离 |
| OSTAG_BRIEF_STORE_PATH / ARTIFACT_DIR | 持久卷路径，启动严格检查 |
| OSTAG_BRIEF_GRANTS_FILE | 受保护的PilotGrant配置与revision，不在仓库保存真实范围/凭据 |
| OSTAG_BRIEF_MODEL_CONFIG | 受保护模型引用，不在状态卡显示认证信息 |

所有开关有owner、首次激活版本和复盘日期；默认全off。A11统一注册全部flag并拒绝启用尚未实现的模块；A09/B02/B04/C02分别提供对应实现并复用flag测试。配置读取层在config.py，Compose env_file与environment覆盖优先级必须写测试，不能以为改.env即生效。停用先禁止新接单/新投递，再有界结束owner，不能只隐藏按钮。


### 运行期暂停的具体协议

选用受保护控制文件，不新增公共管理API。主机`/etc/ostag/brief-control`由可信部署操作者拥有，以独立只读目录bind mount到容器`/run/ostag-brief-control`；不放在服务可写的数据卷父目录内。服务UID10001只读，不能改控制文件或替换父目录。操作者在同一主机发布锁内以原子rename写入boot_id、单调generation、admission与maintenance状态。每次服务启动生成新boot_id，旧boot的on无效；读取失败、字段未知或版本不符均off。不设置admission环境变量，接单状态只由当前boot的控制文件决定。

每次接单、claim及新外部IO前重读；关闭后不新增读取、模型调用或发送。已在途IO可有界结束并记录事实，草稿保持未投递。只要镜像含brief能力，独立状态/排空ack写入器就始终运行，与ENABLED无关。全off不打开业务库或worker，上报admission=off、active lease=0、inflight各项=0；新鲜度/boot/generation正常校验，不能缺文件冒充off。状态目录主机路径拟为`/var/run/ostag-brief-status`，由受信安装过程建立并校验UID10001可写、主机preflight只读观察；重启后重新建立。目录缺失/不可写只使brief-status降级并在health单列，Monitor照常启动；preflight因此not_ready，不允许静默视为off。状态每1秒刷新；独立心跳维护active lease与inflight IO计数（模型、CLI、发送分项，不能只数数据库job行），状态写到服务可写的独立目录。排空应答包含本boot_id、control generation及新鲜时间；部署preflight要求同generation的fresh ack（≤5秒）、admission off、active lease=0、在途IO=0，否则not_ready。控制状态从排空到镜像切换保持off；打开也必须使用同一主机锁。所有阻塞IO必须有终止或明确uncertain结果，不能用lease过期替代排空。

首次从不含brief域的旧镜像升级时，preflight通过不可变镜像/发布manifest核实该版本没有brief消费者，并明确记legacy_no_brief；不能把缺失状态文件一概视为零任务。若已有brief能力，缺状态即not_ready。独立状态文件不能替代数据库/worker的准确事实；D02用真实故障注入证明其一致性，必须包含全off镜像可通过、有能力却缺状态拒绝、legacy例外仅限不含消费者旧版三组。

## 每次发布实例的证据绑定

release profile（A1/A2/B/C）+version+source commit+镜像ID/digest+plan hash+config hash（不含secret值）+模型身份+prompt/预算/验证规则参数版本+brief可读schema区间+数据/评测版本+grant revision+operator。缺任何必需证据就not_ready。维护版变更声明影响哪些profile，重验它们；不能把A1手工材料验收转给A2历史。

## 发布流程

1. **候选准备**：独立分支、明确文件范围、review同一最终diff、完整release gate、目标架构container smoke。CI对codex分支push不一定跑，创建PR后查pull_request CI；不能把push成功当测试成功。
2. **Stage验证**：D01只验A1；D04/D05/D06分别以含A2/B/C实现的候选重跑D01全部三项门槛和对应专项，含撤权/取消/恢复。模型真实调用与业务副作用证据独立。不满足的模块仍off。
3. **发布前排空**：进入维护窗口，通过下述受信brief-control命令关闭admission和投递认领；允许已知在途IO结束，最长等待生成180秒+30秒清理。采集active job、lease、uncertain delivery列表。未排空或在途结果无法界定则不切镜像，保持暂停并人工处理。不是通过“无active行”猜网络已静止。
4. **服务器互斥**：当前仓库脚本未发现主机锁，线上wrapper尚未检查；D02先核验再补齐所有自动、人工deploy及rollback共用锁；覆盖preflight读当前状态、备份、切换、核验和自动rollback。建议单父进程持锁，内部rollback不递归重复抢锁。锁未证明时不得部署本次功能，即使开关off；运维修复单独测试/安装。GitHub concurrency只覆盖Actions。D02还须把上述fresh排空判定放进服务器preflight，不满足在切镜像之前失败，不进入自动rollback。锁核对可提前，但完整验收依赖A02/A11。
5. **备份与恢复演练**：排空brief业务writer后，通过SQLite online backup保存briefs和现有数据库，以及受保护配置与产物清单；检查integrity_check并在临时路径restore。不同DB不是原子snapshot；记录各自时间/关联ID和对账策略。A无业务receipt写，B必须排空所有相关JSONL receipt/apply writer或进入维护以取得一致截面。不能只顺序复制文件后宣称跨库原子。
6. **Merge到生产**：本批部署设施准备窗口内暂不合main，先通过现有受信运维路径安装并验证锁/preflight脚本并同步hash；这是一项未来D02操作，本轮未冻结仓库/关闭workflow。无法完成可信安装就暂不发布本批。试点每个窗口只合一个PR，核对合并会自动部署的既定范围。squash后以新的main SHA重新确认OS Tag CI；旧PR SHA review是审查记录，实际合并tree/CI仍要验证。生产workflow拒绝stale SHA，若main向前移动应等待当前main的CI，不强行发旧版本。排空持续覆盖merge→CI→deploy；CI失败由操作者明确选择继续维护或解除排空。因not_ready失败，补齐后重跑同一当前main SHA对应run，不push空提交；main已移动则不能重跑旧发布。
7. **上线全off**：Actions构建linux/amd64不可变镜像，源码包/镜像包摘要，受限SSH上传与server脚本发布。检查服务端receipt、实际镜像ID/revision、卷/数据库备份、公共和内部health。仅HTTP200不是业务验收。
8. **A1单人激活**：只开启owner allowlist与A1，检查grant/模型配置；先在合并tree重跑A1的16情景与关键语义case各3次，避免把PR旧绿灯带过来；由受信brief-control命令恢复admission，完成一份新的真实材料简报。观察受理、纠错和投递证据；前一小时盯错误、队列、旧Monitor/relay/日报，随后24小时稳定后再扩大。
9. **A2激活**：只在对应scope/历史完整性与权限证明满足后打开A2。至少7天和10真实委托；达到指标后由owner接受再扩大到具名小组。B/C各重走独立stage、release和激活流程。
10. **交接**：squash发布后先将新main正常合回integration并运行CI，保留尚未发布工作；解决冲突需重新评审，不reset/force-push抹掉工作。下个release基于更新后的共同祖先，避免旧diff重复。产物/操作手册/开关/未决gap/恢复镜像/备份/操作者归档。例行巡检仅在用户明确要求后配置，不把计划复盘日期自动变成通知。

现有可信脚本或wrapper变更涉及PRODUCTION_DEPLOY_SCRIPT_SHA256/PRODUCTION_SSH_WRAPPER_SHA256时，在安装/校验后同步对应受保护变量；不能只改Git文件导致下一次deploy被拒。保持现有固定命令SSH入口，不打开任意shell。

## 可核验命令

以下GitHub只读命令与当前仓库流程一致；执行时用实际run id，避免watch到别的任务：

```bash
gh run list --repo osborn-studio/ostag --workflow ostag-ci.yml --branch main --limit 5
gh run list --repo osborn-studio/ostag --workflow deploy-production.yml --branch main --limit 5
gh run watch "$OSTAG_DEPLOY_RUN_ID" --repo osborn-studio/ostag --exit-status
curl --fail --silent --show-error --max-time 15 https://ostag-monitor.ihiking.me/healthz
```

服务器既有preflight入口（仅受信server环境或通过已安装wrapper支持的命令）：

```bash
/usr/local/sbin/ostag-deploy-work-monitor preflight
```

不能用deployment workflow_dispatch传旧SHA回滚，当前workflow只接受当前main。也不能从普通shell绕过强制SSH wrapper。安装/回滚需使用已验证的服务器运维路径；路径不可达时停在准确阻塞，不尝试换成宽权限登录。


### D02拟新增的受信控制命令

以下是待实现接口，当前不能当既有命令执行。固定SSH wrapper/服务器运维入口增加严格枚举子命令：`brief-control show`、`brief-control set admission=on|off maintenance=on|off`；不允许任意路径、JSON或shell。show只读；set自动读取本次服务boot_id、递增generation，持同一主机锁并原子rename，等待对应ack。maintenance=on与admission=on互斥；遇过期状态、boot变化或ack超时失败并保持关闭。on按服务器配置绑定的登录身份/密钥隔离：Actions密钥的forced command只允许show与set off/*，无法调用on；on仅在独立操作者身份的受信入口开放。身份不能来自客户端参数或SSH_ORIGINAL_COMMAND自报；测试CI身份调用on、伪造operator均拒绝。自动CI/deploy不能激活。安装该入口并同步wrapper/script可信hash后才可使用。

第3步用set admission=off maintenance=off开始排空；第8步在全部证据通过后set admission=on maintenance=off；R1用off/off，R3用off/on。show提供boot/generation、实际功能开关、ack年龄和在途分项。D02需证明每条路径确由现有受信运维身份可达，不能保留“需要写文件”却没有受限入口的缺口。

## 恢复分层

**R1功能停用**：通过受信brief-control命令关闭admission，停止接单/投递、取消新生成、等在途状态归档，关相应profile。既有Monitor继续。拟议恢复目标5分钟内停止新动作，需演练计时，不提前宣称可达。

**R2镜像回滚**：使用发布receipt中的previousImage/previousComposeFile和已验的匹配配置，在同一服务器锁内恢复。强制ENABLED/HISTORY/MEETINGS/ACTIONS/ROUTINES/SALES与admission均off，不能原样沿用previousCompose里可能为on的值；使用已验证的显式Compose恢复override并核对实际容器环境，控制文件也off且新boot不得继承on。首批Monitor schema不变；不含brief的旧版不访问brief库，含brief的旧版只在manifest可读区间内访问，否则仅brief域disabled/degraded。数据保留，所有新brief动作停用。旧镜像健康失败则保留现场按已演练恢复方案处理，不反复来回切换。

**R3业务库恢复**：先通过受信brief-control命令设置admission=off、maintenance=on；每次发布验证同一不可变digest的只读维护模式，manifest声明当前brief schema可读，禁止模型/读取外部资料/投递/写业务及迁移；无需另建可能漂移的恢复镜像；必要时将备份恢复到新文件路径检查，保留失败库。不要求事故时临时写兼容代码。不能用旧Monitor备份覆盖期间产生的monitor/relay状态。对备份以后产生的job/outbox/receipt/external effect做逐项reconciliation，恢复后admission默认off，未核对投递不重放。

现有脚本的镜像恢复形状如下，仅用于对照现状，不能直接作为本方案完整回滚命令。D02须增加前述明确全off override并校验最终Compose和容器环境，仍由锁内操作者使用receipt里的值：

```bash
OSTAG_WORK_MONITOR_IMAGE="$OSTAG_PREVIOUS_IMAGE" docker compose -p work-monitor -f "$OSTAG_PREVIOUS_COMPOSE" up -d --no-build --wait --wait-timeout 180
```

数据恢复和镜像恢复都不撤销已发消息、已建任务或已写表格；这些必须查看实际系统状态后单独更正，不能自动批量反向写回。

## 观察与停止条件

立即暂停相关能力：越权/错误接收人/错客户或字段/重复写入、无法解释的旧fence提交、资料撤回后继续外发。保存脱敏证据并修复后重验。

暂缓扩量：关键资料获取不完整、关键事实支持失败、model身份未核验、连续2个技术失败、队列超过试点上限、旧Monitor出现新错误。低量试点看绝对事件，不能只用相对百分比掩盖样本不足。工作队列、模型失败、人工纠错、数据缺口、成本、backup age与restore实测各单列。

资料保留、模型成本、周期例行任务和扩大成员范围均按各自配置管理，不用一个全局“已授权”开关。

## 会议profile与上游新增存储

D07在A2后独立验证Feishu历史+GitHub只读身份，不沿用A2“单源能读”冒充双源会议；目标容器须具有固定gh/lark CLI和实际只读凭据供给、刷新/轮换/撤销路径。GitHub user/App身份和PilotGrant交集逐repo实测，不把开发机登录复制进生产。最初仅owner，至少两repo验证后再扩大冻结清单。

MEETINGS由A11统一注册，M05实现；flag off时不额外采GitHub、不启新worker，旧报告/简报功能照常。release manifest增加GitHub API/CLI版本、repo ID清单摘要、项目映射revision、窗口/观察截止和MC评测版本。刷新与定时会议不改变旧180秒总deadline；若未来改时限必须同步drain与SLO。回滚明确将MEETINGS也off，保留简报领域数据。

U02首个material-effect journal放在已有briefs.sqlite3领域表中；D08仍须核验新schema可读区间、同库事务、备份/restore与当前digest维护。无需新增第三业务DB，但不得用旧schema的A1备份证据放行U/B。沿用ActionReceipt JSONL权威与单独journal，不宣称跨存储原子；Monitor不为上游schema reset被清空或重建。若journal方案尚未定型，只阻塞U/B相关真实写入，不阻塞D07只读会议。U02随B由D05放行并在D08回归；D08负责新增仓库适配范围，不能倒置成“D05写入等待D08”循环。U批验证镜像与注册表拒绝不受管仓库路径的物质性写；不因为打开会议功能而给agent shell/GitHub写凭据。
