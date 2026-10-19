# P1 · 记忆层（特征 / 规则 / 锚点 / 数据端点）

> 分支：`feat/p1-memory-layer`
> 对应方案：`UPGRADE_PLAN.md` §4 L0–L2 / §5 / §6.1 / §9 P1
> 验收命令：
> ```
> python -m portal.virtual.selfcheck     # 7/7
> python portal/backtest_audit.py --real # 11/11（P0 无回归）
> ```

---

## 一、这一期解决什么问题

P0 修好了「数字算得对不对」。P1 修的是**「经验存在哪」**。

原来的状态：`.workbuddy/memory/*.md` 有 12 万字，人读很好，机器读不了。
里面埋着至少 5 条「被自己推翻的假设」，但格式不统一、无结构化键、无法查询，
所以下一轮只能靠「记住」。

P1 把它换成四类**结构化、可查询、可校验**的载体：

| 载体 | 文件 | 谁写 | 是否提交 git |
|---|---|---|---|
| 特征库 | `data/virtual/features/YYYYMMDD.json` | tagging | ✗（可再生产） |
| 规则注册表 | `data/virtual/rules.jsonl` | rules | ✓（审计证据） |
| 拒绝归档 | `data/virtual/rejected.jsonl` | rules | ✓ |
| 参数锚点 | `data/virtual/anchors.json` | anchor | ✓ |
| 规则/计划 | `data/virtual/plans/` · `fills/` | P2 | ✓ |

---

## 二、新增文件

```
portal/virtual/
├── __init__.py        包的入口。**故意不 import 子模块** —— paths 在 import 时
│                      读 PORTAL_VIRTUAL_ROOT，包初始化就把 paths 拉进来会让
│                      测试/多账户再也没法改数据根
├── paths.py           数据根与路径约定（决策记录 vs 派生数据）
├── expr.py            规则表达式求值器（白名单语法树，禁 eval）
├── rules.py           规则注册表（四态生命周期 + 角色权限 + 拒绝归档）
├── anchor.py          参数锚点链（外置 sha256 链，防作弊）
├── tagging.py         特征层（pre/post 两阶段，防时序泄漏）
├── pools.py           涨停/炸板/跌停池 + 情绪快照（刹车，不是方向盘）
├── minute_data.py     分时（腾讯）+ 强制日期护栏
├── ths_reason.py      同花顺题材归因（东财 hybk 会骗人）
└── selfcheck.py       S1–S7 共 39 条离线自检
```

---

## 三、每条设计决定背后的教训

### 3.1 规则必须是 `expr`，不能是散文

`rules.jsonl` 里每条规则的 `desc_human` 只是给人看的，**真正被执行的是 `expr`**。

> 依据：`SKILL.md` §9.7.3 的事故 —— 10/8 冻结的 plan 里 `exit_rule` 只写了人类可读的
> `layers` 文本，漏写了机器可读开关 `z1` / `price_basis` / `use_stop`，引擎全走默认值，
> 结果「文本说 Z1 适用、引擎 Z1 关闭」，二者方向相反。

所以 `rules.validate()` 在 `expr` 缺失时直接报错：**只有 `desc_human` 的规则不许入库。**

### 3.2 求值器禁用 eval

`expr.py` 只支持白名单语法树。理由：`rules.jsonl` 里会有 LLM 起草、人工批准的规则，
一个能执行任意代码的求值器等于给了一条提权接口——一条规则就能把整个进程的读写权限交出去。

自检 S1 里用**标记文件**证明这一点：把 `__import__('pathlib').Path(...).write_text(...)`
塞进字段名，然后断言该文件**没有被创建**。

### 3.3 缺失字段不许蒙混

字段缺失时判「不满足」并在 `evaluate_traced()` 里写明 `missing:<字段名>`，
而不是当成 0 或 False。要大声失败就用 `evaluate(..., missing="raise")`。

理由：A 股数据源经常挂。「拿不到数据」和「数据说不行」是两件事，
混在一起就等于用「接口超时」当「策略否决策」。

### 3.4 权限边界写进代码，不是写进文档

| 迁移 | 谁能做 | 为什么 |
|---|---|---|
| 新建规则 | 任何人（agent 也可） | 提案不需要权限，只需要留痕 |
| `hypothesis → testing` | **只有 `gate`** | 这一关是回测闸门，不是人的判断 |
| `testing → active` | **`human` / `gate`** | P1 阶段必须人工批准（渐进授权第一档） |
| `active → decaying` | **只有 `decay_monitor`** | 机械降级，不靠人发现失效 |
| `→ retired` | `human` / `gate` | 终态，不可复活 |

自检 S2 逐条验证：`agent:propose` 推 `testing` 必须被拒；越级 `hypothesis → active` 必须被拒。

### 3.5 被拒绝的假设也要归档

`record_rejection()` 把失败假设写进 `rejected.jsonl` 并**强制带理由**。

理由：不记录拒绝理由，Agent 会一遍又一遍提同样的坏主意——
这正是 `SKILL.md` §9.4 说的「不许试 50 个条件挑最好的」。

### 3.6 锚点链几乎原样移植

`anchor.py` 逐条保留了原包的三条约定：
① 哈希外置（plan 内部哈希是自我认证=没有认证）；
② 结算前必须 verify，不一致**中止**而不是警告；
③ 规则变更不许原地改，必须新 plan_id。

改动只有一处：`verify()` 从「print + bool」改成返回 `(bool, msg)`，
让调用方能把失败原因写进日志/报告，而不是只往 stdout 丢一行。

### 3.7 post 里不存收益率

`tagging.build_post()` 只存次日 OHLC 事实，**不存收益率**。

理由：收益口径只有一个权威实现（`backtest_core`）。在这里再算一遍就出现了第二套公式——
而**口径分叉本身就是静默错误的来源**（P0 刚吃过这个亏）。

---

## 四、跑在真实数据上的结果（2026-10-09）

```
涨停池        69 只
炸板池        14 只
跌停池         8 只
炸板率        16.87%   （= 14 / (69+14)；若误用 ÷(涨停+跌停) 会得 89.6%）
最高连板       9 板
情绪分        84.0     （标注「待校准」，usage=brake_only）

同花顺归因     72 只，与涨停池交集 69/69（100% 覆盖）
封单/流通      69/69 完整
题材归因       69/69 完整
区间位         0/69    （K 线缓存只有 6 只，且都不在当日涨停池 → 全部 null，不猜）
```

**交叉验证**：`新华传媒(600825)` 当日 9 连板，封单/流通 **10.855%** → 判 `unbuyable`。
原包 `ledger_v2.md` 对同一只票的记录是「7板龙头，封单/流通18.39%完全买不到」。
两条独立路径得出同向结论 —— 可买性判据在真实数据上是可用的。

**日期护栏实测生效**：请求 2026-10-09 的分时，腾讯返回的是 2026-10-19（当天），
`fetch_minute()` 直接抛 `StaleMinuteData` 并中止。这正是设计意图——
腾讯只给当天分时，拿旧日期去要只会静默拿到错的那一天。

---

## 五、自检 7/7（39 条断言）

| ID | 覆盖 | 关键断言 |
|---|---|---|
| S1 | 表达式求值器 | 未知算子/缺 value/空 all/超深/超宽 全部被拒；**标记文件未被创建**证明无 eval |
| S2 | 规则注册表 | append-only 版本、越级迁移被拒、agent 不能激活、拒绝归档、种子幂等 |
| S3 | 锚点链 | 改一个空格被抓、改 `anchors.json` 断链、重复冻结幂等、未锚定不放行 |
| S4 | 特征层 | **未来价格放大 5 倍后 pre 特征一字不变**；1.15/4.85 两个半进位样本被修正 |
| S5 | 池子与情绪 | 跌停池列名别名（封单资金/连续跌停）、炸板率口径、四档可买性 |
| S6 | 题材归因 | 四种外层结构、题材拆标签、0 条必须报错、非法日期被拒 |
| S7 | 分时 | 指数前缀映射、`at_time('0925')` 返回 None、1500 优先于 1530、日期护栏 |

---

## 六、过程中纠正的两处「源头错误」

### 6.1 原包举的涨停价例子复现不了

`SKILL.md` §9.5 写：「`round(6.75*1.1, 2)` 得 7.42，交易所给 7.43」。
本机实测 Python 3.14 下 `round(6.75*1.1, 2)` **就是 7.43**（因为 `6.75*1.1` 的浮点结果
略大于 7.425）。

但**银行家舍入这个问题本身是真的**，只是例子选错了。实测有效的边界样本：

| 昨收 | `round(x*1.1, 2)` | Decimal HALF_UP（交易所口径） |
|---|---|---|
| 1.15 | 1.26 | **1.27** |
| 4.85 | 5.33 | **5.34** |

所以保留 `Decimal + ROUND_HALF_UP` 实现，把注释里的例子换成上面两个，并在
`tagging.limit_up_price` 的 docstring 里如实写明「原文那个具体数字复现不了」。

### 6.2 我自己的种子规则编造了证据字段

第一版 `bootstrap_rules()` 里，我给两条种子规则填了 `median_pct: -0.212` 和
`t_stat: 3.41`。**原文根本没给这两个数**——是我为了让记录「看起来完整」编的。

这恰好是本系统最该防的事。已改为：原文没给的字段一律 `None` + `missing_fields_note`
说明「原文未给，故留空」。现在 `rules list` 里 med% 列显示 `None` 而不是一个编造的数字。

保留的每一条证据都能在原包里逐字对上：
`n / mean_pct / winrate / same_sign_years / by_year / drop_top_window_mean_pct / after_cost_pct`。
`vs_baseline_pp` 是由已知数算出来的（0.580 − (−0.113) = 0.693），属算术而非编造。

---

## 七、没做的（留给后续期）

| 事项 | 归到哪期 |
|---|---|
| 结算引擎 / 账本 / 三把尺子 | P2 |
| 闸门 G1–G5（真正判定能否 active） | P3 |
| 演化循环 `evolve.py` | P3 |
| 审计 `audit.py`（7 条自检） | P3 |
| 游资 analyzer 维度 / agent 工具 / 前端 Tab | P4 |
| K 线缓存预热（当前区间位覆盖 0/69） | P2 的取数环节 |

> 已知限制：`zone_pos_120` 依赖 K 线缓存，而缓存目前只覆盖用户手动分析过的票。
> P2 会加「全市场日K 预热」，在那之前位置类规则在真实池子上无法求值 ——
> 系统会如实返回 `missing:zone_pos_120`，而不是当 0 处理。

---

## 八、怎么复核

```bash
python -m portal.virtual.selfcheck            # 7/7，退出码 0
python portal/backtest_audit.py --real        # 11/11，P0 无回归

python -m portal.virtual.rules list           # 规则一览（含证据提示）
python -m portal.virtual.rules bootstrap      # 导入种子（幂等）
python -m portal.virtual.rules flags          # 只看有问题的证据
python -m portal.virtual.anchor list          # 锚点链
```

---

*本文件为工程记录，不构成投资建议。*
