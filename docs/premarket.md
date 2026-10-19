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
