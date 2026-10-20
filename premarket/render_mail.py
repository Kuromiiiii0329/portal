# -*- coding: utf-8 -*-
"""
portal/premarket/render_mail.py — 邮件正文版完整报告（**无附件**）

为什么不复用 render.py 的 HTML
----------------------------
邮件客户端不是浏览器：

- Gmail 会剥掉 `<style>` 的一部分、Outlook 桌面版**完全不认 CSS 变量**
  （`var(--accent)` 直接失效 → 颜色全白）、`flex` / `grid` 全不支持、
  渐变背景在 Outlook 里也经常丢失。

所以邮件版必须换一套写法：**零 `<style>`、全部内联、表格布局、固定宽度 680px**。
内容与网页版完全对齐（同样 11 节 / 7 节），只是呈现方式不同。

与网页版的分工
-------------
- 网页版 HTML：**只在门户手动点按钮时生成**（存档、可交互、好看）
- 邮件版 HTML：每次自动运行时生成，直接作为邮件正文发出，不需要附件

两者都由同一份 payload 渲染，因此不会出现「邮件和报告不一样」。
"""
from __future__ import annotations

import html
from typing import Any

#: 邮件正文最大宽度。超过 700px 时部分客户端会出现横向滚动条。
WIDTH = 680
MAX_BYTES = 100_000          # Gmail 会在 102 KB 处截断，留出余量

_INK = "#1f2937"
_INK2 = "#4b5563"
_SUB = "#6b7280"
_LINE = "#e5e7eb"
_BG = "#f9fafb"
_UP = "#dc2626"
_DOWN = "#059669"
_ACC = "#4f46e5"


def _e(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def _pct(v) -> str:
    if v is None:
        return "—"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return _e(v)
    color = _UP if x > 0 else (_DOWN if x < 0 else _SUB)
    return f'<span style="color:{color};font-weight:600">{x:+.2f}%</span>'


def _num(v, digits: int = 2, suffix: str = "") -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return _e(v)


def _h2(title: str) -> str:
    return (f'<div style="margin:26px 0 10px;padding-left:10px;'
            f'border-left:4px solid {_ACC};font-size:15px;font-weight:700;'
            f'color:{_INK};line-height:1.3">{_e(title)}</div>')


def _h3(title: str) -> str:
    return (f'<div style="margin:14px 0 6px;font-size:13px;font-weight:600;'
            f'color:{_INK2}">{_e(title)}</div>')


def _box(content: str, *, bg: str = "#eef2ff", border: str = "#c7d2fe",
         color: str = _INK) -> str:
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border-collapse:collapse;margin:10px 0"><tr>'
            f'<td style="background:{bg};border:1px solid {border};border-radius:8px;'
            f'padding:10px 12px;font-size:12.5px;line-height:1.7;color:{color}">'
            f'{content}</td></tr></table>')


def _table(headers: list[str], rows: list[list[str]],
           *, empty: str = "（无数据）") -> str:
    if not rows:
        return f'<div style="color:{_SUB};font-size:12px;padding:6px 0">{_e(empty)}</div>'
    th = "".join(
        f'<th align="left" style="padding:6px 8px;background:{_BG};'
        f'border-bottom:1px solid {_LINE};font-size:11.5px;font-weight:600;'
        f'color:{_INK2};white-space:nowrap">{_e(h)}</th>' for h in headers)
    body = []
    for r in rows:
        tds = "".join(
            f'<td style="padding:6px 8px;border-bottom:1px solid #f3f4f6;'
            f'font-size:12px;line-height:1.6;color:{_INK};vertical-align:top">{c}</td>'
            for c in r)
        body.append(f"<tr>{tds}</tr>")
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border-collapse:collapse;margin:8px 0;'
            f'border:1px solid {_LINE};border-radius:8px;overflow:hidden">'
            f'<tr>{th}</tr>{"".join(body)}</table>')


def _cards(items: list[tuple[str, str]], per_row: int = 3) -> str:
    """指标卡：用表格模拟，邮件里最稳。"""
    out = ['<table role="presentation" width="100%" cellpadding="0" '
           'cellspacing="0" style="border-collapse:separate;border-spacing:8px">']
    for i in range(0, len(items), per_row):
        chunk = items[i:i + per_row]
        cells = []
        for k, v in chunk:
            cells.append(
                f'<td width="{100 // per_row}%" style="background:#fff;'
                f'border:1px solid {_LINE};border-radius:8px;padding:8px 10px;'
                f'vertical-align:top">'
                f'<div style="font-size:11px;color:{_SUB};line-height:1.4">{_e(k)}</div>'
                f'<div style="font-size:16px;font-weight:700;color:{_INK};'
                f'margin-top:2px">{v}</div></td>')
        while len(cells) < per_row:
            cells.append('<td width="%d%%"></td>' % (100 // per_row))
        out.append("<tr>" + "".join(cells) + "</tr>")
    out.append("</table>")
    return "".join(out)


def _pill(text: str, *, tone: str = "mute") -> str:
    styles = {
        "ok": ("#ecfdf5", "#a7f3d0", "#047857"),
        "no": ("#fef2f2", "#fecaca", "#b91c1c"),
        "mid": ("#fffbeb", "#fde68a", "#b45309"),
        "hot": ("#eef2ff", "#c7d2fe", "#3730a3"),
        "mute": ("#f9fafb", "#e5e7eb", _INK2),
    }
    bg, bd, fg = styles.get(tone, styles["mute"])
    return (f'<span style="display:inline-block;padding:1px 7px;border-radius:10px;'
            f'background:{bg};border:1px solid {bd};color:{fg};font-size:11px;'
            f'white-space:nowrap">{_e(text)}</span>')


def _dist_bar(dist: dict) -> str:
    """涨跌分布条：用一行表格的宽度百分比模拟（邮件里 flex 不可用）。"""
    order = ["<-7%", "-7~-5%", "-5~-2%", "-2~0%", "0~2%", "2~5%", "5~7%", ">7%"]
    colors = ["#047857", "#059669", "#34d399", "#a7f3d0",
              "#fecdd3", "#fb7185", "#e11d48", "#9f1239"]
    total = sum(dist.get(k, 0) for k in order) or 1
    cells = []
    for k, c in zip(order, colors):
        n = dist.get(k, 0)
        w = n / total * 100
        if w <= 0:
            continue
        cells.append(f'<td width="{w:.2f}%" style="background:{c};height:18px;'
                     f'font-size:9px;color:#fff;text-align:center;line-height:18px">'
                     f'{n if w > 7 else ""}</td>')
    bar = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
           f'style="border-collapse:collapse;table-layout:fixed;'
           f'border-radius:6px;overflow:hidden"><tr>{"".join(cells)}</tr></table>')
    legend = "　".join(f"{k} <b>{dist.get(k, 0)}</b>" for k in order)
    return bar + (f'<div style="font-size:11px;color:{_SUB};margin-top:5px;'
                  f'line-height:1.7">左绿右红：{legend}</div>')


def _shell(title: str, subtitle: str, body: str, *, accent: str = _ACC) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:{_BG}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
  style="border-collapse:collapse;background:{_BG}"><tr><td align="center" style="padding:16px 8px">
<table role="presentation" width="{WIDTH}" cellpadding="0" cellspacing="0"
  style="border-collapse:collapse;width:{WIDTH}px;max-width:100%;background:#fff;
  border:1px solid {_LINE};border-radius:12px;overflow:hidden">
<tr><td style="background:{accent};padding:16px 18px">
  <div style="font-size:18px;font-weight:700;color:#fff;line-height:1.4">{_e(title)}</div>
  <div style="font-size:11.5px;color:#e0e7ff;margin-top:4px;line-height:1.6">{_e(subtitle)}</div>
</td></tr>
<tr><td style="padding:4px 18px 18px">{body}
  <div style="margin-top:24px;padding-top:12px;border-top:1px solid {_LINE};
  font-size:11px;color:{_SUB};line-height:1.8">
  数字由程序计算（采集 + 本地 market.db + 账户核算），判断文字由 LLM 提供并逐条经内核校验。<br>
  虚拟盘模拟，不含真实资金，<b>不构成投资建议</b>。市场有风险，决策需自负。
  </div>
</td></tr></table></div></td></tr></table></body></html>"""


def _source_note(source: str) -> str:
    if source == "llm":
        return ""
    return _box(
        "<b>本次为规则兜底，不是 agent 判断。</b><br>"
        "未接入 LLM，报告只汇总了数据，<b>没有趋势判断、题材优先级、复盘与个股计划</b>。"
        "配置 LLM 后重新生成才会得到 agent 版本。",
        bg="#fef2f2", border="#fecaca", color="#991b1b")


# ─────────────────────────────────────────────────────────────
# 晚间：11 节（与 render.render_plan 对齐）
# ─────────────────────────────────────────────────────────────
def _mv(p: dict) -> str:
    mv = p or {}
    lo, hi = (mv.get("position_range_pct") or [None, None])
    return _cards([
        ("趋势", f'<b>{_e(mv.get("trend"))}</b>'),
        ("建议仓位", f'{_num(lo, 0)}~{_num(hi, 0)}%'),
        ("置信度", _num(mv.get("confidence"), 0)),
    ], per_row=3) + _reasons(mv.get("reasons"))


def _reasons(items) -> str:
    if not items:
        return ""
    lis = "".join(
        f'<li style="margin:4px 0;font-size:12.5px;line-height:1.7;color:{_INK2}">'
        f'{_e(x)}</li>' for x in items)
    return f'<ul style="margin:8px 0 0 18px;padding:0">{lis}</ul>'


def _sec_market(market: dict) -> str:
    out = []
    rows = []
    for d in market.get("index_detail") or []:
        ab = d.get("above") or {}
        pos = " ".join(
            f'{"↑" if ab.get(k) else "↓"}{k.upper()}' for k in ("ma5", "ma20", "ma60"))
        rows.append([_e(d.get("name")), _num(d.get("close")),
                     _pct(d.get("chg_pct")), _pct(d.get("vs_ma20_pct")),
                     _pct(d.get("chg_5d_pct")), _num(d.get("vol_vs_ma5"), 2),
                     f'{_e(d.get("streak_days"))}{_e(d.get("streak_dir"))}',
                     _e(pos)])
    out.append(_table(["指数", "收盘", "今日", "距MA20", "5日", "量能倍数",
                       "连续", "均线"], rows, empty="（指数数据缺失）"))

    ms = market.get("market_stats") or {}
    vs5 = ms.get("amount_vs_ma5_pct")
    word = ("明显缩量，承接力偏弱" if (vs5 or 0) < -10 else
            "温和放量" if (vs5 or 0) > 10 else "量能平稳")
    out.append(_box(
        f'<b>量能</b>：两市成交额 <b>{_num(ms.get("amount_yi"), 0, " 亿")}</b>'
        f'（5 日均 {_num(ms.get("amount_ma5_yi"), 0, " 亿")}，'
        f'20 日均 {_num(ms.get("amount_ma20_yi"), 0, " 亿")}）'
        f'→ 相对 5 日均 <b>{_pct(vs5)}</b>，{_e(word)}。',
        bg="#fffbeb" if (vs5 or 0) < -10 else "#eef2ff",
        border="#fde68a" if (vs5 or 0) < -10 else "#c7d2fe"))

    nb = market.get("northbound") or []
    if nb:
        rows = [[_e(x.get("board")), _num(x.get("net_buy_yi"), 1, " 亿"),
                 f'{_e(x.get("up"))} / {_e(x.get("down"))}',
                 _pct(x.get("index_chg_pct"))] for x in nb]
        out.append(_h3("北向资金") + _table(
            ["板块", "成交净买额", "涨/跌家数", "指数涨跌"], rows))

    lhb = market.get("lhb_top") or []
    if lhb:
        rows = [[_e(x.get("name")), _e(x.get("code")), _pct(x.get("chg_pct")),
                 _num(x.get("net_buy_wan"), 0, " 万"), _e(x.get("note"))]
                for x in lhb[:8]]
        out.append(_h3("龙虎榜（净买额前列）") + _table(
            ["名称", "代码", "涨跌", "净买额", "席位解读"], rows))
    return "".join(out)


def _sec_sentiment(market: dict) -> str:
    b = market.get("breadth") or {}
    ms = market.get("market_stats") or {}
    out = [_cards([
        ("上涨 / 下跌", f'{_e(ms.get("n_up", b.get("up", "—")))} / '
                        f'{_e(ms.get("n_down", b.get("down", "—")))}'),
        ("上涨占比", _num(ms.get("up_ratio_pct"), 2, "%")),
        ("涨停 / 跌停", f'{_e(b.get("pool_zt", "—"))} / {_e(b.get("pool_dt", "—"))}'),
        ("炸板率", _num(b.get("break_rate_pct"), 2, "%")),
        ("最高连板", f'{_e(b.get("max_boards", "—"))} 板'),
        ("连板家数", _e(b.get("multi_board_n", "—"))),
        ("封板资金", _num(b.get("seal_amount_yi"), 1, " 亿")),
        ("活跃度", _num(b.get("activity"), 1)),
    ])]
    dist = ms.get("distribution") or {}
    if dist:
        out.append(_h3("涨跌分布（全市场逐只统计）") + _dist_bar(dist)
                   + f'<div style="font-size:11px;color:{_SUB}">'
                     f'样本 {_e(ms.get("n_total", "—"))} 只　'
                     f'来源 {_e(ms.get("source") or "")}</div>')
        up5 = dist.get("5~7%", 0) + dist.get(">7%", 0)
        down5 = dist.get("-7~-5%", 0) + dist.get("<-7%", 0)
        verdict = ("跌超 5% 的家数多于涨超 5%，叠加炸板率偏高，接力环境差。"
                   if down5 > up5 else "涨超 5% 的家数占优，但要看明日能否延续。")
        out.append(_box(f'<b>情绪读数</b>：涨超 5% <b>{up5}</b> 只，'
                        f'跌超 5% <b>{down5}</b> 只，'
                        f'炸板率 <b>{_num(b.get("break_rate_pct"), 1, "%")}</b>。'
                        f'{_e(verdict)}'))
    return "".join(out)


def _sec_themes(market: dict, proposal: dict) -> str:
    pri = {s.get("name"): s for s in (proposal.get("sector_priority") or [])}
    trend = market.get("theme_trend") or {}
    series = trend.get("series") or {}
    days = trend.get("days") or []
    labels = "/".join(str(d)[4:6] + str(d)[6:8] for d in days[-5:])
    rows = []
    for i, t in enumerate((market.get("themes") or [])[:12], 1):
        name = t.get("name")
        p = pri.get(name) or {}
        s = series.get(name) or []
        rows.append([
            f'{i}. {_e(name)}', _num(t.get("limit_up_n"), 0),
            _e(t.get("max_boards", "—")), _num(t.get("seal_yi"), 1, " 亿"),
            _e("、".join(l.get("name", "") for l in (t.get("leaders") or [])[:2]) or "—"),
            _e(" → ".join(str(x) for x in s) if s else "—"),
            _num(p.get("strength"), 0) if p else "—",
            _e(p.get("leader") or "—"),
        ])
    out = [_table([f"题材", "涨停", "最高板", "封板资金", "日内龙头",
                   f"近{labels}涨停", "强度", "Agent龙头"], rows,
                  empty="（无题材数据）")]
    for s in (proposal.get("sector_priority") or []):
        out.append(_box(
            f'<b>{_e(s.get("name"))}</b>　{_pill("强度 " + str(s.get("strength") or "—"), tone="hot")}'
            f'　{_pill("龙头 " + str(s.get("leader") or "—"))}'
            f'　{_pill(str(s.get("leader_status") or ""))}<br>'
            f'<b>逻辑</b>：{_e(s.get("logic"))}<br>'
            f'<b>资金</b>：{_e(s.get("fund_flow") or "—")}<br>'
            f'<b>催化</b>：{_e(s.get("catalyst") or "—")}<br>'
            f'<b>持续性</b>：{_e(s.get("sustainability") or "—")}<br>'
            f'<b>证据</b>：{_e(s.get("evidence") or "—")}',
            bg="#fff", border=_LINE))
    return "".join(out)


def _sec_news(proposal: dict) -> str:
    news = proposal.get("news_analysis") or []
    if not news:
        return f'<div style="color:{_SUB};font-size:12px">（本日无筛选后的重要消息）</div>'
    out = []
    for n in news[:6]:
        imp = n.get("importance")
        tone = "no" if imp == "高" else ("mid" if imp == "中" else "mute")
        eff = n.get("impact")
        ecol = _UP if eff == "利好" else (_DOWN if eff == "利空" else _SUB)
        out.append(
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border-collapse:collapse;margin:10px 0"><tr>'
            f'<td style="border-left:3px solid {_ACC};padding:2px 0 2px 10px">'
            f'<div style="font-size:13px;font-weight:600;color:{_INK};line-height:1.6">'
            f'{_e(n.get("title"))} {_pill("重要性 " + str(imp), tone=tone)} '
            f'<span style="color:{ecol};font-weight:600;font-size:12px">{_e(eff)}</span></div>'
            f'<div style="font-size:12.5px;color:{_INK2};margin-top:4px;line-height:1.7">'
            f'{_e(n.get("summary"))}</div>'
            f'<div style="font-size:11.5px;color:{_SUB};margin-top:4px;line-height:1.7">'
            f'影响题材：{_e("、".join(n.get("affected_themes") or []) or "—")}　'
            f'影响个股：{_e("、".join(n.get("affected_stocks") or []) or "—")}</div>'
            f'<div style="font-size:11.5px;color:{_SUB};margin-top:3px;line-height:1.7">'
            f'传导：{_e(n.get("reasoning"))}</div>'
            f'</td></tr></table>')
    return "".join(out)


def _sec_positions(review_ctx: dict, account: dict) -> str:
    if not review_ctx:
        return f'<div style="color:{_SUB};font-size:12px">（无当日复盘素材）</div>'
    a = review_ctx.get("account") or {}
    out = [_cards([
        ("总资产", _num(a.get("total_pct"))),
        ("可用", _num(a.get("cash_pct"))),
        ("持仓市值", _num(a.get("market_value_pct"))),
        ("仓位", _num(a.get("position_ratio_pct"), 1, "%")),
        ("当日盈亏", _pct(a.get("day_pnl_ratio_pct"))),
        ("累计盈亏", _pct(a.get("cum_pnl_ratio_pct"))),
    ])]
    rows = [[_e(p.get("code")), _e(p.get("name")), _num(p.get("entry_price")),
             _num(p.get("last_price")), _pct(p.get("unrealized_pct")),
             _pct(p.get("today_chg_pct")), _num(p.get("stop_loss")),
             _num(p.get("take_profit")), _e(p.get("entry_logic") or "")]
            for p in (review_ctx.get("positions") or [])]
    out.append(_table(["代码", "名称", "成本", "现价", "浮盈", "当日",
                       "止损", "止盈", "买入逻辑"], rows,
                      empty="（调仓前空仓 —— 空仓也是一种决策）"))
    return "".join(out)


def _sec_rebalance(planned: dict, account: dict, review_ctx: dict) -> str:
    review_ctx = review_ctx or {}
    ex = review_ctx.get("executed") or []
    sk = review_ctx.get("skipped") or []
    out = []
    if ex:
        rows = []
        for t in ex:
            act = {"buy": "买入", "sell": "卖出"}.get(t.get("action"), "调整")
            if t.get("action") == "sell":
                price, val = _num(t.get("exit_price")), _pct(t.get("realized_pct"))
            elif t.get("action") == "buy":
                price, val = _num(t.get("price")), _num(t.get("alloc_pct"), 1, "%")
            else:
                price = "—"
                val = f'止损 {_num(t.get("stop_loss"))} / 止盈 {_num(t.get("take_profit"))}'
            rows.append([_e(act), _e(t.get("code")), _e(t.get("name")), price, val,
                         _e((t.get("reason") or "")[:60])])
        out.append(_box("本次调仓<b>已按当日收盘价成交</b>（不计费用）。"))
        out.append(_table(["操作", "代码", "名称", "成交价", "已实现/仓位", "依据"], rows))
    else:
        out.append(f'<div style="color:{_SUB};font-size:12px">'
                   f'本次无调仓（维持原持仓或空仓）。</div>')
    if sk:
        rows = [[_e(s.get("action")), _e(s.get("code")), _e(s.get("reason"))]
                for s in sk]
        out.append(_h3("未执行") + _table(["动作", "代码", "原因"], rows))
    rows = [[_e(p.get("code")), _e(p.get("name")), _num(p.get("weight_pct"), 1, "%"),
             _num(p.get("entry_price")), _num(p.get("last_price")),
             _pct(p.get("unrealized_pct")), _num(p.get("stop_loss")),
             _num(p.get("take_profit"))] for p in (account.get("positions") or [])]
    out.append(_h3("调仓后持仓（明日持有）") + _table(
        ["代码", "名称", "占比", "成本", "现价", "浮盈", "止损", "止盈"], rows,
        empty="（调仓后空仓）"))
    return "".join(out)


_CN = {"pool_add": "观察表新增", "pool_update": "观察表更新", "pool_remove": "观察表移除",
       "info_add": "重要信息新增", "info_remove": "重要信息移除",
       "lesson_add": "经验新增", "lesson_remove": "经验移除"}


def _sec_changes(applied: dict, rejected: list) -> str:
    out = []
    rows = []
    for k, v in (applied or {}).items():
        for it in v or []:
            rows.append([_e(_CN.get(k, k)), _e(it.get("code") or it.get("id") or "—"),
                         _e(it.get("name") or it.get("info") or it.get("lesson") or "")])
    out.append(_table(["表", "对象", "内容"], rows, empty="（本次没有表变动）"))
    if rejected:
        r = [[_e(x.get("kind")), _e(x.get("target")), _e(x.get("reason"))]
             for x in rejected]
        out.append(_h3("被内核拒绝") + _box(
            "以下提议未通过校验，已拒绝并记录原因。", bg="#fef2f2",
            border="#fecaca", color="#991b1b") +
            _table(["类型", "对象", "原因"], r))
    return "".join(out)


def _sec_tables(snapshot: dict) -> str:
    snap = snapshot or {}
    pool = [r for r in (snap.get("pool") or []) if r.get("status") != "removed"]
    rows = [[_e(r.get("code")), _e(r.get("name")), _e(r.get("tier")),
             _e((r.get("logic") or "")[:50]), _num(r.get("stop_loss")),
             _num(r.get("take_profit")), _e((r.get("trigger") or "")[:50])]
            for r in pool]
    out = [_h3(f"选股观察表（{len(pool)} 只）") + _table(
        ["代码", "名称", "层级", "逻辑", "止损", "止盈", "触发条件"], rows,
        empty="（空）")]
    infos = snap.get("infos") or []
    rows = [[_e((i.get("content") or "")[:60]), _e(i.get("scope")),
             _e(i.get("level")), _e(i.get("expires_on"))] for i in infos[:10]]
    out.append(_h3(f"重要信息表（{len(infos)} 条）") + _table(
        ["内容", "影响范围", "级别", "有效期至"], rows, empty="（空）"))
    lessons = snap.get("lessons") or []
    rows = [[_e((x.get("lesson") or "")[:70]), _e(x.get("scenario")),
             _e(x.get("result"))] for x in lessons[:8]]
    out.append(_h3(f"交易经验表（{len(lessons)} 条）") + _table(
        ["经验", "情境", "结果"], rows, empty="（空）"))
    return "".join(out)


def _sec_review(proposal: dict, review_ctx: dict) -> str:
    rv = proposal.get("review") or {}
    out = []
    for key, label, color in (("what_worked", "做对了什么", _UP),
                              ("what_failed", "哪里错了", _DOWN)):
        items = rv.get(key) or []
        if items:
            lis = "".join(
                f'<li style="margin:4px 0;font-size:12.5px;line-height:1.7;'
                f'color:{_INK2}">{_e(x)}</li>' for x in items)
            out.append(f'<div style="font-size:13px;font-weight:600;color:{color};'
                       f'margin:10px 0 4px">{_e(label)}</div>'
                       f'<ul style="margin:0 0 0 18px;padding:0">{lis}</ul>')
    tv = rv.get("theme_verdict") or []
    if tv:
        rows = [[_e(x.get("theme")), _e(x.get("verdict")), _e(x.get("note"))]
                for x in tv]
        out.append(_h3("题材判断复盘") + _table(["题材", "结论", "依据"], rows))
    if not out:
        out.append(f'<div style="color:{_SUB};font-size:12px">（本次无复盘内容）</div>')
    return "".join(out)


def _sec_risk(proposal: dict) -> str:
    out = []
    risks = proposal.get("risk_notes") or []
    if risks:
        lis = "".join(f'<li style="margin:4px 0;font-size:12.5px;line-height:1.7;'
                      f'color:{_INK2}">{_e(x)}</li>' for x in risks)
        out.append(_h3("风险提示") + f'<ul style="margin:0 0 0 18px;padding:0">{lis}</ul>')
    cl = proposal.get("execution_checklist") or []
    if cl:
        lis = "".join(f'<li style="margin:4px 0;font-size:12.5px;line-height:1.7;'
                      f'color:{_INK2}">{_e(x)}</li>' for x in cl)
        out.append(_h3("次日执行清单") + f'<ul style="margin:0 0 0 18px;padding:0">{lis}</ul>')
    return "".join(out) or f'<div style="color:{_SUB};font-size:12px">（无）</div>'


def render_evening(payload: dict) -> tuple[str, str]:
    """返回 (subject, html)。内容与网页版 11 节一一对应。"""
    market = payload.get("market") or {}
    proposal = payload.get("proposal") or {}
    applied = payload.get("applied") or {}
    rejected = payload.get("rejected") or []
    planned = payload.get("planned") or {}
    snapshot = payload.get("snapshot") or {}
    account = payload.get("account") or {}
    review_ctx = payload.get("review_context") or {}
    iso = (payload.get("plan_date_iso")
           or str(payload.get("plan_date") or "")[:8])
    mv = proposal.get("market_view") or {}
    lo, hi = (mv.get("position_range_pct") or [None, None])
    subject = (f"【盘前预案 {iso}】{mv.get('trend', '')} 仓位 {lo:g}~{hi:g}%"
               if lo is not None else f"【盘前预案 {iso}】")

    body = [_h2("一、决策摘要"), _mv(mv),
            _source_note(payload.get("proposal_source") or "llm")]
    body.append(_h2("二、大盘环境与资金"))
    body.append(_sec_market(market))          # 指数 + 量能 + 北向 + 龙虎榜
    body.append(_h2("三、市场情绪与宽度"))
    body.append(_sec_sentiment(market))
    body.append(_h2("四、题材优先级与龙头"))
    body.append(_sec_themes(market, proposal))
    body.append(_h2("五、财经消息与影响"))
    body.append(_sec_news(proposal))
    body.append(_h2("六、当日持仓与盈亏"))
    body.append(_sec_positions(review_ctx, account))
    body.append(_h2("七、调仓（已按收盘价执行）"))
    body.append(_sec_rebalance(planned, account, review_ctx))
    body.append(_h2("八、四张表变动（含原因）"))
    body.append(_sec_changes(applied, rejected))
    body.append(_h2("九、四张表当前状态"))
    body.append(_sec_tables(snapshot))
    body.append(_h2("十、当日复盘与经验"))
    body.append(_sec_review(proposal, review_ctx))
    body.append(_h2("十一、风险与执行"))
    body.append(_sec_risk(proposal))

    sub = (f"数据截至 {_e(payload.get('trade_date'))} 收盘 ｜ "
           f"判断来源 {'LLM' if (payload.get('proposal_source') or 'llm') == 'llm' else '规则兜底'}")
    return subject, _shell(f"盘前预案 · {iso}", html.unescape(sub), "".join(body))


# ─────────────────────────────────────────────────────────────
# 午间：7 节（与 render_mid_day 对齐）
# ─────────────────────────────────────────────────────────────
def render_midday(payload: dict) -> tuple[str, str]:
    market = payload.get("market") or {}
    proposal = payload.get("proposal") or {}
    mvv = proposal.get("morning_view") or {}
    av = proposal.get("afternoon_view") or {}
    iso = payload.get("data_date") or ""
    subject = (f"【午报 {iso}】上午{mvv.get('tone', '')} "
               f"午后{av.get('bias', '')}")

    body = [_cards([
        ("上午盘面", f'<b>{_e(mvv.get("tone"))}</b>'),
        ("午后倾向", f'<b>{_e(av.get("bias"))}</b>'),
        ("风险", _e(av.get("risk_level"))),
    ]), _source_note(payload.get("proposal_source") or "llm")]
    if mvv.get("summary"):
        body.append(_box(_e(mvv["summary"])))
    body.append(_reasons(mvv.get("reasons")))

    body.append(_h2("一、上午概览"))
    rows = [[_e(i.get("name")), _num(i.get("close")), _pct(i.get("chg_pct")),
             _num(i.get("open")), _num(i.get("high")), _num(i.get("low")),
             _num(i.get("amount_yi"), 0, " 亿")]
            for i in (market.get("indices") or [])]
    body.append(_table(["指数", "上午收", "涨跌", "今开", "最高", "最低",
                        "半日成交额"], rows, empty="（指数数据缺失）"))
    ms = market.get("market_stats") or {}
    dist = ms.get("distribution") or {}
    if dist:
        body.append(_h3("上午涨跌分布") + _dist_bar(dist)
                    + f'<div style="font-size:11px;color:{_SUB}">'
                      f'样本 {_e(ms.get("n_total", "—"))} 只</div>')
    b = market.get("breadth") or {}
    pools = market.get("pools") or {}
    body.append(_cards([
        ("上涨 / 下跌", f'{_e(ms.get("n_up", "—"))} / {_e(ms.get("n_down", "—"))}'),
        ("上涨占比", _num(ms.get("up_ratio_pct"), 2, "%")),
        ("涨停(11:30)", _e(pools.get("zt", "—"))),
        ("炸板", _e(pools.get("zb", "—"))),
        ("最高连板", f'{_e(pools.get("max_boards", "—"))} 板'),
        ("活跃度", _e(b.get("activity", "—"))),
    ]))

    body.append(_h2("二、题材延续性"))
    sc = proposal.get("sector_check") or []
    rows = []
    for s in sc:
        cls = {"延续": "ok", "熄火": "no"}.get(s.get("verdict"), "mid")
        rows.append([_e(s.get("name")), _e(s.get("yesterday_n")),
                     _e(s.get("today_n")), _pill(s.get("verdict"), tone=cls),
                     _e(s.get("note"))])
    body.append(_table(["题材", "昨日全天", "今日上午", "结论", "依据"], rows,
                       empty="（无题材延续数据）"))
    raw = market.get("themes") or []
    if raw:
        rows = [[_e(t.get("name")), _e(t.get("yesterday_n")),
                 _e(t.get("today_n")), _e(f'{(t.get("delta") or 0):+d}')]
                for t in raw[:10]]
        body.append(_h3("涨停家数：昨日全天 vs 今日上午") + _table(
            ["题材", "昨日", "上午", "差"], rows))

    body.append(_h2("三、上午消息（9:00–11:30）"))
    news = proposal.get("news_analysis") or []
    if news:
        for n in news[:5]:
            tone = "no" if n.get("importance") == "高" else "mid"
            body.append(
                f'<div style="font-size:13px;font-weight:600;color:{_INK};'
                f'margin:8px 0 3px;line-height:1.6">{_e(n.get("title"))} '
                f'{_pill("重要性 " + str(n.get("importance")), tone=tone)} '
                f'{_pill(str(n.get("impact")))}</div>'
                f'<div style="font-size:12.5px;color:{_INK2};line-height:1.7">'
                f'{_e(n.get("summary"))}</div>')
    else:
        body.append(f'<div style="color:{_SUB};font-size:12px">'
                    f'上午没有筛出对交易有实质影响的消息。</div>')

    body.append(_h2("四、预案 vs 上午现实"))
    pc = proposal.get("plan_check") or []
    rows = []
    for p in pc:
        cls = {"成立": "ok", "推翻": "no"}.get(p.get("verdict"), "mid")
        rows.append([_e(p.get("item")), _e(p.get("expected")),
                     _e(p.get("observed")), _pill(p.get("verdict"), tone=cls),
                     _e(p.get("action"))])
    body.append(_table(["核对项", "昨晚怎么说", "上午实际", "结论", "要不要改"], rows,
                       empty="（本次没有可核对的预案条目）"))

    body.append(_h2("五、持仓上午表现"))
    before = payload.get("account_before") or {}
    body.append(_cards([
        ("总资产", _num(before.get("total_pct"))),
        ("可用", _num(before.get("cash_pct"))),
        ("仓位", _num(before.get("position_ratio_pct"), 1, "%")),
        ("盘中盈亏", _pct(before.get("day_pnl_ratio_pct"))),
        ("累计", _pct(before.get("cum_pnl_ratio_pct"))),
    ], per_row=3))
    rows = [[_e(p.get("code")), _e(p.get("name")), _num(p.get("entry_price")),
             _num(p.get("last_price")), _pct(p.get("unrealized_pct")),
             _num(p.get("weight_pct"), 1, "%")]
            for p in (before.get("positions") or [])]
    body.append(_table(["代码", "名称", "成本", "上午收", "浮盈", "占比"], rows,
                       empty="（空仓 —— 午后无持仓风险）"))

    body.append(_h2("六、午后判断"))
    conds = av.get("conditions") or []
    rows = [[_e(c.get("if")), _e(c.get("then")), _e(c.get("else_note"))]
            for c in conds]
    body.append(_table(["若", "则", "否则"], rows, empty="（未给出条件分支）"))
    if av.get("volume_projection"):
        body.append(_box(f'<b>量能判断</b>：{_e(av.get("volume_projection"))}',
                         bg="#fffbeb", border="#fde68a"))
    watch = proposal.get("watch_afternoon") or []
    if watch:
        lis = "".join(f'<li style="margin:3px 0;font-size:12.5px;line-height:1.7;'
                      f'color:{_INK2}">{_e(x)}</li>' for x in watch)
        body.append(_h3("午后盯什么") +
                    f'<ul style="margin:0 0 0 18px;padding:0">{lis}</ul>')
    risk = proposal.get("risk_notes") or []
    if risk:
        lis = "".join(f'<li style="margin:3px 0;font-size:12.5px;line-height:1.7;'
                      f'color:{_INK2}">{_e(x)}</li>' for x in risk)
        body.append(_h3("风险提示") +
                    f'<ul style="margin:0 0 0 18px;padding:0">{lis}</ul>')

    body.append(_h2("七、午后调仓"))
    ex = payload.get("executed") or []
    if ex:
        rows = []
        for t in ex:
            act = {"buy": "买入", "sell": "卖出"}.get(t.get("action"), "调整")
            rows.append([_e(act), _e(t.get("code")), _e(t.get("name")),
                         _num(t.get("price") or t.get("exit_price"))])
        body.append(_box("午后调仓<b>已按上午收盘价成交</b>（不计费用）。"))
        body.append(_table(["操作", "代码", "名称", "成交价"], rows))
    else:
        body.append(f'<div style="color:{_SUB};font-size:12px">'
                    f'上午无调仓（维持原持仓或空仓）。</div>')
    rej = payload.get("rejected") or []
    if rej:
        rows = [[_e(x.get("kind")), _e(x.get("target")), _e(x.get("reason"))]
                for x in rej]
        body.append(_h3("被内核拒绝") + _table(["类型", "对象", "原因"], rows))

    sub = (f"上午收盘快照（{payload.get('legu_as_of')}）｜ 判断来源 "
           f"{'LLM' if (payload.get('proposal_source') or 'llm') == 'llm' else '规则兜底'}")
    return subject, _shell(f"午间报告 · {iso}", sub, "".join(body), accent="#b45309")


def write_mail(payload: dict, path, *, kind: str) -> dict:
    """把完整报告写成邮件正文 HTML。返回 {subject, bytes, path, oversize}。

    关于体积：Gmail 在 ~102 KB 处会显示"[消息已剪裁]"，但正文**不会丢**（可点开全文）。
    所以超限**不报错**——报错等于今天一封邮件都不发。这里只标记 `oversize`，
    由调用方记一条警告，便于后续精简章节。
    """
    from pathlib import Path
    subject, text = (render_midday(payload) if kind == "midday"
                     else render_evening(payload))
    data = text.encode("utf-8")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return {"ok": True, "subject": subject, "path": str(p), "bytes": len(data),
            "oversize": len(data) > MAX_BYTES}


__all__ = ["render_evening", "render_midday", "write_mail", "WIDTH", "MAX_BYTES"]
