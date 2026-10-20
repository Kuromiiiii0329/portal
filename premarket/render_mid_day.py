# -*- coding: utf-8 -*-
"""
portal/premarket/render_mid_day.py — 午间报告 HTML（独立模板）

为什么用另一套模板而不是复用晚间
--------------------------------
两件事的读者任务不同：

- 晚间报告：全交易日汇总 + 次日计划 → 读者要「明天怎么做」
- 午间报告：半日实况 + 预案核对 + 午后分支 → 读者要「现在改不改」

所以午报：

1. 顶部用**暖橙**配色，一眼区分于晚间的深靛蓝
2. 第一屏就是「上午盘面性质 + 半日成交额外推」
3. **核心章节是「预案 vs 上午现实」**（晚间没有这一节）
4. **午后判断用条件分支表格**（若 X 则 Y，否则 Z），不出现点位预测
5. 不出现：次日计划、5 日题材序列、复盘「做对/做错」、经验沉淀
"""
from __future__ import annotations

import html
from typing import Any

from .render import _e, _n, _pct, _table

_CSS = """
:root{
--ink:#1c1917; --ink2:#44403c; --sub:#78716c; --line:#e7e5e4; --bg:#faf9f7;
--card:#ffffff; --up:#e11d48; --down:#059669; --warn:#b45309;
--accent:#f59e0b; --accent2:#ea580c;
}
*{box-sizing:border-box}
body{margin:0;padding:0 0 36px;background:var(--bg);color:var(--ink);
font:14px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
-webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding:0 20px}
.top{background:linear-gradient(120deg,#7c2d12 0%,#b45309 45%,#d97706 100%);
color:#fff;padding:24px 0 20px;margin-bottom:18px;
box-shadow:0 6px 24px -12px rgba(180,83,9,.5)}
.top h1{font-size:22px;margin:0 0 6px;font-weight:700}
.top .meta{color:#fed7aa;font-size:12px}
.top .chips{display:flex;gap:10px;flex-wrap:wrap;margin-top:13px}
.chip{background:rgba(255,255,255,.14);border:1px solid rgba(255,255,255,.2);
border-radius:10px;padding:8px 14px;min-width:100px}
.chip .k{font-size:11px;color:#fed7aa}
.chip .v{font-size:19px;font-weight:700;margin-top:1px}
h2{font-size:15px;margin:26px 0 11px;font-weight:700;
display:flex;align-items:center;gap:9px}
h2:before{content:"";width:4px;height:16px;border-radius:2px;
background:linear-gradient(180deg,var(--accent),var(--accent2))}
h3{font-size:13px;margin:16px 0 8px;color:var(--ink2);font-weight:600}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(128px,1fr));gap:10px;margin:10px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:10px 13px;box-shadow:0 1px 2px rgba(28,25,23,.03)}
.card .k{color:var(--sub);font-size:11.5px}
.card .v{font-size:18px;font-weight:700;margin-top:2px}
table{width:100%;border-collapse:separate;border-spacing:0;background:var(--card);
border:1px solid var(--line);border-radius:12px;overflow:hidden;margin:8px 0;
box-shadow:0 1px 2px rgba(28,25,23,.03)}
th,td{padding:8px 11px;text-align:left;border-bottom:1px solid #f5f5f4;
font-size:12.5px;vertical-align:top}
th{background:#fafaf9;font-weight:600;color:var(--ink2);white-space:nowrap;font-size:12px}
tbody tr:hover td{background:#fffdf8}
tr:last-child td{border-bottom:none}
.pos{color:var(--up);font-weight:600}
.neg{color:var(--down);font-weight:600}
.dim{color:var(--sub)}
.pill{display:inline-block;padding:1.5px 8px;border-radius:20px;font-size:11px;
border:1px solid var(--line);background:#fafaf9;white-space:nowrap;color:var(--ink2)}
.pill.ok{background:#ecfdf5;border-color:#a7f3d0;color:#047857}
.pill.no{background:#fef2f2;border-color:#fecaca;color:#b91c1c}
.pill.mid{background:#fffbeb;border-color:#fde68a;color:#b45309}
.note{background:#fff7ed;border:1px solid #fed7aa;border-radius:12px;
padding:11px 14px;font-size:12.5px;color:#9a3412;margin:12px 0}
.err{background:#fef2f2;border:1px solid #fecaca;border-radius:12px;
padding:11px 14px;font-size:12.5px;color:#991b1b;margin:12px 0}
.bar{display:flex;height:24px;border-radius:8px;overflow:hidden;border:1px solid var(--line);
margin:8px 0;background:#fff}
.bar span{display:flex;align-items:center;justify-content:center;font-size:10.5px;
color:#fff;font-weight:600}
.news{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:11px 13px;margin:8px 0}
.news .h{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.news .t{font-weight:600;font-size:13px}
.news .s{font-size:12.5px;color:var(--ink2);margin-top:4px}
.news .r{font-size:12px;color:var(--sub);margin-top:4px;padding-left:9px;
border-left:2px solid #fed7aa}
ul{margin:6px 0 6px 18px;padding:0}
li{margin:4px 0;font-size:12.5px;color:var(--ink2)}
.foot{margin-top:30px;color:var(--sub);font-size:11.5px;
border-top:1px solid var(--line);padding-top:13px;line-height:1.9}
code{background:#f5f5f4;padding:1px 6px;border-radius:5px;font-size:11.5px}
"""


def _top(payload: dict, proposal: dict, market: dict) -> str:
    mv = proposal.get("morning_view") or {}
    ms = market.get("market_stats") or {}
    idx = (market.get("indices") or [{}])[0]
    tone = mv.get("tone") or "—"
    proj = None
    try:
        proj = round(float(ms.get("amount_yi") or 0) * 2, 0)
    except (TypeError, ValueError):
        proj = None
    bias = (proposal.get("afternoon_view") or {}).get("bias") or "—"
    risk = (proposal.get("afternoon_view") or {}).get("risk_level") or "—"
    src = "LLM 分析" if (payload.get("proposal_source") or "llm") == "llm" else "规则兜底"

    chips = [
        f'<div class="chip"><div class="k">上午盘面</div>'
        f'<div class="v">{_e(tone)}</div></div>',
        f'<div class="chip"><div class="k">午后倾向</div>'
        f'<div class="v">{_e(bias)}</div></div>',
        f'<div class="chip"><div class="k">半日成交额</div>'
        f'<div class="v">{_n(ms.get("amount_yi"), 0)} 亿</div></div>',
        f'<div class="chip"><div class="k">全天外推</div>'
        f'<div class="v">{_n(proj, 0)} 亿</div></div>',
        f'<div class="chip"><div class="k">风险</div>'
        f'<div class="v">{_e(risk)}</div></div>',
        f'<div class="chip"><div class="k">来源</div>'
        f'<div class="v" style="font-size:14px">{_e(src)}</div></div>',
    ]
    summary = mv.get("summary") or ""
    return f"""
<div class="top"><div class="wrap">
  <h1>午间报告 · {_e(payload.get("data_date"))}</h1>
  <div class="meta">上午收盘快照（官方统计时点 {_e(payload.get("legu_as_of"))}）
    ｜ 生成 {_e(payload.get("generated_at"))}</div>
  <div class="chips">{''.join(chips)}</div>
</div></div>
""" + (f'<div class="wrap"><div class="note"><b>一句话</b>：{_e(summary)}</div></div>'
       if summary else "")


def _morning(payload: dict, market: dict, proposal: dict) -> str:
    out = []
    rows = [[_e(i.get("name")), _n(i.get("close")), _pct(i.get("chg_pct")),
             _n(i.get("open")), _n(i.get("high")), _n(i.get("low")),
             _n(i.get("amount_yi"), 0, " 亿")]
            for i in (market.get("indices") or [])]
    out.append("<h3>指数（上午收盘）</h3>" + _table(
        ["指数", "上午收", "涨跌", "今开", "最高", "最低", "半日成交额"], rows,
        empty="（指数数据缺失）"))

    ms = market.get("market_stats") or {}
    b = market.get("breadth") or {}
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
    out.append("<h3>上午涨跌分布（全市场逐只统计）</h3>"
               f'<div class="bar">{segs}</div>'
               f'<p class="dim" style="font-size:11.5px">左绿右红：'
               + "　".join(f'{k} <b>{dist.get(k, 0)}</b>' for k in order)
               + f'　样本 {_e(ms.get("n_total", "—"))} 只</p>')

    pools = market.get("pools") or {}
    out.append(_cards_str([
        ("上涨 / 下跌", f'{_e(ms.get("n_up", "—"))} / {_e(ms.get("n_down", "—"))}'),
        ("上涨占比", _n(ms.get("up_ratio_pct"), 2, "%")),
        ("涨停（11:30）", _e(pools.get("zt", b.get("limit_up", "—")))),
        ("炸板（11:30）", _e(pools.get("zb", "—"))),
        ("炸板率", _n(pools.get("break_rate_pct"), 2, "%")),
        ("最高连板", f'{_e(pools.get("max_boards", "—"))} 板'),
        ("活跃度", _e(b.get("activity", "—"))),
        ("停牌", _e(b.get("halt", "—"))),
    ]))
    for r in (proposal.get("morning_view") or {}).get("reasons") or []:
        out.append(f'<li>{_e(r)}</li>')
    return "".join(out[:2]) + (("<ul>" + "".join(out[2:]) + "</ul>")
                               if len(out) > 2 else "")


def _cards_str(items: list[tuple[str, str]]) -> str:
    return '<div class="cards">' + "".join(
        f'<div class="card"><div class="k">{_e(k)}</div><div class="v">{v}</div></div>'
        for k, v in items) + "</div>"


def _sectors(proposal: dict, market: dict) -> str:
    out = []
    sc = proposal.get("sector_check") or []
    if sc:
        rows = []
        for s in sc:
            v = s.get("verdict")
            cls = {"延续": "ok", "熄火": "no"}.get(v, "mid")
            rows.append([_e(s.get("name")), _e(s.get("yesterday_n")),
                         _e(s.get("today_n")),
                         f'<span class="pill {cls}">{_e(v)}</span>',
                         _e(s.get("note"))])
        out.append("<h3>昨日强势题材：今天上午还剩下多少</h3>" + _table(
            ["题材", "昨日全天", "今日上午", "结论", "依据"], rows))
    raw = market.get("themes") or []
    if raw:
        rows = [[_e(t.get("name")), _e(t.get("yesterday_n")), _e(t.get("today_n")),
                 _pct(None)] for t in raw[:10]]
        rows = [[_e(t.get("name")), _e(t.get("yesterday_n")),
                 _e(t.get("today_n")),
                 f'<span class="{"pos" if (t.get("delta") or 0) > 0 else "neg"}">'
                 f'{(t.get("delta") or 0):+d}</span>'] for t in raw[:10]]
        out.append("<h3>涨停家数：昨日全天 vs 今日上午</h3>" + _table(
            ["题材", "昨日", "上午", "差"], rows))
        out.append(f'<p class="dim" style="font-size:11.5px">'
                   f'{_e(market.get("themes_note") or "")}</p>')
    return "".join(out) or '<p class="dim">（无题材数据）</p>'


def _news(proposal: dict, market: dict) -> str:
    items = proposal.get("news_analysis") or []
    win = (market.get("news_window") or "")
    if not items:
        return ('<p class="dim">上午没有筛出对交易有实质影响的消息'
                f'（窗口 {_e(win)}）。</p>')
    out = [f'<p class="dim" style="font-size:11.5px">消息窗口：{_e(win)}'
           f'（原始快讯 {len(market.get("news") or [])} 条，筛选后 {len(items)} 条）</p>']
    for n in items:
        imp = n.get("importance")
        cls = {"高": "no", "中": "mid"}.get(imp, "pill")
        eff = n.get("impact")
        eff_cls = "pos" if eff == "利好" else ("neg" if eff == "利空" else "dim")
        out.append(f"""
<div class="news">
  <div class="h"><span class="t">{_e(n.get("title"))}</span>
    <span class="pill {"no" if imp == "高" else "mid"}">重要性 {_e(imp)}</span>
    <span class="pill {eff_cls}">{_e(eff)}</span></div>
  <div class="s">{_e(n.get("summary"))}</div>
  <div class="s"><b>影响题材</b>：{_e("、".join(n.get("affected_themes") or []) or "—")}
    　<b>影响个股</b>：{_e("、".join(n.get("affected_stocks") or []) or "—")}</div>
  <div class="r">{_e(n.get("reasoning"))}</div>
</div>""")
    return "".join(out)


def _plan_check(proposal: dict, payload: dict) -> str:
    """核心章节：昨晚预案 vs 上午现实。"""
    pc = proposal.get("plan_check") or []
    head = (f'<p class="dim" style="font-size:11.5px">对照对象：'
            f'{_e(payload.get("last_plan_date") or "（无昨晚预案）")}</p>')
    if not pc:
        return head + ('<p class="dim">本次没有可核对的预案条目'
                       '（可能昨晚没生成预案）。</p>')
    rows = []
    for p in pc:
        v = p.get("verdict")
        cls = {"成立": "ok", "推翻": "no"}.get(v, "mid")
        rows.append([
            _e(p.get("item")), _e(p.get("expected")), _e(p.get("observed")),
            f'<span class="pill {cls}">{_e(v)}</span>',
            _e(p.get("action")),
        ])
    return head + _table(["核对项", "昨晚怎么说", "上午实际", "结论", "要不要改"], rows)


def _holdings(payload: dict) -> str:
    before = payload.get("account_before") or {}
    pos = before.get("positions") or []
    out = [_cards_str([
        ("总资产", _n(before.get("total_pct"))),
        ("可用", _n(before.get("cash_pct"))),
        ("持仓市值", _n(before.get("market_value_pct"))),
        ("仓位", _n(before.get("position_ratio_pct"), 1, "%")),
        ("盘中盈亏", _pct(before.get("day_pnl_ratio_pct"))),
        ("累计", _pct(before.get("cum_pnl_ratio_pct"))),
    ])]
    rows = [[_e(p.get("code")), _e(p.get("name")), _n(p.get("entry_price")),
             _n(p.get("last_price")), _pct(p.get("unrealized_pct")),
             _n(p.get("weight_pct"), 1, "%"), _n(p.get("stop_loss")),
             _n(p.get("take_profit"))] for p in pos]
    out.append("<h3>持仓（按上午收盘价）</h3>" + _table(
        ["代码", "名称", "成本", "上午收", "浮盈", "占比", "止损", "止盈"], rows,
        empty="（空仓 —— 午后无持仓风险）"))
    return "".join(out)


def _afternoon(proposal: dict) -> str:
    av = proposal.get("afternoon_view") or {}
    out = []
    conds = av.get("conditions") or []
    if conds:
        rows = [[f'<b>{_e(c.get("if"))}</b>', _e(c.get("then")),
                 _e(c.get("else_note"))] for c in conds]
        out.append("<h3>午后条件分支（若…则…否则…）</h3>" + _table(
            ["若", "则", "否则"], rows))
    else:
        out.append('<p class="dim">（未给出条件分支）</p>')
    if av.get("volume_projection"):
        out.append(f'<div class="note"><b>量能判断</b>：'
                   f'{_e(av.get("volume_projection"))}</div>')
    watch = proposal.get("watch_afternoon") or []
    if watch:
        out.append("<h3>午后盯什么</h3><ul>" +
                   "".join(f"<li>{_e(x)}</li>" for x in watch) + "</ul>")
    risk = proposal.get("risk_notes") or []
    if risk:
        out.append("<h3>风险提示</h3><ul>" +
                   "".join(f"<li>{_e(x)}</li>" for x in risk) + "</ul>")
    return "".join(out)


def _executed(payload: dict) -> str:
    ex = payload.get("executed") or []
    sk = payload.get("exec_skipped") or []
    rej = payload.get("rejected") or []
    out = []
    if ex:
        out.append('<div class="note">午后调仓<b>已按上午收盘价成交</b>'
                   '（不计费用；午报额度更保守）。</div>')
        rows = []
        for t in ex:
            act = {"buy": "买入", "sell": "卖出"}.get(t.get("action"), "调整")
            if t.get("action") == "sell":
                price, val = _n(t.get("exit_price")), _pct(t.get("realized_pct"))
            elif t.get("action") == "buy":
                price, val = _n(t.get("price")), _n(t.get("alloc_pct"), 1, "%")
            else:
                price = '<span class="dim">—</span>'
                val = f'止损 {_n(t.get("stop_loss"))} / 止盈 {_n(t.get("take_profit"))}'
            rows.append([_e(act), _e(t.get("code")), _e(t.get("name")), price, val,
                         _e((t.get("reason") or "")[:50])])
        out.append("<h3>已执行</h3>" + _table(
            ["操作", "代码", "名称", "成交价", "已实现/仓位", "依据"], rows))
    else:
        out.append('<p class="dim">上午无调仓（维持原持仓或空仓）。</p>')

    after = payload.get("account_after") or {}
    rows = [[_e(p.get("code")), _e(p.get("name")), _n(p.get("weight_pct"), 1, "%"),
             _n(p.get("entry_price")), _n(p.get("last_price")),
             _pct(p.get("unrealized_pct"))] for p in (after.get("positions") or [])]
    out.append("<h3>调仓后持仓（午后持有）</h3>" + _table(
        ["代码", "名称", "占比", "成本", "现价", "浮盈"], rows,
        empty="（午后空仓）"))

    if sk:
        rows = [[_e(s.get("action")), _e(s.get("code")), _e(s.get("reason"))]
                for s in sk]
        out.append("<h3>未执行</h3>" + _table(["动作", "代码", "原因"], rows))
    if rej:
        rows = [[_e(r.get("kind")), _e(r.get("target")), _e(r.get("reason"))]
                for r in rej]
        out.append('<div class="err"><b>被内核拒绝</b>（含禁止追高打板）</div>' +
                   _table(["类型", "对象", "原因"], rows))
    dropped = (payload.get("proposal") or {}).get("_dropped") or {}
    if dropped.get("info_add"):
        out.append(f'<p class="dim" style="font-size:11.5px">另有 {dropped["info_add"]} 条'
                   f'重要信息因字段不全（缺 scope/level/有效期）被丢弃 —— '
                   f'不猜影响范围，宁可不记。</p>')
    return "".join(out)


def _banner(payload: dict) -> str:
    if (payload.get("proposal_source") or "llm") == "llm":
        return ""
    return ('<div class="wrap"><div class="err" style="margin-top:12px">'
            '<b>本次为规则兜底，不是 agent 判断。</b><br>'
            '未接入 LLM，午报只汇总了上午数据，<b>没有预案核对、没有午后分支、'
            '没有调仓</b>。</div></div>')


def render_midday(payload: dict) -> str:
    market = payload.get("market") or {}
    proposal = payload.get("proposal") or {}
    sections = [
        ("一、上午概览", _morning(payload, market, proposal)),
        ("二、题材延续性", _sectors(proposal, market)),
        ("三、上午消息（9:00–11:30）", _news(proposal, market)),
        ("四、预案 vs 上午现实", _plan_check(proposal, payload)),
        ("五、持仓上午表现", _holdings(payload)),
        ("六、午后判断", _afternoon(proposal)),
        ("七、午后调仓", _executed(payload)),
    ]
    body = [f"<h2>{_e(h)}</h2>{c}" for h, c in sections]
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>午间报告 · {_e(payload.get("data_date"))}</title>
<style>{_CSS}</style></head><body>
{_top(payload, proposal, market)}
{_banner(payload)}
<div class="wrap">{"".join(body)}
<div class="foot">
午间报告只反映<b>上午 11:30 的半日数据</b>；成交额为半日实测，全天数字是 2 倍线性外推（估计值）。<br>
数字由程序计算，判断由 LLM 提供并经内核校验。<b>不构成投资建议</b>，市场有风险。
</div>
</div></body></html>"""


def write_midday(payload: dict, path) -> dict:
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = render_midday(payload)
    p.write_text(text, encoding="utf-8")
    return {"ok": True, "path": str(p), "bytes": len(text.encode("utf-8"))}


__all__ = ["render_midday", "write_midday"]
