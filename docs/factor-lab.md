# 因子实验室操作手册

> 面向"我想让 agent 帮我找因子、并且要能验证它到底有没有用"的完整说明。
> 相关代码都在 `portal/premarket/factors/`。

---

## 一、整体结构

```
portal/premarket/factors/
    base.py            Panel / Factor / 表达式白名单（A 类因子求值）
    sandbox.py         B 类代码因子的语法白名单 + 子进程隔离
    f_*.py             以内置因子（一个文件一个因子）
    playbooks.py       战法 = 因子的布尔组合（阈值都在这里）
    validate.py        统计检验：IC / IR / 覆盖率 / 多空价差
    meeting.py         讨论循环：提议 → 强制回测 → 评审 → 改进/回溯 → 入库
    memory.py          经验库：完整会议记录 + 提炼的可复用经验
    catalog.py         因子库总表：自动生成 docs/factor-library.md
    discovered/        agent 讨论产出、经回测通过后入库的因子
    discovered/_retired/  被淘汰或退化下线的因子（保留证据，不再加载）
```

数据文件（`portal/data/premarket/`）：

| 文件 | 作用 |
|---|---|
| `factors_ledger.jsonl` | 事件流水：每轮决策、入库、淘汰、周检 |
| `factors_memory.jsonl` | 完整会议记录：候选源码 + 逐轮回测数字 + 评审意见 |
| `factor_lessons.jsonl` | 提炼的经验（append-only，带"被拒 N 次"计数） |
| `factor_stats.json` | 每个因子最近一次回测数字（validate/weekly/入库时更新） |
| `factor_catalog.json` | 因子库总表的机器快照 |

报告产物：

| 文件 | 内容 |
|---|---|
| `docs/factor-library.md` | **因子库总表**（自动生成，勿手改） |
| `docs/factors/<id>.md` | **每次入库的讨论档案**（自动生成） |

---

## 二、怎么开始一次 agent 因子探索

### 0. 前置检查

```bash
# 1) 库能不能加载、今天各战法能命中多少
python -m portal.premarket.factors check

# 2) LLM 通不通（.env 里的 DEEPSEEK_API_KEY）
python -c "from portal.premarket.agent import default_llm; \
print('OK' if default_llm() else 'NO KEY')"
```

`check` 会打印因子清单、战法清单（标明哪个是「因子文件贡献」）、条件编译结果、
当日各战法命中数与样例。**它失败就别开始讨论**。

### 1. 先干跑一轮看流程（不烧回测时间）

```bash
python -m portal.premarket.factors discover --dry-run --max-rounds 3
```

dry-run 会真的调 LLM 提议并评审，但**不落盘、不回测**。用来确认提示词与
JSON 解析正常。

### 2. 正式开跑

```bash
# 默认上限 8 轮、回测 10 个交易日
python -m portal.premarket.factors discover

# 想控制成本
python -m portal.premarket.factors discover --max-rounds 3 --days 5
```

轮次上限写在 `meeting.py` 顶部：

```python
MAX_DISCUSS_ROUNDS = 8      # ← 直接改这里
```

### 3. 看结果

```bash
python -m portal.premarket.factors catalog --show   # 刷新并预览因子库总表
python -m portal.premarket.factors lessons          # 经验库（含被拒次数）
python -m portal.premarket.factors memory           # 最近一场会议的完整记录
python -m portal.premarket.factors ledger --tail 20 # 事件流水
```

入库成功的话，`docs/factors/<id>.md` 会出现一份讨论档案。

### 4. 在 GitHub 上跑

`factor-lab.yml`：

- **周六 10:00 自动**：只做周检（复验已入库因子，退化的下线）
- **手动触发**：可选 `weekly` 或 `meeting`；meeting 可设 `max_proposals` 与 `days`

开会默认手动，避免每周固定烧 token。

---

## 三、讨论循环的逻辑

### 一轮做什么

```
提出推演（LLM，自动带上历史经验）
      ↓
【强制】内核回测：沙箱语法审查 → 子进程试跑 → 10 日截面统计
      ↓
回测数字 + 源码 → 评审（LLM）→ verdict = accept / improve / backtrack / give_up
      ↓
决策（decide()，内核）
```

**没有任何一轮可以在没有数字的情况下被评价。** 语法错、跑不出值、覆盖率过低，
都算一轮结果，写进记录，然后进入评审（评审能看到失败原因）。

### 决策优先级（`meeting.decide()`）

| 情况 | 决策 | 理由 |
|---|---|---|
| 内核未达标 + 评审 accept | **improve** | 内核否决优先，评审说了不算 |
| 内核达标 + 评审 accept | accept | 入库 |
| 内核达标 + 评审 improve | accept | 数字优先于意见，意见写入经验 |
| 评审 give_up | give_up | 提前结束 |
| 得分 < 历史最佳 × 0.80 | backtrack | 改坏了，回到最佳变体换方向 |
| 其余 | improve | 继续 |

### 回溯（backtrack）

每轮算一个评分：

```
score = |IC| × min(1, |IR| / 0.30) × 覆盖率
```

循环始终记住**历史最佳变体**。某轮改坏了（低于最佳 80%），下一轮回到最佳变体，
而不是在越改越差的路上继续。

到轮次上限仍未通过 → 如实报告失败，并把落盘文件移到 `_retired/`。

---

## 四、回测逻辑（当前实现）

### 数据口径

- 历史行情：本地 `market.db` 的 `day` 表（约 1600 万行 / 8700+ 交易日）
- 检验窗口：最近 N 个交易日（默认 10，`--days` 可调）
- **无未来函数**：第 d 日只用 `date < d` 的历史构造 Panel；次日收益只用于算 IC

### 每日截面怎么构造

对检验窗口内每一天 d：

1. 取 d 日全部股票的 `open/high/low/close/volume/amount`
2. 每只票的前收盘 = d 之前最近一个交易日的收盘
3. `build_panel(conn, quotes, d, history_days=40)` → 40 日历史 + 当日截面
4. 跑一遍因子 → `{code: 因子值}`
5. 与 d+1 日收益对齐 → 算截面秩相关

### 四个指标

| 指标 | 定义 | 门槛 |
|---|---|---|
| **IC** | 因子值与前向收益的 Spearman 秩相关，逐日算再取均值 | `|IC| ≥ 0.02` |
| **IR** | IC 均值 / IC 标准差（稳定性，不靠某几天） | `|IR| ≥ 0.20` |
| **覆盖率** | 能算出值的股票数 / 全市场 | `≥ 50%` |
| **多空价差** | 因子前 10% 组减后 10% 组的次日平均收益 | 参考，方向要对 |

另外要求有效检验日 ≥ 10 天，否则判"样本不足"——**不因为天数少而放宽标准**。

### 两条不经讨论的硬规则

不管 agent 怎么说、数字多好，内核直接拒：

1. **追高/打板语义**：因子或战法里出现 涨停/连板/封板/打板/追高/追涨 等字样，
   或买入条件出现 `chg_today >= 7~10`
2. **重复**：id 与现有因子冲突、表达式就是一个已有因子名、或只是搬运
   `close/volume` 这类原始字段

### 复现单次回测

```bash
python -m portal.premarket.factors validate --id vol_ratio --days 20
python -m portal.premarket.factors eval            # 看当日取值分布是否合理
```

---

## 五、因子文件的两种写法

### A 类：表达式（安全，只能组合已有因子）

```python
# factors/f_my_combo.py
META = {"id": "my_combo", "name": "量价共振", "kind": "numeric",
        "description": "量比 × 收盘位置", "source": "builtin",
        "direction": "high"}
EXPR = "vol_ratio * close_pos / 100"
```

`evaluate()` 先算代码因子，再算表达式因子，所以表达式可以引用任何已有因子。

### B 类：代码（表达力强，必须过沙箱）

```python
def compute(panel):
    out = {}
    for c in panel.codes():
        closes = panel.closes(c)
        if len(closes) < 20:
            continue
        px = panel.close.get(c)
        if not px:
            continue
        hi = max(panel.highs(c)[-20:])
        out[c] = round((px / hi - 1) * 100, 3)
    return out
```

沙箱允许：算术、比较、`for`、`if`、字典/列表操作、`get/items/keys/values/
append/split/strip/startswith/endswith/join`、Panel 的只读字段与方法。
禁止：`import`、`class`、`while`、`lambda`、`try`、`with`、`global`、
任何以 `_` 开头的属性或名字、`eval/exec/open/getattr/compile`。
运行在子进程，45 秒超时，只拿 600 只样本。

### 顺带加战法

因子文件里可以写 `PLAYS`，注册表会自动合并，**下一份午报/晚报就会用它筛股**：

```python
PLAYS = [{
    "id": "my_setup", "name": "我的买点", "buyable": True,
    "why": "一句话说明为什么这是买点",
    "conditions": ["my_combo >= 0.8", "chg_today >= 1.0",
                   "chg_today < 7.0", "amount_yi >= 2.0"],
    "evidence": ["my_combo", "chg_today", "amount_yi"],
    "sort": [["my_combo", "desc"]],
}]
```

加完跑一次 `python -m portal.premarket.factors check` 确认它进了战法清单。

---

## 六、经验如何自我进化

```
开会 → 记录全过程 → 提炼经验 → 下次提示词自动注入 → 行为改变 → 再记录
```

- **内核规则**（确定性）：每种拒绝原因对应一条可复用教训，被拒几次累加几次。
  例如"因子 id 不能重名（被拒 2 次）"。
- **LLM 归纳**（`source="llm"`）：从整场会议提炼方法论层面的规律。

注入位置：提议者与评审者的提示词开头都会带上

```
【历史经验（来自既往因子会议）】
- ……（被拒 N 次）
```

所以同一个坑不该反复踩。查看：`python -m portal.premarket.factors lessons`。

---

## 七、入库档案（每次成功都留档）

因子通过内核回测后，`catalog.write_admission_record()` 自动生成
`docs/factors/<id>.md`，内容包括：

1. 讨论核心：假设、说明、类型
2. 最终回测结果表（IC / IR / 覆盖率 / 多空价差 / 天数 / 评分）
3. **讨论过程表**：每轮的变体、数字、评审结论、决策
4. 逐轮评审意见（问题 + 下一步）
5. 它支撑的战法与条件
6. 可复现的源码
7. 复核命令

---

## 八、因子库总表（自动维护）

`docs/factor-library.md` 由 `catalog.write_catalog()` 生成，**不要手改**。
每个因子记录：

| 字段 | 来源 |
|---|---|
| 类型 / 来源 / 模块 | 注册表 |
| 提出假设 | 因子文件 `META["hypothesis"]` |
| 参与战法 | 反查所有战法条件里出现的因子名 |
| 最近回测 | `factor_stats.json`，缺则从 ledger 的 `gated` 事件兜底 |
| 评分 | `|IC| × min(1,|IR|/0.3) × 覆盖率` |
| 使用情况 | 扫历史预案的 `market_digest.candidates`：出现次数、覆盖交易日 |
| 适用场景 | 由它支撑的战法说明自动汇总 |
| 综合评价 | 由数字自动生成（没回测就写"尚未回测"） |
| 状态 | active / retired（`_retired/` 里的算 retired） |

刷新时机：报告生成候选池后、讨论入库后、周检后、手动 `factors catalog`。

---

## 九、常见问题

**Q：`check` 显示某战法 0 命中？**
A：可能是当日没有满足条件的票（正常），也可能是它的因子在当日数据里缺失。
先看 `factors eval` 里对应因子的覆盖率。

**Q：讨论跑满 8 轮都没入库？**
A：看 `factors memory` 的逐轮数字。如果 IC 一直在 0.01 附近，是因子本身没预测力，
换假设比调参数更有效——经验库里应该已经有对应提示了。

**Q：入库后第二天就从战法里消失了？**
A：周检（`factors weekly`）把 IC/IR 掉出门槛的因子移到 `_retired/` 了。
看 `docs/factor-library.md` 里它的"综合评价"。

**Q：`market.db` 没有历史数据？**
A：因子回测依赖本地库。先跑
`python -m portal.virtual.marketdb build`（全量，约 13 分钟）
或从 GitHub Actions 的缓存恢复。

**Q：想彻底重开因子库？**
A：删 `factors/discovered/f_*.py` 与三个数据文件（ledger / memory / lessons），
再跑一次 `factors catalog`。内置因子不受影响。

---

## 十、边界与已知限制

- **回测是截面单因子检验**，不包含交易成本、冲击成本、涨跌停不可成交等约束；
  它是"这个因子有没有信息"的粗筛，不是完整策略回测。
- **多空价差**为参考值，未做行业/市值中性化，小盘股可能主导结果。
- 讨论循环每轮都要跑一次全市场回测，**成本不低**；
  默认 `--days 10`、轮次 8，建议先用 `--days 5 --max-rounds 3` 摸清脾气。
- A 类表达式因子只能组合已有因子，**不能创造新的截面计算**。
- 沙箱挡的是代码层逃逸，不挡"逻辑上偷看未来"（例如用当日收盘算次日涨幅）；
  后者靠"只能用 `date < d` 的历史"这条数据口径保证。
