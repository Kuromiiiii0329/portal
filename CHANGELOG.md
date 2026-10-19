# 版本汇总 · CHANGELOG

> 本文件是 portal 的**版本变更总账**。每个大版本一个章节，只增不改（历史记录不许回改）。
> 细则见文末「维护约定」。

| 版本 | 日期 | 主题 | 规模 | 验收 |
|---|---|---|---|---|
| **v1.1** | 2026-10-19 | 从「分析系统」升级为「可自我否证的虚拟盘系统」 | 65 文件 / +17,085 行 | 12/12 + 39/39 |
| v1.0 | 2026-09 | 初版：股票分析配置中心（技术/基本面/产业链 + 多 Agent） | — | — |

---

# v1.1 — 2026-10-19

## 一句话

把 portal 从「拉数据 → 算指标 → 写报告」的**单向流水线**，改造成
**计划 → 机械回放结算 → 账本 → 规则闸门 → 演化**的闭环，
并给它配了一套可复现的回测内核、全市场数据库与 51 条自检断言。

**核心原则：Agent 只提议，确定性内核裁决。**

## 一、为什么要做这一版

原系统的每个数字都「看起来对」，但有三类结构性缺陷：

1. **回测口径错**：收益用 `close[T+d]/close[T]`、不扣成本、无基准、只有均值。
   这类错误**不报错**，只是数字偏高偏低，且方向不定 —— 实测 **10/50 条信号的均值符号被翻转**。
2. **没有记忆**：分析结论从不落地，「我预测了什么、结果如何」无处可查。
   规则改进只能靠人记，无法回测、无法回滚。
3. **无法自我否证**：没有任何机制能证明「这条规则是错的」。
   原素材包里 12 万字工作日志埋着 5 条被自己推翻的假设，但格式不统一、机器读不了。

## 二、新增能力

### 2.1 回测内核（`backtest_core.py` + `backtest_audit.py`）

统一口径，全系统只有这一份收益公式：

```
入场 = T+1 开盘价（信号日收盘确认，次日开盘买入）
出场 = T+1+h 日收盘价（满足 A 股 T+1）
成本 = 往返 0.10%，只在买入/卖出各扣一次
基准 = 同一只票、同一持有期、同口径的无条件收益（零自由度）
回报 = 均值 / 中位数 / 胜率 / t 值 / 分年同向 / 超额
剔除 = 次日开盘涨停（买不进）+ 复权断裂，并报告剔除数
```

`backtest_audit.py` 提供 **C1–C12 十二条对照实验**，退出码非 0 即「这份回测不可信」。

### 2.2 虚拟盘内核（`portal/virtual/`，24 个模块）

| 层 | 模块 | 作用 |
|---|---|---|
| L0 数据 | `minute_data` `ths_reason` `pools` `marketdb` `datafeed` `scan` | 分时（带日期护栏）、题材归因、三个池、回测数据库、统一取数、池重建 |
| L1 特征 | `tagging` `warmup` `dataset` | pre/post 两阶段标签（防时序泄漏）、批量预热、拼装演化输入 |
| L2 规则 | `expr` `rules` | 白名单表达式求值（禁 eval）、四态生命周期注册表 |
| L3 裁决 | `anchor` `plan` `fill_model` `settle` `gate` `decay` `weights` | 锚点链、计划冻结、排队撮合、机械结算、G1–G5 闸门、衰减、权重回测 |
| L4 编排 | `evolve` `audit` `predictions` `toolbox` | 演化提议、报告签发前审计、预测账本、Agent 四把工具 |
| 呈现 | `report` `verify_report` `srv/virtual_api` `js/tabs/virtual` | 自包含 HTML（内联 ECharts）、渲染校验、7 个 API、虚拟盘 Tab |

### 2.3 四个硬机制

1. **参数锚点链**：plan 的 SHA256 写到 plan **之外**成链。
   改一个空格、改 `anchors.json` 都会被抓到并**中止结算**（不是警告）。
   理由：plan 内部的哈希 = 改参数的人顺手重算 = 自我认证 = 没有认证。
2. **三把零自由度尺子**：大盘 / 基准组合（昨日全部涨停股）/ 影子组合（自己选的票不做任何过滤）。
   把「选股贡献」与「执行贡献」拆开，口径统一到**投入资金**。
3. **排队撮合**：`成交条件 = 当前累计成交量 − 挂单时累计 ≥ 排队位置`。
   一字板后面没有新成交量 → 永远轮不到；炸板放量 → 自然成交。不拍概率。
4. **可买性判据**：封单额 ÷ 流通市值（<1% 可买 / 1–3% 竞价判断 / 3–10% 基本买不到 / >10% 完全买不到）。

### 2.4 全市场回测数据库

| 项 | 实测值 |
|---|---|
| 股票 | 5575（主板 3196 / 创业板 1410 / 科创板 618 / 北交所 351） |
| **有日K** | **5224**（主板+创业板+科创板 **100%**） |
| 日K行数 | **16,711,713** |
| 日期范围 | 1990-12-19 ~ 2026-10-19 |
| 复权因子事件 | 66,951 |
| 体积 | 1.86 GB（已 gitignore） |
| 全量构建 | **776.8 秒**（8 进程，108ms/只） |
| **当日增量** | **26 秒**（腾讯批量行情，写 5213 行） |
| 每周全量刷新 | 13 分钟（同时刷新复权因子） |

存储口径：**只存原始价 + 因子事件**，复权价按需重建。
验证：`raw × 因子` 还原新浪自己的 hfq/qfq，最大误差 **0.005 元**（分位舍入）。
除权日连续性：590 个相邻交易日样本，跳空中位 **3.23% → 1.61%**，95 分位 **38.14% → 8.23%**。

**增量有两条路，因为成本差 30 倍**：新浪日K没有区间参数，`update` 必须全量重抓
（13 分钟）；而「补今天一根 bar」用腾讯批量行情只要 26 秒。所以日更走快通道、
每周一次全量兜底（因子只在除权日变化）。

快通道带**盘中护栏**：行情时间 < 15:00 时**拒绝写入** —— 半天快照写进日K会静默污染
回测。回归测试 W2 用注入的盘中行情验证它真的会拒绝。

调度两种方式都做了：
- **Portal 按钮**（虚拟盘 Tab）：`POST /virtual/update` + `GET /virtual/update/status`，
  后台子进程 + 轮询，有进度条与日志；并发启动直接拒绝（W7）。
- **GitHub Action**：`.github/workflows/market-db.yml`，工作日 16:30 走 `update-today`、
  周六 18:00 走全量刷新；数据库放在 Actions 缓存（不进 git），
  检出到 `portal/` 子目录以保持 `from portal.* import` 可用（已用目录联接模拟布局验证 39/39）。

### 2.5 前端「虚拟盘」Tab + 报告图表

- 7 个端点：`/virtual/status|rules|predictions|ledger|warmup|evolve|report`
- **前端零算术** —— 所有数字来自后端，否则就是第二套口径
- 报告内联 ECharts（1010 KB），约 1.04 MB 自包含，零外部引用
- 用 Playwright 真渲染校验：`3 canvas / 0 pageerror / 0 外部引用`

## 三、实测结论（这一版最值钱的部分）

### 3.1 A 股数据源（本机现场实测，不引用文档）

| 接口 | 结果 |
|---|---|
| `ak.stock_zh_a_hist`（东财日K） | ❌ **不可用** `ProxyError` on `push2his` |
| `efinance` 日K | ❌ 同一主机同一错 |
| **`ak.stock_zh_a_daily`（新浪）** | ✅ **352ms/只**，字段最全 |
| `ak.stock_zh_a_daily(hfq-factor)` | ✅ **50ms/只** |
| `tool_trade_date_hist_sina` | ✅ 8797 个交易日 |
| `stock_zt_pool_em` | ✅（`push2ex` 通，`push2his` 不通） |
| `baostock` | ✅ 995ms/只 |

结论：**日K 走新浪 + 因子自算；东财 push2his 系全不可用。**
线程池不可用（V8 解码多线程必崩），必须进程池。

### 3.2 涨停池重建 vs 官方池（10 个交易日）

| 类别 | 数量 | 解释 |
|---|---|---|
| 漏报：北交所 | 4 | 无历史日K |
| **漏报：其他** | **0** | ✅ |
| 多报：ST | 4 | 官方池不收 ST（5% 幅度） |
| 多报：炸板后回封 | 3 | 收盘确实涨停，官方归入炸板池 |
| 多报：其他 | 0 | — |

### 3.3 权重回测（60 个交易日 / 3605 样本）

按综合分选前 20%：

| 技术 | 游资 | 超额pp | t | IC |
|---|---|---|---|---|
| 0.20 | 0.80 | **+1.529** | **5.68** | 0.084 |
| **0.35** | **0.65**（当前） | +0.456 | 1.39 | 0.058 |
| 1.00 | 0.00 | −0.344 | −1.81 | −0.100 |

**结论：技术权重越高越差 —— 但走查验证否定了这个结论。**
训练集选出的最优权重在测试集**没有复现**（+0.597pp，t=−1.61），属过拟合于某一阶段行情。
**所以权重不改，继续标 `hypothesis`。** IC 仅 0.06~0.11，综合分对次日收益解释力本就弱。

### 3.4 种子规则全部过不了闸门

从原素材包导入的 3 条规则（低位 / 回调 / 封单比）**全部卡在 G5**：
原文没给 BH-FDR 校正后 p 值。这不是闸门有问题，是闸门在正确工作。
要活化必须用我们自己的数据重算。

## 四、修掉的缺陷（12 条主要）

| # | 缺陷 | 类型 | 怎么发现的 |
|---|---|---|---|
| 1 | 收益用 `close[T+d]/close[T]`，把隔夜跳空算进去 | 口径 | 手册 §3.1 |
| 2 | `d=1` 等于当日买当日卖，违反 T+1 | 口径 | 手册 §3.2 |
| 3 | 完全不扣成本 | 口径 | 手册 §3.3 |
| 4 | 无基准、无超额、无中位数、无 t 值 | 口径 | 手册 §4.1/4.2 |
| 5 | 依赖 `pct_chg` 字段（该字段在缓存批次边界静默归零 11 行） | 数据 | 实测 |
| 6 | 不剔除「次日开盘即涨停」（买不进却算成交） | 口径 | SKILL §3.5 |
| 7 | **`np.roll` 首行回卷**：末行卷到首行，112→40 的票首行被算成 −64% 假涨跌 | 代码 | 我的两个独立数字对不上（2 行 vs 3 行） |
| 8 | `data_quality` 结果依赖 CSV 行序 | 代码 | 打乱行序后计数变了 |
| 9 | **平仓规则永远静默不触发**：`settle_buy` 不写 `limit_up/limit_down`，Z1 与 L3 永久死掉 | 代码 | 写 T9 时断言字段存在，直接失败 |
| 10 | **闸门缺经济意义下限**：与收益无关的连板数维度以 **+0.034pp** 超额通过 G2 | 逻辑 | U6 植入边缘测试，假信号混进来了 |
| 11 | **银行家舍入复发**：`19.45×1.1=21.395`，Python 给 21.39、交易所 21.40 → 漏判涨停 | 代码 | 重建池与官方池对照，漏报 12 只 |
| 12 | `build_standalone.py` 静默产出坏文件（重名/残留 import/未定义类都不报错） | 工程 | 产物与源码漂移 36 KB 死代码 |

另修：自检污染全局状态（S3 改坏 `anchors.json` 不还原）、`weights.build_panel` 日期格式混用导致面板 0 行、北交所反复重试、`yuzi.py` 条件导入 `NameError`、`lhb_net` 永远传 None、`thin_margin` 措辞自相矛盾。

## 五、纠正的「源头错误」

1. **原素材包举的涨停价例子复现不了**：`SKILL.md` 写 `round(6.75*1.1,2)=7.42`，
   本机 Python 3.14 算出来就是 7.43。银行家舍入问题**是真的**，但例子选错了。
   已换成实测有效的 1.15→1.26/1.27、4.85→5.33/5.34，并在 docstring 写明复现不了。
2. **我自己编造过证据**：第一版种子规则填了 `median_pct=-0.212` 与 `t_stat=3.41`，
   原文根本没给这两个数。已改为 `null` + `missing_fields_note`，只保留能逐字对上的字段。
3. **东财历史涨停池本身不稳定**：同一日期两次调用，家数从 36 变 34。已在 `scan.coverage_check` 里记明。

## 六、验收与自检

```bash
python portal/backtest_audit.py --real            # → 12/12
python -m portal.virtual.selfcheck                # → 39/39
python portal/build_standalone.py                 # → 产物校验通过
python -m portal.virtual.verify_report --selftest # → 探针有效
python -m portal.virtual.warmup --check           # → missed_other=0, extra_other=0
```

**51 条**断言分布：C1–C12（回测内核 12）+ S1–S7（记忆层 7）+ T1–T10（结算 10）
+ U1–U8（闸门/衰减/审计/演化 8）+ V1–V7（融合 7）+ W1–W7（数据层 7，见 2.4）。

其中 C8/C9 需要 `--real` 才计入（不带参数是 10 条），所以
`backtest_audit` 报 12/12、`virtual.selfcheck` 报 39/39，合计 **51** 条。

**每条断言都必须能失败**：塞已知错误值进去确认它报错。已单独验证：
- C6 探针有效性、V7 审计拦截、W2 盘中护栏真的拒绝写入、
  `build_standalone --self-test`、`verify_report --selftest`

## 七、兼容性与破坏性变更

| 项 | 影响 |
|---|---|
| `run_backtest(df, forward_days, code=None)` | 签名兼容，新增可选 `code`；两个调用方已同步传参 |
| 返回结构 | 只增不改（旧 `signals/total_days/stock_days/stats{win_rate,avg_return,valid}` 全保留） |
| **回测数字会变** | `caliber_version` 2→3。旧缓存由 `is_current_caliber()` 判定过期并重算 |
| `DIMENSION_WEIGHTS` | 40/40/20 → 35/30/15/20，并新增 `WEIGHTS_STATUS` 标注为未回测假设 |
| `index-standalone.html` | 重新构建，体积 174 KB → 143 KB（清掉 36 KB 死代码） |
| **数据目录** | `data/market.db`、`data/virtual/features/`、`data/virtual/reports/` 已 gitignore |

## 八、已知限制（必须一起读）

1. **北交所 351 只无历史日K**：新浪不支持 `bj` 前缀，腾讯只回 0~1 行。免费源无解。
2. **权重只有技术 vs 游资被回测过**：基本面/产业链需要历史财报与板块归属快照，数据库没有。
3. **历史日的封单额/流通市值不可重建**：可买性判据只能用于当日实时。
4. **增量做了两条通道，但都还没在真实环境跑满一周**：Portal 按钮与 GitHub Action 的
   代码路径已验证（W2/W7 + 模拟目录布局 39/39），**调度稳定性要看接下来一周的实际运行**。
   数据库放 Actions 缓存（1.9 GB/份、上限 10 GB）长期需裁剪版。
5. **`predictions.jsonl` 尚空**：写入已接在 `settle` 买卖两端并测过，需第一轮真实
   `plan → settle → exit` 才有数据。
6. **报告图表约 1 MB/份**：内联 echarts 的代价，换来零外部引用、离线可看。

## 九、变更清单

`git diff --shortstat d16e0b0..HEAD` → **65 文件 / +17,085 / −1,012**
（新增 **52** 个，修改 **13** 个）。

```
portal/virtual/            32 个 .py（24 核心模块 + 5 套自检 + _check + __init__ + verify_report）
                           anchor audit datafeed dataset decay evolve expr fill_model gate
                           marketdb minute_data paths plan pools predictions report rules
                           scan settle tagging ths_reason toolbox warmup weights
                           selfcheck selfcheck_p2 selfcheck_p3 selfcheck_p4 selfcheck_p5
portal/virtual/assets/     echarts.min.js（1010 KB，报告内联用）
portal/backtest_core.py    754 行  统一收益口径
portal/backtest_audit.py   579 行  C1–C12 对照实验
portal/analyzers/yuzi.py   249 行  游资情绪维度
portal/srv/virtual_api.py  465 行  9 个端点（含 update / update/status）
portal/js/tabs/virtual.js  426 行  虚拟盘 Tab
portal/.github/workflows/market-db.yml  207 行  每日增量 + 每周全量
portal/requirements-marketdb.txt         20 行  建库最小依赖
portal/docs/               6 份工程记录（p0–p4 + data-and-ui）
portal/UPGRADE_PLAN.md     727 行  升级方案（第一性文档）
portal/CHANGELOG.md        本文件
portal/data/virtual/rules.jsonl   种子规则（3 条 hypothesis）
portal/.gitattributes      LF 规范化（避免 Windows 换行把 diff 打满）
```

**13 个修改文件**：`backtester.py`（重写为薄封装）、`analyzers/{__init__,merger}.py`、
`build_standalone.py`（+7 项校验与 `--self-test`）、`index-standalone.html`（重建）、
`js/{app.js,tabs/run.js}`、`lib/data_provider/akshare_fetcher.py`（补 `float_cap`/`seal_ratio_pct`）、
`srv/{http_handler,prompts,tasks}.py`、`README.md`、`.gitignore`。

## 十、分支与提交（全部已推送 GitHub）

| 分支 | commit | 内容 |
|---|---|---|
| `docs/upgrade-plan` | `8fac4d2` | 升级方案 |
| `feat/p0-backtest-caliber` | `75a38f3` | 回测口径修正 |
| `feat/p1-memory-layer` | `fc4817c` | 特征层/规则层/锚点/数据端点 |
| `feat/p2-settlement-loop` | `6b467f3` | 计划/结算/账本/三把尺子 |
| `feat/p3-gate-evolve` | `fcd55e1` | 闸门/演化/衰减/审计 |
| `feat/p4-fusion` | `793e681` | analyzer 维度/Agent 工具/报告 |
| `feat/data-marketdb` | `a67ef2d` | 数据库/池重建/预测账本/权重回测 |
| `feat/ui-virtual-tab` | `9176152` | 虚拟盘 Tab + build 加固 |
| `feat/report-charts` | `19d7ade` | ECharts 内联 + 渲染校验 |
| `fix/data-rounding-panel` | `fc8bbfd` | 交易所舍入 + 池分类 + 面板修复 |
| `docs/v1.1-changelog` | `c79a173` | 本文件 + README 更新；同一提交也含「当日增量两条通道」的实现 |
| `main` | （合并后更新） | 全部合并 |

## 十一、下一步（v1.2 候选）

- 跑通第一轮真实 `plan → settle → exit`，让 `predictions.jsonl` 与账本有数据
- 基本面历史快照（用 tushare 财报接口建表），解锁基本面权重回测
- 北交所数据源（付费源或东财恢复后）
- 权重「分阶段」检验：按市场情绪分档看权重是否应该随 regime 变化
- Actions 缓存的长期策略：1.9 GB/份、上限 10 GB，长期需要「只存近 N 年」的裁剪版

---

# v1.0 — 2026-09

初版：股票分析配置中心。

- 技术面（MA/MACD/RSI/KDJ/布林/量价/背离/形态/波浪/缠论）
- 基本面（财报/成长/分红/主力资金/估值）
- 产业链（板块归属/景气/相对强弱 + 产业链 LLM）
- 加权合并 40/40/20，自包含 HTML 报告，逐指标 LLM 点评
- 多 Agent 框架（ReAct Orchestrator + 4 子 Agent + 18 工具）
- GitHub Action 每日邮件

---

# 维护约定

> 以后每个大版本更新**必须**遵守，否则版本日志会退化成没人看的历史文件。

## 何时写

**只要满足任一条，就必须新增一个版本章节：**

1. 新增或删除了**用户可见的能力**（端点、Tab、分析维度、报告章节）
2. 改动了**任何会影响数字的口径**（收益、成本、基准、复权、样本筛选）
3. 改动了**规则活化/衰减的判定逻辑**
4. 修改了**数据库 schema** 或数据源
5. 有**破坏性变更**（接口、返回结构、配置字段、缓存失效）

纯重构、纯注释、纯文档措辞 → 不单独开版本，可并入当前未发布版本的「其它」。

## 版本号规则

采用 `主.次`：

- **主版本 +1**：口径变更 / 破坏性变更 / 数据库 schema 变更（v1 → v2）
- **次版本 +1**：新增能力、缺陷修复、内部重构
- `caliber_version` / `SCHEMA_VERSION` 这类内部版本号**独立递增**，不跟对外版本号绑定，
  但**必须在章节里写明当前值**

## 每章必须包含（缺一项就不算写完）

| 小节 | 要求 |
|---|---|
| 一句话 | 这个版本把系统从什么变成了什么 |
| 一、为什么做 | 解决什么结构性缺陷，有数据支撑 |
| 二、新增能力 | 按模块列，带文件路径 |
| 三、实测结论 | **只写本机实测数字**，不写「预期」「理论上」 |
| 四、修掉的缺陷 | 表格：缺陷 / 类型 / **怎么发现的** |
| 五、纠正的源头错误 | 如果发现外部资料或自己之前的结论有错，必须写 |
| 六、验收与自检 | 可粘贴执行的命令 + 期望输出 |
| 七、兼容性与破坏性变更 | 逐个字段说清影响 |
| 八、已知限制 | **必须写**，不许只报喜 |
| 九、变更清单 | 新增/修改文件与行数 |
| 十、分支与提交 | 每个 feature 分支的 commit hash |
| 十一、下一步 | 下一版候选 |

## 写作纪律

1. **数字必须来自实测**。写不出实测数字的结论，就写「未验证」，不要估。
2. **缺陷要写「怎么发现的」**。这一栏比缺陷本身有价值 —— 它教下一个人怎么找 bug。
3. **不许删历史**。修错了就在原条目后加「（v1.x 勘误：…）」，不改原文。
4. **已知限制优先于新功能**。一个版本如果只能加功能、说不出限制，说明测得太浅。
5. **每条验收命令都必须真的能跑**。写完自己跑一遍，把真实输出贴进去。

## 配套动作

新增版本时同步做：

- [ ] 在本文顶部「版本一览」表加一行
- [ ] 若改了口径：递增 `backtest_core.CALIBER_VERSION`，并在章节写明新值
- [ ] 若改了 schema：递增对应 `SCHEMA_VERSION`
- [ ] 若新增模块：更新 `README.md` 的文件结构树
- [ ] 跑一遍全部自检，把结果贴进「六、验收」
- [ ] feature 分支逐个推送 GitHub 后再合并 main
