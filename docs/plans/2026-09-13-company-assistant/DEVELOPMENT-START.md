# 当前开发入口

当前交付：R1 本地合成会议预览已实现，Codex 编码、实际 Fable 5.1 审查批准；Python 3.11 全量 3410 项通过。见 [卡点复盘与剩余工作](PROJECT-UNBLOCK.md)、[实际会议包](MEETING-PREVIEW-EXAMPLE.md)、[运行说明](../../ostag/meeting-preview.md)。下一步 R2 是单项目真实双源只读试点；不再默认继续拆 A11。真实采集、飞书产品接入、内容验收和发布尚未完成。

## 接下来怎样推进

1. R1 已可运行：在 integration 工作区执行 `.venv/bin/python -m ostag.main meeting preview --input examples/meeting-preview.json --format markdown`；示例缺一个必需分区，正常输出草稿并退出 3。
2. R2 联合推进 M02/M03、A07 的有界真实读取与 M04/M05 的证据组织，用一个明确 repo/chat/window 完成只读资料会议包；仅在真正需要时补授权与预算配置。不能把 bundle 自带 grant 当真实授权。
3. R3 完成 M06/M07 与所需 A11/D01/D02/D07 的服务接入、恢复、真实平台与发布验收。其余写回、销售和上游适配保持独立，不阻塞第一次会议试点。

每轮以可运行命令、可读输出和具体行为验收结束；不把没有触发场景的新加固层自动追加为前提。既有安全保护和正式发布门禁保留。进度由 [execution.json](execution.json) 与 [复盘](PROJECT-UNBLOCK.md)记录；backlog 是冻结的原始计划。

## 2026-09-13 初版方案快照（历史，不是当前指令）

> 实施已开始：本文保留原方案快照；当前 A01、M01 已通过本地验证，最新进度见 [执行记录](EXECUTION.md)。

本轮成果是完整方案，用户说完成MD后再开发，因此本轮没有修改产品代码或发布。当前文档worktree为`codex/ostag-upgrade-plan-2026-09-13`，基线b73dc1d9；原分支及旧成果保留。实施先保存本方案版本，再从核实后的main建立integration与每切片工作区。不要对未提交计划执行reset/clean，也不要把本backlog直接入osstack队列。

建议开工顺序：

1. P00核对新main、旧分支、Python3.11 locked基线和上游固定SHA；不重做已存在的治理能力。
2. A01/M01固定简报与会议范围、来源、权限、时间与映射契约；先合成材料。
3. A02+A03→A04做手工材料纵向链路，U01→A05/A06接可信投递与纠错；A10/A11保证撤权、运行期暂停和故障隔离。
4. A07/A08/A09完成历史与表快照；M02/M03采GitHub时段事实，M04/M05产出双源会议包；M06/M07验证刷新与真实价值。
5. D01—D04按依赖分别完成A1/A2验证，再D07会议profile试点。真实接入时只补当前切片需要的群、repo、身份和预算，不要求先决定全公司所有配置。
6. U02与B01—B04实现受控行动/例行会议；U03/U04与D08交付其余上游适配，C保持后续销售验证线。

每次固定任务AC与候选tree，Codex实现→目标测试→Fable真实独立评审→修复与回归→integration PR。配置/store/renderer同一时段单writer。缺真平台证据记not_run；禁止以模型回复“完成”替代测试/平台结果。

第一项用户可验收结果：给一组明确日期的合成飞书材料和GitHub事实，OStag生成一份有链接、区分合并与部署、列出真实待决问题的会议包；先在本地/测试账号完成，再接入生产资料。
