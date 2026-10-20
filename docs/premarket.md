# 盘前预案 agent（`portal/premarket`）

> 分支：`feat/premarket-plan`｜自检：`python -m portal.premarket.selfcheck` → **V1–V10 10/10**

---

## 一、它是什么

把「当日市场信息 → 次日可执行预案」固化成一个**可复现的流水线**，并维护你要求的
四张表。**盘后复盘与盘前预案是同一条流程**（两者都在收盘后做）。

核心契约与本项目其它部分一致：

> **LLM 只负责判断，内核负责裁决。**

| 层 | 文件 | 做什么 | 不做什么 |
|---|---|---|---|
| 采集 | `collect.py` | 指数K线细节/涨跌分布/量能/池子/题材多日趋势/北向/龙虎榜/候选池/快讯 | 不判断 |
| 判断 | `agent.py` | 产**一个 JSON**：趋势、仓位、题材四要素、消息分析、个股计划、复盘 | 不写数字结果、不写 HTML |
| 裁决 | `apply.py` | 上限、价格合理性、**禁止追高打板**、选股必须在候选池、必填原因 | 不与 LLM 商量 |
| 核算 | `account.py` | 百分比账户、盈亏、**当场按收盘价调仓**、不计费用 | 不接受 LLM 给的数字 |
| 渲染 | `render.py` | 10 节固定 HTML | 不拼接、不转义失误 |
| 编排 | `pipeline.py` | 串起来 + 锚点 + 落盘 | — |

### 数据存在哪

| 文件（`portal/data/premarket/`） | 内容 |
|---|---|
| **`positions.jsonl`** | **持仓** —— append-only 事件流，每笔开/平/调都带原因与时间 |
| `trades.jsonl` | 已平仓流水（含已实现盈亏） |
| `account.json` | 账户快照（派生，可随时重算） |
| `pool.jsonl` | 选股观察表 |
| `infos.jsonl` | 重要信息表 |
| `lessons.jsonl` | 交易经验表 |
| `plans/plan_YYYYMMDD.json` | 每日预案（含锚点） |
| `reports/*_premarket.html` | 报告 |
| `cache/YYYYMMDD/collect.json` | 采集缓存（gitignore） |

**为什么必须代码生成 HTML**：agent 拼 HTML 会漏标签、忘转义、每次都不一样；
而模板固定后章节数恒定、数字位置稳定，才谈得上「便于程序解析与存档」。

---

## 二、四张表：append-only 事件流

不是「当前状态文件」，而是**每次改动都追加一条事件**，当前状态是重放结果。

```jsonl
{"op":"add","id":"600519","at":"...","plan_date":"20261020","reason":"...","fields":{...}}
{"op":"update","id":"603906","reason":"核心太多，降级",...}
{"op":"remove","id":"002454","reason":"题材催化落地，逻辑失效",...}
```

这样「改表必须写明原因」是**结构上强制**的，不靠自觉：`reason` 为空直接抛
`ValidationError`。而且事后无法悄悄改历史 —— 改一行就要追加一条事件，报告里会显示。

| 表 | 文件 | 键 | 硬约束（代码里） |
|---|---|---|---|
| 选股观察表 | `pool.jsonl` | code | 当日新增 ≤ **8**；`tier=core` 合计 ≤ **6**；`trigger` 必须含数字 |
| 模拟持仓表 | `positions.jsonl` + `trades.jsonl` | code | 单只 ≤ 40%；总仓位 ≤ 100%；止损 < 成本 < 止盈 |
| 重要信息表 | `infos.jsonl` | 内容哈希 | `content` ≤ **50 字**；过期自动清理 |
| 交易经验表 | `lessons.jsonl` | 内容哈希 | 必须可验证（含数字或明确条件），空话被拒 |

**空话检测**是实测有效的：

```
lessons.lesson 太短（5 字），空泛总结不算经验
pool[000002].trigger 必须含具体价格/量能数字，不能是「看情况放量突破」
```

---

## 三、百分比账户

按你的要求：**不记具体资金，初始 100%**。

- 内部总资产 = 100.00 个份额，对外只显示百分比
- `qty = alloc_pct% × 100 / entry_price`，**保留分数股**
- 费用往返 **0.10%**（买 0.05% + 卖 0.05%），与 `backtest_core` 同一个口径

> 为什么不凑 100 股整手：凑整会把「买 10%」变成「买 9.7%」，之后所有归因都对不上。
> 整手是执行层的事，不是账本的假设。

---

## 四、数据源：实测可用性

本机 2026-10 实测（不引用文档）：

| 数据块 | 接口 | 结果 |
|---|---|---|
| 指数快照 | `stock_zh_index_spot_sina` | ✅ 562 行，含成交额 |
| 指数日K | `stock_zh_index_daily` | ✅ 8744 行 → MA5/20/60 与趋势 |
| 涨跌家数/活跃度 | `stock_market_activity_legu` | ✅ |
| 涨停/炸板/跌停/强势股池 | `stock_zt_pool_*_em` | ✅ |
| 龙虎榜 | `stock_lhb_detail_em` | ✅ 358 行 |
| 北向 | `stock_hsgt_fund_flow_summary_em` | ⚠️ 净买额恒为 0（实时额度已停披露，报告里会标注） |
| 快讯 | `stock_info_global_cls` / `_em` | ✅ 20 + 200 行 |
| 行业/概念板块 | `stock_board_*_em` | ❌ **push2 被封** |
| 个股资金流 | `stock_individual_fund_flow` | ❌ **push2his 被封** |

**题材强弱因此改成离线算**：用涨停池的 `所属行业` 聚合「涨停家数 / 连板总数 /
封板资金 / 最高板」。涨停家数是资金真金白银买出来的，比任何板块涨幅都难造假。

采集失败只记进 `errors`，绝不整份预案作废；报告第十二节会列出哪些源不可用，
并明确写「相关结论不要依赖」。

---

## 五、使用

### 一键生成（主页）

「立即运行」页顶部 → **一键生成预案**。

```
POST /premarket/generate     {}                     → 启动后台任务
GET  /premarket/job?tail=60                        → 轮询进度
GET  /premarket/status                             → 四表 + 账户 + 历史份数
GET  /premarket/tables                             → 四张表明细
GET  /premarket/latest                             → 最新报告路径
POST /premarket/open                               → 打开报告
POST /premarket/review       {"date":"20261020"}    → 盘后复盘（按收盘价结算）
```

生成是**后台子进程 + 轮询**（LLM 可能跑几十秒到几分钟），前端有进度条与实时日志。
用子进程而不是线程：`srv/jobrunner.py` 里统一实现，避免两套。

### 命令行

```bash
python -m portal.premarket generate            # 生成（用最新交易日的行情）
python -m portal.premarket generate --no-cache # 不吃采集缓存
python -m portal.premarket generate --no-fallback --json
python -m portal.premarket review --date 20261020
python -m portal.premarket status
python -m portal.premarket tables
```

---

## 六、没有 LLM 时会怎样

**降级为规则兜底，并显著标注**：

> 本次为规则兜底，不是 agent 判断。未接入 LLM（…）。本预案只做了「大盘趋势 →
> 仓位区间」的机械映射与题材排序，**没有生成个股计划、没有改动四张表**。

兜底只做两件事：指数趋势 → 仓位区间（弱势 10–30 / 震荡 20–50 / 强势 40–70），
以及按涨停家数排题材。不新增标的、不做买卖计划 —— 那些必须靠判断。

**这是刻意的**：直接报错会让「一键生成」在没配 key 的机器上完全不可用，而数据采集、
四张表、账户核算在那些机器上是能跑的。能跑 与 是谁判断的，两件事都要说清楚。

配置 LLM（任一即可）：`DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / `LITELLM_MODEL` /
`HAI_BASE_URL` + `HAI_API_KEY`。

---

## 七、锚点：预案不能被事后改

每份预案都带 `anchor.sha256`（对 `plan_date / trade_date / market_digest /
proposal / planned` 做规范化 JSON 的 sha256），并在报告底部显示。同时记录
`prev`（上一份预案的锚点）形成链。

这是从源包 `利弗莫尔/账本/ledger_v2.md` 学来的：那份账本明确写了
「plan 内 `params_frozen_at: 16:40` 为笔误，实际冻结 16:11；锚点链以文件 sha256 为准，
证明锚定后未改」。**写错也要留痕，但证据要能自证。**

---

## 八、自检（V1–V10）

```
python -m portal.premarket.selfcheck      # → 10/10
```

| 编号 | 覆盖 |
|---|---|
| V1 | JSON 抽取：纯 JSON / 代码块 / 前后有废话 / 空代码块必须报错 |
| V2 | 提议校验：趋势枚举、区间上下限、无数字依据、未知键丢弃 |
| V3 | 内核拒绝：缺 reason、价格不合理、触发条件无数字、超单只上限、当日超 8 只 |
| V4 | 裁决通过：四表真的写进去，且买入计划**不成交** |
| V5 | 开仓自动移出观察表；账户算术精确到分 |
| V6 | 平仓：已实现盈亏落流水，持仓行移除，重复平仓被拒 |
| V7 | 渲染：**12 节恒定**、零外部引用、XSS 转义 |
| V8 | 端到端（假 LLM）：plan + HTML + 锚点，拒绝项入报告 |
| V9 | LLM 失败 → 兜底且报告标注；`--no-fallback` 时必须失败 |
| V10 | 事件溯源：append-only、字段完整、重放一致 |

全部离线：LLM 用注入的假实现，价格用固定表，数据根在临时目录。

---

## 九、渲染时抓到的三个真 bug

不是读代码发现的，是**截图看出来的**：

1. **活跃度显示成 `<span class="dim">—</span>` 字面量** —— 已经是 HTML 的值被第二次转义
2. **龙虎榜同一只票出现 3 次**，数字一模一样 —— 一个上榜原因一行，已按代码合并取绝对值最大
3. **标题用了数据日**（看起来像"回顾昨天"）→ 改成预案日 + 「适用于 X 开盘 / 数据截至 Y」

---

## 十、还没做的

| 事项 | 原因 |
|---|---|
| 真实 LLM 路径 | 本机没有 API key，只用注入的假 LLM 验证了 LLM 之后的全链路 |
| 盘后复盘的中文报告 | `review` 已实现并写 JSON，但还没做 HTML 模板 |
| 盘前预案进 GitHub Action | 用户说后续再做；现在是主页按钮 + CLI |
| 节假日日历 | 预案日期用「下一个工作日」粗算，法定节假日会标错 |
| 盘中跟踪 | 目前只做盘前；盘中的动态调仓未实现 |

---

## 午间报告（12:00，独立模板）

### 为什么午报不能复用晚间那套

晚间报告回答「**明天怎么办**」，午报回答「**上午的走势，验证还是推翻了昨晚的判断？
下午要不要改？**」。所以午报是**纠偏**，不是预测。

| 维度 | 晚间报告 | 午间报告 |
|---|---|---|
| 时间尺度 | 全天，趋势可归纳 | 半日，趋势还不成立 |
| 核心问题 | 明天买什么、多少仓 | 昨晚预案还成立吗、下午怎么应对 |
| 量的判断 | 实测成交额 vs 5 日均 | 半日实测 + **2 倍线性外推**（标注为估计值） |
| 题材 | 5 日序列看发酵/兑现 | 昨日强势题材**今日上午是否延续** |
| 消息 | 全夜 + 全天 | 只 9:00–11:30，且判断是否已反映在价格里 |
| 复盘 | 今天做对/做错 | 不做复盘，只做「预案 vs 现实」核对 |
| 输出 | 次日买入计划 + 执行清单 | **午后条件分支**（若 X 则 Y，否则 Z） |
| 调仓额度 | 单只 ≤40%、合计 ≤100% | **单只 ≤10%、合计 ≤20%**（半日信息不足） |
| 经验表 | 可写 | **禁止写**（半天不足以沉淀规律） |

### 数据验证（2026-10-20 12:46 实测）

| 项 | 自算（批量行情 5224 只 / 14.2 秒） | 官方 `legu` 11:30 快照 | 差 |
|---|---|---|---|
| 上涨 | 3568 | 3568 | **0** |
| 下跌 | 1503 | 1503 | **0** |
| 平盘 | 153 | 146 | +7（= 官方「停牌 7」） |
| 半日成交额 | 9,334 亿 | 上证 4418 + 深证 4916 = 9,334 亿 | **0** |

**关键校准**：腾讯行情 `f[30]` 是**服务器刷新时间**（实测 12:05），不是最后成交时间 —
不能用它判断数据新鲜度。所以 `session_guard` 用两道闸：

1. 墙钟必须在 **11:25–13:15**
2. 官方 `legu` 的「统计日期」必须是**今天 11:30**

任一不满足直接抛 `SessionError` —— 宁可报错，也不许拿昨天的收盘假装午报。

### 版面（7 节）

一、上午概览（指数表 + 涨跌分布条 + 8 张卡 + 特征总结）
二、题材延续性（昨日全天 vs 今日上午，含「延续 / 熄火 / 新起」判断）
三、上午消息（9:00–11:30，筛选后 + 是否已反映在价格里）
四、**预案 vs 上午现实**（核心：逐条核对昨晚触发条件/止损/题材判断）
五、持仓上午表现
六、午后判断（条件分支 + 量能判断 + 盯什么 + 风险）
七、午后调仓（已按 11:30 价执行）

### 用法

```bash
python -m portal.premarket midday          # 只在午休窗口可用
# 或主页「一键午报」按钮（POST /premarket/midday）
```

实测一次：`预案核对 10 条、条件分支 3 条、报告 21 KB`。
---

## GitHub Action：每个交易日自动出报告 + 邮件送达

workflow 文件：[`.github/workflows/daily-reports.yml`](../.github/workflows/daily-reports.yml)

| 时间（CST） | cron（UTC） | 跑什么 |
|---|---|---|
| 12:05 | `5 4 * * 1-5` | 午间报告（半日行情 + 预案核对 + 午后判断） |
| 16:50 | `50 8 * * 1-5` | 晚间报告（全天汇总 + 调仓 + 次日计划 + 复盘） |

晚间放在 16:50 而不是 16:30，是为了等 `market-db.yml`（16:30）先把当日增量写完；
即使缓存没跟上，workflow 自己也会 `update-today` 补一次（幂等）。

### 发信：完整报告直接进正文，**不带附件**

用的是 [`dawidd6/action-send-mail@v3`](https://github.com/dawidd6/action-send-mail)：

```yaml
html_body: file://.../mail_body.html     # 正文 = 完整报告
# 没有 attachments
```

### 两种 HTML 的分工（重要）

| 产物 | 何时生成 | 用途 |
|---|---|---|
| 网页版 `*_premarket.html` / `*_midday.html` | **只在门户手动点按钮时** | 存档、可交互、好看 |
| 邮件正文 `mail_body.html` | 每次自动运行（`--mail`） | 直接发信，11 节 / 7 节全在 |

CI 传 `--no-html --mail <path>`，所以 **Action 里不会产出网页版 HTML 文件**。

### 为什么邮件版要单独渲染（`render_mail.py`）

邮件客户端不是浏览器，直接塞网页版会坏：

- Gmail 会剥掉部分 `<style>`
- Outlook 桌面版**完全不认 CSS 变量**（`var(--accent)` 失效 → 颜色全丢）
- `flex` / `grid` 全不支持，渐变背景经常丢失

所以邮件版是另一套写法：**零 `<style>`、全部内联、表格布局、固定 680px**。
内容与网页版一一对应（同一份 payload 渲染），不会出现「邮件和报告不一样」。

实测体积：午报 **16 KB / 7 节**，晚间 **62 KB / 11 节**。
Gmail 在 ~102 KB 处显示「消息已剪裁」（正文不丢，可点开全文），
`render_mail.MAX_BYTES` 只做告警、**不阻断发信** —— 报错等于今天一封邮件都不发。

### 需要配置的 Repository secrets

| Secret | 必填 | 说明 |
|---|---|---|
| `EMAIL_SENDER` | ✅ | 发件邮箱 |
| `EMAIL_PASSWORD` | ✅ | 邮箱**授权码**（不是登录密码；QQ/163 需在网页端开启 SMTP 并生成授权码） |
| `EMAIL_RECEIVERS` | ✅ | 收件人，逗号分隔 |
| `EMAIL_SMTP_HOST` | — | 默认 `smtp.qq.com`（163 用 `smtp.163.com`，Gmail 用 `smtp.gmail.com` + 端口 587） |
| `EMAIL_SMTP_PORT` | — | 默认 `465` |
| `DEEPSEEK_API_KEY` | — | 不配则报告降级为「规则兜底」，报告顶部会标注 |
| `OPENAI_API_KEY` / `LITELLM_MODEL` | — | 同上，三者配任一即可 |

**secrets 缺失时不发信，但也不让 job 变红**：workflow 会打一条 `::error::` 标注并跳过发信，数据与邮件正文仍作为 artifact 上传。

### 节假日的处理

- 晚间：先看「库里最新交易日 == 今天」；不等就整份跳过（不发信、不失败）
- 午间：`session_guard` 会核对官方 `legu` 快照是不是**今天 11:30**，不是就抛
  `SessionError` 并跳过

### 手动触发

Actions 页面 → `daily-reports` → Run workflow → 选 `auto` / `midday` / `evening`。

### 缓存策略（别踩的坑）

- 数据库由 `market-db.yml` 负责写入与缓存维护
- 报告 workflow 只用 `actions/cache/restore@v4`（**不 save**）——
  否则每次未命中都会另存一份 1.9 GB，几次就把仓库 10 GB 缓存配额吃光

---

## 因子库：新增一个因子，下次报告就会用它

### 目录

```
portal/premarket/factors/
    base.py            Panel / Factor / 表达式白名单
    f_*.py             一个因子一个文件（现有 12 个）
    playbooks.py       内置战法（= 因子的布尔组合）
    validate.py        统计检验（IC / IR / 多空价差 / 覆盖率）
    __main__.py        CLI：list / playbooks / check / eval / hit / validate
    discovered/        （预留）agent 讨论产出的因子
```

### 只加因子（不进战法）

新建 `factors/f_xxx.py`：

```python
META = {
    "id": "turnover_rank", "name": "换手排名", "kind": "numeric",
    "unit": "%", "description": "……", "source": "builtin",
    "direction": "high",
}

def compute(panel) -> dict:
    return {c: ... for c in panel.codes()}
```

放进去就会被自动发现：`python -m portal.premarket.factors list` 立刻能看到。
但**不会**影响选股 —— 因为还没被任何战法引用。

### 加因子 + 顺带加战法（下次报告即生效）

在同一个文件里加 `PLAYS`：

```python
PLAYS = [{
    "id": "strong_close", "name": "强势收盘（尾盘走强）", "buyable": True,
    "why": "温和上涨且收在当日振幅上部，尾盘有承接",
    "conditions": ["close_pos >= 80.0", "chg_today >= 1.0",
                   "chg_today < 7.0", "vol_ratio >= 1.10",
                   "above_ma20 == 1", "amount_yi >= 2.0"],
    "evidence": ["close_pos", "chg_today", "vol_ratio", "above_ma20", "amount_yi"],
    "sort": [["close_pos", "desc"], ["amount_yi", "desc"]],
}]
```

注册表会把内置战法与这些 `PLAYS` 合并，**午报和晚报共用同一个 `screen()`**，
所以下一份报告就会用新战法筛候选股，报告里也会多出一个战法分区。

### 安全边界

- 条件表达式由白名单 AST 求值：无属性访问、无 import、只放行
  `abs/min/max/round/int/float/bool`。`__import__` / `eval` / `open` /
  `x.__class__` / `x[0]` / `lambda` 全部拒绝（V14 钉住）。
- 可买战法**不得**引用涨停/追高类条件（`check_no_chase`），否则加载即报错。
- 涨停池始终是 `buyable=False`，只能进观察表 —— 由内核拒绝买入。

### 验证命令

```bash
python -m portal.premarket.factors list        # 因子清单
python -m portal.premarket.factors playbooks   # 战法清单（标明「因子文件贡献」）
python -m portal.premarket.factors check       # 自检：加载/编译/当日命中
python -m portal.premarket.factors eval        # 当日因子截面（看取值是否合理）
python -m portal.premarket.factors validate --days 20   # 统计检验（慢，离线）
```

`check` 是最常用的：一条命令就能看到「因子是否加载、战法是否编译、
今天每个战法命中几只」。

### 缓存注意

候选池结构变化时**必须**把 `premarket/collect.py` 的 `CACHE_SCHEMA` 加 1，
否则旧缓存会被静默复用（踩过两次）。另外：任一数据源失败、或战法没跑出来，
采集结果**不会**写缓存 —— 避免一次失败被永久固化。
