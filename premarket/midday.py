# -*- coding: utf-8 -*-
"""
portal/premarket/midday.py — 午间报告（半日行情 + 午后判断）

为什么午报不能复用晚间那套
--------------------------
晚间报告回答的是「**明天怎么办**」：全交易日汇总 → 定次日仓位与标的。
午报回答的是完全不同的问题：

    「上午的走势，验证还是推翻了昨晚的判断？下午要不要改？」

所以午报的重心是**纠偏**，不是预测：

| 维度 | 晚间报告 | 午间报告 |
|---|---|---|
| 时间尺度 | 全天，趋势可归纳 | 半日，趋势还不成立 |
| 核心问题 | 明天买什么、多少仓 | 昨晚的预案还成立吗、下午怎么应对 |
| 量的判断 | 实测成交额 vs 5 日均 | 半日实测 + **线性外推**（必须标注是外推） |
| 题材 | 5 日序列看发酵/兑现 | 昨日强势题材**今日上午是否延续** |
| 消息 | 全夜 + 全天 | 只 9:00–11:30，并且要判断「是否已反映在价格里」 |
| 复盘 | 今天做对/做错 | 不做复盘，只做「预案 vs 现实」核对 |
| 输出 | 次日买入计划 + 执行清单 | **午后条件分支**（若 X 则 Y），不预测点位 |
| 调仓 | 正常额度（单只 ≤40%、总 ≤100%） | **更保守**：单只 ≤10%、当日买入合计 ≤20% |
| 经验表 | 可写 | **禁止写**（半日结论不足以沉淀经验） |

数据验证（2026-10-20 12:46 实测）
--------------------------------
- 批量行情 5224 只 **14.2 秒**拿到半日数据
- 自算涨跌家数 3568/1503 与官方 `legu` 11:30 快照**完全一致**；平盘差 7 只 = 官方"停牌 7"
- 半日成交额 9,334 亿与新浪指数（上证 4418 + 深证 4916）**完全一致**
- 腾讯 `f[30]` 是**服务器刷新时间**（12:05），不是最后成交时间 —— **不能**用它判断数据新鲜度，
  必须用 `legu` 的「统计日期」或墙钟。这是本模块 `session_guard` 的设计依据。
"""
from __future__ import annotations

import datetime
import json
from typing import Any, Callable

from . import account as account_mod
from . import agent as agent_mod
from . import apply as apply_mod
from . import collect as collect_mod
from . import paths
from . import render_mid_day
from .store import Tables

#: 午休时间窗（可生成午报的时段）。开盘前/收盘后都不该生成"午报"。
LUNCH_START = "1125"
LUNCH_END = "1315"

#: 午报调仓额度（比晚间保守：半日信息不足以支撑大决策）
MIDDAY_MAX_SINGLE_PCT = 10.0
MIDDAY_MAX_TOTAL_BUY_PCT = 20.0
#: 午报观察表当日最多新增
MIDDAY_MAX_POOL_ADDS = 3


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


class SessionError(RuntimeError):
    """数据不在午休窗口内 —— 宁可报错，也不许用昨天的收盘假装午报。"""


def session_guard(*, now: datetime.datetime | None = None,
                  legu_date: str | None = None) -> dict[str, Any]:
    """校验「现在能不能出午报」。

    两道闸：
    1. **墙钟**必须在 11:25–13:15（留 5 分钟容错）
    2. 官方 `legu` 的「统计日期」必须是**今天 11:30** ——
       这是唯一可信的新鲜度证据（腾讯 f[30] 是刷新时间，不能作数）

    返回 {"ok": True, "trade_date":..., "session": "lunch"} 或抛 SessionError。
    """
    now = now or datetime.datetime.now()
    hhmm = now.strftime("%H%M")
    today = now.strftime("%Y-%m-%d")

    if now.weekday() >= 5:
        raise SessionError(f"今天是{'周六' if now.weekday() == 5 else '周日'}，不生成午报")
    if not (LUNCH_START <= hhmm <= LUNCH_END):
        raise SessionError(
            f"当前 {hhmm} 不在午休窗口 {LUNCH_START}–{LUNCH_END}。"
            f"午报只反映上午 11:30 的半日数据；盘前/收盘后请用晚间报告。")

    out = {"ok": True, "trade_date": today, "session": "lunch", "hhmm": hhmm}
    if legu_date:
        # legu 形如 "2026-10-20 11:30:00"
        d = str(legu_date).split()[0]
        t = str(legu_date).split()[-1][:5] if " " in str(legu_date) else ""
        out["legu_date"] = legu_date
        if d != today:
            raise SessionError(
                f"官方宽度快照的统计日期是 {d}，不是今天 {today} —— "
                f"数据没更新，拒绝生成午报（否则会用旧数据假装半日行情）。")
        if t and t not in ("11:30",):
            raise SessionError(
                f"官方宽度快照的时间是 {t}，不是 11:30 —— 数据不是上午收盘快照。")
    return out


# ─────────────────────────────────────────────────────────────
# 采集（半日口径）
# ─────────────────────────────────────────────────────────────
def live_market_stats(quotes: list[dict]) -> dict[str, Any]:
    """用批量行情算半日涨跌分布与成交额（数据库里没有今天的数据）。"""
    buckets = {"<-7%": 0, "-7~-5%": 0, "-5~-2%": 0, "-2~0%": 0,
               "0~2%": 0, "2~5%": 0, "5~7%": 0, ">7%": 0}
    up = down = flat = 0
    amount = 0.0
    for x in quotes:
        p, c = x.get("prev_close"), x.get("close")
        amount += x.get("amount") or 0
        if not p or not c:
            continue
        pct = (c / p - 1) * 100
        up += 1 if pct > 0 else (0 if pct == 0 else 0)
        if pct > 0:
            pass
        elif pct < 0:
            down += 1
        else:
            flat += 1
        if pct >= 7:
            buckets[">7%"] += 1
        elif pct >= 5:
            buckets["5~7%"] += 1
        elif pct >= 2:
            buckets["2~5%"] += 1
        elif pct >= 0:
            buckets["0~2%"] += 1
        elif pct >= -2:
            buckets["-2~0%"] += 1
        elif pct >= -5:
            buckets["-5~-2%"] += 1
        elif pct >= -7:
            buckets["-7~-5%"] += 1
        else:
            buckets["<-7%"] += 1
    # up 计数修正（上面为了简洁写成 pass）
    up = sum(buckets[k] for k in ("0~2%", "2~5%", "5~7%", ">7%"))
    total = up + down + flat
    return {
        "n_total": total, "n_up": up, "n_down": down, "n_flat": flat,
        "up_ratio_pct": round(up / total * 100, 2) if total else None,
        "distribution": buckets,
        "amount_yi": round(amount / 1e8, 1),
        "source": "腾讯批量行情（半日实测）",
    }


def collect_midday(*, progress: Callable[[str], None] = print,
                   use_cache: bool = False) -> dict[str, Any]:
    """午报专用采集。复用晚间的大部分块，但把「宽度/成交额/候选池」换成实时口径。"""
    from portal.virtual import marketdb as M

    now = datetime.datetime.now()
    d8 = now.strftime("%Y%m%d")
    out: dict[str, Any] = {
        "date": d8, "collected_at": _now(), "session": "lunch",
        "errors": [], "caveats": [],
    }

    # 1) 官方宽度快照（同时提供「统计日期」用于 session_guard）
    legu_date = None
    try:
        import akshare as ak
        rows = collect_mod._rows(ak.stock_market_activity_legu())
        kv = {str(r.get("item")).strip(): r.get("value") for r in rows}
        legu_date = str(kv.get("统计日期") or "")
        out["breadth"] = {
            "up": int(collect_mod._f(kv.get("上涨")) or 0),
            "down": int(collect_mod._f(kv.get("下跌")) or 0),
            "flat": int(collect_mod._f(kv.get("平盘")) or 0),
            "halt": int(collect_mod._f(kv.get("停牌")) or 0),
            "limit_up": int(collect_mod._f(kv.get("涨停")) or 0),
            "limit_down": int(collect_mod._f(kv.get("跌停")) or 0),
            "activity": kv.get("活跃度"),
            "as_of": legu_date,
            "source": "stock_market_activity_legu（11:30 快照）",
        }
    except Exception as exc:
        collect_mod._err(out, "breadth_legu", exc)

    # 2) 全市场半日行情（批量）
    quotes: list[dict] = []
    try:
        conn = M.connect()
        codes = [c for c in M.universe(conn) if M.board_of(c) != "bse"] if conn else []
        if conn:
            conn.close()
        if not codes:
            # CI 首次运行可能还没有 market.db。这时退回到 akshare 的代码表，
            # 否则午报会因为拿不到代码列表而整份作废（数据其实完全够用）。
            import akshare as ak
            codes = [str(r.get("code")).strip()
                     for r in collect_mod._rows(ak.stock_info_a_code_name())
                     if str(r.get("code") or "").strip()]
            out.setdefault("caveats", []).append(
                "本地 market.db 不可用，股票列表改从 akshare 获取（不影响半日行情）。")
            if progress:
                progress(f"  market.db 缺失，改用 akshare 代码表 {len(codes)} 只")
        if progress:
            progress(f"  批量取 {len(codes)} 只半日行情…")
        for i in range(0, len(codes), 50):
            quotes.extend(M.fetch_quotes_batch(codes[i:i + 50]))
    except Exception as exc:
        collect_mod._err(out, "batch_quotes", exc)

    out["market_stats"] = live_market_stats(quotes)

    # 3) 指数（实时 = 上午收盘）
    try:
        import akshare as ak
        spot = collect_mod._rows(ak.stock_zh_index_spot_sina())
        by = {str(r.get("代码")): r for r in spot}
        idx = []
        for code, name in (("sh000001", "上证指数"), ("sz399001", "深证成指"),
                           ("sz399006", "创业板指"), ("sh000688", "科创50")):
            r = by.get(code)
            if not r:
                continue
            idx.append({
                "code": code, "name": name,
                "close": collect_mod._num(r.get("最新价"), 2),
                "chg_pct": collect_mod._num(r.get("涨跌幅"), 3),
                "open": collect_mod._num(r.get("今开"), 2),
                "high": collect_mod._num(r.get("最高"), 2),
                "low": collect_mod._num(r.get("最低"), 2),
                "amount_yi": round(collect_mod._f(r.get("成交额"), 0) / 1e8, 1),
                "source": "stock_zh_index_spot_sina（实时=上午收盘）",
            })
        out["indices"] = idx
    except Exception as exc:
        collect_mod._err(out, "indices", exc)

    # 4) 池子（午间快照）
    try:
        import akshare as ak
        zt = collect_mod._rows(ak.stock_zt_pool_em(date=d8))
        zb = collect_mod._rows(ak.stock_zt_pool_zbgc_em(date=d8))
        out["pools"] = {"zt": len(zt), "zb": len(zb)}
        boards = [int(collect_mod._f(r.get("连板数")) or 1) for r in zt]
        out["pools"]["max_boards"] = max(boards) if boards else 0
        out["pools"]["break_rate_pct"] = round(
            len(zb) / (len(zt) + len(zb)) * 100, 2) if (zt or zb) else None

        # 题材：今日半天 vs 昨日全天
        today_ind: dict[str, int] = {}
        for r in zt:
            k = str(r.get("所属行业") or "未分类").strip()
            today_ind[k] = today_ind.get(k, 0) + 1
        y = (now - datetime.timedelta(days=1))
        while y.weekday() >= 5:
            y -= datetime.timedelta(days=1)
        yest_ind: dict[str, int] = {}
        try:
            for r in collect_mod._rows(ak.stock_zt_pool_em(date=y.strftime("%Y%m%d"))):
                k = str(r.get("所属行业") or "未分类").strip()
                yest_ind[k] = yest_ind.get(k, 0) + 1
        except Exception:
            pass
        themes = []
        for k in set(today_ind) | set(yest_ind):
            t, ys = today_ind.get(k, 0), yest_ind.get(k, 0)
            themes.append({"name": k, "today_n": t, "yesterday_n": ys,
                           "delta": t - ys})
        themes.sort(key=lambda x: (-x["today_n"], -x["yesterday_n"]))
        out["themes"] = themes[:15]
        out["themes_note"] = "今日=上午半天涨停家数；昨日=全天，对比时注意口径不同"

        # 候选池（带价，供午后调仓与追高拦截）
        cand = {}
        for r in zt:
            code = str(r.get("代码") or "")
            px = collect_mod._f(r.get("最新价"))
            if code and px:
                cand[code] = {"code": code, "name": r.get("名称"),
                              "close": px, "chg_pct": collect_mod._num(r.get("涨跌幅"), 2),
                              "limit_up": px,
                              "boards": int(collect_mod._f(r.get("连板数")) or 1),
                              "industry": str(r.get("所属行业") or ""),
                              "sources": ["涨停"]}
        for q in quotes:
            if q["code"] in cand or not q.get("close"):
                continue
            if (q.get("chg_pct") or 0) >= 5:
                cand[q["code"]] = {
                    "code": q["code"], "name": q.get("name"),
                    "close": q["close"], "chg_pct": q.get("chg_pct"),
                    "prev_close": q.get("prev_close"),
                    "limit_up": None, "boards": None, "industry": None,
                    "sources": ["涨幅榜"]}
        out["candidates"] = sorted(
            cand.values(), key=lambda x: (-(x.get("boards") or 0),
                                          -(x.get("chg_pct") or 0)))[:40]
        out["candidates_note"] = "上午涨停池 + 上午涨幅≥5% 的票（带价）"
    except Exception as exc:
        collect_mod._err(out, "pools", exc)

    # 5) 上午新闻（9:00–11:30）
    try:
        import akshare as ak
        items = []
        for r in collect_mod._rows(ak.stock_info_global_cls(symbol="全部")):
            items.append({"title": str(r.get("标题") or "").strip(),
                          "content": str(r.get("内容") or "").strip(),
                          "time": f"{r.get('发布日期','')} {r.get('发布时间','')}".strip(),
                          "src": "财联社"})
        for r in collect_mod._rows(ak.stock_info_global_em()):
            items.append({"title": str(r.get("标题") or "").strip(),
                          "content": str(r.get("摘要") or "").strip(),
                          "time": str(r.get("发布时间") or "").strip(),
                          "src": "东方财富"})
        lo = now.replace(hour=9, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")
        hi = now.strftime("%Y-%m-%d %H:%M")
        kept, seen = [], set()
        for it in items:
            t = it["time"][:16]
            if not (lo <= t <= hi):
                continue
            key = (it["title"] or it["content"])[:24]
            if not key or key in seen:
                continue
            seen.add(key)
            kept.append(it)
        kept.sort(key=lambda x: x["time"], reverse=True)
        out["news"] = kept[:30]
        out["news_window"] = f"{lo} ~ {hi}"
    except Exception as exc:
        collect_mod._err(out, "news", exc)

    out["session_info"] = {
        "now": now.strftime("%Y-%m-%d %H:%M:%S"),
        "legu_as_of": legu_date,
        "caveats": [
            "成交额为上午半日实测；与全日比较时按 2 倍线性外推，属**估计值**。",
            "今日涨停家数是截至 11:30 的快照，收盘可能变化。",
        ],
    }
    return out


# ─────────────────────────────────────────────────────────────
# 提议（午报专用 prompt 与结构）
# ─────────────────────────────────────────────────────────────
MIDDAY_SCHEMA = """{
  "morning_view": {
    "tone": "偏强|偏弱|分化",              // 上午盘面性质
    "summary": "两句话说清上午发生了什么",
    "reasons": ["必须引用上午的具体数字"]
  },
  "plan_check": [                          // ★ 午报核心：昨晚预案 vs 上午现实
    {"item":"触发条件/止损位/题材判断","expected":"昨晚怎么说的",
     "observed":"上午实际如何","verdict":"成立|推翻|待观察","action":"要不要改"}
  ],
  "sector_check": [                        // 昨日强势题材今日上午的延续性
    {"name":"题材名","yesterday_n":昨日涨停数,"today_n":上午涨停数,
     "verdict":"延续|熄火|新起","note":"依据"}
  ],
  "news_analysis": [                       // 只保留 9:00-11:30，且要判断是否已反映在价格里
    {"title":"≤30字","importance":"高|中|低","impact":"利好|利空|中性",
     "summary":"本质","affected_themes":["..."],"affected_stocks":["..."],
     "reasoning":"传导逻辑"}
  ],
  "afternoon_view": {
    "bias": "偏强|偏弱|震荡",
    "conditions": [                        // ★ 条件分支，不是点位预测
      {"if":"若上证站回 3737（MA5）且成交额午后放大",
       "then":"可持有现有仓位不动","else_note":"否则减仓一半"}],
    "volume_projection": "半日成交额外推与昨日对比的判断",
    "risk_level": "高|中|低"
  },
  "position_plan": {
    "buy":    [{"code":"600000","name":"某股","alloc_pct":8,"reason":"为什么现在买",
                "stop_loss":10.5,"take_profit":13.0,"logic":"逻辑"}],
    "sell":   [{"code":"600000","reason":"为什么卖","all":true}],
    "adjust": [{"code":"600000","reason":"调止损/止盈","stop_loss":10.8}]
  },
  "pool_actions": {"add":[], "update":[], "remove":[]},
  "info_actions": {"add":[], "remove":[]},
  "watch_afternoon": ["午后要盯的东西1"],
  "risk_notes": ["风险1"]
}"""

MIDDAY_ROLE = """你是一名严谨的 A 股策略交易研究员，现在做**午间报告**（12:00，上午已收盘）。

你和晚上的报告分工不同，请严格遵守：
- 晚间报告回答「明天怎么办」：全交易日汇总 + 次日计划。
- 你现在回答「**上午的走势，验证还是推翻了昨晚的判断？下午要不要改？**」
  你的重心是**纠偏**，不是预测。

规则：
1. 只输出 JSON，不要任何解释文字。
2. 因为只有半天数据，**趋势判断要保守**：不要用半天走势下"趋势反转"的结论。
3. 不预测点位。要给出**条件分支**（若 X 则 Y，否则 Z），让盘中可以机械执行。
4. plan_check 是核心：把昨晚预案里的触发条件、止损位、题材判断逐条拿来和上午实际对照，
   说清是"成立/推翻/待观察"，并给出"要不要改"。
5. 消息只看 9:00–11:30 的，并且要判断「是否已经反映在上午的价格里」——
   已经涨完的利好，价值远低于刚出的。
6. 风险与仓位都要比晚上更保守。"""


def build_midday_prompt(*, market: dict, account: dict, snapshot: dict,
                        last_plan: dict | None) -> str:
    def dump(o) -> str:
        return json.dumps(o, ensure_ascii=False, indent=1)

    pos = [{k: p.get(k) for k in
            ("code", "name", "entry_price", "last_price", "unrealized_pct",
             "weight_pct", "stop_loss", "take_profit", "entry_logic")}
           for p in account.get("positions") or []]
    plan_view = {}
    if last_plan:
        plan_view = {
            "plan_date": last_plan.get("plan_date"),
            "market_view": (last_plan.get("proposal") or {}).get("market_view"),
            "planned_buys": [{k: b.get(k) for k in
                              ("code", "name", "alloc_pct", "trigger",
                               "stop_loss", "take_profit")}
                             for b in (last_plan.get("planned") or {}).get("buy") or []],
            "executed": [{k: t.get(k) for k in ("action", "code", "price", "alloc_pct")}
                         for t in last_plan.get("executed_now") or []],
            "themes": [{k: s.get(k) for k in ("name", "strength", "sustainability")}
                       for s in (last_plan.get("proposal") or {}).get("sector_priority") or []],
            "checklist": (last_plan.get("proposal") or {}).get("execution_checklist"),
        }

    return f"""{MIDDAY_ROLE}

──────────────────────────────
【一、上午行情（程序采集，半日实测）】
{dump({k: v for k, v in market.items() if k != "candidates"})}

【二、候选池（午后只能从这里买，带上午价格）】
{dump(market.get("candidates") or []) if market.get("candidates") else "（无）"}
说明：{market.get("candidates_note") or ""}

【三、当前账户与持仓（程序计算，不计费用）】
{dump({k: account.get(k) for k in
       ("total_pct", "cash_pct", "market_value_pct", "position_ratio_pct",
        "cum_pnl_ratio_pct", "n_positions")})}
{dump(pos) if pos else "（空仓）"}

【四、昨晚的预案（要逐条核对是否仍然成立）】
{dump(plan_view) if plan_view else "（没有昨晚的预案，plan_check 可留空数组）"}

【五、选股观察表】
{dump([{k: r.get(k) for k in ("code", "name", "tier", "trigger", "stop_loss", "take_profit")}
       for r in (snapshot.get("pool") or []) if r.get("status") != "removed"])}

──────────────────────────────
【硬约束（违反会被程序拒绝）】
- **午后买入更保守**：单只 alloc_pct ≤ {MIDDAY_MAX_SINGLE_PCT:.0f}%，
  当日买入合计 ≤ {MIDDAY_MAX_TOTAL_BUY_PCT:.0f}%（半日信息不足以支撑大决策）。
- **禁止追高打板**：上午已涨停或上午涨幅 ≥ 7% 的票，内核直接拒绝买入。
- 选股只能来自候选池。
- **不许写交易经验**：半天数据不足以沉淀规律，经验表只在晚间复盘写。
- 观察表当日最多新增 {MIDDAY_MAX_POOL_ADDS} 只。
- 不计交易费用。
- 消息只保留 9:00–11:30 的，且要判断是否已被上午价格反映。

【输出要求】
只输出一个 JSON 对象，结构如下：
{MIDDAY_SCHEMA}
"""


def validate_midday(payload: dict) -> dict:
    """校验午报结构。比晚间宽松（允许 plan_check 为空），但字段必须合法。"""
    if not isinstance(payload, dict):
        raise agent_mod.AgentOutputError("顶层必须是 JSON 对象")

    mv = payload.get("morning_view") or {}
    if not isinstance(mv, dict):
        raise agent_mod.AgentOutputError("morning_view 必须是对象")
    tone = str(mv.get("tone", "")).strip()
    if tone not in ("偏强", "偏弱", "分化"):
        raise agent_mod.AgentOutputError(
            f"morning_view.tone 只能是 偏强/偏弱/分化，得到 {tone!r}")
    reasons = [str(x).strip()[:140] for x in (mv.get("reasons") or [])][:6]
    if not reasons:
        raise agent_mod.AgentOutputError("morning_view.reasons 不能为空")
    if not any(any(c.isdigit() for c in r) for r in reasons):
        raise agent_mod.AgentOutputError(
            "morning_view.reasons 至少要有一条含具体数字（引用上午涨跌幅/家数/成交额）")

    out: dict[str, Any] = {
        "morning_view": {
            "tone": tone,
            "summary": str(mv.get("summary", "")).strip()[:240],
            "reasons": reasons,
        },
        "plan_check": [], "sector_check": [], "news_analysis": [],
        "afternoon_view": {"bias": "震荡", "conditions": [],
                           "volume_projection": "", "risk_level": "中"},
        "position_plan": {"buy": [], "sell": [], "adjust": []},
        "pool_actions": {"add": [], "update": [], "remove": []},
        "info_actions": {"add": [], "remove": []},
        "watch_afternoon": [str(x).strip()[:160]
                            for x in (payload.get("watch_afternoon") or [])][:8],
        "risk_notes": [str(x).strip()[:160]
                       for x in (payload.get("risk_notes") or [])][:8],
    }

    for p in (payload.get("plan_check") or [])[:10]:
        if not isinstance(p, dict):
            continue
        verdict = str(p.get("verdict", "")).strip()
        if verdict not in ("成立", "推翻", "待观察"):
            verdict = "待观察"
        out["plan_check"].append({
            "item": str(p.get("item", "")).strip()[:60],
            "expected": str(p.get("expected", "")).strip()[:160],
            "observed": str(p.get("observed", "")).strip()[:160],
            "verdict": verdict,
            "action": str(p.get("action", "")).strip()[:160],
        })

    for s in (payload.get("sector_check") or [])[:10]:
        if not isinstance(s, dict):
            continue
        verdict = str(s.get("verdict", "")).strip()
        if verdict not in ("延续", "熄火", "新起"):
            verdict = "延续"
        try:
            tn = int(s.get("today_n") or 0)
            yn = int(s.get("yesterday_n") or 0)
        except (TypeError, ValueError):
            tn = yn = 0
        out["sector_check"].append({
            "name": str(s.get("name", "")).strip()[:20],
            "today_n": tn, "yesterday_n": yn,
            "verdict": verdict,
            "note": str(s.get("note", "")).strip()[:160],
        })

    IMP = ("高", "中", "低")
    IMPACT = ("利好", "利空", "中性")
    for n in (payload.get("news_analysis") or [])[:6]:
        if not isinstance(n, dict):
            continue
        title = str(n.get("title", "")).strip()
        if not title:
            continue
        imp = str(n.get("importance", "")).strip()
        eff = str(n.get("impact", "")).strip()
        out["news_analysis"].append({
            "title": title[:40],
            "importance": imp if imp in IMP else "低",
            "impact": eff if eff in IMPACT else "中性",
            "summary": str(n.get("summary", "")).strip()[:200],
            "affected_themes": [str(x).strip()[:20] for x in
                                (n.get("affected_themes") or [])][:5],
            "affected_stocks": [str(x).strip() for x in
                                (n.get("affected_stocks") or [])][:5],
            "reasoning": str(n.get("reasoning", "")).strip()[:220],
        })
    out["news_analysis"].sort(key=lambda x: ("高", "中", "低").index(x["importance"]))

    av = payload.get("afternoon_view") or {}
    if isinstance(av, dict):
        bias = str(av.get("bias", "")).strip()
        conds = []
        for c in (av.get("conditions") or [])[:6]:
            if isinstance(c, dict) and c.get("if"):
                conds.append({"if": str(c.get("if")).strip()[:160],
                              "then": str(c.get("then", "")).strip()[:160],
                              "else_note": str(c.get("else_note", "")).strip()[:160]})
        out["afternoon_view"] = {
            "bias": bias if bias in ("偏强", "偏弱", "震荡") else "震荡",
            "conditions": conds,
            "volume_projection": str(av.get("volume_projection", "")).strip()[:200],
            "risk_level": (str(av.get("risk_level", "")).strip()
                           if str(av.get("risk_level", "")).strip() in ("高", "中", "低")
                           else "中"),
        }

    pp = payload.get("position_plan") or {}
    for key in ("buy", "sell", "adjust"):
        out["position_plan"][key] = [x for x in (pp.get(key) or [])
                                     if isinstance(x, dict)][:8]

    # ★ 表动作要在**校验阶段**就剔掉残缺项。
    #   实测 LLM 会给出 `info_add` 但没有 content、`pool_update` 但没有要改的字段、
    #   `remove` 但 id 为空。这些是"半成品"，不是被策略拒绝 —— 如果放行到内核，
    #   它们会占满 rejected 列表，把真正的拒绝（比如禁止打板）淹掉。
    pa = payload.get("pool_actions") or {}
    out["pool_actions"]["add"] = [
        x for x in (pa.get("add") or [])
        if isinstance(x, dict) and str(x.get("code", "")).strip()][:8]
    out["pool_actions"]["update"] = [
        x for x in (pa.get("update") or [])
        if isinstance(x, dict) and str(x.get("code", "")).strip()
        and any(x.get(k) is not None for k in
                ("tier", "trigger", "stop_loss", "take_profit", "status"))][:8]
    out["pool_actions"]["remove"] = [
        x for x in (pa.get("remove") or [])
        if isinstance(x, dict) and str(x.get("code", "")).strip()][:8]

    ia = payload.get("info_actions") or {}
    # 重要信息需要 4 个字段齐全才有意义（content/scope/level/expires_on）。
    # 缺任何一个都不猜 —— 猜出来的"影响范围"比丢掉更糟。
    ok_info, dropped_info = [], 0
    for x in (ia.get("add") or []):
        if not isinstance(x, dict):
            continue
        if all(str(x.get(k, "")).strip() for k in
               ("content", "scope", "level", "expires_on")):
            ok_info.append(x)
        else:
            dropped_info += 1
    out["info_actions"]["add"] = ok_info[:8]
    out["info_actions"]["remove"] = [
        x for x in (ia.get("remove") or [])
        if isinstance(x, dict) and str(x.get("id", "")).strip()][:8]
    out["_dropped"] = {"info_add": dropped_info}
    return out


def rule_fallback_midday(market: dict) -> dict:
    """没有 LLM 时的机械兜底：只描述上午，不给午后操作。"""
    b = market.get("breadth") or {}
    ms = market.get("market_stats") or {}
    idx = market.get("indices") or []
    up_ratio = ms.get("up_ratio_pct")
    tone = "偏强" if (up_ratio or 0) >= 55 else ("偏弱" if (up_ratio or 0) <= 45 else "分化")
    reasons = []
    if idx:
        i = idx[0]
        reasons.append(f"{i.get('name')} 上午 {i.get('chg_pct'):+.2f}%，"
                       f"半日成交额 {i.get('amount_yi')} 亿")
    if up_ratio is not None:
        reasons.append(f"上涨 {b.get('up')} 家 / 下跌 {b.get('down')} 家，"
                       f"上涨占比 {up_ratio}%")
    if not reasons:
        reasons = ["数据不足，按最保守处理"]
    return {
        "morning_view": {"tone": tone,
                         "summary": "（规则兜底）仅汇总上午数据，未做判断。",
                         "reasons": reasons},
        "plan_check": [], "sector_check": [], "news_analysis": [],
        "afternoon_view": {"bias": "震荡", "conditions": [],
                           "volume_projection": "", "risk_level": "高"},
        "position_plan": {"buy": [], "sell": [], "adjust": []},
        "pool_actions": {"add": [], "update": [], "remove": []},
        "info_actions": {"add": [], "remove": []},
        "watch_afternoon": ["未接入 LLM，午后无法给出条件分支"],
        "risk_notes": ["本次为规则兜底，不含午后判断"],
    }


# ─────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────
def run(*, llm_call: Callable[[str], str] | None = None,
        allow_fallback: bool = True, write: bool = True,
        now: datetime.datetime | None = None,
        market: dict | None = None,
        emit_html: bool = True, mail_path: str | None = None,
        log: Callable[[str], None] = print) -> dict[str, Any]:
    """生成午报：校验时段 → 采集 → agent（核对预案 + 午后判断）→ 裁决 → 调仓 → 渲染。"""
    paths.ensure_dirs()
    now = now or datetime.datetime.now()
    d8 = now.strftime("%Y%m%d")
    iso = now.strftime("%Y-%m-%d")

    log(f"🕛 午间报告：{iso} {now.strftime('%H:%M')}")

    # 1) 采集（先采一次，为了拿到 legu 的统计日期做时段校验）
    if market is None:
        market = collect_midday(progress=log)
    legu_date = (market.get("breadth") or {}).get("as_of")
    sess = session_guard(now=now, legu_date=legu_date)     # 不在窗口内直接抛
    log(f"  时段校验通过（官方快照 {legu_date}）")

    # 2) 账户（用上午收盘价）
    tables = Tables()
    snap = tables.snapshot()
    held = list(tables.positions.alive())
    quotes = {c["code"]: c for c in (market.get("candidates") or [])}
    prices = {}
    if held:
        from portal.virtual import marketdb as M
        got = M.fetch_quotes_batch(held)
        prices = {q["code"]: q["close"] for q in got if q.get("close")}
    snap_prev = account_mod.load_snapshot()
    acct = account_mod.compute(tables, prices,
                               prev_total=(snap_prev or {}).get("total_pct"),
                               trades_path=paths.TRADES)

    # 3) 昨晚的预案（用于核对）
    last_plan = None
    ppath = paths.plan_path(d8)
    if ppath.exists():
        try:
            last_plan = json.loads(ppath.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            last_plan = None

    # 4) 提议
    source = "llm"
    try:
        call = llm_call or agent_mod.default_llm(log)
        if call is None:
            raise RuntimeError("没有可用的 LLM")
        prompt = build_midday_prompt(market=market, account=acct,
                                     snapshot=snap, last_plan=last_plan)
        log(f"🤖 调用 LLM（prompt {len(prompt)} 字符）…")
        text = call(prompt)
        log(f"   LLM 返回 {len(text)} 字符，解析…")
        proposal = validate_midday(agent_mod.extract_json(text))
        log(f"   校验通过：盘面={proposal['morning_view']['tone']} "
            f"预案核对={len(proposal['plan_check'])} 条 "
            f"条件分支={len(proposal['afternoon_view']['conditions'])} 条")
    except Exception as exc:
        if not allow_fallback:
            raise
        log(f"⚠️  LLM 不可用（{type(exc).__name__}: {str(exc)[:100]}），改用规则兜底")
        source = "rule_fallback"
        proposal = validate_midday(rule_fallback_midday(market))

    # 5) 裁决（midday 模式：额度更紧、禁止写经验）
    verdict = apply_mod.apply_proposal(
        proposal=_as_apply_shape(proposal), tables=tables,
        plan_date=d8, trade_date=iso,
        prices=prices, candidates=market.get("candidates"),
        mode="midday", log=log)

    # 6) 当场按上午收盘价调仓
    need = {c for c in
            ([b.get("code") for b in verdict["planned"].get("buy") or []]
             + [s.get("code") for s in verdict["planned"].get("sell") or []]) if c}
    exec_prices = {c: quotes[c]["close"] for c in need
                   if c in quotes and quotes[c].get("close")}
    exec_result = account_mod.execute_changes(
        tables, verdict["planned"], exec_prices,
        plan_date=d8, iso_date=iso, log=log)
    acct_after = account_mod.compute(tables, {**prices, **exec_prices},
                                     prev_total=(snap_prev or {}).get("total_pct"),
                                     trades_path=paths.TRADES)

    mid = {
        "session": "lunch",
        "data_date": iso,
        "legu_as_of": legu_date,
        "generated_at": _now(),
        "proposal_source": source,
        "proposal": proposal,
        "applied": verdict["applied"],
        "rejected": verdict["rejected"],
        "executed": exec_result["executed"],
        "exec_skipped": exec_result["skipped"],
        "account_before": {k: acct.get(k) for k in
                           ("total_pct", "cash_pct", "market_value_pct",
                            "position_ratio_pct", "day_pnl_ratio_pct",
                            "cum_pnl_ratio_pct", "n_positions")},
        "account_after": {k: acct_after.get(k) for k in
                          ("total_pct", "cash_pct", "market_value_pct",
                           "position_ratio_pct", "cum_pnl_ratio_pct", "n_positions")},
        "last_plan_date": (last_plan or {}).get("plan_date"),
    }
    if not write:
        return {"ok": True, "midday": mid, "written": False}

    out_dir = paths.REPORTS
    out_dir.mkdir(parents=True, exist_ok=True)
    rpath = out_dir / f"{d8}_midday.html"

    # 网页版 HTML 只在**门户手动点击**时生成（用户要求）；GitHub Action 传
    # emit_html=False，内容全部进邮件正文。
    payload_mid = {
        "data_date": iso, "generated_at": mid["generated_at"],
        "market": market, "proposal": proposal,
        "applied": verdict["applied"], "rejected": verdict["rejected"],
        "executed": exec_result["executed"], "exec_skipped": exec_result["skipped"],
        "account_before": acct, "account_after": acct_after,
        "snapshot": tables.snapshot(), "session_info": market.get("session_info"),
        "proposal_source": source, "legu_as_of": legu_date,
        # ★ 必须传下去，否则「预案 vs 上午现实」会显示「无昨晚预案」，
        #   而下面又列出六条核对项 —— 自相矛盾（第一版就是这个 bug）。
        "last_plan_date": mid["last_plan_date"],
    }
    r: dict | None = None
    if emit_html:
        r = render_mid_day.write_midday(payload_mid, rpath)

    mail: dict | None = None
    if mail_path:
        from . import render_mail
        mail = render_mail.write_mail(payload_mid, mail_path, kind="midday")
        log(f"📧 邮件正文：{mail['bytes']} 字节（{mail['subject']}）")
        if mail.get("oversize"):
            log(f"    ⚠️  正文超过 {render_mail.MAX_BYTES} 字节，Gmail 会显示「消息已剪裁」")

    (out_dir / f"{d8}_midday.json").write_text(
        json.dumps(mid, ensure_ascii=False, indent=2), encoding="utf-8")
    account_mod.save_snapshot(acct_after)
    if r:
        log(f"✅ 午报已生成：{rpath.name}（{r['bytes']} 字节）")
    else:
        log("✅ 午报已生成（仅数据；HTML 按配置跳过）")
    return {"ok": True, "written": True, "midday": mid,
            "report_path": str(rpath) if r else None,
            "report_bytes": r["bytes"] if r else 0,
            "mail_path": mail["path"] if mail else None,
            "mail_bytes": mail["bytes"] if mail else 0,
            "mail_subject": mail["subject"] if mail else None,
            "proposal_source": source}


def _as_apply_shape(p: dict) -> dict:
    """午报的 proposal 结构与晚间不同，转成 apply 认识的形状。"""
    mv = p.get("morning_view") or {}
    tone = mv.get("tone")
    trend = {"偏强": "强势", "偏弱": "弱势"}.get(tone, "震荡")
    return {
        "market_view": {
            "trend": trend,
            "position_range_pct": [0, 100],     # 午报不重设仓位区间
            "confidence": 50,
            "reasons": mv.get("reasons") or ["午报不设仓位区间"],
        },
        "sector_priority": [],
        "news_analysis": p.get("news_analysis") or [],
        "review": {"what_worked": [], "what_failed": [], "theme_verdict": []},
        "pool_actions": p.get("pool_actions") or {},
        "position_plan": p.get("position_plan") or {},
        "info_actions": p.get("info_actions") or {},
        "lesson_actions": {"add": [], "remove": []},
        "risk_notes": p.get("risk_notes") or [],
        "execution_checklist": p.get("watch_afternoon") or [],
    }


__all__ = ["run", "collect_midday", "session_guard", "SessionError",
           "live_market_stats", "build_midday_prompt", "validate_midday",
           "MIDDAY_MAX_SINGLE_PCT", "MIDDAY_MAX_TOTAL_BUY_PCT",
           "MIDDAY_MAX_POOL_ADDS", "LUNCH_START", "LUNCH_END"]
