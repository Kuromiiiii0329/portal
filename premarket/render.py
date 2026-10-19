# -*- coding: utf-8 -*-
"""
portal/premarket/render.py — 盘前预案 + 盘后复盘（一份 HTML，代码生成）

版面（按用户要求的顺序：信息 → 当日盈亏 → 次日计划 → 复盘）
-----------------------------------------------------------
一、决策摘要          明日趋势 / 仓位区间 / 置信度（顶部深色区块）
二、大盘环境与资金     指数 K 线细节 + 量能 + 北向 + 龙虎榜（资金面并入此处）
三、市场情绪与宽度     涨跌分布 / 量能变化 / 涨停跌停炸板 / 连板高度
四、题材优先级与龙头   题材五项（资金/催化/持续性/龙头/地位）+ 多日涨停趋势
五、财经消息与影响     **LLM 筛选后的分析**，不是新闻列表
六、当日持仓与盈亏     逐只浮盈 + 当日/累计
七、次日计划与调仓     买入（含触发与止损止盈）/ 卖出 / 调整
八、四张表变动         每条都带原因
九、四张表当前状态
十、当日复盘与经验     做对/做错/题材兑现 + 新增经验
十一、风险与执行清单

设计约束
--------
- **代码生成**，绝不由 agent 拼 HTML
- 自包含单文件，零外部引用
- 数字全部来自程序；agent 只提供判断文字
- 不设「价格来源」「数据来源与缺口」两栏（用户明确要求去掉）
"""
from __future__ import annotations

import html
from typing import Any

_CSS = """
:root{
--ink:#0f172a; --ink2:#334155; --sub:#64748b; --line:#e2e8f0; --bg:#f6f8fb;
--card:#ffffff; --up:#e11d48; --down:#059669; --warn:#d97706;
--accent:#4f46e5; --accent2:#7c3aed;
}
*{box-sizing:border-box}
body{margin:0;padding:0 0 40px;background:var(--bg);color:var(--ink);
font:14px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
-webkit-font-smoothing:antialiased}
.wrap{max-width:1200px;margin:0 auto;padding:0 20px}
.top{background:linear-gradient(120deg,#1e1b4b 0%,#312e81 45%,#4c1d95 100%);
color:#fff;padding:26px 0 22px;margin-bottom:20px;
box-shadow:0 6px 24px -12px rgba(49,46,129,.55)}
.top h1{font-size:23px;margin:0 0 6px;font-weight:700;letter-spacing:.3px}
.top .meta{color:#c7d2fe;font-size:12px}
.top .chips{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}
.chip{background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.18);
border-radius:10px;padding:8px 14px;min-width:104px}
.chip .k{font-size:11px;color:#c7d2fe}
.chip .v{font-size:20px;font-weight:700;margin-top:1px;letter-spacing:.5px}
.chip.weak .v{color:#fda4af}.chip.mid .v{color:#fcd34d}.chip.strong .v{color:#6ee7b7}
h2{font-size:15px;margin:28px 0 12px;font-weight:700;color:var(--ink);
display:flex;align-items:center;gap:9px}
h2:before{content:"";width:4px;height:16px;border-radius:2px;
background:linear-gradient(180deg,var(--accent),var(--accent2))}
h3{font-size:13px;margin:18px 0 8px;color:var(--ink2);font-weight:600}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:10px;margin:10px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:11px 13px;box-shadow:0 1px 2px rgba(15,23,42,.03)}
.card .k{color:var(--sub);font-size:11.5px;letter-spacing:.2px}
.card .v{font-size:19px;font-weight:700;margin-top:3px;color:var(--ink)}
table{width:100%;border-collapse:separate;border-spacing:0;background:var(--card);
border:1px solid var(--line);border-radius:12px;overflow:hidden;margin:8px 0;
box-shadow:0 1px 2px rgba(15,23,42,.03)}
th,td{padding:8px 11px;text-align:left;border-bottom:1px solid #f1f5f9;
font-size:12.5px;vertical-align:top}
th{background:#f8fafc;font-weight:600;color:var(--ink2);white-space:nowrap;
font-size:12px;letter-spacing:.2px}
tbody tr:hover td{background:#fbfdff}
tr:last-child td{border-bottom:none}
.pos{color:var(--up);font-weight:600}
.neg{color:var(--down);font-weight:600}
.dim{color:var(--sub)}
.pill{display:inline-block;padding:1.5px 8px;border-radius:20px;font-size:11px;
border:1px solid var(--line);background:#f8fafc;white-space:nowrap;color:var(--ink2)}
.pill.hot{background:#fef2f2;border-color:#fecaca;color:#b91c1c}
.pill.ok{background:#ecfdf5;border-color:#a7f3d0;color:#047857}
.pill.mid{background:#fffbeb;border-color:#fde68a;color:#b45309}
.pill.mute{background:#f1f5f9;border-color:#e2e8f0;color:#64748b}
.note{background:#eff6ff;border:1px solid #bfdbfe;border-radius:12px;
padding:11px 14px;font-size:12.5px;color:#1e40af;margin:12px 0}
.warn{background:#fffbeb;border:1px solid #fde68a;border-radius:12px;
padding:11px 14px;font-size:12.5px;color:#92400e;margin:12px 0}
.err{background:#fef2f2;border:1px solid #fecaca;border-radius:12px;
padding:11px 14px;font-size:12.5px;color:#991b1b;margin:12px 0}
.bar{display:flex;height:26px;border-radius:8px;overflow:hidden;border:1px solid var(--line);
margin:8px 0;background:#fff}
.bar span{display:flex;align-items:center;justify-content:center;font-size:10.5px;
color:#fff;font-weight:600}
.news{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:12px 14px;margin:9px 0;box-shadow:0 1px 2px rgba(15,23,42,.03)}
.news .h{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.news .t{font-weight:600;font-size:13px;color:var(--ink)}
.news .s{font-size:12.5px;color:var(--ink2);margin-top:5px}
.news .r{font-size:12px;color:var(--sub);margin-top:5px;padding-left:10px;
border-left:2px solid #e2e8f0}
ul{margin:6px 0 6px 18px;padding:0}
li{margin:4px 0;font-size:12.5px;color:var(--ink2)}
.foot{margin-top:32px;color:var(--sub);font-size:11.5px;
border-top:1px solid var(--line);padding-top:14px;line-height:1.9}
code{background:#f1f5f9;padding:1px 6px;border-radius:5px;font-size:11.5px}
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
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _cards(items: list[tuple[str, str]]) -> str:
    return '<div class="cards">' + "".join(
        f'<div class="card"><div class="k">{_e(k)}</div><div class="v">{v}</div></div>'
        for k, v in items) + "</div>"


# ─────────────────────────────────────────────────────────────
# 顶部：决策摘要
# ─────────────────────────────────────────────────────────────
def _top(proposal: dict, payload: dict, market: dict) -> str:
    mv = proposal.get("market_view") or {}
    lo, hi = (mv.get("position_range_pct") or [None, None])
    trend = mv.get("trend") or "—"
    cls = {"强势": "strong", "弱势": "weak"}.get(trend, "mid")
    src = "LLM 分析" if (payload.get("proposal_source") or "llm") == "llm" else "规则兜底"

    chips = [
        f'<div class="chip {cls}"><div class="k">趋势判断</div>'
        f'<div class="v">{_e(trend)}</div></div>',
        f'<div class="chip"><div class="k">建议总仓位</div>'
        f'<div class="v">{("—" if lo is None else f"{lo:g}~{hi:g}%")}</div></div>',
        f'<div class="chip"><div class="k">置信度</div>'
        f'<div class="v">{_n(mv.get("confidence"), 0)}</div></div>',
        f'<div class="chip"><div class="k">判断来源</div>'
        f'<div class="v" style="font-size:15px">{_e(src)}</div></div>',
    ]
    reasons = mv.get("reasons") or []
    blocks = "".join(
        f'<li>{_e(r)}</li>' for r in reasons)
    return f"""
<div class="top"><div class="wrap">
  <h1>盘前预案 · {_e(payload.get("plan_date_iso") or payload.get("plan_date"))}</h1>
  <div class="meta">数据截至 {_e(market.get("date"))} 收盘 ｜
    生成 {_e(payload.get("generated_at"))} ｜ 锚点
    <code style="background:rgba(255,255,255,.14);color:#e0e7ff">
    {_e((payload.get("anchor") or {}).get("sha256", "")[:16])}…</code></div>
  <div class="chips">{''.join(chips)}</div>
</div></div>
""" + (f'<div class="wrap"><ul>{blocks}</ul></div>' if blocks else "")


# ─────────────────────────────────────────────────────────────
# 二、大盘环境与资金
# ─────────────────────────────────────────────────────────────
def _market(market: dict) -> str:
    out = []

    rows = []
    for d in market.get("index_detail") or []:
        ab = d.get("above") or {}
        marks = "".join(
            f'<span class="pill {"ok" if ab.get(k) else "mute"}">'
            f'{"站上" if ab.get(k) else "跌破"}{k.upper()}</span>'
            for k in ("ma5", "ma20", "ma60"))
        rows.append([
            _e(d.get("name")), _n(d.get("close")), _pct(d.get("chg_pct")),
            _pct(d.get("vs_ma20_pct")), _pct(d.get("chg_5d_pct")),
            _n(d.get("amplitude_pct"), 2, "%"),
            _n(d.get("vol_vs_ma5"), 2),
            f'{_e(d.get("streak_days"))}{_e(d.get("streak_dir"))}',
            marks,
        ])
    out.append("<h3>指数 K 线</h3>" + _table(
        ["指数", "收盘", "今日", "距MA20", "5日", "振幅", "量能倍数", "连续", "均线位置"],
        rows, empty="（指数数据缺失）"))

    ms = market.get("market_stats") or {}
    vs5 = ms.get("amount_vs_ma5_pct")
    tone = "warn" if (vs5 if vs5 is not None else 0) < -10 else "note"
    word = ("明显缩量，承接力偏弱" if (vs5 or 0) < -10 else
            "温和放量" if (vs5 or 0) > 10 else "量能平稳")
    out.append(f"""<div class="{tone}"><b>量能</b>：两市成交额
<b>{_n(ms.get("amount_yi"), 0, " 亿")}</b>（5 日均 {_n(ms.get("amount_ma5_yi"), 0, " 亿")}，
20 日均 {_n(ms.get("amount_ma20_yi"), 0, " 亿")}）→ 相对 5 日均 <b>{_pct(vs5)}</b>，
{_e(word)}。</div>""")

    nb = market.get("northbound") or []
    if nb:
        rows = [[_e(x.get("board")), _n(x.get("net_buy_yi"), 1, " 亿"),
                 f'{_e(x.get("up"))} / {_e(x.get("down"))}',
                 _pct(x.get("index_chg_pct"))] for x in nb]
        out.append("<h3>北向资金</h3>" +
                   _table(["板块", "成交净买额", "涨/跌家数", "指数涨跌"], rows))
        if all(abs(x.get("net_buy_yi") or 0) < 1e-9 for x in nb):
            out.append('<p class="dim" style="font-size:11.5px">'
                       '注：沪深港通实时额度已停止披露，净买额恒为 0，仅涨跌家数可用。</p>')

    lhb = market.get("lhb_top") or []
    if lhb:
        rows = [[_e(x.get("name")), _e(x.get("code")), _pct(x.get("chg_pct")),
                 _n(x.get("net_buy_wan"), 0, " 万"), _e(x.get("note"))]
                for x in lhb[:8]]
        out.append("<h3>龙虎榜（净买额前列）</h3>" +
                   _table(["名称", "代码", "涨跌", "净买额", "席位解读"], rows))
    return "".join(out)


# ─────────────────────────────────────────────────────────────
# 三、市场情绪与宽度
# ─────────────────────────────────────────────────────────────
def _sentiment(market: dict) -> str:
    b = market.get("breadth") or {}
    ms = market.get("market_stats") or {}
    out = [_cards([
        ("上涨 / 下跌", f'{_e(ms.get("n_up", b.get("up", "—")))} / '
                        f'{_e(ms.get("n_down", b.get("down", "—")))}'),
        ("上涨占比", _n(ms.get("up_ratio_pct"), 2, "%")),
        ("涨停 / 跌停", f'{_e(b.get("pool_zt", "—"))} / {_e(b.get("pool_dt", "—"))}'),
        ("炸板率", _n(b.get("break_rate_pct"), 2, "%")),
        ("最高连板", f'{_e(b.get("max_boards", "—"))} 板'),
        ("连板家数", _e(b.get("multi_board_n", "—"))),
        ("封板资金", _n(b.get("seal_amount_yi"), 1, " 亿")),
        ("活跃度", _n(b.get("activity"), 1)),
    ])]

    dist = ms.get("distribution") or {}
    order = ["<-7%", "-7~-5%", "-5~-2%", "-2~0%", "0~2%", "2~5%", "5~7%", ">7%"]
    colors = ["#047857", "#059669", "#34d399", "#a7f3d0",
              "#fecdd3", "#fb7185", "#e11d48", "#9f1239"]
    total = sum(dist.get(k, 0) for k in order) or 1
    segs = "".join(
        f'<span style="width:{dist.get(k, 0) / total * 100:.2f}%;background:{c}" '
        f'title="{k}: {dist.get(k, 0)}">'
        f'{(dist.get(k, 0) if dist.get(k, 0) / total > .06 else "")}</span>'
        for k, c in zip(order, colors))
    leg = "　".join(f'{k} <b>{dist.get(k, 0)}</b>' for k in order)
    out.append("<h3>涨跌分布（全市场逐只统计）</h3>"
               f'<div class="bar">{segs}</div>'
               f'<p class="dim" style="font-size:11.5px">左绿右红：{leg}'
               f'　样本 {_e(ms.get("n_total", "—"))} 只</p>')

    up5 = dist.get("5~7%", 0) + dist.get(">7%", 0)
    down5 = dist.get("-7~-5%", 0) + dist.get("<-7%", 0)
    verdict = ("跌超 5% 的家数多于涨超 5%，叠加炸板率偏高，接力环境差。"
               if down5 > up5 else "涨超 5% 的家数占优，但要看明日能否延续。")
    out.append(f'<div class="note"><b>情绪读数</b>：涨超 5% <b>{up5}</b> 只，'
               f'跌超 5% <b>{down5}</b> 只，炸板率 '
               f'<b>{_n(b.get("break_rate_pct"), 1, "%")}</b>，最高板 '
               f'<b>{_e(b.get("max_boards", "—"))}</b> 板。{_e(verdict)}</div>')
    return "".join(out)


# ─────────────────────────────────────────────────────────────
# 四、题材优先级与龙头（连板梯队并入）
# ─────────────────────────────────────────────────────────────
def _themes(market: dict, proposal: dict) -> str:
    pri = {s["name"]: s for s in (proposal.get("sector_priority") or [])}
    trend = (market.get("theme_trend") or {})
    series = trend.get("series") or {}
    days = trend.get("days") or []
    labels = " / ".join(str(d)[4:6] + "/" + str(d)[6:8] for d in days[-5:])

    rows = []
    for i, t in enumerate((market.get("themes") or [])[:12], 1):
        name = t["name"]
        p = pri.get(name) or {}
        s = series.get(name) or []
        trend_txt = " → ".join(str(x) for x in s) if s else "—"
        leaders = "、".join(l["name"] for l in (t.get("leaders") or [])[:2])
        rows.append([
            f'{i}. {_e(name)}', _n(t.get("limit_up_n"), 0),
            _e(t.get("max_boards", "—")), _n(t.get("seal_yi"), 1, " 亿"),
            _e(leaders or "—"),
            f'<span class="dim">{_e(trend_txt)}</span>',
            _n(p.get("strength"), 0) if p else '<span class="dim">—</span>',
            _e(p.get("leader") or "—"), _e(p.get("leader_status") or "—"),
        ])
    out = ["<h3>题材强度（按当日涨停家数）</h3>" + _table(
        ["题材", "涨停", "最高板", "封板资金", "日内龙头",
         f"近几日涨停（{labels}）", "Agent强度", "Agent龙头", "龙头地位"],
        rows, empty="（无题材数据）")]

    for s in proposal.get("sector_priority") or []:
        out.append(f"""
<div class="news">
  <div class="h"><span class="t">{_e(s.get("name"))}</span>
    <span class="pill hot">强度 {_n(s.get("strength"), 0)}</span>
    <span class="pill">龙头 {_e(s.get("leader") or "—")}</span>
    <span class="pill mute">{_e(s.get("leader_status") or "")}</span></div>
  <div class="s"><b>逻辑</b>：{_e(s.get("logic"))}</div>
  <div class="s"><b>资金</b>：{_e(s.get("fund_flow") or "—")}</div>
  <div class="s"><b>催化</b>：{_e(s.get("catalyst") or "—")}</div>
  <div class="s"><b>持续性</b>：{_e(s.get("sustainability") or "—")}</div>
  <div class="r"><b>证据</b>：{_e(s.get("evidence") or "—")}</div>
</div>""")
    return "".join(out)


# ─────────────────────────────────────────────────────────────
# 五、财经消息与影响（LLM 分析）
# ─────────────────────────────────────────────────────────────
def _news(proposal: dict) -> str:
    items = proposal.get("news_analysis") or []
    if not items:
        return ('<p class="dim">本次未筛出对交易有实质影响的消息'
                '（噪音已被丢弃，这本身是判断）。</p>')
    out = []
    for n in items:
        imp = n.get("importance")
        cls = {"高": "hot", "中": "mid"}.get(imp, "mute")
        eff = n.get("impact")
        eff_cls = "pos" if eff == "利好" else ("neg" if eff == "利空" else "dim")
        themes = "、".join(n.get("affected_themes") or []) or "—"
        stocks = "、".join(n.get("affected_stocks") or []) or "—"
        out.append(f"""
<div class="news">
  <div class="h"><span class="t">{_e(n.get("title"))}</span>
    <span class="pill {cls}">重要性 {_e(imp)}</span>
    <span class="pill {eff_cls}">{_e(eff)}</span></div>
  <div class="s">{_e(n.get("summary"))}</div>
  <div class="s"><b>影响题材</b>：{_e(themes)}　<b>影响个股</b>：{_e(stocks)}</div>
  <div class="r">{_e(n.get("reasoning"))}</div>
</div>""")
    return "".join(out)


# ─────────────────────────────────────────────────────────────
# 六、当日持仓与盈亏
# ─────────────────────────────────────────────────────────────
def _positions(review_ctx: dict, account: dict) -> str:
    if not review_ctx:
        return '<p class="dim">（无当日复盘素材）</p>'
    a = review_ctx.get("account") or {}
    out = [_cards([
        ("总资产", _n(a.get("total_pct"))),
        ("可用", _n(a.get("cash_pct"))),
        ("持仓市值", _n(a.get("market_value_pct"))),
        ("仓位", _n(a.get("position_ratio_pct"), 1, "%")),
        ("当日盈亏", _pct(a.get("day_pnl_ratio_pct"))),
        ("累计盈亏", _pct(a.get("cum_pnl_ratio_pct"))),
    ])]
    rows = [[_e(p.get("code")), _e(p.get("name")),
             _n(p.get("entry_price")), _n(p.get("last_price")),
             _pct(p.get("unrealized_pct")), _pct(p.get("today_chg_pct")),
             _n(p.get("stop_loss")), _n(p.get("take_profit")),
             _e(p.get("entry_logic") or "")]
            for p in (review_ctx.get("positions") or [])]
    out.append("<h3>持仓明细</h3>" + _table(
        ["代码", "名称", "成本", "现价", "浮盈", "当日", "止损", "止盈", "买入逻辑"],
        rows, empty="（今日空仓 —— 空仓也是一种决策，见第十节复盘）"))

    ex = review_ctx.get("executed") or []
    if ex:
        rows = []
        for t in ex:
            act = {"buy": "买入", "sell": "卖出"}.get(t.get("action"), "调整")
            val = (t.get("realized_pct") if t.get("realized_pct") is not None
                   else t.get("alloc_pct"))
            rows.append([_e(act), _e(t.get("code")), _e(t.get("name")),
                         _n(t.get("price") or t.get("exit_price")),
                         _pct(val),
                         _e((t.get("reason") or "")[:60])])
        out.append("<h3>今日按预案执行的成交</h3>" + _table(
            ["操作", "代码", "名称", "成交价", "已实现/仓位", "依据"], rows))
    else:
        out.append('<p class="dim">今日无成交（预案未触发或空仓）。</p>')
    return "".join(out)


# ─────────────────────────────────────────────────────────────
# 七、次日计划与调仓
# ─────────────────────────────────────────────────────────────
def _plan(planned: dict, account: dict) -> str:
    out = []
    buys = planned.get("buy") or []
    if buys:
        rows = [[_e(b.get("code")), _e(b.get("name")), _n(b.get("alloc_pct"), 1, "%"),
                 _n(b.get("stop_loss")), _n(b.get("take_profit")),
                 _n(b.get("ref_close")),
                 _e(b.get("trigger") or ""), _e(b.get("logic") or "")] for b in buys]
        out.append("<h3>计划买入</h3>" + _table(
            ["代码", "名称", "仓位", "止损", "止盈", "参考收盘", "触发条件", "逻辑"], rows))

    rows = [[_e(p.get("code")), _e(p.get("name")), _n(p.get("weight_pct"), 1, "%"),
             _n(p.get("entry_price")), _n(p.get("last_price")),
             _pct(p.get("unrealized_pct")), _n(p.get("stop_loss")),
             _n(p.get("take_profit"))] for p in (account.get("positions") or [])]
    if rows:
        out.append("<h3>当前持仓（明日管理对象）</h3>" + _table(
            ["代码", "名称", "占比", "成本", "现价", "浮盈", "止损", "止盈"], rows))

    sells = planned.get("sell") or []
    if sells:
        rows = [[_e(s.get("code")), _e(s.get("name")),
                 "全部" if s.get("all") else "部分", _e(s.get("reason") or "")]
                for s in sells]
        out.append("<h3>计划卖出</h3>" + _table(["代码", "名称", "比例", "原因"], rows))

    adj = planned.get("adjust") or []
    if adj:
        rows = [[_e(a.get("code")), _n(a.get("stop_loss")),
                 _n(a.get("take_profit")), _e(a.get("reason") or "")] for a in adj]
        out.append("<h3>计划调整止损/止盈</h3>" +
                   _table(["代码", "新止损", "新止盈", "原因"], rows))
    return "".join(out) or '<p class="dim">（本日无买卖计划）</p>'


# ─────────────────────────────────────────────────────────────
# 八、四张表变动
# ─────────────────────────────────────────────────────────────
def _changes(applied: dict, rejected: list) -> str:
    out = []
    LABELS = {"pool_add": "观察表 · 新增", "pool_update": "观察表 · 更新",
              "pool_remove": "观察表 · 移除", "info_add": "重要信息 · 新增",
              "info_remove": "重要信息 · 移除", "lesson_add": "经验表 · 新增",
              "lesson_remove": "经验表 · 移除"}

    def block(key: str, fmt) -> None:
        items = applied.get(key) or []
        if not items:
            return
        label = LABELS.get(key, key)
        rows = [[label, fmt(it)] for it in items]
        out.append(f"<h3>{_e(label)}</h3>" + _table(["项", "内容 / 原因"], rows))

    block("pool_add", lambda it: f"{_e(it.get('code'))} {_e(it.get('name'))} "
                                 f"[{_e(it.get('tier'))}] — {_e(it.get('reason'))}")
    block("pool_update", lambda it: f"{_e(it.get('code'))} 改 "
                                    f"{_e(', '.join(it.get('fields') or []))} — "
                                    f"{_e(it.get('reason'))}")
    block("pool_remove", lambda it: f"{_e(it.get('code'))} — {_e(it.get('reason'))}")
    block("info_add", lambda it: f"[{_e(it.get('level'))}] {_e(it.get('content'))}")
    block("info_remove", lambda it: f"{_e(it.get('id'))} — {_e(it.get('reason'))}")
    block("lesson_add", lambda it: f"[{_e(it.get('result'))}] {_e(it.get('lesson'))}")
    block("lesson_remove", lambda it: f"{_e(it.get('id'))} — {_e(it.get('reason'))}")

    if rejected:
        rows = [[_e(r.get("kind")), _e(r.get("target")), _e(r.get("reason"))]
                for r in rejected]
        out.append('<div class="err"><b>被内核拒绝的提议</b>'
                   '（没有静默丢弃，原因如下）</div>' +
                   _table(["类型", "对象", "拒绝原因"], rows))
    return "".join(out) or '<p class="dim">本日四张表无任何增删改。</p>'


# ─────────────────────────────────────────────────────────────
# 九、四张表当前状态
# ─────────────────────────────────────────────────────────────
def _tables(snapshot: dict) -> str:
    out = []
    pool = [r for r in (snapshot.get("pool") or []) if r.get("status") != "removed"]
    if pool:
        rows = [[_e(r.get("code")), _e(r.get("name")),
                 f'<span class="pill {"hot" if r.get("tier") == "core" else "mute"}">'
                 f'{_e("核心" if r.get("tier") == "core" else "普通")}</span>',
                 _e(r.get("trigger")), _n(r.get("stop_loss")),
                 _n(r.get("take_profit")), _e(r.get("added_date")),
                 _e(r.get("logic"))] for r in pool]
        out.append("<h3>选股观察表</h3>" + _table(
            ["代码", "名称", "等级", "触发条件", "止损", "止盈", "入选日", "入选逻辑"],
            rows))
    infos = snapshot.get("infos") or []
    if infos:
        rows = [[f'<span class="pill {"hot" if r.get("level") == "高" else "mute"}">'
                 f'{_e(r.get("level"))}</span>', _e(r.get("scope")),
                 _e(r.get("content")), _e(r.get("expires_on"))] for r in infos]
        out.append("<h3>重要信息表</h3>" +
                   _table(["等级", "范围", "内容", "有效期至"], rows))
    lessons = snapshot.get("lessons") or []
    if lessons:
        rows = [[f'<span class="pill {"ok" if r.get("result") == "成功经验" else "mid"}">'
                 f'{_e(r.get("result"))}</span>', _e(r.get("lesson")),
                 _e(r.get("scenario")), _e(r.get("recorded_on"))]
                for r in lessons[:12]]
        out.append("<h3>交易经验表（最近）</h3>" +
                   _table(["结果", "经验", "场景", "记录日"], rows))
    return "".join(out) or '<p class="dim">四张表当前均为空。</p>'


# ─────────────────────────────────────────────────────────────
# 十、当日复盘
# ─────────────────────────────────────────────────────────────
def _review(proposal: dict, review_ctx: dict) -> str:
    rv = proposal.get("review") or {}
    out = []
    worked = rv.get("what_worked") or []
    failed = rv.get("what_failed") or []
    if worked:
        out.append("<h3>做对了什么</h3><ul>" +
                   "".join(f"<li>{_e(x)}</li>" for x in worked) + "</ul>")
    if failed:
        out.append("<h3>做错了什么 / 没做到的</h3><ul>" +
                   "".join(f"<li>{_e(x)}</li>" for x in failed) + "</ul>")
    tv = rv.get("theme_verdict") or []
    if tv:
        rows = []
        for v in tv:
            vd = v.get("verdict")
            cls = {"兑现": "ok", "证伪": "hot"}.get(vd, "mute")
            rows.append([_e(v.get("theme")),
                         f'<span class="pill {cls}">{_e(vd)}</span>',
                         _e(v.get("note"))])
        out.append("<h3>题材兑现情况</h3>" + _table(["题材", "结论", "依据"], rows))

    ex = (review_ctx or {}).get("executed") or []
    sk = (review_ctx or {}).get("skipped") or []
    if ex:
        rows = [[_e(t.get("code")), _e(t.get("name")),
                 _pct(t.get("trade_return_pct")
                      if t.get("trade_return_pct") is not None
                      else t.get("realized_pct")),
                 _e((t.get("reason") or "")[:60])] for t in ex]
        out.append("<h3>成交与结果（程序结算）</h3>" +
                   _table(["代码", "名称", "本笔收益", "依据"], rows))
    if sk:
        rows = [[_e(s.get("action")), _e(s.get("code")), _e(s.get("reason"))]
                for s in sk]
        out.append("<h3>未执行项</h3>" + _table(["动作", "代码", "原因"], rows))
    return "".join(out) or '<p class="dim">（本次无复盘内容）</p>'


# ─────────────────────────────────────────────────────────────
# 十一、风险与执行
# ─────────────────────────────────────────────────────────────
def _risk(proposal: dict) -> str:
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


def _source_banner(payload: dict) -> str:
    if (payload.get("proposal_source") or "llm") == "llm":
        return ""
    reason = (payload.get("proposal") or {}).get("_fallback_reason") or ""
    return (f'<div class="wrap"><div class="err" style="margin-top:14px">'
            f'<b>本次为规则兜底，不是 agent 判断。</b><br>'
            f'未接入 LLM（{_e(reason[:160])}）。本预案只做了「大盘趋势 → 仓位区间」的'
            f'机械映射与题材排序，<b>没有生成个股计划、没有改动四张表</b>。'
            f'配置 LLM 后重新生成才会得到 agent 版本。</div></div>')


# ─────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────
def render_plan(payload: dict) -> str:
    """生成「盘前预案 + 盘后复盘」合并报告。"""
    market = payload.get("market") or {}
    proposal = payload.get("proposal") or {}
    applied = payload.get("applied") or {}
    rejected = payload.get("rejected") or []
    planned = payload.get("planned") or {}
    snapshot = payload.get("snapshot") or {}
    account = payload.get("account") or {}
    review_ctx = payload.get("review_context") or {}

    sections = [
        ("二、大盘环境与资金", _market(market)),
        ("三、市场情绪与宽度", _sentiment(market)),
        ("四、题材优先级与龙头", _themes(market, proposal)),
        ("五、财经消息与影响", _news(proposal)),
        ("六、当日持仓与盈亏", _positions(review_ctx, account)),
        ("七、次日计划与调仓", _plan(planned, account)),
        ("八、四张表变动（含原因）", _changes(applied, rejected)),
        ("九、四张表当前状态", _tables(snapshot)),
        ("十、当日复盘与经验", _review(proposal, review_ctx)),
        ("十一、风险与执行", _risk(proposal)),
    ]
    body = [f"<h2>{_e(h)}</h2>{c}" for h, c in sections]

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>盘前预案 · {_e(payload.get("plan_date_iso") or payload.get("plan_date"))}</title>
<style>{_CSS}</style></head><body>
{_top(proposal, payload, market)}
{_source_banner(payload)}
<div class="wrap">{"".join(body)}
<div class="foot">
本报告由 portal.premarket 生成：<b>所有数字由程序计算</b>（采集 + 本地 market.db +
账户核算），判断文字由 LLM 提供并逐条经内核校验；HTML 由代码渲染，非模型拼接。<br>
虚拟盘模拟，不含真实资金，<b>不构成投资建议</b>。市场有风险，决策需自负。
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
