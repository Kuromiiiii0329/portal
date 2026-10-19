# -*- coding: utf-8 -*-
"""
portal/virtual/report.py — 自包含 HTML 报告（复盘 / 闸门状态）

两条硬要求（都来自原包的实测教训）：

1. **自包含**。原包踩过：HTML 挂 CDN 引 echarts，本机网络抖动导致整页图表全灭
   （`pageerror: echarts is not defined`）。所以这里**不引任何外部资源**，
   纯 inline CSS + 表格。生成后用正则断言「外部引用 = 0」。

2. **发报前过审计**。`audit.assert_signable()` 不通过就拒绝写文件 ——
   一份数字无来源、参数与引擎不一致、把同步指标写成预测的报告，
   不该出现在磁盘上。

「生成了文件」≠「图表能画」：本模块不做 canvas，所以不需要 Playwright 数 canvas；
但如果将来加图表，必须补上这一步（原包 §4 的教训）。
"""
from __future__ import annotations

import datetime
import html
import re
from pathlib import Path
from typing import Any

from . import audit as audit_mod
from . import paths

_CSS = """
:root{--ink:#1a1a1a;--sub:#666;--line:#e3e3e3;--red:#c62828;--green:#2e7d32;
--amber:#ef6c00;--bg:#fafafa;--card:#fff}
*{box-sizing:border-box}
body{margin:0;padding:28px;background:var(--bg);color:var(--ink);
font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1080px;margin:0 auto}
h1{font-size:21px;margin:0 0 4px}
h2{font-size:15px;margin:26px 0 10px;padding-left:9px;border-left:3px solid var(--ink)}
.meta{color:var(--sub);font-size:12px;margin-bottom:18px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin:14px 0}
.card{flex:1 1 150px;background:var(--card);border:1px solid var(--line);
border-radius:8px;padding:12px 14px}
.card .k{color:var(--sub);font-size:12px}
.card .v{font-size:20px;font-weight:600;margin-top:3px}
table{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line);
font-size:13px;vertical-align:top}
th{background:#f0f0f0;font-weight:600;color:#333}
tr:last-child td{border-bottom:none}
.pos{color:var(--red)}.neg{color:var(--green)}.warn{color:var(--amber)}
.pill{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;
border:1px solid var(--line);background:#f7f7f7}
.pill.ok{background:#e8f5e9;border-color:#a5d6a7;color:#256029}
.pill.no{background:#ffebee;border-color:#ef9a9a;color:#b71c1c}
.note{background:#fff8e1;border:1px solid #ffe082;border-radius:8px;
padding:10px 13px;font-size:12.5px;color:#6d4c00;margin:12px 0}
.foot{margin-top:26px;color:var(--sub);font-size:11.5px;
border-top:1px solid var(--line);padding-top:12px}
code{background:#f2f2f2;padding:1px 5px;border-radius:4px;font-size:12px}
"""


def _esc(x: Any) -> str:
    return html.escape(str(x if x is not None else "—"))


def _cls(v: float | None) -> str:
    if v is None:
        return ""
    return "pos" if v > 0 else ("neg" if v < 0 else "")


def _fmt(v: Any, suffix: str = "", digits: int = 2) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, (int, float)):
        return f"{v:+.{digits}f}{suffix}" if suffix else f"{v:.{digits}f}"
    return _esc(v)


def _cards(kpis: list[dict]) -> str:
    if not kpis:
        return ""
    items = "".join(
        f'<div class="card"><div class="k">{_esc(k.get("label"))}</div>'
        f'<div class="v {_cls(k.get("value") if isinstance(k.get("value"), (int, float)) else None)}">'
        f'{_esc(k.get("display") if k.get("display") is not None else _fmt(k.get("value")))}</div></div>'
        for k in kpis
    )
    return f'<div class="cards">{items}</div>'


def _gate_table(gate: dict | None) -> str:
    if not gate:
        return ""
    rows = []
    for c in gate.get("checks") or []:
        rows.append(
            f'<tr><td>{_esc(c.get("code"))}</td>'
            f'<td>{"<span class=\'pill ok\'>通过</span>" if c.get("passed") else "<span class=\'pill no\'>拒绝</span>"}</td>'
            f'<td>{_esc(c.get("detail"))}</td></tr>'
        )
    return ("<table><tr><th>闸门</th><th>结果</th><th>依据</th></tr>"
            + "".join(rows) + "</table>")


def _rulers_table(rulers: dict | None) -> str:
    if not rulers:
        return ""
    a = rulers.get("attribution") or {}
    rows = [
        ("实际（自己选的票）", a.get("actual_pct")),
        ("影子组合（不做 gate/过滤）", a.get("shadow_pct")),
        ("基准组合（昨日全部涨停股）", a.get("baseline_pct")),
        ("大盘（沪深300）", (rulers.get("index") or {}).get("ret_pct")),
    ]
    body = "".join(
        f'<tr><td>{_esc(k)}</td><td class="{_cls(v)}">{_fmt(v, "%", 3)}</td></tr>'
        for k, v in rows
    )
    extra = ""
    if a.get("execution_pp") is not None or a.get("selection_pp") is not None:
        extra = (
            "<table style='margin-top:10px'>"
            "<tr><th>归因</th><th>数值</th><th>含义</th></tr>"
            f"<tr><td>执行贡献</td><td class='{_cls(a.get('execution_pp'))}'>"
            f"{_fmt(a.get('execution_pp'), 'pp', 3)}</td>"
            f"<td>执行规则是加分还是减分</td></tr>"
            f"<tr><td>选股贡献</td><td class='{_cls(a.get('selection_pp'))}'>"
            f"{_fmt(a.get('selection_pp'), 'pp', 3)}</td>"
            f"<td>选股有超越「无脑买昨日涨停」吗</td></tr>"
            "</table>"
        )
    basis = a.get("basis")
    note = f'<div class="note">归因口径：{_esc(basis)}</div>' if basis else ""
    return ("<table><tr><th>尺子</th><th>收益率</th></tr>" + body + "</table>"
            + extra + note)


def _rules_table(rules: list[dict] | None) -> str:
    if not rules:
        return "<p class='meta'>尚无规则记录。</p>"
    body = []
    for r in rules:
        flags = r.get("flags") or []
        flag_html = "<br>".join(f"<span class='warn'>⚠ {_esc(f)}</span>" for f in flags)
        body.append(
            f"<tr><td><code>{_esc(r.get('rule_id'))}</code></td>"
            f"<td>{_esc(r.get('version'))}</td>"
            f"<td><span class='pill'>{_esc(r.get('status'))}</span></td>"
            f"<td>{_esc(r.get('scope'))}</td>"
            f"<td>{_esc(r.get('n'))}</td>"
            f"<td class='{_cls(r.get('mean_pct'))}'>{_fmt(r.get('mean_pct'), '%', 3)}</td>"
            f"<td class='{_cls(r.get('after_cost_pct'))}'>{_fmt(r.get('after_cost_pct'), '%', 3)}</td>"
            f"<td>{_esc(r.get('desc'))}{flag_html}</td></tr>"
        )
    return ("<table><tr><th>规则</th><th>v</th><th>状态</th><th>范围</th>"
            "<th>n</th><th>均值</th><th>扣费后</th><th>说明 / 提示</th></tr>"
            + "".join(body) + "</table>")


def _findings(findings: list[dict] | None) -> str:
    if not findings:
        return "<p class='meta'>审计未发现问题。</p>"
    items = []
    for f in findings:
        sev = "no" if f.get("severity") == "blocker" else ""
        items.append(
            f"<li><span class='pill {sev}'>{_esc(f.get('code'))}</span> "
            f"{_esc(f.get('message'))}</li>"
        )
    return "<ul>" + "".join(items) + "</ul>"


def render(payload: dict) -> str:
    """把结构化载荷渲染成自包含 HTML 字符串（不写盘、不审计）。"""
    title = payload.get("title") or "虚拟盘报告"
    now = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    sections = []

    if payload.get("kpis"):
        sections.append(_cards(payload["kpis"]))
    if payload.get("gate"):
        sections.append("<h2>开仓前提打分</h2>" + _gate_table(payload["gate"]))
    if payload.get("rulers"):
        sections.append("<h2>三把零自由度尺子</h2>" + _rulers_table(payload["rulers"]))
    if payload.get("rules") is not None:
        sections.append("<h2>规则库</h2>" + _rules_table(payload.get("rules")))
    if payload.get("body_html"):
        sections.append("<h2>明细</h2>" + str(payload["body_html"]))
    if payload.get("audit"):
        sections.append("<h2>签发前审计</h2>" + _findings(payload["audit"].get("findings")))
    if payload.get("notes"):
        sections.append('<div class="note">' + "<br>".join(
            _esc(n) for n in payload["notes"]) + "</div>")

    caliber = payload.get("caliber")
    caliber_html = f"<div class='note'>口径：{_esc(caliber)}</div>" if caliber else ""

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title><style>{_CSS}</style></head>
<body><div class="wrap">
<h1>{_esc(title)}</h1>
<div class="meta">生成时间 {_esc(now)} ｜ 数据根 <code>{_esc(paths.VIRTUAL_ROOT)}</code></div>
{caliber_html}
{''.join(sections)}
<div class="foot">
本报告由 portal.virtual 生成，成交与收益均由确定性内核计算，未经人工修饰。<br>
所有历史数字均为虚拟盘或历史统计结果，<b>不构成投资建议</b>。市场有风险，决策需自负。
</div>
</div></body></html>"""


_EXTERNAL_REF = re.compile(
    r"""(?:<script[^>]+src=|<link[^>]+href=|<img[^>]+src=)\s*['"]?(?:https?:)?//""",
    re.IGNORECASE,
)


def external_refs(html_text: str) -> list[str]:
    """返回外部资源引用（应为空）。自包含是本模块的硬要求。"""
    return _EXTERNAL_REF.findall(html_text or "")


def build_and_write(payload: dict, *, filename: str | None = None,
                    out_dir: Path | None = None,
                    require_audit_pass: bool = True) -> dict[str, Any]:
    """渲染 → 审计 → 写盘。审计不通过则不写文件。"""
    payload = dict(payload)
    if require_audit_pass:
        payload.setdefault("audit", audit_mod.audit(payload))
        payload["audit"] = audit_mod.audit(payload)

    html_text = render(payload)
    refs = external_refs(html_text)
    if refs:
        raise ValueError(
            f"报告含 {len(refs)} 处外部引用，违反自包含要求：{refs[:3]}")

    if require_audit_pass and payload.get("audit", {}).get("blocked"):
        return {
            "ok": False,
            "written": False,
            "reason": "未通过签发前审计",
            "audit": payload["audit"],
        }

    paths.ensure_dirs()
    out_dir = out_dir or (paths.VIRTUAL_ROOT / "reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    name = filename or f"virtual_{datetime.date.today():%Y%m%d}.html"
    p = out_dir / name
    p.write_text(html_text, encoding="utf-8")
    return {"ok": True, "written": True, "path": str(p),
            "bytes": len(html_text.encode("utf-8")),
            "external_refs": 0,
            "audit": payload.get("audit")}


__all__ = [
    "render",
    "external_refs",
    "build_and_write",
]
