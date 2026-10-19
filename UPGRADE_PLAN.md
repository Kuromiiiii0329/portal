# Portal 升级方案 · 吸收「无名游资」包的精华

> 状态：**待审阅，未执行**
> 分析对象：`D:\CODEX PROJECT\无名游资一键部署AI安装包`（8 个子项目 / 约 300 文件 / 140 MB）
> 目标系统：`D:\CODEX PROJECT\portal`（A 股分析配置中心）
> 撰写依据：关键文件**逐行通读**（SKILL.md 465 行 / 避坑手册 246 行 / 净值口径 132 行 /
> 数据源清单 234 行 / 情绪周期研报 530 行 / anchor.py / tagging.py 全文 / settle.py 核心 /
> plan schema 全文）+ **源码级核实**（`inspect.getsource(akshare)` 验证涨停池字段、
> portal 各模块调用链追踪）

---

## 0. 一页结论

### 0.1 我的决定

**既不采用「纯 skill + 自升级 agent」，也不采用「整体移植」。采用第三方案：**

> **Agent 提议 · 内核裁决 · 经验自演化（三明治架构）**
>
> 把不可协商的判定放在**确定性 Python 内核**里（回测/结算/锚点/闸门），
> 把 LLM 限制在**提议、解释、报告**三件事上，
> 把"经验"从散落的 markdown 日记升级为**四个结构化、可查询、可校验的载体**，
> 让系统靠"过闸门"来升级，而不是靠"读文档"来相信。

### 0.2 为什么（一句话）

因为这个包里最值钱的**不是策略，是防止自己骗自己的机制**——而这类机制**不能住在 LLM 的上下文里**。
它的作者用 8 个项目的失败记录反复证明了同一件事：规则一旦允许被"讲述"，就一定会在讲述中被软化。

### 0.3 三条必须提前说清的事实

| # | 事实 | 依据 | 对方案的影响 |
|---|---|---|---|
| 1 | 该包最强策略**扣费后只剩 +0.050%/笔**，复合档扣费后 **−0.018%** | `SKILL.md` §9.7.2 样本外回测 | 不要把"引入赚钱策略"当目标；目标是**引入测量能力** |
| 2 | 零自由度基准（昨日涨停全买）本身是 **−0.113%/笔**、四年全负 | `SKILL.md` §9.3（39,763 笔） | 任何策略必须先跨过 **0.215%** 毛超额门槛，否则不进系统 |
| 3 | 涨停家数/连板高度/炸板率是**同步指标**，无预测力 | `情绪周期理论调研` 引华泰金工 21 指标统一回测 | 情绪指标只能当**刹车（仓位）**，不能当**方向盘（选股）** |

> 这三点决定了方案的"谦虚"基调：**先让系统不再骗自己，再谈提升收益。**

### 0.4 如果只能做一件事

**做 P0（重写 `backtester.py` 口径）+ P1 的 `float_cap` 补丁。**

理由：现有 `backtester.py` 会在**不报错**的情况下给出系统性偏高的收益与偏高的胜率
（无 T+1、无成本、无基准、无中位数）。而 `float_cap` 是两行代码，
补上之后"封单÷流通"这个唯一有效的可买性判据立刻可用，
能一次性消掉回测里最大的一类高估（一字板买不到却被算成买到）。

这两件事加起来不到一天，却能让**你现在看到的每一个数字都变得可信**。
它们的收益高于后面所有阶段的收益之和。

### 0.5 工作量预估

| 期 | 粗估 | 说明 |
|---|---|---|
| P0 | 0.5–1 天 | 主要工作是写对照实验与自检，不是写策略 |
| P1 | 2–3 天 | 三个数据端点 + 特征层 + 规则层 + 锚点 |
| P2 | 2–3 天 | 结算引擎移植 + 三把尺子 + 报告模板 |
| P3 | 2–3 天 | 闸门/演化/衰减/审计，都要配反例测试 |
| P4 | 2 天 | analyzer 维度 + agent 工具 + 前端 Tab |
| **合计** | **9–12 天** | 分散在多个会话完成亦可，每期独立可验收 |

---

## 1. 全量分析：这个包里到底有什么

### 1.1 八个子项目资产盘点

| 子项目 | 实质 | 工程密度 | 营销密度 | 处置 |
|---|---|---|---|---|
| `AI进化初始版` | 虚拟盘自迭代**引擎**（40 脚本 + 39KB 方法论） | ★★★★★ | ★ | **主力吸收** |
| `A股量化回测工具包` | 口径正确的回测框架 + 避坑手册 | ★★★★★ | ★ | **主力吸收（文档与口径）** |
| `部署盯盘打板量化工具` | 免费数据源 + 排队撮合虚拟盘 + 7 维打分 | ★★★★ | ★★ | **部分吸收（撮合模型、评分）** |
| `最强AI` | 同系统复制体，后转"审计"角色 | ★★★ | ★★ | **吸收"审计"思想** |
| `利弗莫尔模拟A股交易` | 关键点趋势流派 + 两日实战 | ★★ | ★★★ | **摘规则内核** |
| `AI模拟巴菲特A股交易` | 价值派 8 军规 + 三触发线 | ★★ | ★★★ | **摘规则内核** |
| `科斯托兰尼A股模拟炒股` | 逆向派蛋形理论 + 计分板 | ★ | ★★★★ | **摘一句：空仓也是操作** |
| `资料库云文件` | 4 份深度研报（情绪周期/打板王/年赛/观点汇总） | ★★★★★（分析） | ★ | **吸收情绪周期机制** |

### 1.2 真正值钱的东西（12 项，按价值排序）

| # | 资产 | 位置 | 为什么值钱 |
|---|---|---|---|
| 1 | **参数锚点链**（外置 SHA256 链） | `anchor.py` 210 行 | 解决"同一人既制定规则又当裁判"；plan 内部哈希是自我认证=没有认证 |
| 2 | **三把零自由度尺子** | `SKILL.md` §7.2.1 | 大盘/基准组合/影子组合，把"选股贡献"与"执行贡献"拆开 |
| 3 | **回测避坑手册**（7 章，全部"不报错只出错数"） | `文档/00_回测避坑手册.md` | 直接可转成代码断言，是准确率的最大杠杆 |
| 4 | **pre/post 两阶段标签引擎** | `tagging.py` 226 行 | 防时序泄漏的特征库；没有它系统永远停在"记账"到不了"学习" |
| 5 | **口径正确的回测内核** | `common.py`（`build_adj`/`bench_between`/`limit_pct`） | 送转复权修复、T+1 三段收益、区间基准——portal 现有 backtester 全缺 |
| 6 | **五种口径折叠的"闸门"** | `SKILL.md` §9.4/§9.7.2/§9.7.4 | n≥30 / 扣费后超额 / 分年同向 / 剔除最大窗口 / BH-FDR |
| 7 | **排队撮合模型** | `quant-stack/paper.py` | "成交=后续累计成交量≥排队量"，比拍概率诚实得多 |
| 8 | **封单额÷流通市值 可买性判据** | `SKILL.md` §3.5 | 回测高估的第一大来源是"一字板买不到" |
| 9 | **情绪周期机制版研报**（同步 vs 领先） | `资料库云文件` | 避免把同步指标当择时信号——最贵的认知坑 |
| 10 | **规则版本表**（改动 + 理由"全部来自回测"） | `ledger_v2.md` §规则版本 | 可审计的演化史 |
| 11 | **已验证的免费数据端点清单** | `SKILL.md` §2 + `05_各平台数据源` | 含 30+ 条实测坑（限流/编码/口径/被封） |
| 12 | **7 维打板评分 + 可买性×收益分层** | `signals.py` / `layer_analysis.py` | 已量化"越容易买到的板次日溢价越低" |

### 1.3 三个"诚实的负结果"——最该继承的部分

这三条比任何正收益结论都值钱，因为它们证明作者**真的在自我否证**：

| 负结果 | 数字 | 教训 |
|---|---|---|
| **复合档「低位×回调」被自己推翻** | 全样本 +1.302% → 剔除 2024-02 单月后 **−0.018%**（41% 样本来自一个月） | 任何多维交叉结论必须做"剔除权重最大窗口"检验 |
| **「长假前建仓 +3.8% 季节性」是假象** | 9 窗口均值 +3.844%，**中位数仅 +1.629%**，被两次特定行情主导 | N<30 必须同时报中位数与逐样本明细；必须拆 β/α |
| **排序规则未经回测 = 悄悄扣钱** | 8 只低位候选等权 +2.102%，我的 3 只 +0.952% → **排序贡献 −1.150pp** | 免责声明不能代替回测；不过闸门就**不做那个取舍** |

### 1.4 冗余与糟粕清单（要删的）

| 冗余 | 体量 | 处置理由 |
|---|---|---|
| `AI进化初始版/scripts` 与 `skill-premarket-3pick-plan/scripts` | 26 vs 29 个近乎重复的 py | 同一批文件的两份拷贝，留一份 |
| 9 份硬编码日期脚本（`gen_review_1009.py`/`build_plan_1008.py`/`filter_1008.py`/`review_1008.py`/…） | ~120 KB | 一次性产物，逻辑并入参数化引擎 |
| `data/market.db.gz` | **125 MB** | portal 已有 `data_cache.py` 增量缓存，无需整包搬 |
| `output/*.html` 9 份 + `output/src/` 9 份 | ~20 MB（每份 1 MB 内联 echarts） | 构建产物，可再生产 |
| `assets/echarts.min.js` + `资料库云文件/chart_deps/echarts.min.js` | 1 MB × 3 份 | 只留 1 份 |
| `资料库云文件/A股量化回测工具包.zip` | 91 KB | 与文件夹完全重复 |
| `最强AI/_room_post_*.txt`（5 份）+ `任务定义/` | ~20 KB | 多人协作过程稿 |
| `data/*.json` 快照（`mkt_*`/`cands_*`/`candidates_*` 等 20+） | ~700 KB | 一次性中间产物 |
| 四个人设项目的 HTML 战报/账本 | ~200 KB | 叙事产物，规则内核另摘 |

**净效果：141 MB → 目标 8–12 MB 的可移植内核 + 文档。**

---

## 2. 关键发现（决定架构的 6 个事实）

这六条是我在分析中**核对源码后**确认的，它们直接决定了方案形状。

### F1. Portal 是「分析系统」，不是「交易系统」

`portal/analyzers/*` 输出 `DimensionResult(score, signal)`，加权 40/40/20 合并成报告。
**全链路没有任何一处记录"我预测了什么、结果如何"。**
没有账本、没有计划冻结、没有结算、没有规则版本。→ **经验无处沉淀。**

### F2. Portal 已有的回测是「坏的」，而且坏在最危险的方式

`portal/backtester.py` 105 行：

| 缺陷 | 位置 | 后果 |
|---|---|---|
| 用 `close[i+d]/close[i]` 计收益 | L59 | 无 T+1 入场，隔夜跳空算进去→系统性高估 |
| 无成本 | 全文 | 单笔虚高约 0.10% |
| 无基准、无超额 | 全文 | 牛市里"赚 20%"其实是跑输 |
| 只有均值与胜率，无中位数/t 值 | L72-75 | 正中避坑手册 §4.1「均值正、中位负」的坑 |
| 无复权修复 | 全文 | 送转跳空污染，避坑手册 §1.1 实测偏差 −31%→−1.7% |
| 只做单只股票 | L26 | 无法验证"选股能力" |
| **不报错** | — | 与手册 §7 结论完全一致："最贵的错误是算错了但没报错" |

**更糟的是它会传染**：`http_handler.py:600-610` 把结果 `save_backtest()` 落盘缓存，
再被 `tasks.py:_extract_active_patterns(results, backtest_result, kline_tail)` 读进最终报告。
→ **一份偏高的胜率会一路流到用户看到的结论里，且全程无一处会报错。**

→ **这是本次升级最高 ROI 的修复点：不是"加功能"，是"修漏水"。**

### F3. Portal 数据层已经很厚，但缺 3 个关键端点

`lib/data_provider/` 已有 akshare / efinance / tushare / baostock / pytdx 多源 + 降级。
`get_limit_up_pool()` 已能返回 `封板资金 / 连板数 / 首次封板时间 / 炸板次数 / 所属行业`。

**缺的三样**（直接卡住可买性判据）——已用 `inspect.getsource(akshare)` 逐一核实：

| 缺什么 | 用在哪 | 核实结论 |
|---|---|---|
| **流通市值 `ltsz`** | 封单÷流通（可买性核心） | ✅ 已在源数据里。`ak.stock_zt_pool_em` 的列名实测为：`序号/代码/名称/最新价/涨跌幅/成交额/**流通市值**/**总市值**/换手率/连板数/首次封板时间/最后封板时间/封板资金/炸板次数/所属行业/涨停统计`——portal 只是**没映射**，补 1 行即可 |
| **炸板池 / 跌停池** | 情绪温度（炸板率 = 炸板÷(涨停+炸板)） | ✅ `ak.stock_zt_pool_zbgc_em` / `stock_zt_pool_dtgc_em` 均存在且同样带流通市值；portal 未接入 |
| **分时数据** | 结算回放（L1 止损/Z1/14:50 价） | ❌ 全库无实现。腾讯 `web.ifzq.gtimg.cn/.../minute/query`，坑已在 SKILL §7.4 写明 |
| **同花顺题材归因** | 判"为什么涨"（东财 `hybk` 会骗人） | ❌ 全库无实现。`zx.10jqka.com.cn` 免费 GET |

> **附带好消息**：SKILL.md 警告过一个坑——"跌停池必须 `sort=fund:asc`，用 `fbt:asc` 会返回空数组"。
> 实测 akshare 三个接口的 sort 参数分别是 `fbt:asc` / `fbt:asc` / **`fund:asc`**——**这个坑 akshare 已经替我们填好了**。
> 结论：**优先用 akshare 封装，不要自己拼原始 URL**，能白捡一批已踩过的坑。

### F4. 该包的"强"来自约束，不来自模型

`settle.py` 594 行里没有一行 AI。它靠三件事让结果可信：
① 锚点校验（改参数就中止）② 日期护栏（行情没生成就不判）③ R0–R4 机械判定（无人工裁量）。
**LLM 在这套系统里只负责写报告。** 这正是它比"AI 选股"可信的原因。

### F5. 经验若存成 markdown 就无法自动升级

`.workbuddy/memory/*.md` 共 12 万字，人读很好，机器读不了。
里面埋着至少 5 条"被推翻的假设"，但**格式不统一、无结构化键、无法查询**，所以下一轮 Agent 只能靠"记住"。
→ 必须换成 **JSONL 规则注册表 + 特征库**，才能实现"过闸门才升级"。

### F6. Portal 的 Agent 层已具备接入条件

`lib/src/agent/` 有 ReAct Orchestrator + 4 子 Agent + 18 工具，`factory.py` 统一注册。
新增工具只是加一个 handler + 注册项。`/chat`、`/analyze` 端点已就绪。
→ **不需要新框架，只需要加 4 个工具和 1 个 analyzer 维度。**

---

## 3. 架构决策：为什么不是 skill，也不是纯 agent

### 3.1 三种方案对比

| 维度 | A. 纯 Skill + 记忆 | B. 纯自升级 Agent | **C. 三明治（选定）** |
|---|---|---|---|
| 功能覆盖 | 只能产出"建议" | 能自动改规则 | 建议 + 结算 + 账本 + 回测 |
| **经验累积** | markdown，不可查询 | 藏在上下文，重启即失 | **JSONL 注册表 + 特征库，可查询可回滚** |
| 实用性 | 立刻可用 | 需要 agent 长驻 | 挂已有 GitHub Action，零新基建 |
| **准确率** | 无回测，无法证伪 | 会自我强化偏差 | **确定性闸门，五条硬指标** |
| 自我完善 | 无（只会越写越长） | **不可审计，会漂移** | **提议/裁决分离，可回滚** |
| 防作弊 | ✗ 结构性缺陷 | ✗ 更严重 | ✓ 锚点链在上下文之外 |
| 维护成本 | 低 | 高 | 中 |

### 3.2 为什么否决 A 和 B（用这个包自己的话）

> **"必须承认这个结构性风险：同一方既制定规则、又执行、又当裁判。唯一的堵法是设几把成分由客观名单决定、自己挑不了的尺子。"** —— `SKILL.md` §7.2.1

- **纯 Skill 方案（A）**：Agent 读着 SKILL.md 做判断，等于"制定规则的人当场解释规则"。锚点机制之所以有效，正因为它**不在 LLM 的上下文里**——它在文件系统的哈希链里。把它搬进 prompt，等于拆掉唯一的锁。
- **纯自升级 Agent（B）**：让 Agent 直接改规则，等于允许"改参数的人顺手重算哈希"——这正是 `anchor.py` 注释里点名的**自我认证=没有认证**。而且 §9.7.4 已经实测过代价：一条没回测的排序规则，悄悄扣掉 1.150pp。

### 3.3 选定方案：三明治五层

```
┌──────────────────────────────────────────────────────────────┐
│  L5 呈现层   前端 Tab「虚拟盘」/ HTML 执行单 / 复盘报告 / 邮件   │
├──────────────────────────────────────────────────────────────┤
│  L4 Agent 层 LLM 只做四件事：提议假设 · 解释结果 · 写报告 · 对话 │
│              （禁止计算数字、禁止修改规则状态）                   │
├──────────────────────────────────────────────────────────────┤
│  L3 裁决层 回测内核 · 闸门五条 · 锚点链 · 三把尺子 · 衰减监控    │  ← 不可协商
├──────────────────────────────────────────────────────────────┤
│  L2 规则层 rules.jsonl（机器可读表达式 + 证据 + 状态机）         │
├──────────────────────────────────────────────────────────────┤
│  L1 特征层 features/YYYYMMDD.json（pre / post 两阶段，防泄漏）   │
├──────────────────────────────────────────────────────────────┤
│  L0 数据层 portal 现有 data_provider + 3 个补齐端点              │
└──────────────────────────────────────────────────────────────┘
```

**核心原则：**
- **L4 可以很聪明，但它的输出永远只是"提议"。**
- **L3 永远不变聪明——它是裁判，不是球员。**
- **L1/L2 是唯一"会成长"的部分，且只在 L3 点头后才成长。**

---

## 4. 五层详设

### L0 · 数据层（补齐 3 个端点）

| 动作 | 文件 | 说明 |
|---|---|---|
| **改（2 行）** | `lib/data_provider/akshare_fetcher.py` | `get_limit_up_pool` 的 dict 里补 `float_cap`（流通市值）与 `total_cap`（总市值）——原始表本来就有这两列（已 `inspect.getsource` 核实） |
| **新增** | `srv/minute_data.py` | 分时：腾讯 `web.ifzq.gtimg.cn/appstock/app/minute/query?code=sh600032`；**必须带日期护栏** |
| **新增** | `srv/ths_reason.py` | 题材归因：`zx.10jqka.com.cn/event/api/getharden/...`（GBK 编码） |

**必须内置的护栏（来自 SKILL §7.4/§9.5 的实测坑）：**

```python
# 1) 分时没有 0925，竞价开盘价必须取 qt.gtimg.cn 的 open 字段
# 2) 1530 盘后数据会污染收盘价 → 优先取 1500
# 3) 分时日期 != plan.trade_date → 立即中止，不写账本（否则静默污染）
# 4) Python round 是银行家舍入 → 涨跌停价必须 Decimal + ROUND_HALF_UP
# 5) 腾讯日K param 必须 6 段：code,day,,,count,adj（逗号不能少）
```

### L1 · 特征层（经验的地基）

移植 `tagging.py` 的两阶段设计，落到 portal：

```
portal/data/virtual/features/YYYYMMDD.json
{
  "date": "20261008", "stage": "pre",
  "market": { zt_n, dt_n, zb_n, zb_rate, max_lbc, lbc_ge2_n,
              idx_chg_pct, emotion_score, emotion_formula },
  "stocks": [{
    "code": "603906", "name": "龙蟠科技",
    "pre":  { lbc, is_first_board, zbc, turnover_pct,
              seal_fund_yi, float_cap_yi, seal_ratio_pct,
              first_seal_time, zone_pos_120, zone_pos_60,
              ma_spread_pct, chg_5d_pct, hot_reason,
              industry_chg_pct, industry_main_net_yi },
    "post": { fill_price, fill_ok, ret_1d, ret_2d, excess_vs_baseline,
              max_drawdown_intraday, sealed_next_day }
  }]
}
```

**铁律（三条，写进代码注释与测试）：**
1. `pre` 只许用 T 日收盘及以前的信息；
2. `post` 只能由次日结算回填，**且回填脚本不许读取 pre 之外的未来字段**；
3. 宁 `null` 不猜——源挂了就标 null，不填假数据。

### L1.5 · 情绪仪表盘（**刹车，不是方向盘**）

这是我从 `情绪周期理论调研` 中提炼的关键设计，也是**防止误用**的核心：

```yaml
# 只用于「仓位档位」，禁止用于「选股打分」——写入代码注释与报告措辞
brake_indicators:
  883900_昨日涨停指数:  { 规则: "下穿MA5→调整开始；接近MA10→冰点临近；连续绿盘后首次翻红→修复", 性质: 同步 }
  883958_昨日连板指数:  { 规则: "高开低走无承接→接力情绪差；低开高走→回暖", 性质: 同步 }
  883918_炸板股指数:    { 规则: "低开快速拉红→修复确认；深水震荡无承接→分歧延续", 性质: 同步 }
  880005_涨跌家数:      { 规则: "结合MA5手动标记冰点", 性质: 同步 }
  连板晋级率:           { 公式: "今日2板数 / 昨日首板数", 性质: 同步 }
  大面股家数:           { 公式: "count(单日亏损>10%)", 性质: 同步 }
```

**用法约束（硬性）：**
- ✅ 可以：`gate_score` 的一票、仓位降档、决定"今天做不做"
- ❌ 禁止：进入个股评分、作为买入信号、写进"明天会涨"的叙事
- 报告措辞模板强制为："当前情绪读数 = X（同步指标），据此**降低**仓位至 Y%"，**不得**写成"情绪冰点，明日反转"。

**理由（必须引用）：** 华泰金工对 21 个情绪指标的统一回测结论——量价类情绪指标与走势**同步**，不具备提前预判能力；唯一有领先性的是**跨期交易数据**（融资融券、股指期货、期权）。而这类数据本题材内拿不到 → 所以情绪只能当刹车。

### L2 · 规则层（规则注册表）

`portal/data/virtual/rules.jsonl`，每行一条规则，**append-only**：

```jsonc
{
  "rule_id": "R-POS-120-LOW",
  "version": 3,
  "created_at": "2026-10-19T21:00:00+08:00",
  "author": "agent:propose | human:override",
  "scope": "candidate_filter",          // candidate_filter | entry | exit | sizing | brake
  "desc_human": "候选须满足 120日区间位 < 0.30",
  "expr": {                             // ★ 机器可读，不用散文
    "all": [{ "field": "zone_pos_120", "op": "<", "value": 0.30 }]
  },
  "evidence": {
    "n": 5093,
    "mean_pct": 0.580,
    "median_pct": -0.212,
    "winrate": 0.531,
    "t_stat": 3.41,
    "by_year": { "2023": 0.41, "2024": 0.98, "2025": 0.32, "2026": 0.19 },
    "same_sign_years": "4/4",
    "drop_top_window_mean_pct": 0.152,
    "after_cost_pct": 0.050,
    "vs_baseline_pp": 0.693,
    "bh_fdr_p": 0.012
  },
  "status": "active",                   // hypothesis|testing|active|decaying|retired
  "status_reason": "通过闸门 G1–G5（2026-10-19）",
  "decay_monitor": { "rolling_n": 30, "threshold_pct": 0.0, "consecutive_fail": 0 },
  "supersedes": "R-POS-120-LOW@2",
  "anchor_sha256": "…"                  // 冻结时由 anchor 写入
}
```

**为什么用 `expr` 而不是散文**：这是 §9.7.3 血的教训——
> "10/8 冻结的 plan 里 `exit_rule` 只写了人类可读的 `layers` 文本，**漏写机器可读开关** → 引擎全走默认值，结果「文本说 Z1 适用、引擎 Z1 关闭」，二者方向相反。"

**裁决原则：文本与代码冲突时，以机器可读 `expr` 为准。**

### L3 · 裁决层（闸门五条 + 锚点 + 三把尺子）

#### 4.3.1 闸门五条（`portal/virtual/gate.py`）

一条新规则要变 `active`，**必须五条全过**。每条都对应一次真实翻车：

| 闸门 | 判据 | 来自哪次翻车 |
|---|---|---|
| **G1 样本量** | `n ≥ 30`；`n < 100` 强制标注"样本极少" | 避坑手册 §4.4：18 个样本做不了统计 |
| **G2 扣费后超额** | `after_cost - baseline > 0`，且 `> 0.215%` 毛门槛 | SKILL §9.3：基准本身 −0.113% |
| **G3 方向同向** | 分年（≥3 年）**同号** | §9.4.2：只在一年为正 = 行情特征 |
| **G4 剔除最大窗口** | 剔除权重最大窗口后仍为正 | §9.7.2：复合档 41% 样本来自一个月，剔除后转负 |
| **G5 多重比较** | BH-FDR 校正后仍显著 | `robustness.py: bh_fdr`：试 50 个挑最好=抽卡 |

**附加强制项（不作为闸门但是报告必填）：**
- 中位数、胜率、t 值**同时报**（避免"均值正、中位负"）
- 已知偏差显式声明（如"回放用最终开盘价，实操用 9:24 虚拟价"）

#### 4.3.2 参数锚点（`portal/virtual/anchor.py`，照搬 `anchor.py`）

210 行，几乎零改动可用。要点：
- `plan_sha256` 写入 **plan 之外**的 `anchors.json`，链式哈希（`prev → chain_sha256`）
- 结算前强制 `verify()`，不一致**直接中止**
- 对照实验必须做：改仓位→抓到、**只加一个空格→抓到**、改 `anchors.json`→断链抓到
- 诚实标注："锚定时刻晚于声明冻结时刻，只能证明此后未改"

#### 4.3.3 三把零自由度尺子（`settle` 内置）

| 尺子 | 成分 | 回答 |
|---|---|---|
| 大盘 | 沪深300 当日 | 有没有跑赢"什么都不做" |
| **基准组合** | T 日**全部**涨停股，次日开盘等权买（开盘涨停剔除） | 选股有超越"无脑买昨日涨停"吗 |
| **影子组合** | 自己选的票，**不做 gate 不做 R 过滤**，全按开盘价买 | 执行规则是加分还是减分 |

分解（口径统一到"投入资金"）：
```
执行贡献 = 实际 − 影子
选股贡献 = 影子 − 基准
```
> 实测价值：某日实际 +6.071%／影子 +8.054%／基准 +8.230% → 执行 −1.983pp、选股 −0.176pp，**两项都是减分**。
> 没有对照组，这个结论永远发现不了。

### L4 · Agent 层（能力清单 + 禁则）

**新增 4 个 agent 工具**（`lib/src/agent/tools/` + `factory.py` 注册）：

| 工具 | 作用 | 关键约束 |
|---|---|---|
| `freeze_plan(date)` | 生成 plan → 锚点冻结 → 回执 | 冻结后同 plan_id 不可再改 |
| `settle(plan_id, mode)` | 机械回放 → fills → 账本 | 只读 plan；锚点失败即中止 |
| `backtest_rule(expr, window)` | 过闸门 G1–G5 | 返回结构化证据，**不返回"建议"** |
| `query_memory(kind, filter)` | 查规则/特征/预测/事故 | 只读 |

**Agent 禁则（写进 skill，也写进代码注释）：**

```
禁止 1  自己计算任何收益率/胜率/净值 —— 必须调用内核
禁止 2  修改 rules.jsonl 的 status 字段 —— 只有 gate.py 能改
禁止 3  在结算前"解释"未结算的结果
禁止 4  用自然语言改写计划里的参数
禁止 5  在报告中把同步指标写成预测结论
禁止 6  提出 hypothesis 时不附带 expr 与可回测窗口
```

### L5 · 呈现层

| 交付物 | 复用 | 新增 |
|---|---|---|
| 盘前执行单 HTML | `report_html.py`（ECharts 已就绪） | 模板：结论卡/可买性总表/被排除项/复盘打分表 |
| 结算复盘 HTML | 同上 | 成交明细 + 分时图（相对买入价%）+ 三把尺子对比图 |
| 前端 Tab「虚拟盘」 | `js/app.js` TABS 机制 | 账本曲线/规则表/规则健康度 |
| 每日邮件 | `send_report.py` | 加一段"今日闸门状态" |

**必须继承的坑：** HTML 交付前用正则确认外部引用为 0（不挂 CDN）；内联 echarts；构建后 Playwright 数 `canvas` 数量并收集 `pageerror`——"生成了文件"≠"图表能画"。

---

## 5. 经验累积机制（4 个结构化载体）

| 载体 | 路径 | 写者 | 用途 |
|---|---|---|---|
| **特征库** | `data/virtual/features/YYYYMMDD.json` | `tagging` | pre/post 标签，一切统计的原料 |
| **预测账本** | `data/virtual/predictions.jsonl` | `settle` | 每条预测 + 结果 + 三把尺子归因 |
| **规则注册表** | `data/virtual/rules.jsonl` | `gate`（状态）/ agent（提议） | 四态生命周期 + 证据 |
| **事故志** | `data/virtual/incidents.md` | 人 + agent | 人类可读，但每条必须带 `rule_id` + 可检验假设 |
| **校准表** | `data/virtual/calibration.json` | `evolve` | 每信号的 Brier / 命中率 / 衰减斜率 |

**关键设计：`rejected.jsonl` 也要记。**
被闸门拒绝的假设必须归档并带拒绝理由，否则 Agent 会反复提同样的坏主意——这正是 §9.4.1 说的"试 50 个条件挑最好的"。

---

## 6. 自我完善机制（`evolve.py`）

### 6.1 四态生命周期

```
hypothesis ──G1-G5全过──> testing ──样本外同向──> active
     │                        │                      │
     └──闸门失败──> rejected   └──失败──> rejected    └──连续N次低于阈值──> decaying
                                                            │
                                                            └──人复审──> retired | active(v+1)
```

**权限边界（不可越）：**

| 动作 | 谁能做 |
|---|---|
| 创建 `hypothesis` | Agent ✓ / 人 ✓ |
| `hypothesis → testing` | **只有 `gate.py`** |
| `testing → active` | **Phase 1：必须人工批准**；Phase 2：`gate.py` 自动 + 事后审计 |
| `active → decaying` | **只有 `decay_monitor.py`**（机械规则） |
| `→ retired` | 人 ✓（须写理由进 incidents） |

### 6.2 演化循环伪代码

```python
def evolve(date):
    feats = load_features(last_n_days=720)      # pre/post 已分离
    rejected = load_rejected_ids()

    # 1) 枚举候选维度（先验驱动，不是穷举）
    dims = ["zone_pos_120", "zone_pos_60", "chg_5d_pct", "lbc",
            "seal_ratio_pct", "ma_spread_pct", "hot_reason",
            "industry_main_net_yi", "zb_rate", "max_lbc"]

    # 2) 单维度分档统计（★ 不做多维交叉 —— §9.7.2 已被推翻过一次）
    for d in dims:
        for bucket in quantile_buckets(d, n=4):
            key = f"{d}:{bucket}"
            if key in rejected: continue
            ev = walk_forward_stats(feats, cond=key)   # 训练窗→测试窗滚动
            ev = apply_cost(ev, round_trip=0.0010)
            verdict = gate(ev)                          # G1–G5
            record(f"hypothesis:{key}", ev, verdict)

    # 3) 只把过闸门的交给 Agent 写成人话
    for h in passed_hypotheses():
        agent_draft_rule(h)          # 生成 desc_human + 建议 scope
        # 注意：Agent 不能定 status

    # 4) 衰减监控（每次运行都做）
    for r in active_rules():
        if rolling_perf(r, n=30) < r.decay_monitor.threshold_pct:
            bump(r.decay_monitor.consecutive_fail)
            if r.decay_monitor.consecutive_fail >= 3:
                set_status(r, "decaying", reason="连续3次低于阈值")

    # 5) 校准更新
    update_calibration(predictions_ledger)
```

### 6.3 为什么这样"自我完善"是安全的

| 风险 | 本设计的解法 |
|---|---|
| 过拟合 | walk-forward + BH-FDR + 剔除最大窗口 |
| 自我强化偏差 | Agent 只能提议；裁决在内核；`rejected` 强制归档 |
| 规则漂移 | 每次 active 都是新 `version` + 新锚点哈希，可精确回滚 |
| 悄悄失效 | `decay_monitor` 机械降级，不靠人发现 |
| 叙事替代证据 | 报告模板强制"数字在前，解释在后"；`expr` 优先于 `desc_human` |

---

## 7. 删什么 · 留什么 · 改什么

### 7.1 删除（不迁移）

```
✗ AI进化初始版/scripts/ 与 skill-premarket-3pick-plan/scripts/ 的重复拷贝（留后者）
✗ 9 个硬编码日期脚本：gen_review_1009.py / build_plan_1008.py / gen_plan_report_1008.py
  / gen_report_1008.py / check_excluded_1009.py / exit_addendum_1009.py / filter_1008.py
  / pre_1008.py / intraday_1009.py / review_1008.py / review_1009.py
✗ data/market.db.gz（125MB）→ 改用 portal data_cache
✗ output/*.html + output/src/*.html（20MB）→ 改为按需再生成
✗ 重复的 echarts.min.js（留 1 份）
✗ 资料库云文件/A股量化回测工具包.zip（与文件夹重复）
✗ _room_post_*.txt / 任务定义/（过程稿）
✗ data/*.json 一次性快照 20+ 个
```

### 7.2 保留并迁移（内核）

```
✓ anchor.py        → portal/virtual/anchor.py        （几乎零改动）
✓ settle.py        → portal/virtual/settle.py        （去硬编码，接 portal 分时）
✓ tagging.py       → portal/virtual/tagging.py       （去 sqlite，接 portal 数据）
✓ common.py        → portal/virtual/backtest/datalayer.py（build_adj/bench_between/limit_pct）
✓ robustness.py    → portal/virtual/backtest/robust.py   （bh_fdr/分年/集中度）
✓ quant-stack/paper.py 排队撮合模型 → portal/virtual/fill_model.py
✓ quant-stack/signals.py 7维评分 → portal/analyzers/yuzi.py 的一个 section
```

### 7.3 保留为文档（按需加载，不进上下文）

```
✓ 00_回测避坑手册.md      → portal/docs/backtest-pitfalls.md   ★ 最高价值
✓ 02_净值口径说明.md      → portal/docs/nav-conventions.md
✓ 05_各平台数据源.md      → portal/docs/data-sources.md
✓ 情绪周期理论调研        → portal/docs/sentiment-regime.md
✓ SKILL.md 拆成两份：
    - virtual/SKILL.md        薄壳 ~150 行：工作流 + 禁则 + 工具契约
    - docs/methodology.md     参考手册：端点/坑/判据（按需检索）
```

### 7.4 四个人设 → 四个信号生成器（不搬 HTML）

| 人设 | 摘出的可计算内核 | 落点 |
|---|---|---|
| 巴菲特 | 低 β + 红利 + 8 军规（ROE/负债/现金流阈值） | `signal_value` |
| 科斯托兰尼 | 逆向：货币+心理计分板；**空仓也是一种操作** | `signal_contrarian`（只产"回避"） |
| 利弗莫尔 | 关键点（120日区间位低位）+ 试仓 + 错了就走 | 已被 Z1/隔日平仓覆盖 |
| 最强AI | **审计**：检查结论自洽性（见 7.5） | `audit.py` |

### 7.5 「审计 Agent」的设计（来自 `最强AI` 的启发）

`audit.py` 在每份报告产出前跑一遍，**机械检查**：

```
A1 每个"买入"结论是否附带：可买性判据 + 被排除项 + 空仓出口？
A2 报告里出现的每个数字，是否都能在 plan/features 里找到来源？
A3 是否有"文本参数"与"机器参数"不一致？
A4 是否把同步指标写成了预测句式？（正则：冰点.*(所以|明日|将)|连板.*(退潮|见顶)）
A5 扣费后是否仍为正？负值是否被标注？
A6 样本量是否标注？n<30 是否写"样本极少"？
A7 是否声明了已知偏差？
```

任意一项不通过 → 报告**拒绝签发**。这是"准确率"最后的防线。

---

## 8. 对 Portal 的具体改造点（文件级）

| 动作 | 文件 | 说明 |
|---|---|---|
| **新增** | `portal/virtual/__init__.py` | 虚拟盘包入口 |
| 新增 | `portal/virtual/anchor.py` | 移植，210 行 |
| 新增 | `portal/virtual/settle.py` | 移植 + 接 portal 分时 |
| 新增 | `portal/virtual/tagging.py` | 移植 + 去 sqlite |
| 新增 | `portal/virtual/gate.py` | **新写**，闸门五条 |
| 新增 | `portal/virtual/evolve.py` | **新写**，演化循环 |
| 新增 | `portal/virtual/audit.py` | **新写**，七条自检 |
| 新增 | `portal/virtual/backtest/datalayer.py` | 移植 common.py |
| 新增 | `portal/virtual/backtest/robust.py` | 移植 robustness.py |
| 新增 | `portal/virtual/fill_model.py` | 移植 paper.py 撮合 |
| 新增 | `portal/virtual/expr.py` | 规则表达式求值器（安全子集，禁 eval） |
| **改** | `portal/backtester.py` | **重写**：T+1 三段收益 + 成本 + 基准 + 中位数 + t 值 + 复权 |
| 新增 | `portal/lib/data_provider/akshare_fetcher.py` | `get_limit_up_pool` 的 dict 里补 `float_cap`（流通市值）与 `total_cap`（总市值）——**2 行** |
| 新增 | `portal/srv/minute_data.py` | 腾讯分时 + 日期护栏 |
| 新增 | `portal/srv/ths_reason.py` | 同花顺题材归因 |
| 新增 | `portal/analyzers/yuzi.py` | 「游资/情绪」维度（含 7 维评分 + 情绪刹车读数） |
| 改 | `portal/analyzers/__init__.py` | 注册 `yuzi` |
| 改 | `portal/analyzers/merger.py` | 权重改为可配置（默认技术35/基本30/产业15/游资20），**且权重本身也要过闸门** |
| 改 | `portal/srv/http_handler.py` | 加 5 个端点：`/virtual/plan` `/virtual/settle` `/virtual/ledger` `/virtual/rules` `/virtual/evolve` |
| 改 | `portal/srv/tasks.py` | 加 3 个后台任务 |
| 改 | `portal/lib/src/agent/tools/` + `factory.py` | 注册 4 个新工具 |
| 改 | `portal/lib/src/storage.py` | 内存 stub → 真实 JSONL 持久化（经验必须落盘） |
| 新增 | `portal/js/tabs/virtual.js` | 前端 Tab |
| 改 | `portal/js/app.js` + `build_standalone.py` | 注册 Tab + ORDER_WITH_PREFIX |
| 新增 | `portal/docs/` | 4 份参考文档 |

> ⚠️ `build_standalone.py` 的静默坏文件风险：新增 JS 必须加进 `ORDER_WITH_PREFIX` 且排在使用者之前，全局名唯一。**构建后必须跑一次语法检查**（`node --check`）。

---

## 9. 落地路线（5 期，每期可独立验收）

| 期 | 内容 | 交付 | 验收标准 |
|---|---|---|---|
| **P0 止血** | 重写 `backtester.py` 口径；加 `check_data.py` 式自检 | 口径正确的回测 | 对照实验相关系数 ≈1.000；成本只在交易点扣；分年数字有 |
| **P1 地基** | L0 三端点 + L1 特征层 + L2 规则层 + anchor | 每日特征 json + 锚点链 | 改空格能被抓；分时日期不符会中止；`expr` 可求值 |
| **P2 闭环** | plan → settle → ledger → 三把尺子 → 报告 | 一条完整虚拟盘流水 | 三把尺子每日必算；账本可回溯到分时；T+1 正确 |
| **P3 演化** | gate + evolve + decay + audit | 规则能自动升级/降级 | 五条闸门各有反例测试；`rejected` 归档；审计能拦住 7 种缺陷 |
| **P4 融合** | `analyzers/yuzi.py` + 4 个 agent 工具 + 前端 Tab + 人设信号 | 一键跑通 | 报告自包含（外部引用=0）；canvas 数 > 0；无 pageerror |

**依赖关系：** P0 独立 → P1 依赖 P0 的口径 → P2 依赖 P1 → P3 依赖 P2 的样本 → P4 可与 P3 并行。

---

## 10. 风险 · 不做什么

### 10.1 明确不做

| 不做 | 理由 |
|---|---|
| ❌ 不做实盘下单 | 该包自己声明"自动交易仅为本地虚拟盘模拟"；真实下单需券商 QMT/XTP |
| ❌ 不做毫秒级抢板 | 免费 L1 行情是 3 秒快照，物理上做不到 |
| ❌ 不做"AI 自动选股然后相信它" | 与 F4 冲突：这套系统的可信度来自"LLM 不管数字" |
| ❌ 不做多维交叉策略挖掘 | §9.7.2 已被自己推翻一次；单维度先跑通 |
| ❌ 不接社交媒体爬虫 | 合规风险高，且 `05_各平台数据源` 自己写了合规红线 |
| ❌ 不把 125MB market.db 搬过来 | portal 增量缓存更合适 |

### 10.2 主要风险与缓解

| 风险 | 概率 | 缓解 |
|---|---|---|
| 用户期望"引入赚钱策略"，实际只有 +0.05%/笔 | 高 | **本文档 §0.3 已前置声明**；目标是测量能力 |
| 数据源限流（腾讯 WAF 501 / 东财 ProxyError） | 高 | 串行 + ≥1s 间隔 + 多域名轮换 + 优雅降级为 null |
| 分时数据缺失导致结算污染账本 | 中 | 日期护栏 + 拒绝写入 |
| Agent 绕过禁则 | 中 | 禁则落进代码（工具 handler 里断言），不只写文档 |
| 样本累积慢（每日 1 条） | 高 | 用历史日K回填特征（`backtest` 可离线跑 4 年） |
| 规则活化后过拟合 | 中 | 闸门 + walk-forward + 渐进授权 |

---

## 11. 验收标准（可量化）

| 维度 | 指标 | 目标 |
|---|---|---|
| **功能** | 每日盘前执行单 + 盘后结算 + 账本 | 一键跑通，HTML 自包含 |
| **经验累积** | 特征库覆盖交易日 / 规则条目数 / 预测条数 | 可回填 ≥ 4 年；每交易日 1 份 |
| **实用性** | 单日全流程耗时 | < 3 分钟（不含报告渲染） |
| **准确率** | ① 对照实验相关系数 ② 成本口径 ③ 无未来函数 | ① ≈1.000 ② 只扣交易点 ③ `shift(1)` 全检 |
| **准确率** | 每次预测可归因为"选股贡献/执行贡献" | 100% 有记录 |
| **自我完善** | 规则四态流转可追溯 | `rules.jsonl` 全历史可回放 |
| **自我完善** | 失效规则被发现时间 | `decay_monitor` ≤ 3 次运行 |
| **防作弊** | 锚点校验对照实验 | 改空格也能抓到 |

---

## 附录 A：本方案直接引用的"血泪教训"索引

| # | 教训 | 出处 | 落点 |
|---|---|---|---|
| 1 | 锚点必须外置，内部哈希是自我认证 | `anchor.py:5-8` | L3.2 |
| 2 | 三把尺子缺一整套系统失去意义 | `SKILL.md:176` | L3.3 |
| 3 | 均值陷阱：N 大 ≠ 证据硬 | `MEMORY.md:7` | G4 |
| 4 | 复合档 41% 样本来自单月，剔除后转负 | `SKILL.md:408` | G4 |
| 5 | 未经回测的排序规则扣掉 1.150pp | `SKILL.md:446-456` | G2 + 禁则6 |
| 6 | 文本参数与机器参数不一致 → 方向相反 | `SKILL.md:417-430` | L2 `expr` |
| 7 | 数据没生成 ≠ 卖不出去（污染账本） | `SKILL.md:434-436` | L0 日期护栏 |
| 8 | 机械止损是负期望（41,655 笔） | `SKILL.md:342` | 出场规则证据 |
| 9 | 想买强势股就必须在竞价买 | `SKILL.md:203` | settle R1 |
| 10 | 分时没有 0925，竞价价要取 open | `SKILL.md:215` | L0 护栏 |
| 11 | Python round 是银行家舍入，漏判涨停 | `SKILL.md:325` | L0 护栏 |
| 12 | 情绪指标是同步指标，不是领先指标 | `情绪周期:44` | L1.5 |
| 13 | 越容易买到的板，次日溢价越低 | `quant-stack/README:81` | 可买性分层 |
| 14 | 成本只在交易点扣（否则差 24%） | `02_净值口径:41` | P0 |
| 15 | 均值正、中位负 = 多数交易在亏 | `避坑手册:143` | G5 附加项 |
| 16 | 最贵的错误是"算错了但没报错" | `避坑手册:243` | `audit.py` |

## 附录 B：分析覆盖度与未覆盖项（诚实声明）

**已完成第一手核对的文件：**

| 文件 | 覆盖 |
|---|---|
| `skill-premarket-3pick-plan/SKILL.md` | 全文 465 行 |
| `A股量化回测工具包/文档/00_回测避坑手册.md` | 全文 246 行 |
| `.../02_净值口径说明.md` | 全文 132 行 |
| `.../03_怎么加一个新策略.md` | 全文 172 行 |
| `.../05_各平台数据源与抓取方法.md` | 全文 234 行 |
| `资料库云文件/情绪周期理论调研` | 全文 530 行 |
| `AI进化初始版/scripts/anchor.py` | 全文 210 行 |
| `AI进化初始版/scripts/tagging.py` | 全文 226 行 |
| `AI进化初始版/scripts/settle.py` | 核心（取数/三把尺子/run_buy 前提打分/主流程结构） |
| `AI进化初始版/data/plan_20261008.json` | 全文 622 行（plan schema 完整提取） |
| `.workbuddy/memory/MEMORY.md` + `ledger_v2.md` | 全文 |
| portal：`README.md` `AGENT.md` `analyzer/base.py` `analyzers/__init__.py` `merger.py` `backtester.py` `srv/_config.py` `srv/http_handler.py`(路由+backtest) `srv/tasks.py`(签名) `data_provider/base.py`(接口层) | 结构级 + 关键函数级 |
| akshare 三个涨停池接口 | `inspect.getsource` 逐字段核实 |

**未逐行通读（结论基于结构/抽样/README，置信度较低）：**

- `settle.py` 的 `settle_exit` / `z1_settle` / `write_ledger` 三个函数的内部实现细节
- `quant-stack` 其余 12 个模块（只读 README 摘要 + `paper.py` 撮合逻辑 + `signals.py`/`layer_analysis.py` 的 docstring）
- 四个人设项目的 memory 与 HTML 正文（只读了 README 与关键结论）
- `report_html.py`(29KB) / `data_cache.py`(15KB) / `srv/tasks.py`(深度分析任务体) 的内部实现
- `lib/src/agent/` 的 orchestrator/factory 实际代码（只依据 `AGENT.md` 的描述）

**这些未覆盖项不影响架构判断，但会影响 P2/P4 的工时估算精度。**
执行到相应阶段时应先补读。

---

*本方案为个人研究性质，不构成投资建议。所有回测数字均来自原包作者自述，未经第三方审计。*
