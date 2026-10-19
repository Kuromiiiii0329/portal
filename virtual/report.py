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

「生成了文件」≠「图表能画」：带图表的报告必须用
`python -m portal.virtual.verify_report <html>` 做一次真实渲染校验
（Playwright 数 canvas + 收集 pageerror）。这是原包 §4 的教训。
"""
from __future__ import annotations

import datetime
import html
import json
import re
from pathlib import Path
from typing import Any

from . import audit as audit_mod
from . import paths

#: 内联 echarts 的默认路径（仓库自带，保证离线可用）
ECHARTS_PATH = Path(__file__).resolve().parent / "assets" / "echarts.min.js"
_ECHARTS_CACHE: str | None = None


class ChartAssetError(RuntimeError):
    """缺少 echarts 资源 —— 必须报错，不能静默降级成空白图。"""


def echarts_js() -> str:
    """读取 echarts 源码（进程内缓存）。缺文件就抛错，不返回空串。"""
    global _ECHARTS_CACHE
    if _ECHARTS_CACHE is None:
        if not ECHARTS_PATH.exists():
            raise ChartAssetError(
                f"找不到 {ECHARTS_PATH}。图表需要内联 echarts（离线可用）；"
                f"把 echarts.min.js 放到 virtual/assets/ 下，"
                f"或调用 render(..., with_charts=False) 生成纯表格版。")
        _ECHARTS_CACHE = ECHARTS_PATH.read_text(encoding="utf-8", errors="ignore")
    return _ECHARTS_CACHE


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
.chart{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:8px;margin:10px 0}
.chart-t{font-size:12.5px;font-weight:600;color:#333;padding:4px 6px 0}
.chart-note{font-size:11.5px;color:var(--sub);padding:0 6px 6px}
.chart-box{width:100%;height:300px}
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


def _notes_block(notes: list | None) -> str:
    if not notes:
        return ""
    return '<div class="note">' + "<br>".join(_esc(n) for n in notes) + "</div>"


# ─────────────────────────────────────────────────────────────
# 图表
# ─────────────────────────────────────────────────────────────
def _js(obj: Any) -> str:
    """把 Python 对象安全嵌入 <script>：转义 `<` 防止提前闭合标签。"""
    return json.dumps(obj, ensure_ascii=False).replace("<", "\\u003c")


def _chart_html(spec: dict, idx: int) -> str:
    title = spec.get("title") or "图表"
    note = spec.get("note")
    return (
        f'<div class="chart"><div class="chart-t">{_esc(title)}</div>'
        + (f'<div class="chart-note">{_esc(note)}</div>' if note else "")
        + f'<div class="chart-box" id="chart{idx}"></div></div>'
    )


def _chart_script(specs: list[dict]) -> str:
    """生成初始化脚本。**不含 echarts 本体**，本体由 render() 单独内联。"""
    configs = []
    for i, s in enumerate(specs):
        cfg = {
            "id": f"chart{i}",
            "type": s.get("type", "line"),
            "x": s.get("x") or [],
            "series": s.get("series") or [],
            "yName": s.get("y_name") or "",
            "valueFmt": s.get("value_fmt") or "num",
            "zeroLine": bool(s.get("zero_line", False)),
        }
        configs.append(cfg)
    return """
<script>
(function(){
  var CFG = %s;
  function fmt(v, kind){
    if (v === null || v === undefined || isNaN(v)) return '-';
    if (kind === 'pct') return (v>0?'+':'') + Number(v).toFixed(2) + '%%';
    if (kind === 'money') return Number(v).toLocaleString('zh-CN', {maximumFractionDigits:0});
    return String(v);
  }
  function build(c){
    var el = document.getElementById(c.id);
    if (!el || typeof echarts === 'undefined') return;
    var isBar = c.type === 'bar';
    var opt = {
      animation: false,
      tooltip: {trigger: 'axis', valueFormatter: function(v){ return fmt(v, c.valueFmt); }},
      legend: {show: c.series.length > 1, bottom: 0, textStyle: {fontSize: 11}},
      grid: {left: 8, right: 16, top: 16, bottom: c.series.length > 1 ? 34 : 26,
             containLabel: true},
      xAxis: {type: 'category', data: c.x,
              axisLabel: {fontSize: 10, color: '#666'}, axisTick: {show: false}},
      yAxis: {type: 'value', name: c.yName, nameTextStyle: {fontSize: 10, color: '#999'},
              axisLabel: {fontSize: 10, color: '#666',
                          formatter: function(v){ return fmt(v, c.valueFmt); }},
              splitLine: {lineStyle: {color: '#f0f0f0'}}},
      series: c.series.map(function(s, i){
        var base = {name: s.name, type: isBar ? 'bar' : 'line', data: s.data,
                    itemStyle: {color: (s.color || '#c62828')},
                    lineStyle: {width: 1.6, color: (s.color || '#c62828')},
                    symbol: 'circle', symbolSize: 3};
        if (isBar) base.barMaxWidth = 34;
        if (i === 0) base.markLine = c.zeroLine ? {silent: true, symbol: 'none',
          lineStyle: {color: '#bbb', type: 'dashed'},
          data: [{yAxis: 0}]} : undefined;
        return base;
      })
    };
    var chart = echarts.init(el, null, {renderer: 'canvas'});
    chart.setOption(opt);
    window.addEventListener('resize', function(){ chart.resize(); });
    window.__PORTAL_CHARTS__ = (window.__PORTAL_CHARTS__ || 0) + 1;
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function(){ CFG.forEach(build); });
  } else { CFG.forEach(build); }
})();
</script>""" % _js(configs)


def equity_chart(points: list[dict], *, title: str = "净值曲线") -> dict:
    """从 `[{date, total_asset}]` 生成净值曲线配置。"""
    pts = [p for p in points if isinstance(p.get("total_asset"), (int, float))]
    return {
        "title": title,
        "note": "纵轴为期末总资产（元）。数据来自每次买入日结算。",
        "type": "line",
        "x": [str(p.get("date") or "") for p in pts],
        "series": [{"name": "总资产", "data": [round(float(p["total_asset"]), 2) for p in pts],
                    "color": "#c62828"}],
        "value_fmt": "money",
    }


def ruler_chart(rulers: dict | None, *, title: str = "三把零自由度尺子") -> dict | None:
    """实际 / 影子 / 基准 / 大盘 的收益率对比（柱状）。"""
    if not rulers:
        return None
    a = rulers.get("attribution") or {}
    idx = (rulers.get("index") or {}).get("ret_pct")
    items = [
        ("实际", a.get("actual_pct"), "#c62828"),
        ("影子", a.get("shadow_pct"), "#ef6c00"),
        ("基准", a.get("baseline_pct"), "#5c6bc0"),
        ("大盘", idx, "#78909c"),
    ]
    items = [(k, v, c) for k, v, c in items if isinstance(v, (int, float))]
    if not items:
        return None
    return {
        "title": title,
        "note": "口径统一到「投入资金」。执行贡献 = 实际 − 影子；选股贡献 = 影子 − 基准。",
        "type": "bar",
        "x": [k for k, _, _ in items],
        "series": [{"name": "收益率%", "data": [round(float(v), 3) for _, v, _ in items],
                    "color": "#c62828"}],
        "value_fmt": "pct",
        "zero_line": True,
    }


def calibration_chart(rows: list[dict], *, title: str = "每笔已实现收益") -> dict | None:
    """每笔平仓的已实现收益率（按时间顺序），用来看分布而不是看均值。"""
    pts = [(i + 1, r.get("realized_pct")) for i, r in enumerate(rows)
           if isinstance(r.get("realized_pct"), (int, float))]
    if not pts:
        return None
    return {
        "title": title,
        "note": "逐笔而非均值：均值会掩盖「少数大赢撑起平均数」。",
        "type": "bar",
        "x": [f"#{i}" for i, _ in pts],
        "series": [{"name": "已实现%", "data": [round(float(v), 3) for _, v in pts],
                    "color": "#2e7d32"}],
        "value_fmt": "pct",
        "zero_line": True,
    }


def _charts(payload: dict) -> tuple[str, str]:
    """返回 (容器 HTML, 脚本 HTML)。无图表时两者都为空。"""
    specs = [c for c in (payload.get("charts") or []) if c]
    if not specs or payload.get("with_charts") is False:
        return "", ""
    html_part = "".join(_chart_html(s, i) for i, s in enumerate(specs))
    script = "<script>" + echarts_js() + "</script>" + _chart_script(specs)
    return html_part, script


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

    chart_html, chart_script = _charts(payload)
    if chart_html:
        sections.insert(0, "<h2>图形</h2>" + chart_html)

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
</div>{chart_script}</body></html>"""


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
