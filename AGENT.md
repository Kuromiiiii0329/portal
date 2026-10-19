# Portal AI Agent 说明文档

> 本文档记录 portal 集成的 multi-agent 能力：**作用、行为准则、旗下工具**。
> 集成实施记录见 `AGENT_INTEGRATION_PROGRESS.md`；架构方案见 `../../../.claude/plans/twinkly-twirling-quilt.md`。

---

## 一、概述：portal 从"固定管道"升级为"管道 + Agent"双模式

portal 原本是一个**固定分析流水线**：拉 K 线 → 跑 `analyzers/`（技术/基本面/产业链指标计算）→ 加权 merge → 生成报告，LLM 仅被当作"写点评的笔"（一次性文本函数），**不是 agent**。

现在集成了 `portal/lib/src/agent/` 里已有的 **multi-agent 框架**（ReAct Orchestrator + 四个专用子 Agent + 18 个工具），portal 新增两种 **真正的 agent 能力**：

| 场景 | 入口 | 机制 | 说明 |
|------|------|------|------|
| **AI 综合研判** | 「立即运行」Tab 勾选「🤖 Agent 综合研判」 | **选项 B**：把已算好的全部指标喂给**单次 LLM** | 复用 portal 已算好的均线/背离/量能等数据，**零重复取数**，快、稳、省。不走 multi-agent 框架。 |
| **AI 对话** | 「🤖 AI 对话」Tab | **选项 A**：走 `AgentOrchestrator` 四子 agent | 多轮对话、按需调工具、多专家视角。适合探索式追问。 |

**设计理由**：固定管道已经精确计算了技术指标，深度分析研判只需 LLM 综合（选项 B 最忠实"复用已算数据"）；而开放式对话才需要 agent 的自主编排与多轮能力（选项 A）。

---

## 二、Agent 作用与角色（四子 Agent 架构）

参考「ReAct Orchestrator + 专用子 Agent 混合架构」方案。

### 顶层：ReAct Orchestrator（调度层）
- 无业务执行能力，只负责思考、判断、调度、聚合输出。
- 拥有「调用四大子 Agent」的能力，可单环节调用、多环节串联、或全流程闭环。
- 自动识别用户指令类型，触发三种执行模式（见下）。

### 底层：四个垂直专用子 Agent（执行层，工具/Prompt 隔离）

| 子 Agent | agent_name | 职责 | 可用工具（`tool_names`） |
|----------|-----------|------|------------------------|
| **技术面** | `technical` | K线形态、量价、均线/MACD/KDJ、支撑压力位、趋势结构；输出 signal + key_levels（support/resistance/stop_loss）+ trend_score 的 JSON | get_realtime_quote, get_daily_history, analyze_trend, calculate_ma, get_volume_analysis, analyze_pattern, get_chip_distribution, get_analysis_context |
| **产业情报** | `intel` | 新闻/公告/情绪、主力资金流、行业催化剂与风险识别 | search_stock_news, search_comprehensive_intel, get_stock_info, get_capital_flow |
| **风险** | `risk` | 强制风险筛查（内幕/业绩/监管/估值/解禁/流动性/舆情）；可产出 `veto_buy`（否决买入）/ signal_adjustment | search_stock_news, get_realtime_quote, get_stock_info |
| **决策** | `decision` | **无工具**，纯综合前序各方意见 + 风险标记，产出最终 Decision Dashboard（analysis 模式）或自然语言回答（chat 模式） | `[]`（无工具） |

---

## 三、行为准则 / 执行模式

### 四种编排模式（`AGENT_ORCHESTRATOR_MODE`，链路见 `lib/src/agent/orchestrator.py`）

| 模式 | 子 agent 链路 | 适用 |
|------|--------------|------|
| `quick` | technical → decision | 单点快答，最低开销 |
| `standard`（**默认**）| technical → intel → decision | 常规问答，速度/质量平衡 |
| `full` | technical → intel → risk → decision | 完整研判，含风险校验 |
| `specialist` | full 链 + decision 前插入 SkillAgent | 带交易技能评估 |

portal 默认用 `standard`（`_apply_agent_env` 里 setdefault）。

### 三种执行流程（Orchestrator 自动识别）

1. **轻量单点问答**："帮我看这只票的技术走势" → 只调 technical 子 agent，简单整理后应答。
2. **局部多维度分析**："结合技术面和风险看看" → 按需串联调用对应子 agent，聚合输出。
3. **标准全链路投研**："完整分析这只股" → 强制 Technical→Intel→Risk→Decision 串行，可溯源。

### 硬性规则（防失控）

1. Decision 子 agent **不调工具**，只综合前序标准化结果。
2. Risk 子 agent 发现高危时可 **否决买入（veto_buy）**，对冲乐观偏差。
3. 子 Agent 只执行、不自主调度；所有流程跳转/终止权限归 Orchestrator。
4. 所有子 Agent 输出标准化格式，便于顶层聚合。
5. 注入 `analysis_context_pack_summary` 时，子 agent 优先复用已提供数据，不必重复取数。

---

## 四、旗下工具清单（18 个）

工具经 `factory.py` 的 `get_tool_registry()` 一次性注册，所有子 agent 从中按 `tool_names` 取用。工具定义在 `lib/src/agent/tools/`，handler 返回 dict。

### 数据类（data）
| 工具 | 作用 |
|------|------|
| `get_realtime_quote` | 实时行情（价格、涨跌幅、量） |
| `get_daily_history` | 日线 OHLCV 历史 |
| `get_chip_distribution` | 筹码分布分析 |
| `get_analysis_context` | 从 DB 取历史分析上下文（portal stub 下返回空） |
| `get_stock_info` | 个股基本面信息（估值、成长、财务要点） |
| `get_capital_flow` | 主力（主力）资金流 |
| `get_portfolio_snapshot` | 组合快照 + 可选风险块 |

### 分析类（analysis）
| 工具 | 作用 |
|------|------|
| `analyze_trend` | 综合技术趋势分析（MA 排列/MACD/RSI） |
| `calculate_ma` | 计算移动均线（MA5/10/20/30/60/120/250） |
| `get_volume_analysis` | 量价关系分析 |
| `analyze_pattern` | K线/图形形态识别 |

### 搜索类（search）
| 工具 | 作用 |
|------|------|
| `search_stock_news` | 个股最新新闻 |
| `search_comprehensive_intel` | 多维情报搜索（新闻/情绪/催化剂等） |

### 市场类（market）
| 工具 | 作用 |
|------|------|
| `get_market_indices` | 主要指数（上证/深证/创业板等） |
| `get_sector_rankings` | 板块/行业表现排名 |

### 回测类（category 标注为 data）
| 工具 | 作用 |
|------|------|
| `get_stock_backtest_summary` | 个股回测表现 |
| `get_skill_backtest_summary` | 特定交易技能回测数据 |
| `get_strategy_backtest_summary` | 整体策略回测（legacy alias） |

> 实际工具总数以 `len(get_tool_registry()._tools)` 为准（当前 = **18**）。

---

## 五、配置与环境变量

agent 框架经 `get_config()` 读环境变量。portal 复用现有 `HAI_*` 网关，`server.py` 的 `_apply_agent_env()` 用 `os.environ.setdefault` 做映射（不覆盖用户显式设置）：

| portal 现有（HAI 网关） | 映射到 agent 框架 | 说明 |
|------------------------|-------------------|------|
| `HAI_API_KEY` | `OPENAI_API_KEY` | OpenAI 兼容网关密钥 |
| `HAI_BASE_URL` | `OPENAI_BASE_URL` | 网关地址 |
| `HAI_MODEL`（默认 gpt-4.1） | `LITELLM_MODEL = openai/<model>` | 模型 |
| — | `AGENT_ARCH = multi` | 启用 AgentOrchestrator |
| — | `AGENT_ORCHESTRATOR_MODE = standard` | 默认编排模式 |
| — | `AGENT_MAX_STEPS = 10` | 单 agent 最大步数 |

---

## 六、依赖补齐说明（stub）

agent 框架顶层 import 了 `src.storage` / `src.notification`，portal/lib/src 原本没有这两个文件。为保持 portal「轻量自包含」（不引入 sqlalchemy / bot.models 等重依赖），补了**轻量内存 stub**：

- `portal/lib/src/storage.py`：`DatabaseManager` 用进程内 dict 实现对话历史（支撑多轮 AI 对话），其余落库/分析历史/provider-trace 方法均 no-op；`get_analysis_context` 返回 None。带 `__getattr__` 兜底防未覆盖方法报错。
- `portal/lib/src/notification.py`：`NotificationService.send` no-op 返回 True，`NotificationBuilder` 占位。

**局限**：对话历史 / 分析历史**不持久化**（server 重启即丢失），不写数据库，不发通知。若需真持久化，可替换为主项目 `daily/src/storage.py` 的真实实现（需额外装 sqlalchemy 并处理其依赖）。

---

## 七、API 与使用

| 端点 | 方法 | 说明 |
|------|------|------|
| `/chat` | POST | `{message, session_id, stock_code?, stock_name?}` → `{ok, task_id, session_id}`，随后用 `/run/stream/<task_id>` 收 SSE，`/run/report/<task_id>` 取最终回答 |
| `/analyze` | POST | 原有深度分析，新增可选字段 `agent_review: true` 触发「AI 综合研判」，结果写入 `final_report["agent_review"]` |

- session_id 由前端生成并持久化到 `localStorage['dsa_chat_session']`，保证多轮连续。
- 未启用 agent（`agent_review` 默认 false）时，portal 行为与集成前完全一致。

---

## 八、回归保护

- `agent_review` 默认 `false`；`AGENT_*` 用 `setdefault`；`/chat` 为独立端点。
- 不改动现有 `/analyze` / `/run` / `/market_review` 行为。未启用 agent 时 portal 与集成前一致。
