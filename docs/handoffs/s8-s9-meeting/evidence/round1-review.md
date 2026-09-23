# Opus 5.5 初审（修改前）

# OT-2 离线相关性与重复判定评测：S9 独立审查

**审查范围：** 我只看了你提供的 4 个文件，没有运行测试，也没有使用任何工具。以下结论都来自静态审查。你说全量测试套件正在装齐 extras 后重跑，结果还没出来，所以本审查不以"全量通过"为前提。

**结论：没有 P0。有 2 个 P1，都和"误导性声明"或"标签泄漏"有关，改动量都很小。** 按离线合成切片的定位，其余问题都是 P2。缺少真实模型试点是已声明的范围限制，我没有把它当作阻塞项。

---

## P1

### P1-1：`--snapshot` 被称为"无标签快照"，但标识符里带着答案
- **位置：** `examples/meeting-relevance-eval.json` 中所有 case 的 `id`、`family` 和 material 的 `id`、`origin`；`input_snapshot()`；文档中"`--snapshot` emits the label-free inputs"一句。
- **问题：** 快照删掉了 `gold`，但保留了 `keyword-collision`、`insufficient-context`、`independent-events`、`paraphrase`、`budget-paraphrase`、`whitespace-copy` 这类描述性命名，`independent-events-origin-0/1` 也在里面。以后把快照交给任何候选方法（尤其是 LLM），它读名字就能猜出 irrelevant、uncertain、distinct、duplicate。这是真实的标签泄漏，和"holdout 公开"是两回事：公开 holdout 已有文档说明；"快照不含标签"则是一个错误的声明。
- **建议（二选一）：**
  - (a) fixture 改用不透明 ID，例如 `c01`/`f01`/`c01-m0`/`o01`。场景说明移到快照之外，比如放进文档，或放在 gold 旁边的 `note` 字段里，由 `input_snapshot` 一并删除。
  - (b) 至少把文档和测试里的说法收窄为"已删除 gold 字段；标识符可能带有描述性"。
- **推荐 (a)。** 再加一个测试：快照序列化后，不应包含任何 gold 标签词，也不应包含场景名单里的任何词。

### P1-2：机器可读的报告没有写明 holdout 标签是公开的
- **位置：** `evaluate()` 返回值中的 `limitations`。
- **问题：** 文档写明了 holdout "not a sealed or blinded evaluation set"，但报告的 5 条 limitations 里没有这一条。可报告里又有一个叫 `holdout` 的指标块。报告才是会被单独转发的产物，读者很容易把它当成盲测结果。
- **建议：**
  - 加一条 limitation，例如 `"Holdout is a split with published reference labels, not a sealed or blinded test."`
  - 可选：再加一个常量字段，如 `"holdout_sealed": false`，并写进测试。

---

## P2

1. **输出编码失败会被误报为输入无效**（`main()`）。
   - `print(_bytes(result).decode("utf-8"))` 在 try 块内，所以非 UTF-8 的 stdout（如 `LANG=*.ISO-8859-1`、部分 Windows 控制台）会抛出 `UnicodeEncodeError`。这个异常被 `except UnicodeError` 接住，结果输出 "Invalid meeting evaluation input" 并退出 2。这违反了文档中"exit 2 仅代表输入无效"的约定。
   - **修复：** 改成 `sys.stdout.buffer.write(_bytes(result) + b"\n")`，并把 try 缩到只包住解析和评测两步。`BrokenPipeError` 目前会打印 traceback，可以顺带处理。

2. **fixture 里 origin 可以走捷径。**
   - 所有 origin 不同的 case（keyword-collision、semantic-miss、independent-events、instruction-text）都是 `distinct`；所有 origin 为 null 的 case 都是 `uncertain`。
   - 于是规则"origin 不同就判 distinct，否则弃权"在 distinct 类上可以拿到 100% 正确，而且完全不需要看文本。
   - **建议：** 文档里写清 `origin` 的语义（是"同一事件来源"还是"同一发布渠道"）。至少补两类 case：origin 不同但 gold 为 duplicate（例如同一通知在两个渠道转发）；origin 为 null 但 gold 为确定标签。

3. **隔离测试没有验证"不导入生产代码"。**
   - `test_offline_evaluation_has_no_network...` 只 patch 了 `socket` 和 `Popen`。
   - 另一个风险是：`python -m ostag.briefs.meeting_relevance_eval` 会先执行 `ostag/__init__.py` 和 `ostag/briefs/__init__.py`。如果这两个包的 `__init__` 会加载配置或日志，文档里"no settings load"的说法就不成立。这两个文件不在我的审查范围内，请核实。
   - **建议：**
     - 用 AST 检查本模块的 import 白名单（`__future__`、`collections`、`hashlib`、`json`、`sys`）。
     - 在新解释器里导入本模块后断言 `sys.modules` 中没有其他 `ostag.*` 模块。
   - 如果包的 `__init__` 确实有副作用，这一项升为 P1。

4. **`network_calls: 0` 是写死的常量，不是测出来的。** 建议改名（如 `network_client_present: false`），或者在文档中注明这是"构造上保证"，避免被读成观测结果。

5. **递归测试没有真正测到递归路径。**
   - `b'[' * 2000` 本身就是不合法的 JSON。在不同 Python 版本上，它可能因 `JSONDecodeError` 而不是 `RecursionError` 被拒，测试照样会通过。
   - **建议：** 改用合法的深层嵌套 `b"[" * N + b"]" * N`。

6. **缺少边界值的"接受"测试。** 恰好 4096 字节的文本应被接受（配一个 4097 字节被拒的用例），100 个 case 接受、101 个拒绝，16 个 term 接受、17 个拒绝。现有测试只测了明显超限的情况。

7. **缺少指标恒等式测试。** 建议断言：
   - `total == known_reference + reference_uncertain`
   - `known_reference == scored + (已知参考上的 abstain 数) + (已知参考上的 not_run 数)`

   这样以后改分母时能兜住回归。

8. **`test_cli_rejects_bad_input_without_echoing_private_text` 没有传 `cwd=root`。** 目前依赖可编辑安装才能导入模块，建议和前一个 CLI 测试保持一致。

9. **归一化只做了 casefold 和空白折叠，没有做 NFKC。** 全角"ＡＰＩ"与"API"匹配不上，只会导致弃权，不会导致误判，所以只需在文档中注明。

10. **文档预先写了"S9 records … the disposition of its findings"。** 合并前请确认处置记录已经真的写上。

---

## 已声明的范围限制，不是 bug

- holdout 公开且未封存；gold 是作者编写的合成期望，不是独立人工标注；`provenance` 和 `method` 都只是声明。
- 快照里的 `input_sha256` 不包含 gold。这是正确的设计：修改标签不会让已有预测失效。但也意味着看到候选结果后仍可以改 gold，报告只会记录新的 `suite_sha256`。将来做真实试点时，应在生成候选之前预先公开 `suite_sha256`。这一点文档中"predeclared rubric/split"已经覆盖。
- 候选方法能看到 `split` 字段。
- 泄漏检查只覆盖 family、origin 和归一化后完全相同的文本，查不出改写后的近似重复。
- baseline 只会输出正类或弃权，所以它的 `accuracy_on_decided` 恒等于 precision。建议在文档里提一句，避免被当成两个独立指标来读。

## 已核实正确的部分

- **分母：** recall 的分母包含弃权和缺失的行；coverage 是 scored / 确定性参考数；在不确定参考上作出的判定不进入 precision 和 accuracy；零分母保留为整数对，不会凭空变成 0% 或 100%。
- **候选绑定：** 候选必须带正确的 `input_sha256`；未知 case、重复 case、多余字段、非法标签都会让整个评测失败；缺失的行记为 `not_run`，与 `abstained` 分开统计。
- **严格 JSON 解析：** 重复键、NaN 和 Infinity、`1e999`、单独代理项、超过 1 MiB 的输入都会被拒；错误信息是固定文本，不回显输入内容。
- **文本长度限制按 UTF-8 字节计算：** `"中"*4096` 这个测试能区分字节语义和字符语义。
- **fixture 的数值和测试断言一致：** development 集相关性 FP=2（keyword-collision 的两条材料），TP=7；holdout 集重复判定的 recall 为 1/2（whitespace-copy 命中，budget-paraphrase 弃权）。
- **不改动现有代码：** 按会话开始时的 git status，这次只有新增的未跟踪文件，没有修改现有的 control 或 meeting 模块，符合"不改动现有 collect"的约束。

**建议处置：** 两个 P1 修完即可合入这个离线切片。P2 中第 1 项（stdout 编码）和第 3 项（import 隔离核实）建议在本切片内一起处理，其余可以放到后续切片。

**补充：** 本会话中 claude.ai Google Drive 需要重新授权（在 claude.ai 的连接器设置中完成），GitHub MCP 连接失败。两者都与本次审查无关。
