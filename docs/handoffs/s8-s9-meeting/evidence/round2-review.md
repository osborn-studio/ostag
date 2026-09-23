# Opus 5.5 二审

## 结论

两个 P1 均已关闭。

- **ID 泄露答案**:已关闭。`case`/`family`/`material`/`origin` 全部改为 `c001`、`f001`、`m001`、`o001` 这类不透明编号,snapshot 中也没有标签字符串,`test_fixture_snapshot_ids_do_not_disclose_scenario_or_gold` 覆盖了这一点。`origin` 相同不能直接推出答案,因为 c002/c012(duplicate)和 c003/c007(distinct)的 origin 结构相同,而 c013 与 c014 分别是不同 origin 和 null origin 下的 duplicate。
- **公开 holdout 未说明**:已关闭。报告输出 `holdout_sealed=False`,`limitations` 写明 "published reference labels, not a sealed or blinded test",文档也有对应段落并加粗。

另外核对了几项声明:
- 测试数:按参数化展开共 49 个,与你们报告的一致。
- fixture 断言:开发集 relevance FP=2(c004 两条都命中"网关"),holdout duplicate recall=1/3(只命中 c011)。
- 深度边界:根节点深度为 0,16 层接受,17 层拒绝,与文档一致。envelope 实际最大深度只有 5,留有余量。
- 断管处理:CPython 中 `BufferedWriter.close` 即使 flush 失败也会先关闭底层 raw 再抛出异常,所以关闭 stdout 后,解释器退出时不会再次 flush。
- 3.11 兼容性:没有使用 3.12 及以上才有的语法或 API。深层嵌套在 3.11 的 C 扫描器中会触发 `RecursionError`,已被捕获。

## 剩余的具体问题

**P2:两组 fixture 的输入结构相同,但金标相反,且输入中没有区分依据。**

| 对照 | 输入 | 金标 |
|---|---|---|
| c008 vs c013 | 两条材料文本完全相同,origin 不同 | c008 为 `distinct`,c013 为 `duplicate` |
| c006 vs c014 | 两条材料文本完全相同,origin 均为 null | c006 为 `uncertain`,c014 为 `duplicate` |

唯一的差别是文本内容的"具体程度"(如"发布已完成"与"成本核对将在周四结束"),而这个依据既没有写进输入,也没有写进文档。后果是:任何只依据输入做判断的候选方法,在这两组中都至多答对一个;标签实际上依赖作者脑中的场景。其中 c008 被标为 determinate `distinct` 尤其没有依据,因为两份不同文档写"发布已完成",既可能是两次发布,也可能是转发同一通知。

建议二选一:
- 把 c008 改为 `uncertain`;
- 或在材料文本中加入可判定的线索(如通知编号、不同的日期或对象),让金标可以从输入推出。

两种做法都会改变 holdout 或开发集的计数,需要同步更新 `test_cli_replays_fixture…` 中的期望值和 baseline-report 证据文件。

**P3:3.11 环境尚未跑完。** 目前的通过结论只适用于 3.13。那 2 个 `card_control_bridge` 失败在 `0bc3d88f` 上同样复现,可以接受为与本次改动无关,但在证据里应注明这是 3.13 的结果,3.11 结果待补。

除以上两点外,没有发现其他具体缺陷。
