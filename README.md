# Portal — 股票分析配置中心

## 作用

Portal 是一个**独立的股票分析系统**，分两种使用模式：

| 模式 | 场景 | 是否需要 server |
|------|------|----------------|
| **纯静态模式** | 管理自选股、调参数、导出配置、GitHub Action 每日邮件 | ❌ 不需要 |
| **本地增强模式** | 立即触发深度分析、直接保存配置文件 | ✅ 需要 |

整个流程：**网页选股 → 提交 JSON → 每日收邮件**；需要立即分析时：**点击按钮 → 自动提示启动 server → 实时查看日志 → 查看双维度报告**。

**批量分析**：在「自选股」Tab 用 checkbox 勾选多只股票（勾选状态本地持久化），到「立即运行」点「📑 批量分析勾选的自选股」，队列按顺序逐个生成报告。添加股票时若 server 在线会自动查询名称，列表显示股票名称而非代码。

**HTML 报告**：每次个股/大盘分析后，server 生成图文并茂的独立 HTML 报告（K线蜡烛图/雷达图/各指标卡片 + 🤖 逐指标 LLM 点评偏多/偏空/影响）并自动用浏览器打开。同一股票只保留最新报告（标题记日期），历史列表点击可重新打开。逐指标点评有「批量/逐指标详细」双模式（设置页切换），结果按交易日缓存复用。

**大盘复盘**：点「🌐 大盘复盘」分析上证指数+创业板指（指数历史K线跑完整技术指标含 MA250 年线/超买超卖/背离），不判断交易日以最新交易日为准，两指数汇总成一份 HTML。

---

## 文件结构

```
portal/
├── index.html                  主页面（单页应用，GitHub Pages 直接托管）
├── index-standalone.html       单文件版（所有 JS 内联，双击可直接打开）
├── server.py                   本地增强服务薄入口（端口 7788；main + 路径注入 + re-export srv/）
├── srv/                        server 实现包（原 server.py 1578 行按职责拆分）
│   ├── _config.py              常量 / 路径注入 / 日志设置（最先导入）
│   ├── state.py                全局任务状态 _tasks / _tasks_lock
│   ├── prompts.py              build_review_prompt / build_forecast_prompt
│   ├── llm_gateway.py          LLM 调用闭包 / agent 环境映射 / 报告摘要
│   ├── data_access.py          .env 加载 / K线获取 / 技术维度重算 / JSON 兜底
│   ├── tasks.py                4 个 _run_*_task 后台任务
│   └── http_handler.py         Handler（HTTP 路由 + 各 _handle_*，13 端点）
├── send_report.py              邮件发送脚本（GitHub Action 调用）
├── data_cache.py               数据缓存管理器（K线增量缓存 + 商品搜索缓存）
├── 启动本地服务.bat             Windows 双击启动 server.py
├── analyzers/                  深度分析引擎（全在 portal/ 内）
│   ├── __init__.py             注册所有分析器（ANALYZER_REGISTRY）
│   ├── base.py                 BaseAnalyzer 基类 + Section/DimensionResult 数据类
│   ├── _common.py              共享工具：extract_llm_score / strip_score_json
│   ├── technical/              技术面（包）：MA/MACD/RSI/KDJ/布林/超买超卖/背离/量价/形态/波浪/缠论
│   │   ├── __init__.py         TechnicalAnalyzer 类壳（analyze 编排 + 薄委托）
│   │   ├── indicators.py       compute_indicators / normalize_volume_scale
│   │   ├── sections_basic.py   ma/macd/rsi/kdj/bollinger/volume/overbought（纯量化）
│   │   ├── sections_data.py    chip/turnover/margin（依赖 akshare）
│   │   ├── sections_llm.py     pattern/wave/chan/llm_tech（LLM）
│   │   └── divergence.py       背离检测引擎（分形+ATR+成熟度状态机）
│   ├── fundamental.py          基本面（财报/成长/分红/主力资金/估值）
│   ├── industry/               产业链（包）
│   │   ├── __init__.py         IndustryAnalyzer 类壳（analyze 编排 + 薄委托）
│   │   ├── sectors.py          板块分析（真实数据，不依赖 LLM）
│   │   └── chain.py            产业链分析（LLM + 搜索）+ 大宗商品映射
│   ├── sector.py               板块数据层（efinance 个股→板块 + 板块K线 + 缓存降级）
│   ├── market.py               轻量大盘分析器（上证+创业板，复用技术指标+MA250年线）
│   └── merger.py               合并报告（技术40% + 基本面40% + 产业链20%）
├── report_html.py              独立 HTML 报告生成器（个股+大盘两套模板,ECharts,去重,自动打开）
├── llm_notes.py                逐指标 LLM 点评（偏多/偏空/影响,双模式+交易日缓存）
├── lib/                        捆绑的依赖库（portal 独立运行所需）
│   ├── src/                    核心分析模块副本
│   └── data_provider/          数据源模块副本
├── data/                       本地数据缓存（运行后自动生成）
│   ├── stocks/
│   │   └── {股票代码}/
│   │       ├── kline.csv       日线 K 线（增量更新）
│   │       ├── meta.json       元信息（名称、关键词、最后更新时间）
│   │       ├── llm_notes/{日期}.json  逐指标 LLM 点评缓存（同股同日复用）
│   │       └── commodities/    相关商品/产品价格搜索缓存
│   │           └── {关键词}.csv
│   ├── reports/                独立 HTML 报告（自动打开，同 code 只留最新）
│   │   ├── {股票代码}_{日期}.html
│   │   └── market_{日期}.html  大盘复盘（上证+创业板）
│   └── sectors/                板块数据（全市场共享，非按股票）
│       ├── boards/{代码}.csv    个股所属板块（TTL 24h）
│       ├── kline/{BK}.csv       板块日线（增量，efinance/同花顺双源）
│       ├── concept_snapshot.csv 全市场板块行情快照
│       ├── concept_universe.csv 同花顺概念全集（区分题材/行业，TTL 7天）
│       └── _blacklist.json      宽泛板块黑名单（可编辑）
└── js/                         前端 UI 模块
    ├── app.js                  主入口：Tab 路由 + 服务状态轮询
    ├── store.js                状态管理 + localStorage 持久化
    ├── tabs/
    │   ├── run.js              立即运行 Tab（深度分析 + 大盘 + 全量）
    │   ├── watchlist.js        选股管理 Tab
    │   ├── settings.js         分析设置 Tab
    │   └── guide.js            使用说明 Tab
    └── components/
        ├── modal.js            保存配置弹窗（直接保存 / 复制 JSON）
        ├── toast.js            操作反馈通知条
        └── report-view.js      双栏结构化报告渲染组件
```

---

## 各文件职责详解

### 前端 JS

| 文件 | 职责 | 修改场景 |
|------|------|----------|
| `js/app.js` | Tab 路由、服务状态轮询（每5秒）、底栏绑定 | 注册新 Tab |
| `js/store.js` | 全局配置状态、localStorage 持久化、JSON 导出。**stock_list 为对象数组 `[{code,name,checked}]`，toJSON 降维回代码数组** | 新增配置字段时加 `DEFAULTS` 条目 |
| `js/tabs/run.js` | 深度分析入口、维度勾选、SSE日志、报告渲染、**批量队列（分析勾选自选股）**；自动检测并提示启动 server | 新增分析维度时同步 `DIM_DEFS` |
| `js/tabs/watchlist.js` | 自选股增删查、**checkbox 勾选（持久化）、代号自动转名称、显示名称** | 修改股票卡片样式 |
| `js/tabs/settings.js` | 分析参数表单（JS驱动 toggle，不依赖 Tailwind peer） | 新增参数时加 `_row()` 并绑定事件 |
| `js/tabs/guide.js` | 静态使用说明 | 更新步骤/Secrets说明 |
| `js/components/modal.js` | 保存配置弹窗：在线时"直接保存到文件"，离线时复制JSON | 修改保存逻辑 |
| `js/components/report-view.js` | 双栏报告渲染（技术面/基本面+产业链 + 底部综合结论） | 修改报告展示格式 |

### 后端 Python

| 文件 | 职责 | 修改场景 |
|------|------|----------|
| `server.py` + `srv/` 包 | HTTP 服务（7788端口）：`/health` `/save` `/run` `/analyze` + SSE流。server.py 为薄入口，实现在 srv/（http_handler/tasks/llm_gateway/prompts/data_access/…） | 新增 API 接口改 `srv/http_handler.py`；新增后台任务改 `srv/tasks.py` |
| `data_cache.py` | K 线增量缓存 + 商品搜索缓存（TTL自适应） | 调整缓存策略 |
| `send_report.py` | 读取 `reports/*.md`，发送 HTML 邮件 | 修改邮件格式 |
| `analyzers/technical/` | 包：MA/MACD/RSI/KDJ/布林带/量价 + **超买超卖综合(RSI+KDJ+WR+布林)/背离引擎(分形摆动点+常规&隐藏背离+成熟度状态机 迹象浮现→进行中→已确认)** + 形态/波浪/缠论（LLM）。子模块见 indicators/sections_basic/sections_data/sections_llm/divergence | 新增技术指标改对应子模块，section.key 保持不变 |
| `analyzers/fundamental.py` | 财报/成长/分红/主力资金/估值（复用 fundamental_adapter） | 新增财务指标 |
| `analyzers/industry.py` | 板块归属/景气/相对强弱（真实数据）+ 产业链关键词/竞争/政策（LLM） | 新增行业关键词模板 |
| `analyzers/sector.py` | 板块数据层：efinance 个股→板块 + 板块K线 + 全市场快照 + 缓存降级 | 调整黑名单/数据源 |
| `analyzers/merger.py` | 加权合并各维度结果，LLM生成综合结论 | 调整权重 |

---

## 数据缓存系统（`data_cache.py`）

### 设计原则

- **增量更新**：首次全量拉取120日K线，后续只拉最新增量
- **按股票分文件夹**：每只股票独立目录，互不干扰
- **商品价格缓存**：搜索结果本地存档，避免重复调用搜索API
- **TTL自适应**：盘中2h / 盘后6h / 非交易日24h

### 目录结构示例（天齐锂业）

```
portal/data/stocks/002466/
├── kline.csv          ← 日线K线（date/open/high/low/close/volume/amount/pct_chg/fetch_source）
├── meta.json          ← {"code":"002466","name":"天齐锂业","last_date":"2026-08-14",
│                           "commodity_keywords":["碳酸锂价格","锂矿供需",...]}
└── commodities/
    ├── 碳酸锂价格.csv  ← fetched_at / query / snippet（TTL内直接使用）
    ├── 锂矿供需.csv
    └── 新能源电池产业链.csv
```

### 三种拉取模式

| 模式 | 触发条件 | 行为 |
|------|----------|------|
| `full` | 本地无缓存（首次） | 拉取120日历史数据，写入 kline.csv |
| `incremental` | 缓存存在且非最新 | 只拉 last_date+1 到今天，合并到缓存 |
| `up_to_date` | 缓存日期 = 今天 | 直接返回本地缓存，不发网络请求 |

### 扩展商品关键词

在 `analyzers/industry.py` 的 `INDUSTRY_KEYWORDS_FALLBACK` 字典中添加：
```python
"铜": ["铜价格走势", "铜矿供需", "铜期货"],
# 添加新行业 → 对应关键词列表（LLM失败时的降级方案）
```

---

## server.py API 接口

| 接口 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查，用于前端检测服务状态 |
| `/save` | POST | 写入 `config/watchlist.json` |
| `/run` | POST | 启动快速任务（全量分析），返回 task_id |
| `/analyze` | POST | 深度双维度分析 → 生成独立 HTML + 逐指标 LLM 点评 + 自动打开浏览器（llm_mode/open_report 参数） |
| `/market_review` | POST | 大盘复盘（上证+创业板），生成一份 HTML 并打开（废弃旧 main.py 大盘链路） |
| `/open_report` | GET | `?code=xxx`（`__market__`=大盘）按 code 打开最新 HTML 报告 |
| `/run/stream/{id}` | GET | SSE 实时推送任务日志 |
| `/run/report/{id}` | GET | 获取任务完成后的报告 |
| `/run/status/{id}` | GET | 查询任务状态 |

**启动方式：**
```bash
# 方式1：双击
portal/启动本地服务.bat

# 方式2：命令行（从项目根目录运行）
cd C:\Users\I762120\Desktop\incident\daily
python portal/server.py
```

---

## 深度分析引擎（`analyzers/`）

### 三个维度

| 维度 | 文件 | 默认子模块 | 数据来源 |
|------|------|-----------|----------|
| 技术面 | `technical/`（包） | MA/MACD/RSI/KDJ/布林带/量价 | 本地K线缓存 → 网络 |
| 基本面 | `fundamental.py` | 财报/成长/估值/主力资金 | fundamental_adapter |
| 产业链 | `industry.py` + `sector.py` | **所属板块/板块景气/相对强弱**（真实数据）+ 商品价格/产业链地位/竞争/政策（LLM） | efinance 板块数据 + LLM |

### 板块子模块（真实数据，不依赖 LLM）

产业链维度新增 3 个基于**真实板块数据**（efinance 东财口径）的子模块：

| 子模块 | 说明 | 数据来源 |
|--------|------|----------|
| `sector_membership` | 个股所属核心题材板块清单（如"AI应用""固态电池"），带当日涨幅 | `ef.stock.get_belong_board` |
| `sector_momentum` | 板块景气 + **个股 alpha = 个股涨幅 − 板块涨幅**（相对强弱） | 板块涨幅 + 个股K线 |
| `sector_fund_flow` | 板块在全市场的涨幅排名、换手/量比 | `ef.stock.get_realtime_quotes` |

- **细分概念自动更新**：板块名直接来自 efinance 实时结果，未来新增的细分概念（如新题材）自动出现，无需硬编码。
- **题材优先**：用同花顺概念全集区分"题材概念"（可拿板块K线）与"行业分类"，题材排前。
- **双源降级**：板块K线优先 efinance（东财），失败降级 akshare 同花顺 `stock_board_concept_index_ths`；全部失败则板块子模块跳过，不阻断整体分析。

### 加权评分

```
综合评分 = 技术面×40% + 基本面×40% + 产业链×20%
```

### 新增分析器

1. 在 `portal/analyzers/` 新建 `my_dim.py`（或 `my_dim/` 包），继承 `BaseAnalyzer`，实现 `analyze()` 方法
2. 在 `portal/analyzers/__init__.py` 的 `ANALYZER_REGISTRY` 中注册
3. 在 `portal/js/tabs/run.js` 的 `DIM_DEFS` 中添加维度定义

---

## 独立运行（portal/ 脱离主项目）

`portal/lib/` 包含所有依赖的副本（`src/` 和 `data_provider/`），server.py 优先从此加载：

```python
PORTAL_DIR = Path(__file__).parent       # portal/
LIB_DIR    = PORTAL_DIR / "lib"          # portal/lib/（优先）
PROJECT_ROOT = PORTAL_DIR.parent         # 主项目根目录（回退）
```

如需完全脱离主项目：把 `portal/` 整个目录移走，在 `server.py` 顶部修改 `PROJECT_ROOT` 即可。

---

## 本地预览

```bash
# 推荐：使用单文件版（双击直接打开，无需服务器）
portal/index-standalone.html

# 或启动 HTTP 服务（ES Modules 需要 HTTP 协议）
cd portal
python -m http.server 8080
# 访问 http://localhost:8080
```

> `index-standalone.html` 是通过脚本将所有 JS 内联生成的，每次修改 `js/` 文件后需重新生成：
> ```bash
> cd C:\Users\I762120\Desktop\incident\daily
> python portal/build_standalone.py   # 见下方扩展指南
> ```

---

## GitHub Pages 部署

1. 仓库 **Settings → Pages → Source: main, Folder: /portal**
2. 访问 `https://<用户名>.github.io/<仓库名>/`

每次 push 到 main 分支自动更新，无需额外操作。

---

## 扩展指南

### 新增 Tab

1. `portal/js/tabs/my_tab.js` — 导出类，实现 `init()` 和可选的 `setServerStatus(online)`
2. `portal/js/app.js` — 在 `TABS` 数组注册

### 新增配置字段

1. `js/store.js` 的 `DEFAULTS` 加默认值
2. `js/tabs/settings.js` 加 `_row()` 表单项并绑定 `_bindInput()`
3. `.github/workflows/portal-daily-analysis.yml` 读取新字段写入 `GITHUB_ENV`

### 重新生成单文件版

`js/` 是源，`index-standalone.html` 是构建产物。每次修改 `js/` 下任何文件后运行：
```bash
cd C:\Users\I762120\Desktop\incident\daily
python portal/build_standalone.py
```

构建脚本 `build_standalone.py` 会按 `ORDER_WITH_PREFIX` 顺序把各 JS 模块内联进 `index.html`：去掉 `import`、剥离 `export` 前缀、按依赖顺序拼接成单个 `<script>`。

**⚠️ 新增 JS 文件时必须**：
1. 把新文件加进 `build_standalone.py` 的 `ORDER_WITH_PREFIX`，且排在其使用者**之前**（共享常量/工具如 `js/config.js` 排最前）。
2. 确保新文件的顶层 `const/class/function` 名**全局唯一**——构建产物是单一作用域，重名会导致 `Identifier already declared` 运行时错误，而构建脚本**不会报错**（静默产出坏文件）。
   （历史上唯一冲突的 `SERVER` 已统一到 `js/config.js`，故 `COLLIDING` 现为空集。）
