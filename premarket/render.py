# -*- coding: utf-8 -*-
"""
portal/premarket/render.py — 盘前预案 HTML（代码生成，非 agent 拼接）

为什么必须代码生成
-----------------
用户明确要求「通过 code based 的方法加上最新的数据和 agent 分析 json 来生成，
而不是用 agent 自己拼接 html」。原因很实在：

- agent 拼 HTML 会漏标签、会忘转义、会把 `<` 写进正文，而且**每次都不一样**
- 模板固定后，样式/结构可回归测试，数字位置稳定，便于程序解析存档
- 数字来自程序计算，agent 只提供判断文本 → 报告里的数永远与表一致

输出是**自包含单文件**：不引外部 CSS/JS/图片，双击即可看，可直接归档。
"""
from __future__ import annotations

import datetime
import html
import json
from typing import Any

_CSS = """
:root{--ink:#1a1a1a;--sub:#666;--line:#e3e3e3;--red:#c62828;--green:#2e7d32;
--amber:#ef6c00;--blue:#1565c0;--bg:#fafafa;--card:#fff}
*{box-sizing:border-box}
body{margin:0;padding:26px;background:var(--bg);color:var(--ink);
font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1180px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:15px;margin:26px 0 10px;padding-left:9px;border-left:3px solid var(--ink)}
h3{font-size:13px;margin:16px 0 8px;color:#333}
.meta{color:var(--sub);font-size:12px;margin-bottom:14px}
.hero{background:linear-gradient(135deg,#1f2937,#374151);color:#fff;border-radius:12px;
padding:16px 18px;margin:12px 0}
.hero .k{font-size:11px;opacity:.7}
.hero .v{font-size:24px;font-weight:700;letter-spacing:.5px}
.hero-grid{display:flex;gap:26px;flex-wrap:wrap}
.cards{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0}
.card{flex:1 1 140px;background:var(--card);border:1px solid var(--line);
border-radius:8px;padding:10px 12px}
.card .k{color:var(--sub);font-size:11.5px}
.card .v{font-size:19px;font-weight:600;margin-top:2px}
table{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--line);border-radius:8px;overflow:hidden;margin:8px 0}
th,td{padding:6px 9px;text-align:left;border-bottom:1px solid var(--line);
font-size:12.5px;vertical-align:top}
th{background:#f0f0f0;font-weight:600;color:#333;white-space:nowrap}
tr:last-child td{border-bottom:none}
.pos{color:var(--red)}.neg{color:var(--green)}.warn{color:var(--amber)}
.dim{color:var(--sub)}
.pill{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;
border:1px solid var(--line);background:#f7f7f7;white-space:nowrap}
.pill.ok{background:#e8f5e9;border-color:#a5d6a7;color:#256029}
.pill.no{background:#ffebee;border-color:#ef9a9a;color:#b71c1c}
.pill.mid{background:#fff3e0;border-color:#ffcc80;color:#e65100}
.note{background:#fff8e1;border:1px solid #ffe082;border-radius:8px;
padding:9px 12px;font-size:12.5px;color:#6d4c00;margin:10px 0}
.err{background:#ffebee;border:1px solid #ef9a9a;border-radius:8px;
padding:9px 12px;font-size:12.5px;color:#b71c1c;margin:10px 0}
.bad{background:#e3f2fd;border:1px solid #90caf9;border-radius:8px;
padding:9px 12px;font-size:12.5px;color:#0d47a1;margin:10px 0}
ul{margin:6px 0 6px 18px;padding:0}
li{margin:3px 0;font-size:12.5px}
.foot{margin-top:26px;color:var(--sub);font-size:11.5px;
border-top:1px solid var(--line);padding-top:12px}
code{background:#f2f2f2;padding:1px 5px;border-radius:4px;font-size:11.5px}
details{margin:6px 0}
summary{cursor:pointer;font-size:12.5px;color:var(--blue)}
"""


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def _n(v: Any, digits: int = 2, suffix: str = "") -> str:
    if v is None or v == "":
        return '<span class="dim">—</span>'
    try:
        return f"{float(v):,.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return _e(v)


def _pct(v: Any, digits: int = 2) -> str:
    """涨跌类数字：正红负绿，带符号。"""
    if v is None:
        return '<span class="dim">—</span>'
    try:
        x = float(v)
    except (TypeError, ValueError):
        return _e(v)
    cls = "pos" if x > 0 else ("neg" if x < 0 else "dim")
    return f'<span class="{cls}">{x:+.{digits}f}%</span>'


def _table(headers: list[str], rows: list[list[str]], *, empty: str = "（无）") -> str:
    if not rows:
        return f'<p class="dim">{_e(empty)}</p>'
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


# ─────────────────────────────────────────────────────────────
# 各区块
# ─────────────────────────────────────────────────────────────
def _hero(proposal: dict) -> str:
    mv = proposal.get("market_view") or {}
    lo, hi = (mv.get("position_range_pct") or [None, None])
    trend = mv.get("trend") or "—"
    conf = mv.get("confidence")
    return f"""
<div class="hero">
  <div class="hero-grid">
    <div><div class="k">趋势判断</div><div class="v">{_e(trend)}</div></div>
    <div><div class="k">建议总仓位</div>
         <div class="v">{("—" if lo is None else f"{lo:g}% ~ {hi:g}%")}</div></div>
    <div><div class="k">置信度</div>
         <div class="v">{("—" if conf is None else f"{conf:g}")}</div></div>
  </div>
</div>"""


def _market_view(proposal: dict) -> str:
    mv = proposal.get("market_view") or {}
    reasons = mv.get("reasons") or []
    if not reasons:
        return ""
    return "<ul>" + "".join(f"<li>{_e(r)}</li>" for r in reasons) + "</ul>"


def _indices(market: dict) -> str:
    rows = []
    for i in market.get("indices") or []:
        trend = i.get("trend") or "—"
        cls = "ok" if trend == "强势" else ("no" if trend == "走弱" else "mid")
        rows.append([
            f"{_e(i.get('name'))}",
            _n(i.get("close")),
            _pct(i.get("chg_pct")),
            _n(i.get("amount_yi"), 0, "亿"),
            _pct(i.get("chg_5d_pct")),
            f'<span class="pill {cls}">{_e(trend)}</span>',
            "是" if i.get("above_ma20") else "否",
            _n(i.get("ma20")),
        ])
    return _table(["指数", "收盘", "涨跌", "成交额", "5日", "趋势", "站上MA20", "MA20"], rows)


def _breadth(market: dict) -> str:
    b = market.get("breadth") or {}
    # ★ 这里**不能**对外层再做 _e()：_n/_pct 返回的已经是 HTML，
    #   再转义一次就会把 <span> 原样打印到页面上（第一版就这么翻车的）。
    cards = [
        ("上涨/下跌", f"{_e(b.get('up', '—'))} / {_e(b.get('down', '—'))}"),
        ("上涨占比", _n(b.get("up_ratio_pct"), 1, "%")),
        ("涨停/跌停",
         f"{_e(b.get('pool_zt', b.get('limit_up', '—')))} / "
         f"{_e(b.get('pool_dt', b.get('limit_down', '—')))}"),
        ("炸板率", _n(b.get("break_rate_pct"), 1, "%")),
        ("最高连板", _e(b.get("max_boards", "—"))),
        ("连板家数", _e(b.get("multi_board_n", "—"))),
        ("封板资金", _n(b.get("seal_amount_yi"), 1, "亿")),
        ("活跃度", _n(b.get("activity"), 1)),
    ]
    cells = "".join(
        f'<div class="card"><div class="k">{_e(k)}</div><div class="v">{v}</div></div>'
        for k, v in cards)
    return f'<div class="cards">{cells}</div>'


def _themes(market: dict, proposal: dict) -> str:
    pri = {s["name"]: s for s in (proposal.get("sector_priority") or [])}
    rows = []
    for t in (market.get("themes") or [])[:12]:
        p = pri.get(t["name"]) or {}
        leaders = "、".join(l["name"] for l in (t.get("leaders") or [])[:3])
        rows.append([
            _e(t.get("name")),
            _n(t.get("limit_up_n"), 0),
            _n(t.get("max_boards"), 0),
            _n(t.get("seal_yi"), 1, "亿"),
            _e(leaders or "—"),
            _n(p.get("strength"), 0) if p else '<span class="dim">—</span>',
            _e(p.get("logic") or "") if p else "",
        ])
    body = _table(["题材/行业", "涨停数", "最高板", "封板资金", "龙头", "Agent强度", "Agent 逻辑"], rows)
    if pri:
        names = [s["name"] for s in proposal.get("sector_priority") or []]
        body += f'<p class="dim">Agent 给出的题材优先级：{" → ".join(_e(n) for n in names)}</p>'
    return body


def _leaders(market: dict) -> str:
    rows = [[_e(l.get("name")), _e(l.get("code")), _n(l.get("boards"), 0),
             _pct(l.get("chg_pct")), _n(l.get("seal_yi"), 1, "亿"),
             _e(l.get("industry")), _n(l.get("break_times"), 0)]
            for l in (market.get("leaders") or [])[:12]]
    return _table(["名称", "代码", "连板", "涨跌", "封板资金", "行业", "炸板次数"], rows,
                  empty="（今日无 3 板以上个股）")


def _funds(market: dict) -> str:
    out = []
    nb = market.get("northbound") or []
    if nb:
        rows = [[_e(x.get("board")), _n(x.get("net_buy_yi"), 1, "亿"),
                 f"{x.get('up', '—')} / {x.get('down', '—')}",
                 _pct(x.get("index_chg_pct"))] for x in nb]
        out.append("<h3>北向资金</h3>" +
                   _table(["板块", "成交净买额", "涨/跌家数", "指数涨跌"], rows))
    lhb = market.get("lhb_top") or []
    if lhb:
        rows = [[_e(x.get("name")), _e(x.get("code")), _pct(x.get("chg_pct")),
                 _n(x.get("net_buy_wan"), 0, "万"), _e(x.get("note"))] for x in lhb[:8]]
        out.append("<h3>龙虎榜（净买额前列）</h3>" +
                   _table(["名称", "代码", "涨跌", "净买额", "解读"], rows))
    return "".join(out)


def _news(market: dict) -> str:
    items = market.get("news") or []
    if not items:
        return '<p class="dim">（未取到快讯）</p>'
    rows = []
    for n in items[:30]:
        text = n.get("title") or n.get("content") or ""
        rows.append([_e(str(n.get("time"))[:16]), _e(n.get("src")), _e(text[:100])])
    return _table(["时间", "来源", "内容"], rows)


def _position_plan(planned: dict, account: dict) -> str:
    out = []
    buys = planned.get("buy") or []
    if buys:
        def src(it) -> str:
            s = it.get("price_source")
            if s == "auto":
                return '<span class="pill mid">内核补全</span>'
            if s == "llm":
                return '<span class="pill ok">模型给出</span>'
            return '<span class="pill">—</span>'

        rows = [[_e(b.get("code")), _e(b.get("name")), _n(b.get("alloc_pct"), 1, "%"),
                 _n(b.get("stop_loss")), _n(b.get("take_profit")), src(b),
                 _e(b.get("trigger") or ""), _e(b.get("logic") or "")] for b in buys]
        out.append("<h3>计划买入</h3>" +
                   _table(["代码", "名称", "仓位", "止损", "止盈", "价格来源",
                           "触发条件", "逻辑"], rows))

    rows = []
    for p in account.get("positions") or []:
        rows.append([_e(p.get("code")), _e(p.get("name")), _n(p.get("weight_pct"), 1, "%"),
                     _n(p.get("entry_price")), _n(p.get("last_price")),
                     _pct(p.get("unrealized_pct")), _n(p.get("stop_loss")),
                     _n(p.get("take_profit"))])
    if rows:
        out.append("<h3>当前持仓</h3>" +
                   _table(["代码", "名称", "占比", "成本", "现价", "浮盈", "止损", "止盈"], rows))

    sells = planned.get("sell") or []
    if sells:
        rows = [[_e(s.get("code")), _e(s.get("name")),
                 "全部" if s.get("all") else "部分", _e(s.get("reason") or "")]
                for s in sells]
        out.append("<h3>计划卖出</h3>" + _table(["代码", "名称", "比例", "原因"], rows))

    adj = planned.get("adjust") or []
    if adj:
        rows = [[_e(a.get("code")), _n(a.get("stop_loss")), _n(a.get("take_profit")),
                 _e(a.get("reason") or "")] for a in adj]
        out.append("<h3>计划调整止损/止盈</h3>" +
                   _table(["代码", "新止损", "新止盈", "原因"], rows))
    return "".join(out) or '<p class="dim">（本日无买卖计划）</p>'


def _account_summary(account: dict) -> str:
    cards = [
        ("总资产", _n(account.get("total_pct"), 2)),
        ("可用资金", _n(account.get("cash_pct"), 2)),
        ("持仓市值", _n(account.get("market_value_pct"), 2)),
        ("仓位占比", _n(account.get("position_ratio_pct"), 1, "%")),
        ("累计收益", _pct(account.get("cum_pnl_ratio_pct"))),
        ("已实现", _n(account.get("realized_pct"), 2)),
        ("持仓数", account.get("n_positions", 0)),
    ]
    cells = "".join(
        f'<div class="card"><div class="k">{_e(k)}</div><div class="v">{v}</div></div>'
        for k, v in cards)
    note = ('<p class="dim">口径：总资产以 100 为基准（份额），全部数字为百分比；'
            f'费用 {account.get("fee_model", {}).get("note", "")}</p>')
    return f'<div class="cards">{cells}</div>{note}'


def _changes(applied: dict, rejected: list) -> str:
    """四张表的变动 + 原因（用户要求：变动必须写明原因）。"""
    out = []
    # 英文键 → 中文标题。**h3 也必须用映射后的标签**，
    # 否则页面上会出现「pool_add」这种内部键名（第一版就是）。
    LABELS = {"pool_add": "观察表 · 新增", "pool_update": "观察表 · 更新",
              "pool_remove": "观察表 · 移除", "info_add": "重要信息 · 新增",
              "info_remove": "重要信息 · 移除", "lesson_add": "经验表 · 新增",
              "lesson_remove": "经验表 · 移除"}

    def block(key: str, items: list, fmt) -> None:
        if not items:
            return
        label = LABELS.get(key, key)
        rows = [[label, fmt(it)] for it in items]
        out.append(f"<h3>{_e(label)}</h3>" + _table(["项", "内容/原因"], rows))

    block("pool_add", applied.get("pool_add") or [],
          lambda it: f"{_e(it.get('code'))} {_e(it.get('name'))} "
                     f"[{_e(it.get('tier'))}] — {_e(it.get('reason'))}")
    block("pool_update", applied.get("pool_update") or [],
          lambda it: f"{_e(it.get('code'))} 改 {_e(', '.join(it.get('fields') or []))} "
                     f"— {_e(it.get('reason'))}")
    block("pool_remove", applied.get("pool_remove") or [],
          lambda it: f"{_e(it.get('code'))} — {_e(it.get('reason'))}")
    block("info_add", applied.get("info_add") or [],
          lambda it: f"[{_e(it.get('level'))}] {_e(it.get('content'))}")
    block("info_remove", applied.get("info_remove") or [],
          lambda it: f"{_e(it.get('id'))} — {_e(it.get('reason'))}")
    block("lesson_add", applied.get("lesson_add") or [],
          lambda it: f"[{_e(it.get('result'))}] {_e(it.get('lesson'))}")
    block("lesson_remove", applied.get("lesson_remove") or [],
          lambda it: f"{_e(it.get('id'))} — {_e(it.get('reason'))}")

    if rejected:
        rows = [[_e(r.get("kind")), _e(r.get("target")), _e(r.get("reason"))]
                for r in rejected]
        out.append('<div class="err"><b>被内核拒绝的提议</b>（没有静默丢弃，'
                   '原因如下）</div>' +
                   _table(["类型", "对象", "拒绝原因"], rows))

    if not out:
        # 不能因为"今天没改表"就把整节删掉：章节编号会断，而且用户会以为渲染出错。
        return '<p class="dim">本日四张表无任何增删改。</p>'
    return "".join(out)


def _tables_snapshot(snapshot: dict) -> str:
    out = []
    pool = [r for r in (snapshot.get("pool") or []) if r.get("status") != "removed"]
    if pool:
        rows = [[_e(r.get("code")), _e(r.get("name")),
                 f'<span class="pill {"ok" if r.get("tier") == "core" else ""}">'
                 f'{_e(r.get("tier"))}</span>',
                 _e(r.get("logic")), _e(r.get("trigger")),
                 _n(r.get("stop_loss")), _n(r.get("take_profit")),
                 _e(r.get("added_date"))] for r in pool]
        out.append("<h3>选股观察表</h3>" +
                   _table(["代码", "名称", "等级", "入选逻辑", "触发条件",
                           "止损", "止盈", "入选日期"], rows))

    infos = snapshot.get("infos") or []
    if infos:
        rows = [[_e(r.get("level")), _e(r.get("scope")),
                 _e((r.get("scope_detail") or "")), _e(r.get("content")),
                 _e(r.get("expires_on"))] for r in infos]
        out.append("<h3>重要信息表</h3>" +
                   _table(["等级", "范围", "标的", "内容", "有效期至"], rows))

    lessons = snapshot.get("lessons") or []
    if lessons:
        rows = [[_e(r.get("result")), _e(r.get("lesson")), _e(r.get("scenario")),
                 _e(r.get("recorded_on"))] for r in lessons[:15]]
        out.append("<h3>交易经验表（最近）</h3>" +
                   _table(["结果", "经验", "场景", "记录日期"], rows))
    if not out:
        return ('<p class="dim">四张表当前均为空。'
                '生成第一份预案后，agent 的增删改会出现在统计里。</p>')
    return "".join(out)


def _source_banner(payload: dict) -> str:
    """把「判断是谁做的」放在最显眼处 —— 规则兜底绝不能冒充 agent。"""
    src = payload.get("proposal_source") or "llm"
    if src == "llm":
        return ""
    reason = (payload.get("proposal") or {}).get("_fallback_reason") or ""
    return (f'<div class="err"><b>本次为规则兜底，不是 agent 判断。</b><br>'
            f'未接入 LLM（{_e(reason[:160])}）。本预案只做了「大盘趋势 → 仓位区间」'
            f'的机械映射与题材排序，<b>没有生成个股计划、没有改动四张表</b>。'
            f'配置 LLM 后重新生成才会得到 agent 版本。</div>')


def _anchor_line(payload: dict) -> str:
    a = payload.get("anchor") or {}
    if not a.get("sha256"):
        return ""
    prev = a.get("prev") or "（首个预案）"
    return (f'<p class="dim">预案锚点 <code>{_e(str(a.get("sha256"))[:32])}…</code>'
            f' ｜ 上一份 <code>{_e(str(prev)[:32])}</code>'
            f'<br>锚点 = 预案内容的 sha256。事后任何改动都会让锚点对不上。</p>')


def _lists(proposal: dict) -> str:
    out = []
    risk = proposal.get("risk_notes") or []
    if risk:
        out.append("<h3>风险提示</h3><ul>" +
                   "".join(f"<li>{_e(r)}</li>" for r in risk) + "</ul>")
    chk = proposal.get("execution_checklist") or []
    if chk:
        out.append("<h3>开盘执行清单</h3><ul>" +
                   "".join(f"<li>{_e(c)}</li>" for c in chk) + "</ul>")
    return "".join(out)


def _provenance(market: dict) -> str:
    """数据来源与缺口：不许假装数据齐全。"""
    parts = []
    errs = market.get("errors") or []
    caves = market.get("caveats") or []
    if caves:
        parts.append('<div class="note"><b>口径与数据说明</b><br>' +
                     "<br>".join(_e(c) for c in caves) + "</div>")
    if errs:
        rows = [[_e(e.get("source")), _e(e.get("error"))] for e in errs]
        parts.append('<div class="err"><b>以下数据源本次不可用</b>'
                     '（相关结论不要依赖）</div>' +
                     _table(["数据源", "错误"], rows))
    srcs = set()
    if (market.get("indices") or []):
        srcs.add("指数：stock_zh_index_spot_sina / stock_zh_index_daily")
    if (market.get("breadth") or {}).get("pool_zt") is not None:
        srcs.add("池子：stock_zt_pool_em / zbgc / dtgc")
    if market.get("themes"):
        srcs.add("题材：涨停池「所属行业」聚合（板块接口 push2 不可用）")
    if market.get("lhb_top"):
        srcs.add("龙虎榜：stock_lhb_detail_em")
    if market.get("news"):
        srcs.add("快讯：stock_info_global_cls / stock_info_global_em")
    if market.get("db_latest_date"):
        srcs.add(f"本地库：market.db（最新 {market.get('db_latest_date')}）")
    if srcs:
        parts.append('<p class="dim">数据来源：' + "；".join(_e(s) for s in sorted(srcs)) + "</p>")
    return "".join(parts)


# ─────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────
def render_plan(payload: dict) -> str:
    """生成盘前预案 HTML。

    payload 需要：
        plan_date / trade_date / generated_at
        market      —— collect.collect() 的结果
        proposal    —— agent.validate() 后的 JSON
        applied / rejected / planned —— apply.apply_proposal() 的结果
        snapshot    —— Tables.snapshot()
        account     —— account.compute() 的结果
    """
    market = payload.get("market") or {}
    proposal = payload.get("proposal") or {}
    applied = payload.get("applied") or {}
    rejected = payload.get("rejected") or []
    planned = payload.get("planned") or {}
    snapshot = payload.get("snapshot") or {}
    account = payload.get("account") or {}

    # 标题用**预案日**（这份预案是哪天的），数据日期单独标注 ——
    # 之前用 trade_date 做标题，会让人以为这是"回顾昨天"。
    plan_date_iso = payload.get("plan_date") or ""
    if len(str(plan_date_iso)) == 8:
        plan_date_iso = f"{str(plan_date_iso)[:4]}-{str(plan_date_iso)[4:6]}-{str(plan_date_iso)[6:8]}"
    title = f"盘前预案 · {plan_date_iso}"
    meta = (f"生成时间 {_e(payload.get('generated_at'))} ｜ "
            f"适用于 {_e(plan_date_iso)} 开盘 ｜ "
            f"数据截至 {_e(market.get('date'))} ｜ "
            f"判断来源 {'LLM' if (payload.get('proposal_source') or 'llm') == 'llm' else '规则兜底'}")

    # 章节**恒定 12 节**：空也要渲染（带空态说明）。
    # 否则某天少两节，编号就断，程序和人都对不上。
    sections = [
        ("一、决策摘要", _hero(proposal) + _market_view(proposal)),
        ("二、大盘环境", _indices(market)),
        ("三、市场情绪与宽度", _breadth(market)),
        ("四、题材优先级", _themes(market, proposal)),
        ("五、连板梯队", _leaders(market)),
        ("六、资金面", _funds(market)),
        ("七、财经与消息", _news(market)),
        ("八、账户与持仓计划",
         _account_summary(account) + _position_plan(planned, account)),
        ("九、四张表变动（含原因）", _changes(applied, rejected)),
        ("十、四张表当前状态", _tables_snapshot(snapshot)),
        ("十一、风险与执行", _lists(proposal)),
        ("十二、数据来源与缺口", _provenance(market)),
    ]

    body = [f"<h2>{_e(head)}</h2>{content}" for head, content in sections]

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)}</title><style>{_CSS}</style></head>
<body><div class="wrap">
<h1>{_e(title)}</h1>
<div class="meta">{meta}</div>
{_source_banner(payload)}
{''.join(body)}
{_anchor_line(payload)}
<div class="foot">
本预案由 portal.premarket 生成：数字全部由程序计算，判断文字由 LLM 提供并逐条经内核校验。<br>
这是**虚拟盘**的盘前预案，不含真实资金、不构成任何投资建议。市场有风险，决策需自负。
</div>
</div></body></html>"""


def write_plan(payload: dict, path) -> dict:
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = render_plan(payload)
    p.write_text(text, encoding="utf-8")
    return {"ok": True, "path": str(p), "bytes": len(text.encode("utf-8")),
            "external_refs": 0}


__all__ = ["render_plan", "write_plan"]
