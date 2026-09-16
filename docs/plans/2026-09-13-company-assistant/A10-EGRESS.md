# A10 发送边界与回执验证

A10 已完成本地集成：Codex 修复原授权在保留策略数据库等待期间失效的问题，诊断字段和迟到发送失败破坏清理完整性的问题，并修复真实连接等待后的发送授权与卡片ACK凭据轮换问题，独立 Kimi K3 会话批准准确候选 `9ef70ccbbfccb1e4e494846d34381d1b93bcfa24b8e1d90449d555530ddf7943`。macOS 与原生 Linux 全量各 **2819 项通过**，各 11 条既有警告；A10 目标 304 项、21组额外检查、Ruff 和锁文件检查通过。代码提交 `8812e8087f5b40d6ea8b0a7426202b00f5804608`。

[验证证据](reviews/a10-codex-validation.json) · [最终独立审查](reviews/a10-kimi-k3-round-1.json) · [旧候选复现](reviews/a10-authority-old-red.json) · [修复后检查](reviews/a10-authority-final-green.json)。此前 Claude 周额度耗尽没有产生批准；本轮按用户最新要求由实际 Codex CLI（gpt-6-astra）编码、实际 Kimi Code CLI（kimi-code/k3，配置提供方managed:kimi-code、模型k3）独立审计并核查测试证据。Kimi没有亲自执行这些测试；固定验证器执行，Kimi审阅结果。首轮Kimi因Read分页失败给出inconclusive，不计批准；最终将源码拆成带原行号的小文件，验证材料按字符上限分块；Kimi在同一审计会话完成补读。

Linux 为隔离 ARM64 环境（Ubuntu6.8内核、Debian12、Python3.11），未运行需挂载其他仓库的 osstack 跨仓库冒烟，不代表 Ubuntu x86_64 CI 或腾讯生产文件系统验收。完整会议采集、A11服务接线、真实飞书平台验收和生产部署尚未完成。下一步 A05过期预览恢复、A11；保留监控命令发布/ACK 生命周期后续核查。

## 修复的两个边界

模型计数和生成的正文包含原始资料，不能仅在调用 HTTPX send 前授权。当前适配器使用一次性 AsyncByteStream，在连接池、DNS、TCP/TLS 及请求头等待之后，正文交给传输写入之前再次检查当前权限、保留期限、租约、取消及原操作截止时间。飞书文本与卡片也覆盖这一正文边界。既有自定义传输的单次授权门契约需要保持；具体实现与兼容性结果以最终候选为准。

卡片 ACK 的当前凭据摘要必须在保留策略数据库等待之后复核，然后检查最早保留期限和操作截止时间。仅绑定 generation 相同不足以证明访问凭据没有轮换。失效 ACK 不得使按钮成为可用能力；已开始发送仍保留 unknown 等保守事实，不能当作未发送重试。

这定义本地正文交付点。底层写入内部仍有调度、背压、TLS和操作系统缓冲，已提交的字节不能原子撤回。远端请求成功、内容当前可验证、人已收到是不同事实；A10不提供远程删除或备份擦除承诺。

## 可重复证据

旧候选51707a的真实本机TCP/TLS试验和SQLite竞争写锁试验分别复现了失效正文发送与轮换凭据后ACK被接受。最终候选 `9ef70ccbbfccb1e4e494846d34381d1b93bcfa24b8e1d90449d555530ddf7943` 在macOS和隔离Linux上重新执行目标用例、全量测试和21组额外检查。目标验收拒绝跳过TCP/TLS测试；子进程沙箱中的跳过只用于编码迭代，不能充当验收。验收请求均使用合成数据和本机服务，没有向OpenAI或飞书业务接口发送真实资料。

- [旧TCP/TLS复现](reviews/a10-real-tls-old-red.json)
- [旧ACK凭据复现](reviews/a10-ack-credentials-old-red.json)
- [最终TCP/TLS检查](reviews/a10-parent-real-tls-egress-green.json)
- [最终ACK检查](reviews/a10-parent-ack-credentials-green.json)
- [同候选测试记录](reviews/a10-codex-validation.json)
- [实际Kimi K3最终审计](reviews/a10-kimi-k3-round-1.json)

## 审计说明

Kimi 的原始结论按原文保留，其中材料总数和额外检查数量有口述误差：实际为44份原始审计材料完整覆盖、21组额外检查通过，均由逐项记录核对。审计批准适用于上述准确代码摘要；Kimi没有亲自执行测试。U01记录已认证的历史内容证据，卡片ACK则激活后续导航能力，两者的当前凭据约束不同，详见最终审计回执。

## 后续开发与发布

下一步是A05过期预览恢复，再进行A11可信运行时接线：显式初始化拥有的数据库、先登记来源和期限再派发、同运行时共享协调器、接入取消/清理维护、绑定现有身份权限和私聊目标镜像。随后完成Feishu CLI与GitHub指定时间窗的资料采集、分页完整性与撤权处理，以及会议评估集。进入真实平台验收时再执行签名回调、凭据轮换、重启恢复、单一权威时间同步与回退告警，以及目标文件系统身份验证。本轮隔离Linux曾因NTP与Lima主机同步冲突每10秒回退约6秒，触发既有ClockError拒绝；暂停专用虚拟机NTP、保留主机同步并确认稳定后才重跑全量。产品未弱化时钟保护，失败记录保留。发布仍须通过既有release_check、CI、受控部署、healthz与回滚验证；本次本地完成不等于已部署。
