# 数据层实测 · 回测数据库 · 前端虚拟盘 · 报告图表

> 分支：`feat/data-marketdb` → `feat/ui-virtual-tab` → `feat/report-charts` → `fix/data-rounding-panel`
> 本文记录的是**实测数字**，不是文档转述。

---

## 一、A 股数据接口实测（本机 2026-10-19）

不引用文档，全部现场跑一遍。

| 接口 | 结果 | 延迟 | 说明 |
|---|---|---|---|
| `ak.stock_zh_a_hist`（东财日K） | ❌ **不可用** | 268ms | `ProxyError` on `push2his.eastmoney.com` |
| `efinance` 日K | ❌ **不可用** | 454ms | 同一主机，同一错误 |
| `ak.stock_zh_a_daily`（新浪，不复权） | ✅ | **352ms/只** | 字段最全：含 `outstanding_share` / `turnover` |
| `ak.stock_zh_a_daily`（新浪，hfq-factor） | ✅ | **50ms/只** | 复权因子事件（茅台 24 年共 33 个） |
| `ak.tool_trade_date_hist_sina` | ✅ | 230ms | 8797 个交易日 |
| `ak.stock_info_a_code_name` | ✅ | 7394ms | 5575 只（比预想的慢但可用） |
| `ak.stock_info_sh_name_code` | ✅ | 0ms（缓存） | 带上市日期 |
| `ak.stock_info_sz_name_code` | ✅ | 0ms（缓存） | 带总股本/流通股本/行业 |
| `ak.stock_zh_index_daily_tx` | ✅ | 7150ms | 指数日K，慢 |
| `ak.fund_etf_hist_sina` | ✅ | 114ms | ETF 日K |
| `ak.stock_zt_pool_em` | ✅ | 155ms | 涨停池（`push2ex` 通，`push2his` 不通） |
| `baostock` 日K | ✅ | 995ms | 不复权，速度慢于新浪 |

**结论：日K 一律走新浪，复权用 `hfq-factor` 自算；东财 `push2his` 系全不可用。**

### 并发与吞吐

| 方式 | 结果 |
|---|---|
| 线程池 | 不可用（新浪的 JS 解码要 V8，多线程初始化必崩） |
| **进程池 x8** | **108ms/只** → 全市场约 10 分钟 |

实测全量：5546 只 → **776.8 秒（13 分钟）**，1649 万条。

---

## 二、回测数据库（`virtual/marketdb.py`）

### 存储口径：只存原始价 + 因子事件

```
hfq_close(t) = close(t) × factor(t)        factor 按事件日向前填充
qfq_close(t) = close(t) × factor(t) / factor(最后一个交易日)
```

**验证**（茅台，6023 个交易日）：`raw × factor` 还原新浪自己的 hfq/qfq 序列，
最大绝对误差 **0.005 元**（就是分位四舍五入），中位 0.0024 元 → **可精确还原**。

好处：复权价随时可重建，存的是不可逆操作最少的原始数据；同时天然支持两套价格体系。

### schema

```
stock(code,name,market,board,limit_pct,is_st,listed_date,total_share,float_share,industry)
calendar(date,is_open)
day(code,date,open,high,low,close,volume,amount,outstanding_share,turnover)
adj_event(code,date,hfq_factor)
ingest_log(code,last_date,rows,factor_rows,status,err,updated_at)
```

- `day` 用 `WITHOUT ROWID` + `(code,date)` 主键；另建 `idx_day_date` 支持横截面查询
- `ingest_log` 支撑**断点续传**：`resume` 跳过已完工的票

### 实测规模

| 项 | 值 |
|---|---|
| 股票 | 5575（主板 3196 / 创业板 1410 / 科创板 618 / 北交所 351） |
| 有日K的股票 | **5223**（主板+创业板+科创板 **100%**） |
| 日K行数 | **16,711,712** |
| 日期范围 | 1990-12-19 ~ 2026-10-19 |
| 复权因子事件 | 66,951 |
| 数据库大小 | **1.86 GB** |
| 全量构建 | 13 分钟（8 进程） |

### 北交所：明确不支持

351 只北交所股票**拿不到历史日K**：

- 新浪 `stock_zh_a_daily('bj...')` → `KeyError: 'date'`
- 腾讯 `fqkline` → 只返回 0~1 行

所以 `fetch_one()` 对 bse 直接标 `unsupported`，**不再反复重试**（否则每次构建都白跑 351 次）。
要北交所数据必须换付费源。

### 增量更新

`update()` 对每只票从 `last_date - 5 天` 重抓，UPSERT 幂等。
**回看 5 天而不是 1 天**：复权会因为新的分红送转而**改写历史**，多抓几天才能覆盖。

---

## 三、从日K重建涨停池（`virtual/scan.py`）

官方涨停池只保留约 14 个交易日。重建口径：

```
涨停 ⟺ 收 == 昨收 × (1 + 板块幅度)，四舍五入到分
```

### 与官方池对照（2026-10-13 ~ 10-19，10 个交易日）

| 类别 | 数量 | 解释 |
|---|---|---|
| 漏报：北交所 | **4** | 无历史日K数据 |
| **漏报：其他** | **0** | ✅ |
| 多报：ST 股 | 4 | 官方涨停池**不收录 ST**（5% 幅度） |
| 多报：炸板后回封 | 3 | 收盘确实等于涨停价，官方归入**炸板池**（`break_count>0`） |
| 多报：其他 | 2 | 官方池自身不稳定（同一日期两次调用，家数 36 → 34） |

**除了北交所，重建本身没有漏报。**

### 过程中抓到的真 bug：银行家舍入（漏报 5%）

第一次对照时漏报 12 只，其中 3 只（001319 / 001358 / 600721）是这一条：

```
001319 昨收 19.45 → 19.45 × 1.1 = 21.395
    Python round() → 21.39   ← 银行家舍入，漏判涨停
    交易所         → 21.40   ← 四舍五入 ✓
```

我在 `tagging.limit_up_price` 里早就修过这个坑，却在 `scan._classify` 和
`datafeed.daily_limit_up` 里用了原生 `round()` —— **同一个 bug 换了个地方又长出来**。

修复：统一到 `marketdb.round_half_up()` / `limit_prices()`，三个调用点共用。
修复后漏报 12 → 4（剩下的全是北交所）。

> 这就是「口径分叉」的代价：同一个公式写在三处，修一处不等于修好。

---

## 四、全市场预热（`virtual/warmup.py`）

以前 `zone_pos_120` 依赖 `data_cache`（只覆盖你手动分析过的票），在真实涨停池上
覆盖率是 **0/69**。接上数据库后：

```
python -m portal.virtual.warmup --days 30
→ 30 天 / 1723 条涨停 / zone_pos_120 覆盖率 1723/1723 (100.0%)
```

代价与边界（如实写进特征文件）：

- `seal_amount` / `float_cap` **无法从日K重建** → `seal_ratio_pct` 在历史回补上必然为 null
  （可买性判据只能用于当日实时）
- 历史日的 `hot_reason`（同花顺归因）为 null

---

## 五、权重回测（`virtual/weights.py`）—— 本轮最重要的结果

### 先说清楚能测什么

| 维度 | 历史评分能否复现 | 原因 |
|---|---|---|
| 技术面 | ✅ | 只要日K |
| 游资情绪 | ✅（部分） | 涨停/连板/位置可重建；封单结构不可 |
| 基本面 | ❌ | 数据库没有历史财报快照 |
| 产业链 | ❌ | 没有历史板块归属 |

所以只回测**技术 vs 游资**的相对权重，另外两维明确标为未验证。

### 实测（2026-01 ~ 10，60 个交易日，3605 条样本）

选股方式：按综合分选**前 20%**（测排序能力，而非固定阈值——代理分未校准到 70 分档）。

| 技术权重 | 游资权重 | 选中n | 均值% | 中位% | 胜率 | 超额pp | t | IC |
|---|---|---|---|---|---|---|---|---|
| **0.20** | **0.80** | 757 | **+1.388** | +0.318 | 51.9 | **+1.529** | **5.68** | 0.084 |
| 0.00 | 1.00 | 773 | +1.356 | +0.308 | 51.7 | +1.497 | 5.65 | 0.111 |
| 0.30 | 0.70 | 810 | +1.171 | +0.164 | 51.0 | +1.311 | 5.00 | 0.072 |
| **0.35** | **0.65** | 770 | **+0.315** | **−0.433** | 46.5 | +0.456 | 1.39 | 0.058 |
| 0.50 | 0.50 | 773 | +0.048 | −0.782 | 43.7 | +0.189 | 0.21 | 0.002 |
| 1.00 | 0.00 | 786 | −0.485 | −1.497 | 41.6 | −0.344 | −1.81 | −0.100 |

全样本均值 **−1.146%**（T+1 开盘买、持有到次日收盘、扣费）—— 涨停股本身是负期望。

### 结论（必须一起读）

1. **技术权重越高越差**。技术权重从 0.20 提到 1.00，超额从 +1.53pp 掉到 −0.34pp，
   IC 从 +0.084 掉到 −0.100。**技术代理分在涨停股上没有排序能力。**
2. **当前权重（技术 0.35 / 游资 0.65 归一后）超额只有 +0.456pp，t=1.39 —— 不显著。**
3. **但走查验证否定了「降技术、提游资」这个结论**：

   ```
   训练集样本 2163 / 测试集 1442
   verdict: 训练集选出的权重在测试集上没有复现（超额 +0.597pp，t=−1.61）→ 属于过拟合，不应采用
   ```

   也就是说：全样本上「游资权重更高更好」看起来很强（t=5.68），
   **但切分后不复现** —— 它更像是 2026 上半年某一阶段的行情特征，不是规律。

4. **IC 只有 0.06~0.11**，说明这个综合分对次日收益的解释力本来就弱。

所以：**权重暂不改，35/30/15/20 继续标为 `hypothesis`。** 这比「按全样本最优把技术权重
砍到 0.2」诚实得多 —— 那正是 SKILL §9.7.4 说的「未经检验的规则会悄悄扣钱」。

---

## 六、预测账本（`virtual/predictions.py`）

闭环的最后一块：每条预测 → 结果 → 校准。

- `record_prediction()`：盘前/买入时记下「看到什么、下了什么注」
- `record_settlement()`：结算后追加结果；平仓时补 `outcome`（0/1）
- `calibration()`：命中率、平均已实现、**Brier**（只有计划里声明了 `confidence` 才算，
  没有就留空，不假装有）

已挂到 `settle_buy` / `settle_exit` 上，写入失败不会影响结算本身。

---

## 七、前端「虚拟盘」Tab

`js/tabs/virtual.js` + `srv/virtual_api.py`（7 个端点）。

**前端不做任何算术** —— 所有数字来自后端，否则就是第二套口径。

界面：数据库覆盖 / 规则表（带证据告警）/ 预测校准 / 账本，外加三个动作：
预热、跑一轮演化（默认不落库）、生成复盘报告。

数据库缺失时不显示红叉，而是直接告诉你该跑哪条命令。

---

## 八、报告图表（内联 ECharts + 真实渲染校验）

- `virtual/assets/echarts.min.js`（1010 KB）内联进报告，文件约 **1.04 MB**，零外部引用
- 三张图：净值曲线、三把尺子、逐笔已实现收益
- 校验：`python -m portal.virtual.verify_report <html> --expect-canvas 3`

实测结果：

```json
{"ok": true, "canvases": 3, "charts_initialized": 3, "echarts_defined": true,
 "page_errors": [], "console_errors": [], "has_external_refs": false, "bytes": 1042750}
```

探针自检（`--selftest`）用「引 CDN 且 echarts 未定义」的坏样本验证它**能失败**：

```
✅ 探针有效：坏样本触发 pageerror（echarts is not defined…）
✅ 探针有效：坏样本判定为失败
✅ 对照样本判定为通过
```

---

## 九、`build_standalone.py` 加固

以前它出坏文件**不报错**（README 里点名过）。现在构建后强制校验：

1. `ORDER_WITH_PREFIX` 里每个文件必须存在
2. 产物里不许残留 `import` / `export`
3. 顶层声明不许重名（单作用域，重名会在浏览器里抛 `Identifier already declared`）
4. 每个 TAB 的 `Cls` 必须在产物里被定义
5. 不许残留 `type="module"` 脚本标签（`file://` 下加载不了）
6. 死代码告警（类只有定义、注释之外从不被引用）
7. `--self-test` 注入已知坏样本，证明上述每条都能失败

**它抓到的问题**：提交进仓库的 `index-standalone.html` 里躺着一个 36 KB 的 `ResearchTab`
类和一个「基本面」独立 Tab，而 `js/` 里早就没有它们了 —— 产物与源码漂移，无人发现。

> 基本面能力本身没丢：它已经并进「立即运行」的维度勾选里，所以源码才删掉了独立 Tab。
> 重建后的产物与源码一致，`node --check` 通过。

---

## 十、每日增量的两种执行方式

### 为什么要两条路，而不是一个 `update` 跑到底

`update()` 是**全量重抓**：新浪 `stock_zh_a_daily` 一次返回全历史，**没有区间参数**，
所以「更新一天」和「重建十年」的成本一样。实测全市场：

| 方式 | 请求量 | 本机耗时 | 用途 |
|---|---|---|---|
| `update-today` | ~110 次（每批 50 只） | **26 秒** | 每日补当天一根 bar |
| `update` | 5570 只 × 2 次 | **13 分钟** | 刷新复权因子 + 补缺口 |

于是：**日更走快通道，每周走一次全量兜底**。因子只在除权除息日变化，
每天全量是纯浪费；但完全不刷因子会让送转之后的复权价错掉。

### 快通道的数据源与单位（已实测对齐）

腾讯 `qt.gtimg.cn` 批量行情，一次 50 只：

```
f[3]=最新价  f[4]=昨收  f[5]=今开  f[6]=成交量(手)  f[30]=时间戳
f[33]=最高  f[34]=最低  f[37]=成交额(万元)
```

实测 600519 / 000001 / 300750 三只与库内新浪数据对比：

- 成交量比值 **恰好 100.00** → 手 ×100 = 股
- 成交额比值 **1.0000** → 万元 ×10000 = 元
- 开高低收**完全相同**

> 顺带纠正一个易错点：`f[31]` 是**涨跌额**、`f[32]` 才是涨跌%，容易写反。

### ★ 盘中护栏（这条不能删）

盘中抓到的 bar 是**半天快照**（成交量/成交额都没走完）。写进日K会**静默污染回测**，
而且事后完全看不出来 —— 这是「静默错误」的典型。

所以 `update_today()` 默认要求行情时间 **≥ 15:00**：

```json
{"intraday": true, "written": 0,
 "error": "行情时间是盘中（样本时间 1015），不是收盘后的完整 bar。写进日K会静默污染回测，已拒绝。"}
```

确实要盘中数据必须显式 `--allow-intraday`，且返回里会标 `intraday: true`。
回归测试 **W2** 用注入的盘中行情验证它真的会拒绝（不是靠文档承诺）。

### 方式一：Portal 里的按钮

「虚拟盘」Tab → **⚡ 拉取今日行情** / **🔄 全量刷新（慢）**。

后端是**后台子进程 + 轮询**，不是同步请求：

```
POST /virtual/update        {"kind": "today" | "full"}
GET  /virtual/update/status?tail=60
```

为什么用子进程而不是线程里的 `ProcessPoolExecutor`：全量更新会建进程池，
而 Windows 的 spawn 会重新 import `__main__`；在 HTTP 服务进程里混进程池，
出事时的现象是「进程池被整体终止」，极难查。子进程隔离后
`python -m portal.virtual.marketdb` 自带 `__main__` 保护，stdout 天然就是进度流。

并发护栏：已有任务在跑时**直接拒绝**（不排队），避免两个进程同时写 `market.db`。
回归测试 **W7** 覆盖。

前端显示进度条与实时日志；失败时变琥珀色并给出原因，不会假装成功。

### 方式二：GitHub Action

`.github/workflows/market-db.yml`

| 触发 | 时间（CST） | 动作 |
|---|---|---|
| 周一至周五 | 16:30 | `update-today`（几十秒） |
| 周六 | 18:00 | `update`（全量刷新） |
| 手动 | — | 可选 `today` / `full` / `auto`，可勾「强制重建」 |

**关键实现细节**

1. **检出到 `portal/` 子目录**（`actions/checkout` 的 `path: portal`）。
   代码里大量 `from portal.virtual import ...`，要求「仓库根 = portal 的父目录」。
   直接检出到根目录会让这些导入全部失败。已用**目录联接模拟 CI 布局**验证：
   `status` / `virtual_api.status()` / `selfcheck` 在该布局下 **39/39 通过**。

2. **数据库只存在于 Actions 缓存**，不进 git（1.9 GB）。
   `key: marketdb-${{ github.run_id }}` + `restore-keys: marketdb-` → 每次跑都命中
   最近一份、并另存一份新的。仓库缓存上限 10 GB，约能留 5 份，LRU 淘汰；
   所以**每周的全量刷新同时兼作兜底重建**。

3. **首次运行 / 缓存丢失**：自动 `init` + `build --workers 4`，失败后再跑一次
   `build`（断点续传补齐），最后 `status` 打印覆盖。

4. **当日增量被护栏拒绝时**（节假日 / 数据源时间戳未到 15:00），
   步骤用 `::warning::` 显式告警，**不静默跳过**。

5. 收尾：`warmup --days 5`、生成复盘报告并作为 artifact 上传、
   写 `$GITHUB_STEP_SUMMARY` 表格。

### 怎么确认它真的在工作

```bash
# 本地：看库里最新日期与当天行数
python -m portal.virtual.marketdb status

# 本地：模拟一次当日增量（收盘后）
python -m portal.virtual.marketdb update-today

# CI：看 job summary 里的「最新日期」是否推进、
#     「缓存命中」是否为 true
```

实测一次完整链路：`update-today` → `fetched 5224 / written 5213 / 26 秒`，
库内 `2026-10-19` 行数从 0 变为 5213，`PRAGMA integrity_check` 仍为 `ok`。

---

## 十一、怎么复现

```bash
# 1) 建库（首次约 13 分钟，可断点续传）
python -m portal.virtual.marketdb init
python -m portal.virtual.marketdb build --workers 8

# 2) 数据库状态 / 单票抽查
python -m portal.virtual.marketdb status

# 3) 每日增量（收盘后，26 秒）
python -m portal.virtual.marketdb update-today

# 4) 每周全量刷新（13 分钟，刷新复权因子）
python -m portal.virtual.marketdb update --workers 8 --lookback 7

# 5) 重建池 vs 官方池（地基自检）
python -m portal.virtual.warmup --check

# 6) 预热特征
python -m portal.virtual.warmup --days 30

# 7) 权重回测
python -m portal.virtual.weights --start 20260101 --days 60

# 8) 报告图表校验
python -m portal.virtual.verify_report <html> --expect-canvas 3

# 9) 数据层自检（W1–W7）
python -m portal.virtual.selfcheck
```

---

## 十二、仍然没做的

| 事项 | 原因 |
|---|---|
| 北交所历史数据 | 免费源都没有，必须换付费源 |
| 基本面/产业链权重回测 | 数据库里没有历史财报与板块归属快照 |
| 官方池不稳定的量化 | 只能观察到（36→34），无法证伪原因 |
| Actions 缓存上限 | 1.9 GB/份，10 GB 上限约留 5 份，靠每周全量兼作兜底 |
| 节假日判断 | 不做特殊处理：当日增量在节假日只会重写上一交易日（UPSERT 幂等），护栏会告警 |

---

*本文为工程记录，所有数字均为本机实测，不构成投资建议。*
