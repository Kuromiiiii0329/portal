# -*- coding: utf-8 -*-
"""
portal/premarket/mail_summary.py — 生成**邮件正文**（不是报告本身）

为什么单独做一层
----------------
GitHub Action 里发邮件用的是 marketplace 的 action（`dawidd6/action-send-mail`），
它接收的是**字符串正文** + **附件文件**。所以：

- 正文：一份 3–6 KB 的紧凑 HTML，手机上直接能读（关键数字 + 判断 + 调仓）
- 附件：完整报告 HTML（几十 KB，存档用）

**不用 portal 自带的邮件发送器**（`send_report.py` / lib 里的 EmailSender）——
按用户要求，发送完全交给 Action，仓库里不需要 SMTP 代码，也不需要邮箱密码。

退出码约定（workflow 靠它决定要不要发信）
    0 = 已生成正文
    3 = 今天没有可发的报告（节假日 / 任务跳过）→ workflow 跳过发信，不算失败
    1 = 出错
"""
from __future__ import annotations

import argparse
import datetime
import html
import json
import sys
from pathlib import Path

from . import paths


def _e(v) -> str:
    return html.escape("" if v is None else str(v))


def _pct(v) -> str:
    if v is None:
        return "—"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return _e(v)
    color = "#c00" if x > 0 else ("#080" if x < 0 else "#666")
    return f'<span style="color:{color}">{x:+.2f}%</span>'


def _num(v, digits: int = 2, suffix: str = "") -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return _e(v)


_CSS = ("font:14px/1.7 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif;"
        "color:#111;background:#fafafa;padding:0;margin:0")
_H = ("margin:18px 0 8px;font-size:14px;font-weight:700;color:#111;"
      "border-left:4px solid #4f46e5;padding-left:8px")
_T = ("width:100%;border-collapse:collapse;font-size:12.5px;background:#fff;"
      "margin:6px 0")
_TH = "background:#f2f2f2;padding:6px 8px;text-align:left;font-weight:600"
_TD = "border-top:1px solid #eee;padding:6px 8px;vertical-align:top"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return ""
    head = "".join(f'<th style="{_TH}">{_e(h)}</th>' for h in headers)
    body = "".join(
        "<tr>" + "".join(f'<td style="{_TD}">{c}</td>' for c in r) + "</tr>"
        for r in rows)
    return (f'<table style="{_T}" cellspacing="0" cellpadding="0">'
            f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")


def _wrap(title: str, subtitle: str, body: str) -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="{_CSS}">
<div style="max-width:760px;margin:0 auto;padding:16px">
<div style="background:linear-gradient(120deg,#1e1b4b,#4c1d95);color:#fff;
border-radius:10px;padding:14px 16px">
<div style="font-size:17px;font-weight:700">{_e(title)}</div>
<div style="font-size:12px;color:#c7d2fe;margin-top:3px">{_e(subtitle)}</div>
</div>
{body}
<p style="color:#888;font-size:11.5px;margin-top:22px;border-top:1px solid #e5e5e5;
padding-top:10px">完整报告见附件。虚拟盘模拟，不含真实资金，不构成投资建议。</p>
</div></body></html>"""


# ─────────────────────────────────────────────────────────────
# 晚间报告摘要
# ─────────────────────────────────────────────────────────────
def build_evening(plan: dict) -> tuple[str, str, str]:
    prop = plan.get("proposal") or {}
    mv = prop.get("market_view") or {}
    lo, hi = (mv.get("position_range_pct") or [None, None])
    plan_iso = plan.get("plan_date") or ""
    if len(plan_iso) == 8:
        plan_iso = f"{plan_iso[:4]}-{plan_iso[4:6]}-{plan_iso[6:8]}"
    subject = (f"【盘前预案 {plan_iso}】{mv.get('trend', '')} "
               f"仓位 {lo:g}~{hi:g}%" if lo is not None else f"【盘前预案 {plan_iso}】")

    parts = []
    # 摘要
    parts.append(
        f'<div style="{_H}">决策摘要</div>'
        f'<div style="background:#fff;border-radius:8px;padding:10px 12px">'
        f'趋势 <b>{_e(mv.get("trend"))}</b>　建议仓位 <b>{_num(lo, 0)}~{_num(hi, 0)}%</b>　'
        f'置信度 <b>{_num(mv.get("confidence"), 0)}</b></div>')
    reasons = mv.get("reasons") or []
    if reasons:
        parts.append("<ul style='margin:8px 0 0 18px;padding:0;font-size:12.5px'>" +
                     "".join(f"<li>{_e(r)}</li>" for r in reasons[:4]) + "</ul>")

    # 当日盈亏
    aa = plan.get("account_after") or {}
    if aa:
        parts.append(f'<div style="{_H}">调仓后账户</div>' + _table(
            ["总资产", "可用", "仓位", "累计"],
            [[_num(aa.get("total_pct")), _num(aa.get("cash_pct")),
              _num(aa.get("position_ratio_pct"), 1, "%"),
              _pct(aa.get("cum_pnl_ratio_pct"))]]))

    # 执行
    ex = plan.get("executed_now") or []
    if ex:
        rows = []
        for t in ex:
            act = {"buy": "买入", "sell": "卖出"}.get(t.get("action"), "调整")
            val = (_pct(t.get("realized_pct")) if t.get("action") == "sell"
                   else _num(t.get("alloc_pct"), 1, "%"))
            rows.append([_e(act), f'{_e(t.get("code"))} {_e(t.get("name"))}',
                         _num(t.get("price") or t.get("exit_price")), val])
        parts.append(f'<div style="{_H}">本次调仓（收盘价成交）</div>' +
                     _table(["操作", "标的", "成交价", "已实现/仓位"], rows))

    # 题材
    sp = prop.get("sector_priority") or []
    if sp:
        rows = [[_e(s.get("name")), _num(s.get("strength"), 0),
                 _e(s.get("leader") or "—"), _e((s.get("catalyst") or "")[:40])]
                for s in sp[:4]]
        parts.append(f'<div style="{_H}">题材优先级</div>' +
                     _table(["题材", "强度", "龙头", "催化"], rows))

    # 消息
    news = prop.get("news_analysis") or []
    if news:
        rows = [[_e(f'[{n.get("importance")}]'), _e(n.get("title")),
                 _e(n.get("impact")), _e((n.get("summary") or "")[:60])]
                for n in news[:5]]
        parts.append(f'<div style="{_H}">重要消息</div>' +
                     _table(["度", "消息", "影响", "要点"], rows))

    # 复盘
    rv = (prop.get("review") or {})
    worked, failed = rv.get("what_worked") or [], rv.get("what_failed") or []
    if worked or failed:
        items = ""
        for x in worked[:3]:
            items += f"<li>✅ {_e(x)}</li>"
        for x in failed[:3]:
            items += f"<li>⚠️ {_e(x)}</li>"
        parts.append(f'<div style="{_H}">当日复盘</div>'
                     f"<ul style='margin:6px 0 0 18px;padding:0;font-size:12.5px'>"
                     f"{items}</ul>")

    # 风险
    risk = prop.get("risk_notes") or []
    if risk:
        parts.append(f'<div style="{_H}">风险提示</div>'
                     f"<ul style='margin:6px 0 0 18px;padding:0;font-size:12.5px'>" +
                     "".join(f"<li>{_e(r)}</li>" for r in risk[:4]) + "</ul>")

    headline = f"数据截至 {_e(plan.get('trade_date'))} 收盘 ｜ 判断来源 " \
               f"{'LLM' if (plan.get('proposal_source') or 'llm') == 'llm' else '规则兜底'}"
    return subject, _wrap(f"盘前预案 · {plan_iso}", headline, "".join(parts)), plan_iso


# ─────────────────────────────────────────────────────────────
# 午间报告摘要
# ─────────────────────────────────────────────────────────────
def build_midday(mid: dict) -> tuple[str, str, str]:
    prop = mid.get("proposal") or {}
    mvv = prop.get("morning_view") or {}
    av = prop.get("afternoon_view") or {}
    iso = mid.get("data_date") or ""
    subject = (f"【午报 {iso}】上午{mvv.get('tone', '')} "
               f"午后{av.get('bias', '')}")

    parts = []
    parts.append(
        f'<div style="{_H}">上午盘面</div>'
        f'<div style="background:#fff;border-radius:8px;padding:10px 12px">'
        f'上午 <b>{_e(mvv.get("tone"))}</b>　午后倾向 <b>{_e(av.get("bias"))}</b>　'
        f'风险 <b>{_e(av.get("risk_level"))}</b></div>')
    if mvv.get("summary"):
        parts.append(f'<p style="font-size:12.5px;margin:8px 0">'
                     f'{_e(mvv["summary"])}</p>')

    # 预案核对（核心）
    pc = prop.get("plan_check") or []
    if pc:
        rows = [[_e(p.get("item")), _e(p.get("verdict")),
                 _e((p.get("observed") or "")[:56])] for p in pc[:6]]
        parts.append(f'<div style="{_H}">昨晚预案 vs 上午现实</div>' +
                     _table(["核对项", "结论", "上午实际"], rows))

    # 题材延续
    sc = prop.get("sector_check") or []
    if sc:
        rows = [[_e(s.get("name")), _num(s.get("yesterday_n"), 0),
                 _num(s.get("today_n"), 0), _e(s.get("verdict"))]
                for s in sc[:6]]
        parts.append(f'<div style="{_H}">题材延续性</div>' +
                     _table(["题材", "昨日", "上午", "结论"], rows))

    # 消息
    news = prop.get("news_analysis") or []
    if news:
        rows = [[_e(f'[{n.get("importance")}]'), _e(n.get("title")),
                 _e(n.get("impact"))] for n in news[:4]]
        parts.append(f'<div style="{_H}">上午消息</div>' +
                     _table(["度", "消息", "影响"], rows))

    # 午后条件分支
    conds = av.get("conditions") or []
    if conds:
        rows = [[_e(c.get("if")), _e(c.get("then")),
                 _e(c.get("else_note"))] for c in conds[:4]]
        parts.append(f'<div style="{_H}">午后条件分支</div>' +
                     _table(["若", "则", "否则"], rows))
    if av.get("volume_projection"):
        parts.append(f'<p style="font-size:12.5px;background:#fff7ed;'
                     f'border-left:3px solid #f59e0b;padding:8px 10px;margin:8px 0">'
                     f'<b>量能</b>：{_e(av["volume_projection"])}</p>')

    # 调仓
    ex = mid.get("executed") or []
    if ex:
        rows = []
        for t in ex:
            act = {"buy": "买入", "sell": "卖出"}.get(t.get("action"), "调整")
            rows.append([_e(act), f'{_e(t.get("code"))} {_e(t.get("name"))}',
                         _num(t.get("price") or t.get("exit_price"))])
        parts.append(f'<div style="{_H}">午后调仓（11:30 价成交）</div>' +
                     _table(["操作", "标的", "成交价"], rows))
    else:
        parts.append(f'<p style="font-size:12.5px;color:#666">'
                     f'上午无调仓（维持原持仓或空仓）。</p>')

    headline = (f"上午收盘快照（{_e(mid.get('legu_as_of'))}）｜ 判断来源 "
                f"{'LLM' if (mid.get('proposal_source') or 'llm') == 'llm' else '规则兜底'}")
    return subject, _wrap(f"午间报告 · {iso}", headline, "".join(parts)), iso


# ─────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────
def _newest(directory: Path, pattern: str) -> Path | None:
    if not directory.exists():
        return None
    files = sorted(directory.glob(pattern), key=lambda f: f.stat().st_mtime)
    return files[-1] if files else None


def build(kind: str, *, date8: str | None = None) -> tuple[str, str, str]:
    """返回 (subject, html, date_iso)。找不到报告抛 FileNotFoundError。"""
    if kind == "midday":
        cand = ((paths.REPORTS / f"{date8}_midday.json") if date8
                else _newest(paths.REPORTS, "*_midday.json"))
        if not cand or not Path(cand).exists():
            raise FileNotFoundError("没有午报数据（今天可能没跑午报）")
        mid = json.loads(Path(cand).read_text(encoding="utf-8"))
        return build_midday(mid)

    # ★ 预案 JSON 在 plans/ 下，不在 reports/ —— 第一版 glob 错了目录，
    #   结果明明有预案却报"没有晚间预案数据"。
    cand = (paths.plan_path(date8) if date8
            else _newest(paths.PLANS, "plan_*.json"))
    if not cand or not Path(cand).exists():
        raise FileNotFoundError("没有晚间预案数据")
    plan = json.loads(Path(cand).read_text(encoding="utf-8"))
    return build_evening(plan)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m portal.premarket.mail_summary")
    ap.add_argument("--kind", choices=["evening", "midday"], required=True)
    ap.add_argument("--date", default=None, help="YYYYMMDD（默认取最新）")
    ap.add_argument("--out", default=None, help="正文 HTML 输出路径")
    ap.add_argument("--subject-out", default=None, help="主题写入的文件")
    ap.add_argument("--attachment-out", default=None, help="完整报告路径写入的文件")
    args = ap.parse_args(argv)

    try:
        subject, body, iso = build(args.kind, date8=args.date)
    except FileNotFoundError as exc:
        # 退出码 3 = 今天没有可发的报告，workflow 跳过发信（不算失败）
        print(json.dumps({"ok": False, "skip": True,
                          "reason": str(exc)}, ensure_ascii=False))
        return 3
    except Exception as exc:
        print(json.dumps({"ok": False,
                          "error": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False))
        return 1

    d8 = iso.replace("-", "")[:8]
    attach = (paths.REPORTS / f"{d8}_midday.html" if args.kind == "midday"
              else paths.REPORTS / f"{d8}_premarket.html")
    out = Path(args.out) if args.out else (paths.REPORTS / f"{d8}_{args.kind}_mail.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")
    if args.subject_out:
        Path(args.subject_out).write_text(subject, encoding="utf-8")
    if args.attachment_out:
        Path(args.attachment_out).write_text(str(attach), encoding="utf-8")

    print(json.dumps({
        "ok": True, "kind": args.kind, "date": iso,
        "subject": subject, "body_path": str(out),
        "body_bytes": out.stat().st_size,
        "attachment": str(attach), "attachment_exists": attach.exists(),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
