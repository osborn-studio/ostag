# A10 撤权与保留：本地实现与验证

A10 已完成本地集成：Codex 修复原授权在保留策略数据库等待期间失效的问题，诊断字段和迟到发送失败破坏清理完整性的问题，并修复真实连接等待后的发送授权与卡片ACK凭据轮换问题，独立 Kimi K3 会话批准准确候选 `9ef70ccbbfccb1e4e494846d34381d1b93bcfa24b8e1d90449d555530ddf7943`。macOS 与原生 Linux 全量各 **2819 项通过**，各 11 条既有警告；A10 目标 304 项、21组额外检查、Ruff 和锁文件检查通过。代码提交 `8812e8087f5b40d6ea8b0a7426202b00f5804608`。

[验证证据](reviews/a10-codex-validation.json) · [最终独立审查](reviews/a10-kimi-k3-round-1.json) · [旧候选复现](reviews/a10-authority-old-red.json) · [修复后检查](reviews/a10-authority-final-green.json)。此前 Claude 周额度耗尽没有产生批准；本轮按用户最新要求由实际 Codex CLI（gpt-6-astra）编码、实际 Kimi Code CLI（kimi-code/k3，配置提供方managed:kimi-code、模型k3）独立审计并核查测试证据。Kimi没有亲自执行这些测试；固定验证器执行，Kimi审阅结果。首轮Kimi因Read分页失败给出inconclusive，不计批准；最终将源码拆成带原行号的小文件，验证材料按字符上限分块；Kimi在同一审计会话完成补读。

Linux 为隔离 ARM64 环境（Ubuntu6.8内核、Debian12、Python3.11），未运行需挂载其他仓库的 osstack 跨仓库冒烟，不代表 Ubuntu x86_64 CI 或腾讯生产文件系统验收。完整会议采集、A11服务接线、真实飞书平台验收和生产部署尚未完成。下一步 A05过期预览恢复、A11；保留监控命令发布/ACK 生命周期后续核查。

## 清理后的晚到结果

独立Codex复审发现的成本诊断文本残留和迟到TEXT观察记录重建均纳入修复。验收覆盖旧schema合法诊断字段内的合成私有标记、清理后的新诊断写入、真实暂停发送恢复后的失败以及数值结算。清理完成后继续保留原调用/发送身份、已开始和未知状态，不能退款或重新发送来消除不确定性。验证的是当前受控本地SQLite，不是内存、历史备份或远端内容删除。[旧候选复现](reviews/a10-retirement-old-red.json) · [最终回归](reviews/a10-retirement-final-green.json)。


## 使用和边界

本地显式配置 RetentionLedger / RetentionCoordinator，并把对应 target 的 guard 注入生成、预览、TEXT 投递、卡片、回调和纠错服务。原有来源、grant、请求者、私聊与安装代次检查仍然必须执行。未配置的旧流程维持原行为；A11 负责默认关闭的服务接线。

可信服务先保存任务，再以固定 RetentionPolicy 登记，再派发 worker。生成完成后按同一 policy 再次登记并固定原 revision/outbox，之后才开放预览、投递及纠错。请求方不能提供自己的来源清单、文件路径或权限证明。登记间隙崩溃的未跟踪任务拒绝继续。

到期采用排他边界，at == expires 已失效。多任务共享同一来源快照时采用最早的有效截止时间。source_id、观察时间、grant 别名不能使已经失效的相同来源版本和内容重新入库；实际版本或内容变化可以作为新快照。资源撤权和 grant 撤权永久影响该作用域下对应身份的所有版本。永久记录不自动轮换或释放容量。

## 清理与恢复

先持久化撤权，再通知本进程 worker，失效主存储发布状态，最后依次清理已登记目标。各数据库之间没有原子事务；每一步可在崩溃、失败或提交回执丢失后重试。完成状态必须建立在实际内容和状态核对之上，不能只根据 SQL 无异常判断成功。

| 存储 | 清理内容 | 保留含义 |
| --- | --- | --- |
| BriefStore | 原任务输入、原始草稿/摘录、成本 prompt 配置 | 任务与发布失效状态、必要标识和数字成本 |
| U01 | TEXT intent/receipt 正文及 observations | 原发送身份、已开始或未知发送的事实 |
| A05 card | preview/card/wire/response 正文与活动按钮能力 | 原逻辑发送身份和发送状态，防止再次发送 |
| A05 callback | 私有 binding 和可用能力 | 已退役/使用的永久状态 |
| A06 revision | 根快照及纠错/采用历史正文 | 原事件身份、摘要及不可复活的退役状态 |

清理后的 hash 只是历史标识，不能代表当前仍可复核、内容真实、远端已删除或真人已收到。未知模型调用不能因清理而退款；未知发送不能因删除正文而换 ID 重发。

目标集合和文件身份固定，缺失、替换或省略目标不算清理完成。相同文件重开允许；备份恢复、主机搬迁和目标集合迁移需要专门设计，不能悄悄重新绑定。生产兼容性须结合最终实现核对，尤其是 Ubuntu/Python3.11 的文件身份支持。

当前普通 SQLite 表使用 secure_delete 和受控 VACUUM 去除活动数据库中的逻辑正文与旧空闲页内容。这不覆盖 RAM、历史备份、SSD 快照、外部索引或远端卡片，也不会删除飞书/GitHub 原始资料。SQLite 对 secure_delete 的适用范围及 EXTRA 的目录同步语义见[官方说明](https://www.sqlite.org/pragma.html#pragma_secure_delete)和[同步说明](https://www.sqlite.org/pragma.html#pragma_synchronous)。

## 审查历史

早期候选经过多轮测试和独立审查，先后修复清理完整性、Linux文件身份、授权顺序、晚到结果、真实网络发送和ACK凭据问题。Fable/Opus的额度或超时失败没有计为批准；后续按用户指定由Codex实现、Kimi K3审计。最终候选和完整结果见本文开头；失败记录与模型回执保存在[执行历史](EXECUTION.md)及reviews目录。

## Linux文件身份修复的实际范围

先前实现直接要求Python的birthtime字段，会影响Ubuntu/Python3.11测试。新候选在该字段缺失时，通过Linux libc的statx读取文件创建时间；使用打开的普通文件描述符，并交叉核对设备、inode、返回能力标记和采样后的路径。校验固定ABI布局、时间戳范围，任何不支持或不一致均拒绝，不降级成仅inode或会随写入变化的ctime/mtime。

新增30项测试涵盖成功路径、缺失能力标记、错误返回、ABI和时间字段、采样期间替换、文件描述符关闭，以及正常更新/VACUUM后的重开。macOS上的Linux分支使用模拟ABI；另有未模拟的原生文件身份测试，在Linux运行时会实际调用statx，本轮已经在隔离Linux ARM64环境实际执行通过。生产所用libc、内核和挂载卷必须支持所需能力，否则该功能拒绝启动/使用。[Linux statx布局与能力标记](https://raw.githubusercontent.com/torvalds/linux/master/include/uapi/linux/stat.h) · [文件描述符调用标记](https://raw.githubusercontent.com/torvalds/linux/master/include/uapi/linux/fcntl.h)。

元数据读取和同步SQLite操作仍依赖可信本地存储。忙等待参数不是对底层磁盘/文件系统IO的硬时限，A11需要专用执行上下文、实际负载测量和停机恢复验证。迁移到不同主机或恢复成新文件不会自动继承旧文件身份。

## 集成状态

最终13个文件的内容摘要已与实际Kimi批准回执、macOS/Linux测试、21组独立检查和本地代码提交逐一对应。osstack完成本地集成；本次A10队列归档，原13个历史任务和冻结会议评测保持不变。审计读取与参数/JSON解析故障单独保留，不计为模型批准，也没有通过修改产品代码来消除这些工具故障。发送边界和ACK细节见[A10发送验证](A10-EGRESS.md)。

## 下一步实施顺序

1. A05-refresh：为已经确认创建、后来过期的卡片提供显式授权重新预览。保持原消息绑定与发送记录；测试旧按钮、重复操作、跨用户、撤权和过期边界。测量锁内渲染时间。
2. A11：完成默认关闭开关、可信授权/资料镜像、登记与派发顺序、worker 生命周期、签名按钮和精确版本纠错/采用入口。实现显式维护清理 runner 与失败恢复，不以缓存授权。上线前解决目标 Linux 平台和备份恢复身份策略。
3. A07/A08/A09 与会议能力：采集限定时间和范围内的 Feishu 聊天/线程、GitHub 仓库变化并组合会议背景、议程、待决策问题和来源链接。对截断、分页、授权缺口、冲突证据给出可见提示。资料刷新生成新的授权根快照，不能当成人工注释覆盖旧证据。
4. 验收：运行冻结语义评测，使用授权测试私聊核对实际平台回执、签名按钮、桌面/手机显示、采用/撤回和过期恢复。创建成功、内容核验、真人采用分别记录。
5. 部署：完成 release_check、Ubuntu CI、容器 smoke、备份/恢复和中断清理演练。经既有 GitHub Actions → Tencent Lighthouse 流程发布，检查 healthz、不可变版本回执和核心私聊旅程。回滚必须保留撤权账本与拒绝机制，不能启用不认识撤权状态的旧服务路径。

本轮仅本地合成数据验证；生产清理、真实消息、push、main 合并及部署均未执行。
