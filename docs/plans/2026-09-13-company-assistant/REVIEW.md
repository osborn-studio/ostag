# Claude Fable 5.1挑战、回应与最终判断

本轮使用安装的Claude Code CLI显式调用`claude-fable-5-1`，没有fallback；工具、MCP、hooks和浏览器关闭，真实模型身份以返回的canonicalModel核验。CLI返回还列有Haiku辅助用量，单独记录，不把它当Fable正文。咨询不等于代码审查或上线批准：Fable读取提供的方案和源码事实，没有自己运行仓库或服务器。

## 第一轮：一致性、权限与价值验证

原文：[第一轮](reviews/fable-round1.md)。Codex没有全盘接受模型意见：

| 挑战 | 核查与处理 |
|---|---|
| brief与receipt跨库存储可能部分成功 | 接受一致性问题；纠正“receipt是SQLite”的事实错误，当前是JSONL RunLedger。A不创建业务receipt；B用核心receipt引用与关联ID对账 |
| 模型比lease活得久，旧结果可能覆盖 | 纳入30秒lease、10秒续租、独立心跳、每次提交/发送fence检查 |
| 自动部署打断任务 | 纳入有界排空、主机互斥、持久卷和备份演练，第二轮进一步闭合 |
| bot能读并不等于请求者可读 | A2逐资源验证实际用户身份和ACL，不能只有群成员检查 |
| 生成期间撤权、多源部分失效 | 整版失效、停止后续IO；已送provider的prompt不可技术撤回。拒绝未经资料owner决定就改成小时级保留 |
| 数字正确和引用存在被混为语义正确 | 统计槽位由确定性代码生成，人工核对关键断言。拒绝“正文所有数字/姓名出现过即正确”的机械规则 |
| 事故才临时造恢复镜像 | 改为提前演练当前digest只读维护模式。拒绝“顺序备份多库就是原子截面” |
| 无隐藏列如何保证append唯一 | 不保证通用append恰好一次。真实目标须提供可证明的条件写/唯一关联；否则待人工核对，不新增原表隐藏列 |
| 试点缺人工基线 | 加3份人工计时任务、至少7天/10真实委托和修订时间对照 |

## 第二轮：把口头要求变成门槛

原文：[第二轮](reviews/fable-round2.md)。结论为changes_required，主要阻断与对应修订如下：

| 问题 | 最终候选的承接位置 |
|---|---|
| 忘记排空也可能被Actions自动上线 | D02服务端preflight校验关闭接单、当前boot/generation应答、零lease及零在途；not_ready不切换也不rollback |
| 环境变量无法不重启就停止接单 | A11采用受保护只读bind mount控制文件，按boot隔离、运行期检查；每次重启默认off |
| 回滚旧Compose可能重新打开功能 | D02/R2显式全off override并检查实际容器环境，不盲用旧配置 |
| 新brief schema可能拖垮旧服务 | A02/A11只让brief域degraded；manifest记录可读区间，当前digest只读维护 |
| A2没有自己的stage门槛 | D01仅A1；D04/D05/D06各重跑stage三项并加入自身专项 |
| A12依赖尚无A2却承诺24情景 | A12只A1的16情景，至少4保留变体；D04全24和全部6保留变体 |
| 部署/撤权/读回任务缺依赖 | D02补A02/A11，A09补A10，B02补A08；DAG验证器检查 |
| A2真实user身份没有供给方案 | A07目标容器契约，D04服务端OAuth/存储/刷新/轮换/撤销，不能复制Mac钥匙串或静默bot降级 |
| 模型与预算改变让评测不可比 | 冻结参数版本/config hash；180秒为取数生成含全部重试的总墙钟 |

## 第三轮与最后窄复核

[第三轮完整JSON](reviews/fable-round3.json)给出`approved_for_planning`，同时列出6项待落实实施门槛。不能把verdict截取成“全部完成”。Codex已逐项落实：

1. 全off仍运行独立状态/排空ack报告器，状态目录是主机可读bind mount；无需启动业务DB或worker。
2. A12补A11依赖（包含A10），服务器恢复另由D02验收；D03全off上线后、激活前在合并tree重跑16情景。
3. A09/B02/B04/C02补齐A11接线依赖、config/flag测试候选文件，仍每任务≤5文件；恢复用例合并至同任务测试。
4. D02有明确受信brief-control show/set子命令、主机锁和可信hash同步；不需要任意shell。
5. 删除admission环境变量，避免“true究竟如何处理”的歧义。
6. 开发PR仅进入专用integration分支；main每窗口一个release PR，D02可信设施先行。

并明确24情景为18开发+6私有案例、成功延迟目标120秒且失败另计、成本预算版本、mock凭据与真实OAuth分层。[第四轮JSON](reviews/fable-round4.json)再次给出`approved_for_planning`，确认F1—F6落实，同时给出最后的F7—F10实施要求。Codex已明确写入：Actions密钥只能show/off、操作者独立身份才能on且不可自报身份；status目录故障仅brief-status降级；squash后正常合回main到integration并保留未发布工作；每日成本限额与单任务评测可比性分开。B02/B04合并测试文件不得损失原覆盖。

**结论和范围**：完成四轮真实Fable规划共创，可以据此按任务开工。第四轮只审核第三轮之后的diff；F7—F10最后的文本落实由Codex完成并运行结构检查，未再交Fable第五次复读。不能声称Fable审查了尚不存在的实现，或批准了最后文档每一个字节。四轮模型身份、原始响应摘要和受审文本hash见[元数据](reviews/metadata.json)；最终交付hash另记[证据台账](EVIDENCE.json)。

## 仍须实施证据解决的事项

目标平台权限与真实身份供给、生产容量和可信部署路径、实际模型身份/成本、原表条件写能力均须由相应任务实测。当前用户只要求方案，未用附件中的历史命令启动开发队列、读取真实业务数据或部署。

## 后续增强版

本文件保留此前四轮历史。用户新增会议/GitHub与OpenTag增量要求后，当前方案修订为40任务，新增两轮共创见[会议增强共创](MEETING-REVIEW.md)；不能用旧四轮hash冒称新规格已受审。
