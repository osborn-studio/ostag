# 会议准备：飞书讨论 × GitHub同期变化

版本：2026-09-13 revision 2；状态：开发规格，尚未实现。用户本轮明确要求此功能集成OStag，并希望完成MD后再开始开发。本文件扩充A2，沿用[主方案](README.md)、[任务](TASKS.md)、[部署](DEPLOYMENT.md)，不另起机器人或审批系统。

## 目标和用户流程

用户在飞书私聊交办：“准备下周项目例会，看看最近一周讨论和osborn-studio的变化，重点找需要我决定的事。”第一版提供单次准备；日历读取、发邀请、主动发群不隐含开启。输入可以指定会议时间/时长，也可只给工作日期范围。

1. 先呈现目的、具体日期和时区、项目、授权群与仓库清单、资料截止、接收人。已有明确映射可直接使用并回显；“本周”等相对词有明确用户时区才解析，歧义进入needs_input，不能静默用服务器时区。
2. 一次受理，返回稳定任务ID与状态。逐来源读取，显示已覆盖/缺失范围；不能拿“已排队”表示开始执行。
3. 给一份可阅读的会议包：一句话会议目标、会前阅读、重要变化、冲突风险、建议议程、待决问题、建议行动、来源和缺口。私聊卡片为摘要，本地Markdown为完整导出；不自动创建云文档。首次默认30分钟草案可修改，最多突出3个关键决策议题，其余收进附录。
4. 用户可纠正、补来源、刷新或采用当前版本。刷新显示新增/撤回/变化，不覆盖人工更正；采用会议包不代表批准建议中的任务、日期、表更新或代码发布。
5. B阶段可选择会议模板例行运行，明确频率/时区/目的/范围/受众/预算，每次实例重新检查授权；本轮未建立任何提醒或自动任务。

## 输出示例（全部合成，仅展示格式）

> 项目例会准备｜9月7日00:00—9月14日00:00，Asia/Shanghai；观察于9月14日08:30。
>
> 目标：决定是否开放下一批试用。
>
> 会前阅读：本周PR变更摘要、测试覆盖与资料缺口。
>
> 已知变化：功能PR合并；当前观察到检查通过；尚无同SHA生产部署证据。
>
> 冲突：聊天说“已上线”，GitHub只证明“已合并”。来源[S1]、[G2]分别附时间。
>
> 议题1（8分钟）：现在开放试用，还是先补部署与验证证据？列选项、影响与需要核实的信息。
>
> 建议行动：请确认验证负责人及日期；不把建议写成某人已经承诺。
>
> 覆盖：已纳入2/2授权仓库、1/2授权群；另一个关键群读取失败，状态为“资料待补充”。

每个关键事实/议题理由指向可信SourceRef；结论可标事实、人工陈述、推断、建议、待确认。模型不得生成任意URL、伪造负责人或把技术活动数量当绩效/价值。项目进展围绕功能/里程碑组织，PR与其中commit避免重复计成果。

## 输入契约与权限

MeetingRequest至少含namespace、requesting_actor、meeting_purpose、project_mapping_revision、timezone、window_start_inclusive、window_end_exclusive、requested_as_of（可选）；采集后封存collection_started_at/collection_finished_at/observation_cutoff、repo_id/full_name/default_branch/head_sha、chat_ids、audience、shareable_sources（默认空，仅请求者私聊）、required_sources、grant_revision、budget_profile。observation_cutoff是采集结束后封存的实际最大观察时间，不是受理时伪造的原子快照时刻；各API观测有时间跨度，历史requested_as_of不能由当前内容冒充。Snapshot记录event_at、observed_at、provider_updated_at、分页/缺口和内容hash。日期保持原始时区与UTC归一值，DST/跨午夜/未来窗口/时钟误差有测试。

本轮GitHub组织API分页返回12个当前身份可见仓库，均private。此为发现证据，不证明拥有者之外的人可见、不证明未来服务器凭据可见，也不保证组织没有其他不可见仓库。产品先发现再冻结用户授权清单；新增、改名、转移的仓库不会默默加入。repo ID绑定身份，名称和归属变化重新校验。测试初期可用2仓库，但只有覆盖冻结全清单才显示“该范围已覆盖”；不能用2仓库结果冒称整个组织完整。

Feishu采用A07已核验的user身份路径；GitHub只读凭据独立供给，优先符合部署条件的GitHub App user授权或细粒度只读身份，通过CLI/凭据服务引用使用。App安装可见不等于请求者可见；逐资源对请求者证明读取权限，与PilotGrant取交集。组织列表、群名、私有仓库名和链接本身也不能泄露给无权受众。每次读取、模型出网、提交结果、投递与刷新前检查当前授权；缓存key包含tenant/principal/resource/grant revision，304不能绕过撤权检查。

每条事实保留source_visibility；输出默认只给请求者。导出/分享给其他人需另外核对受众与原资料权限；当前M不自动群发、发邀请、建任务或写表。B外发必须通过每个来源与受众的可分享交集；不能自动“脱敏一下”就扩大受众。非可分享来源只能由用户明确裁剪并形成新revision，重新核对含义和引用。真实资料保留/出网政策沿用主方案，合成材料开发不等待凭据。

## GitHub取数与时间语义

组织动态Events和Search仅用于发现线索，不作为完整历史账本；1000搜索上限反例验证不得将Search结果当完整主读取，不要求实现Search主路径。主读取按冻结repo逐个执行，API版本和CLI版本随manifest固定，链接来自验证过的repo/provider对象。接口参数以目标版本schema/help为准，以下为实现契约而非已存在的新CLI：

| 对象 | 读取与选择规则 | 必须避免 |
|---|---|---|
| PR/Issue | repository issues/pulls以all状态收候选；updated_at≥start只是候选，窗口后更新也保留；按事件实际created/merged/closed/reopened时间归档 | 只取当前open或把updated_at≤end作为筛选，漏掉后来又更新的PR |
| 讨论 | 对相关候选有界读取timeline、issue comments、PR reviews/review comments；评论创建与后编辑分别标注 | 把今天编辑的正文当窗口内当时原文；忽略缺页 |
| commits | 窗口内PR的merged_at与merge_commit_sha作为合入锚；固定当前head；上次可信head存在时compare旧head到新head观察变化；日期筛选只作记录时间附录 | 老作者日期commit本周才合并、force push、直接用push数算完成 |
| Releases | published_at在窗口内，draft不当已发布，prerelease单标 | tag/Release等同部署或上线 |
| Actions/checks | run_id/run_attempt/head_sha及对应时点；沿用完整非空current-head检查语义 | 用今天rerun绿灯改写过去检查状态；Actions成功等同生产成功 |
| Deployments | repo+SHA+environment+部署记录/状态/可信release receipt匹配 | merge、CI、预览环境冒充production，忽略后来rollback |

GitHub Issues接口包含PR，按provider ID+类型去重；一个PR有多个活动事件但成果计数只一次。无可靠PR关联的commit列“未关联提交”，不猜是直接push。PR正文链接先按github.com+冻结repo+允许资源类型验证；不抓任意链接、全文diff、CI日志或二进制附件，也不访问runner本地仓库执行命令。

窗口采用[start,end)，每个来源同时标发生时间和本次观察时间。**结果含“窗口内可验证活动”与“当前观察到的状态”两层。** 首次接入不承诺重建过去任意时刻的完整仓库状态；删除、重写、编辑旧正文或无历史部署记录需标不可重建，不用当前内容补写过去事实。后续快照可以生成观察版本差异，不能制造采集前不存在的审计历史。旧head只能来自持久SourceSnapshot：repo_id、default_branch、principal、grant可用性匹配且在TTL内，另核验当前仓库归属；否则按首次处理unknown。刷新可观察到新head，旧head是比较基线而非永久固定目标；窗口/资料范围不变仍需新observation revision。首次无旧head的非PR合入时间为unknown；compare非祖先/force push必须单列，不能当净新增。没有找到deployment证据标unknown，preview成功也不能推导production不存在。已部署只在配置的production环境、目标发布策略和同SHA平台receipt证明下成立；是否必须main由项目发布策略决定，不添加所有项目一律main可达的假规则。

分页使用Link/cursor与去重集合，动态排序时做有界重叠复扫和候选集合变化检测；不能以一次页数到头推断全局一致快照。两次稳定只增加置信度，不证明历史无删改。coverage分开记录：清单覆盖、分页抓取、具体事件可验证性、历史状态重建能力。全局预算外再给每repo×资料类型和每群保留子预算，先公平完成候选扫描，再为相关timeline/讨论分配剩余额度；bot活动不抢尽人类请求，安全/发布机器人高影响事件仍可入议题。记录各分区cursor/最小已见时间/停止原因，分页未跨到窗口起点且无provider终止证明则partial。已验证事实可显示在“已覆盖部分”，不能形成全量计数或无变化断言；任何required分区partial则meeting_ready=false。某项历史不可重建不必否定已验证的合并事件；但会议目的所需关键源或关键问题仍无证据时只能needs_input。空结果、无权限、超预算和API失败必须区分。

## 飞书与GitHub关联规则

最高可信关联为显式PR/Issue/commit链接，其次是资料owner批准的项目→群→repo映射。每群记录API可证明的历史可见范围；visible_since不可取得时记unknown/无法证明早期覆盖，不能杜撰加入时间。消息创建/编辑/撤回字段仅按实际API返回记录，旧正文不可取得要标明。thread API无日期参数：若grant不允许为过滤进行有限的范围外临时读取，则不展开并标gap；允许时预算内短暂解析、立即丢弃范围外内容，不进入模型/持久快照/日志。祖先背景只有独立获准才标context_only纳入。消息中的编号只有在唯一已映射repo下才解释；否则列歧义。Feishu open_id与GitHub login只能通过批准映射关联，不能按显示名相同推断同一人。

聊天适合解释意图、风险、承诺与业务背景；PR/commit/check/deploy分别证明技术事实。没有一个来源被笼统定为所有问题的权威。冲突标时间、状态及来源，只有解释所需证据充分才给推断。GitHub无变化不能推断团队没工作，聊天“完成”不能推断已部署。源失败时包首部直接列不可用来源。议题优先用户明确待决问题、已证实阻塞、跨源矛盾与发布风险，阈值按项目配置；附选择理由和证据，不按提交量排序、不仅因bot活动多就生成议题。

## 架构与资源预算

复用BriefService、SourceSnapshot、BriefRevision、grant、受控worker和专属outbox；新增M01—M06的contract/reader/fact reducer/meeting renderer。禁止新设会议调度器、第二个审批库或公共控制平面。自然语言仅生成经过验证的请求契约，不把用户/仓库文本变成shell参数或工具指令。

会议预算profile建议初值：最多20个授权repo、全局500个GitHub事实与500条Feishu消息（thread计入）、最多200次provider调用、模型输入1MiB；默认7天，可请求其他窗口但不得越过grant。M01受理前按冻结清单×资料类型核算最少调用数，并结合配置的保守延迟估计做可行性预检；明显超过预算则直接needs_input，估计通过不承诺能够完成全部动态分页。初值取数软截止为总deadline前60秒（即最多120秒取数），保留最多60秒生成；软截止、保留值进入参数版本，模型仍以剩余总时间为上限。若软截止时关键源不全，优先确定性输出已覆盖事实与缺口，模型只在剩余预算内生成部分草稿。单job取数+生成仍受180秒总deadline和现有租约/排空约束，所有重试计入；预算消耗、deadline、已提交页及cursor原子持久化。恢复时不重置额度/时限，先检查grant/快照TTL/接口cursor可用性，失效进入needs_input；过deadline不能借重启续命。已发生但未计得provider账单的请求保守预扣并标unknown，不保证崩溃后零重复计费；输入资源预算与模型成本上限分别记录。不要把500解释成每repo额度。分页/相关讨论/状态复扫均计调用预算；超限逐来源给gap，状态遵循下表；用户可缩范围、拆次准备，不能悄悄截成“完整组织周报”。后续如需更长时限，先更新任务/排空/测试参数版本，本轮不默默改成十分钟。

只读客户端用参数数组、受保护身份、增量字节限制、进程退出回收、429/Retry-After和有界退避；原GhCLI没有完整覆盖这些约束，M02应抽出有界read-only transport，不直接继承不足。同一worker、有限串行读取，不抢占Monitor共用平台配额。新配置OSTAG_BRIEF_MEETINGS_ENABLED由A11注册、M05实现，默认off；M02的只读GitHub凭据在部署配置里独立登记，不能把开发Mac登录搬到生产。


### 统一结果状态

| 条件（按优先级） | BriefJob状态 | meeting_ready | 可以保留什么 |
|---|---|---|---|
| 用户取消/授权失效 | cancelled或needs_input（按既有撤权原因） | false | 脱敏状态；失效源产物不可投递 |
| lease/fence失效 | 旧attempt不得提交；由当前owner恢复 | false | 不接纳迟到结果 |
| 180秒硬deadline到达且未完成 | failed，reason=deadline_exceeded | false | 已持久化事实/诊断，不冒成会议包 |
| 可行性预检失败、required分区partial/失败或关键问题无依据 | needs_input | false | 有限“已覆盖部分”与缺口，不能全量断言 |
| required完整且关键论断通过，只有optional分区partial | draft_ready | true | 草稿带醒目optional gap |
| required完整且论断通过，无gap | draft_ready | true | 正常会议草稿；仍待用户采用 |

预算触顶按受影响required/optional分区映射，不能另造ready=false却声称准备完成的状态；模型格式/语义验证失败按原失败/待补规则处理。低时间预算直接渲染事实和缺口，无须付费调用才能说明失败。

## 会议专项验收

MC01—MC24是新套件，与旧Q套件分开；24情景实例化18开发+6私有保留案例。开发者可见情景规格，保留输入与标签由独立评审者保管。

| ID | 情景及通过要求 |
|---|---|
| MC01 | 双源正常→议程、阅读、决策理由与来源可回查 |
| MC02 | 相对时间/DST/跨午夜→半开窗口一致，无边界重复 |
| MC03 | 窗口前开PR、内合并、后更新→仍含合并事件 |
| MC04 | Issue含PR/重复分页→事件和成果分别去重 |
| MC05 | 超300/超过30天→不以Events宣称完整 |
| MC06 | 乱序页/活动变化/搜索上限→可见gap，不假全量 |
| MC07 | 老作者日期/本周合入→不等同本周编写或push |
| MC08 | force push/默认分支变动→冻结head和不可重建提示 |
| MC09 | 合并成功无部署证据→只写已合并 |
| MC10 | 当前rerun绿灯/不同SHA→不改写窗口内状态 |
| MC11 | Release/prerelease/预览/rollback→分别描述 |
| MC12 | 一边403/429/timeout→空与失败分开，关键源缺失needs_input |
| MC13 | 新repo/改名/转移→冻结清单不扩大权限 |
| MC14 | 不同请求者/撤权/304缓存→无私有名称/链接/正文泄露 |
| MC15 | 同名人/裸#编号→不误匹配或错误归因 |
| MC16 | 聊天已上线但仅merged→冲突展示和待决问题 |
| MC17 | 无代码活动但有业务讨论→不推导无进展/低绩效 |
| MC18 | 代码/评论/聊天注入→无额外工具、凭据、写入或URL访问 |
| MC19 | thread/媒体占位/删除记录→缺口清楚，不伪造转写 |
| MC20 | 刷新/人工更正/旧卡确认→版本差异与更正可追踪 |
| MC21 | 180秒预算/取消/过期fence→无迟到发布、无失控进程 |
| MC22 | 发送响应丢失/未发送状态合并→不重复投递、不误认领别人的消息 |
| MC23 | 会议flag off/重启/排空/恢复→旧服务与状态报告可用 |
| MC24 | 例行模板/分享/行动按钮→不隐含发群/日历/业务批准 |

M01先冻结情景规格、评分规则与独立保留集保管协议，实际私有案例由operator/reviewer保管，builder不读取；M07只运行/汇总规定的评测，不随模型表现修改标准。M07执行L0—L3，D07执行真实平台stage和生产试点，D07依赖D04和D02。先至少两个repo验证完整链路，再验证冻结组织清单覆盖；只读会议不等待B/C或受管仓库写功能。验收建议沿用3份人工计时基线、至少7天10份不同会议委托；除事实正确性外，owner判断是否找出真正需要讨论的事项，记录不必要议题、人修时间与遗漏。目标≥8/10会议包可用、关键事实有依据或标待确认、准备加修订时间中位数下降≥30%；小样本不外推。

## 开放社区实践的采用

采用GitLab“明确会议目的、先预读、单一议程、可异步贡献、把行动跟踪放回原系统”的方法。OStag根据资料给会议必要性建议，不能自行取消或邀请会议。提前24/72小时是可配置准备偏好，不是硬编码事实或强制通知时间。Rakazo的持续任务和例行委托用于延续会前准备，实际授权和状态仍由OStag已有机制持有。来源与读取限制见[SOURCES](SOURCES.md)。
