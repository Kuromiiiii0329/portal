# P4 · 融合（analyzer 维度 / Agent 工具 / 报告 / 数据拼装）

> 分支：`feat/p4-fusion`
> 对应方案：`UPGRADE_PLAN.md` §4 L4–L5 / §8 / §9 P4
> 验收命令：
> ```
> python -m portal.virtual.selfcheck        # 32/32（S1–S7 + T1–T10 + U1–U8 + V1–V7）
> python portal/backtest_audit.py --real    # 11/11（P0 无回归）
> ```

---

## 一、这一期做了什么

前四期把内核建在 `portal/virtual/` 里，但它还没接进 portal 的日常使用。
这一期把它接到两个入口：

| 入口 | 对象 | 文件 |
|---|---|---|
| **分析流水线** | 人点「立即运行」 | `analyzers/yuzi.py` + 注册 + 前端维度 |
| **Agent** | LLM 对话/编排 | `virtual/toolbox.py`（4 把工具） |

配套两件支撑：`virtual/dataset.py`（特征 + 收益拼装）与 `virtual/report.py`（自包含 HTML）。

---

## 二、游资 analyzer：一个「不许越界」的维度

`YuziAnalyzer` 四个子模块：

| 子模块 | 内容 | 进评分？ |
|---|---|---|
| `limit_up_status` | 涨停池定位（连板/首封/炸板/封单） | ✅ |
| `buyability` | 封单÷流通 四档可买性 | ✅ |
| `emotion_brake` | 情绪读数（涨停/跌停/炸板率/最高板） | ❌ **禁止** |
| `dragon_tiger` | 龙虎榜净买 | ✅ |

### 2.1 为什么情绪绝对不能进评分

涨停家数、连板高度、炸板率与走势是**同步**的（华泰金工对 21 个情绪指标的统一回测）。
把它加进个股评分，等于用「今天热不热」来决定「买哪只」——那是把刹车当方向盘。

这个约束**写进了代码结构**，不只是一句注释：

```python
def _score_from_stock_level(*, seal_ratio_pct, boards, lhb_net, in_pool) -> int:
    """★ 评分只吃个股级事实（seal_ratio / boards / lhb），情绪不进分"""
```

`analyze()` 里情绪节只写 `section.data`，从不参与 `_score_from_stock_level()`。

### 2.2 自检 V2 是这个约束的回归测试

V2 构造两份**同一个股、不同市场情绪**的池子：

```
炸板 0  →  评分 70
炸板 25 →  评分 70      ← 情绪剧烈变化，评分一动不动
封单/流通 18% → 评分 45  ← 个股级事实变了，评分才变
```

第二条断言同样重要：它证明评分**不是死的**，否则「不变」这个断言毫无意义。

### 2.3 真实数据跑通（2026-10-19）

```
今日涨停 46 只
样本：海洋王(002724) 连板 2  封单/流通 2.497%
  → 维度评分 56 / 信号 watch
  → buyability: auction_decide（需在 9:20—9:25 竞价窗口判断）
情绪：涨停 46｜跌停 22｜炸板 21（炸板率 31.34%）｜最高 3 板｜情绪分 22.0
```

炸板率核对：`21 / (46 + 21) = 0.3134` ✓
（若误用 `涨停 ÷ (涨停+跌停)` 会得 `46/68 = 67.6%`，差 36 个百分点。）

---

## 三、Agent 工具：4 把，且只许做这 4 件事

| 工具 | 作用 |
|---|---|
| `freeze_virtual_plan` | 校验 → 落盘 → 锚定 |
| `settle_virtual_plan` | 机械回放 → fills → 账本 |
| `backtest_virtual_rule` | 单条规则取证据 + 过闸门 |
| `query_virtual_memory` | 只读查询 |

**统一契约：失败也返回 `{"ok": False, "error": ...}`，不抛异常。**
理由：Agent 需要能**叙述失败**（「锚点不符，所以没有结算」），
抛异常只会让它猜，或者干脆不提。

五条禁则同时是文档与常量（`toolbox.FORBIDDEN`），会被一起注册进框架：

```
禁止 1  自己计算任何收益率/胜率/净值 —— 必须调用内核
禁止 2  修改 rules.jsonl 的 status —— 只有 gate.py / decay.py 能改
禁止 3  在结算前解释未结算的结果
禁止 4  用自然语言改写计划里的参数
禁止 5  把同步指标写成预测结论
```

工具优先复用 `lib/src/agent/tools/registry.py` 的 `ToolDefinition`；
拿不到框架时用本文件的等价实现，所以**离线也能跑测试**（V5）。

---

## 四、数据拼装：`dataset.py`

`evolve.py` 只吃一种东西：摊平的特征行 + 已扣费净收益。

```
特征（tagging 的 pre 字段）  +  收益（backtest_core 的 T+1 口径）
```

收益**绝不在这里自己算**，一律调 `backtest_core.forward_return_after()`
（本期新增）。理由和 P0/P2 一样：口径分叉本身就是静默错误的来源。

`build_rows()` 会返回丢弃统计：

```python
stats = {"no_kline": 0, "no_return": 0, "limit_up_open": 0, "kept": 0}
```

不许静默丢样本 —— 丢了多少条、为什么丢，必须能说出来。

---

## 五、报告：自包含 + 发报前审计

`report.py` 两条硬要求：

1. **自包含**：不引任何外部资源（原包踩过 CDN 抖动导致整页图表全灭）。
   `external_refs()` 用正则断言外部引用 == 0，非零直接抛错。
2. **发报前过审计**：`audit.assert_signable()` 不通过 → **不写文件**。

V7 验证的正是「不写」：

```
审计不通过 → ok=False, written=False，磁盘上确实没有这个文件
  被拦的检查：A1 A4 A5 A6 A7
审计通过   → 写盘，写出的文件同样自包含
```

---

## 六、自检 32/32

| 段 | ID | 条数 |
|---|---|---|
| P1 记忆层 | S1–S7 | 7 |
| P2 结算闭环 | T1–T10 | 10 |
| P3 闸门/衰减/审计/演化 | U1–U8 | 8 |
| P4 融合 | V1–V7 | 7 |

V1–V7：

| ID | 覆盖 |
|---|---|
| V1 | 有池子 / 不在池子 / 数据源挂掉 三种情形都不崩，缺数据显式标注 |
| V2 | **情绪不进评分**（评分不随情绪变化）+ 个股事实变了评分才变 |
| V3 | 前瞻收益 T+1 语义、只扣一次成本、开盘涨停标记 excluded、越界返回 None |
| V4 | 数据集拼装：缺K线/缺收益分别计数，行里带 ret_pct 与 m_ 前缀 |
| V5 | 4 把工具、manifest、注册、非法输入返回 ok=False、样本不足说明缺什么 |
| V6 | 报告外部引用为 0、关键区块齐全、HTML 转义 |
| V7 | 审计不通过不写文件；通过后写出的文件仍自包含 |

---

## 七、这一期顺带暴露/修正的问题

| 问题 | 处理 |
|---|---|
| `yuzi.py` 里 `pools` 只在某个分支内导入，`grep` 不到的地方用了 `P` → NameError | 改为方法开头统一导入，失败也降级 |
| `lhb_net` 计算后没传给评分函数（永远传 None） | 改为先算再传 |
| `merger.py` 权重从 40/40/20 改为 35/30/15/20，但**权重本身没回测** | 加 `WEIGHTS_STATUS` 常量，把「这是假设不是结论」写进返回值和报告 |
| 自检里用数值比较函数比日期字符串 | 改为直接字符串比较（是测试的错，不是被测代码的错） |

> `WEIGHTS_STATUS` 这一条值得单说：改权重是**最容易被忽略的隐形势优化**。
> 原包 §9.7.4 的教训是「未经回测的规则不是中性选项，是负期望的默认值」。
> 权重也是规则。这里没能回测（缺少跨维度样本），所以只能标注为假设 ——
> 这是诚实的做法，不是完成的做法。

---

## 八、怎么用

```bash
# 1) 网页端：「立即运行」Tab 勾选「🔥 游资情绪」维度
#    （已注册进 ANALYZER_REGISTRY 与前端 DIM_DEFS，默认选中）

# 2) 生成一份复盘/闸门报告
python -c "
from portal.virtual import report, rules
payload = {'title':'虚拟盘复盘', 'kpis':[...], 'gate':{...}, 'rulers':{...},
           'rules': rules.summary_table()}
print(report.build_and_write(payload))
"

# 3) Agent 工具清单（给不支持 ToolRegistry 的调用方）
python -c "
import json
from portal.virtual import toolbox
print(json.dumps(toolbox.manifest(), ensure_ascii=False, indent=2))
"

# 4) 全量自检
python -m portal.virtual.selfcheck
```

---

## 九、仍然没做的（明确的欠账）

| 事项 | 为什么没做 | 影响 |
|---|---|---|
| 前端「虚拟盘」独立 Tab（账本曲线/规则健康度） | 需要改 `build_standalone.py` 的 `ORDER_WITH_PREFIX` 并重建单文件版，风险与工时都较高 | 目前规则/账本通过 CLI 与报告查看，网页端只多了「游资情绪」维度 |
| 复盘 HTML 接入 `report_html.py`（ECharts 图表） | 本期用的是纯表格自包含版；加图表要引入内联 echarts（1MB）并补 Playwright canvas 校验 | 报告无图，但完全自包含、零外部依赖 |
| 全市场日K 预热 | 需要新增取数任务与限流处理 | `zone_pos_120` 覆盖率仍取决于用户分析过哪些票；缺失时显式 null，不猜 |
| 权重回测 | 需要跨维度样本 | 权重标注为假设 |
| `predictions.jsonl` 写入 | 依赖每日结算跑起来 | 目前只有 plans/fills/ledger |

这些都不影响「内核可信」这一点，属于**覆盖度**而非**正确性**的欠账。

---

*本文件为工程记录，不构成投资建议。*
