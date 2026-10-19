"""
portal/report_html.py — 独立 HTML 报告生成器

两套模板：
  - render_stock_report(report, llm_notes)  个股深度分析报告
  - render_market_report(index_results)     大盘复盘报告（双指数并排）

特性：
  - 纯 Python f-string，零新依赖；ECharts CDN 内联（雷达图 + 蜡烛图）
  - 自包含内联 CSS（不依赖 Tailwind CDN，离线也能看文字/布局）
  - 同 code/prefix 去重：只保留当日最新 HTML（_purge_old）
  - webbrowser 自动打开（open_in_browser，失败不阻断）
  - 文件名含日期：{code}_{YYYYMMDD}.html / market_{YYYYMMDD}.html
"""
from __future__ import annotations

import html as _htmllib
import json
import logging
import re
import webbrowser
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

TZ_CN = timezone(timedelta(hours=8))
ECHARTS_CDN = "https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"

# 信号 → 颜色/中文（与 report-view.js 的 SIGNAL_CONFIG 对齐）
SIGNAL_CFG = {
    "buy":   {"label": "买入", "bg": "#dcfce7", "text": "#15803d", "dot": "#16a34a"},
    "watch": {"label": "关注", "bg": "#dbeafe", "text": "#1d4ed8", "dot": "#2563eb"},
    "hold":  {"label": "持有", "bg": "#f3f4f6", "text": "#4b5563", "dot": "#6b7280"},
    "sell":  {"label": "卖出", "bg": "#fee2e2", "text": "#b91c1c", "dot": "#dc2626"},
}
STANCE_CFG = {
    "bullish": {"label": "偏多", "color": "#16a34a", "bg": "#dcfce7"},
    "bearish": {"label": "偏空", "color": "#dc2626", "bg": "#fee2e2"},
    "neutral": {"label": "中性", "color": "#6b7280", "bg": "#f3f4f6"},
}


def _now_cn() -> datetime:
    return datetime.now(TZ_CN)


def _today_str() -> str:
    return _now_cn().strftime("%Y%m%d")


def _reports_dir() -> Path:
    d = Path(__file__).parent / "data" / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _esc(s) -> str:
    return _htmllib.escape(str(s if s is not None else ""))


def _score_color(score: int) -> str:
    if score >= 70: return "#16a34a"
    if score >= 55: return "#2563eb"
    if score >= 40: return "#6b7280"
    return "#dc2626"


def _md_to_html(md: str) -> str:
    """极简 Markdown → HTML（移植 report-view.js 的 _mdToHtml）。"""
    if not md:
        return ""
    out = []
    for line in str(md).split("\n"):
        if re.match(r"^\*\*(.+)\*\*$", line):
            out.append(f'<p class="b">{_esc(line.replace("**",""))}</p>'); continue
        if re.match(r"^#{1,3} (.+)", line):
            out.append(f'<p class="b">{_esc(re.sub(r"^#+\s","",line))}</p>'); continue
        if re.match(r"^[-*] (.+)", line):
            out.append(f'<p class="li">• {_esc(line[2:])}</p>'); continue
        if line.strip() == "":
            continue
        h = _esc(line)
        h = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", h)
        h = re.sub(r"`(.+?)`", r'<code>\1</code>', h)
        h = re.sub(r"(✅|🟢)", r'<span style="color:#16a34a">\1</span>', h)
        h = re.sub(r"(⚠️|🔴|❌)", r'<span style="color:#dc2626">\1</span>', h)
        out.append(f"<p>{h}</p>")
    return "\n".join(out)


def _purge_old(prefix: str):
    """删除同 prefix 的非当日 HTML（去重，只留最新）。prefix 如 '002230' 或 'market'。"""
    today = _today_str()
    try:
        for f in _reports_dir().glob(f"{prefix}_*.html"):
            # 保留今天的，删其余
            if f.name != f"{prefix}_{today}.html":
                try: f.unlink()
                except Exception: pass
    except Exception as e:
        logger.warning("[report_html] purge %s 失败: %s", prefix, e)


# ── 通用 CSS ──────────────────────────────────────────────────
_BASE_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;background:#f8fafc;color:#1f2937;line-height:1.6;padding:20px}
.wrap{max-width:1080px;margin:0 auto}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:16px;padding:18px 20px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.04)}
.hd{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}
.title{font-size:22px;font-weight:700}.code{font-family:monospace;color:#6b7280;font-size:14px;margin-left:8px}
.date{color:#9ca3af;font-size:13px;margin-top:4px}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:600}
.ring{font-size:26px;font-weight:800}
.concl{background:linear-gradient(to right,#f8fafc,#eff6ff);border:1px solid #dbeafe;border-radius:12px;padding:14px 16px;margin-bottom:16px;font-size:14px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:760px){.grid2{grid-template-columns:1fr}}
.chart{height:300px;width:100%}
.dim-hd{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;background:#f9fafb;border-bottom:1px solid #eee;border-radius:12px 12px 0 0;font-weight:600}
.sec{border-bottom:1px solid #f1f5f9;padding:10px 14px}
.sec:last-child{border-bottom:none}
.sec-hd{display:flex;align-items:center;justify-content:space-between;font-weight:600;font-size:14px;margin-bottom:4px}
.sec-bd{font-size:13px;color:#4b5563}.sec-bd p{margin:2px 0}.sec-bd .b{font-weight:600;color:#374151;margin-top:4px}
.sec-bd .li{padding-left:8px}.sec-bd code{background:#eef2ff;color:#4338ca;padding:0 4px;border-radius:4px;font-size:12px}
.note{margin-top:8px;padding:8px 10px;border-radius:8px;font-size:13px;border-left:3px solid}
.note b{font-weight:700}
.forecast-meta{display:flex;gap:20px;flex-wrap:wrap;padding:10px 14px 0}
.forecast-meta-item{background:#f8fafc;border:1px solid #e5e7eb;border-radius:10px;padding:8px 14px;font-size:13px;flex:1;min-width:160px}
.forecast-meta-item .fm-label{color:#6b7280;font-size:11px;margin-bottom:2px}
.forecast-meta-item .fm-value{font-weight:700;font-size:15px}
.forecast-note{padding:6px 14px 10px;font-size:11px;color:#9ca3af}
.foot{text-align:center;color:#9ca3af;font-size:12px;margin-top:20px;padding:12px}
.small{font-size:12px;color:#6b7280}
"""


def _dim_icon(dim: str) -> str:
    return {"technical": "📊", "fundamental": "📈", "industry": "🏭"}.get(dim, "📋")


def _render_sections_html(sections: list, llm_notes: dict) -> str:
    """渲染一组 section 卡片（含 LLM 打分说明块）。"""
    parts = []
    for s in sections:
        sig = SIGNAL_CFG.get(s.get("signal", "hold"), SIGNAL_CFG["hold"])
        note_html = ""
        note = (llm_notes or {}).get(s.get("key"))
        if note and isinstance(note, dict):
            nsig = SIGNAL_CFG.get(note.get("signal", "hold"), SIGNAL_CFG["hold"])
            nscore = note.get("score", "")
            reason = _esc(note.get("reason", ""))
            impact = _esc(note.get("impact", ""))
            note_html = (
                f'<div class="note" style="border-color:{nsig["dot"]};background:{nsig["bg"]}">'
                f'🤖 <b style="color:{nsig["text"]}">{nsig["label"]}'
                + (f' · {nscore}' if nscore != "" else '') + '</b>'
                + (f' — {reason}' if reason else '')
                + (f'（影响：{impact}）' if impact else '') + '</div>'
            )
        parts.append(
            f'<div class="sec"><div class="sec-hd"><span>{_esc(s.get("title"))}</span>'
            f'<span class="badge" style="background:{sig["bg"]};color:{sig["text"]}">{sig["label"]} · {s.get("score",50)}</span></div>'
            f'<div class="sec-bd">{_md_to_html(s.get("content",""))}{note_html}</div></div>'
        )
    return "\n".join(parts)


def _render_forecast_html(forecast: dict, kline_data: list, chart_id: str) -> tuple:
    """渲染走势预测模块：次日高低点文字 + 7日模拟K线图（ECharts）。

    返回 (html_fragment, js_fragment)，js_fragment 需追加到 init_js 里执行。
    """
    if not forecast:
        return "", ""

    next_day = forecast.get("next_day") or {}
    week = forecast.get("week_forecast") or []
    active_patterns = forecast.get("active_patterns") or []

    # ── 次日预测摘要 ──────────────────────────────────────────
    nd_high  = next_day.get("high", "")
    nd_low   = next_day.get("low", "")
    nd_trend = _esc(next_day.get("trend", "震荡"))
    nd_reason = _esc(next_day.get("reason", ""))

    trend_colors = {
        "上涨": "#16a34a", "震荡上行": "#2563eb",
        "震荡": "#6b7280", "震荡下行": "#f97316", "下跌": "#dc2626",
    }
    trend_raw = next_day.get("trend", "震荡")
    tc = trend_colors.get(trend_raw, "#6b7280")

    meta_html = f"""
    <div class="forecast-meta">
      <div class="forecast-meta-item">
        <div class="fm-label">次日预测高点</div>
        <div class="fm-value" style="color:#dc2626">{_esc(str(nd_high)) if nd_high else "—"}</div>
      </div>
      <div class="forecast-meta-item">
        <div class="fm-label">次日预测低点</div>
        <div class="fm-value" style="color:#16a34a">{_esc(str(nd_low)) if nd_low else "—"}</div>
      </div>
      <div class="forecast-meta-item">
        <div class="fm-label">次日走势判断</div>
        <div class="fm-value" style="color:{tc}">{nd_trend}</div>
      </div>
    </div>
    {('<div style="padding:6px 14px 2px;font-size:12px;color:#6b7280">💬 ' + nd_reason + '</div>') if nd_reason else ''}
    """

    # ── 当前触发形态回测表 ────────────────────────────────────
    patterns_html = ""
    if active_patterns:
        rows = []
        for p in active_patterns:
            triggered = p.get("triggered")
            stats = p.get("stats") or {}
            badge = '<span style="background:#fef3c7;color:#92400e;border-radius:4px;padding:1px 5px;font-size:10px;font-weight:600">⚡今日</span>' if triggered else ''

            def _stat(d):
                s = stats.get(str(d))
                if not s or s.get("win_rate") is None:
                    return '<td style="color:#d1d5db;text-align:center">—</td><td style="color:#d1d5db;text-align:center">—</td>'
                wr = s["win_rate"]
                avg = s["avg_return"]
                wr_c = "#16a34a" if wr >= 60 else "#6b7280" if wr >= 45 else "#dc2626"
                avg_c = "#16a34a" if (avg or 0) > 0 else "#dc2626" if (avg or 0) < 0 else "#6b7280"
                return f'<td style="text-align:center;color:{wr_c};font-weight:{"600" if wr>=60 else "400"}">{wr}%</td><td style="text-align:center;color:{avg_c}">{avg:+.2f}%</td>'

            row_bg = "background:#fffbeb" if triggered else ""
            rows.append(
                f'<tr style="{row_bg}">'
                f'<td style="padding:5px 8px;white-space:nowrap">{badge} {_esc(p["name"])}</td>'
                f'<td style="text-align:center;color:#6b7280">{p.get("count") or 0}次</td>'
                f'<td style="text-align:center;color:#9ca3af;font-size:10px">{p.get("last_date") or "—"}</td>'
                + _stat(5) + _stat(10) + _stat(20) +
                f'</tr>'
            )
        patterns_html = f"""
    <div style="padding:8px 14px 4px">
      <div style="font-size:12px;font-weight:600;color:#374151;margin-bottom:6px">📊 形态回测统计</div>
      <div style="overflow-x:auto">
        <table style="width:100%;border-collapse:collapse;font-size:11px">
          <thead>
            <tr style="border-bottom:1px solid #e5e7eb;color:#9ca3af">
              <th style="text-align:left;padding:4px 8px;font-weight:500">形态</th>
              <th style="text-align:center;padding:4px 6px;font-weight:500">触发次数</th>
              <th style="text-align:center;padding:4px 6px;font-weight:500">最近日期</th>
              <th style="text-align:center;padding:4px 6px;font-weight:500" colspan="2">5日</th>
              <th style="text-align:center;padding:4px 6px;font-weight:500" colspan="2">10日</th>
              <th style="text-align:center;padding:4px 6px;font-weight:500" colspan="2">20日</th>
            </tr>
            <tr style="border-bottom:1px solid #f3f4f6;color:#d1d5db;font-size:10px">
              <th></th><th></th><th></th>
              <th style="text-align:center">胜率</th><th style="text-align:center">均收益</th>
              <th style="text-align:center">胜率</th><th style="text-align:center">均收益</th>
              <th style="text-align:center">胜率</th><th style="text-align:center">均收益</th>
            </tr>
          </thead>
          <tbody>{''.join(rows)}</tbody>
        </table>
      </div>
    </div>"""
    # 历史：取最近 30 条真实 K 线
    hist = [r for r in (kline_data or []) if isinstance(r, dict) and r.get("close")][-30:]

    # 预测部分拼接（open 字段可能来自 LLM，week[].open/high/low/close）
    forecast_candles = []
    for i, w in enumerate(week[:7]):
        if not isinstance(w, dict):
            continue
        forecast_candles.append({
            "day": i + 1,
            "open":  w.get("open"),
            "high":  w.get("high"),
            "low":   w.get("low"),
            "close": w.get("close"),
            "note":  w.get("note", ""),
        })

    hist_js  = json.dumps(hist,             ensure_ascii=False)
    fore_js  = json.dumps(forecast_candles, ensure_ascii=False)

    js = f"""
    (function(){{
      var hist={hist_js};
      var fore={fore_js};
      var el=document.getElementById('{chart_id}');
      if(!el||typeof echarts==='undefined')return;

      // 历史区日期标签
      var hDates=hist.map(function(r){{return (r.date||'').slice(5)}});
      var hCand=hist.map(function(r){{
        var o=r.open!=null?r.open:r.close, h=r.high!=null?r.high:r.close,
            l=r.low!=null?r.low:r.close, c=r.close;
        return [o,c,l,h];
      }});

      // 预测区日期标签（D+1 … D+7）
      var fDates=fore.map(function(r){{return 'D+'+(r.day||'?')}});
      var fCand=fore.map(function(r){{return [r.open,r.close,r.low,r.high];}});

      var allDates=hDates.concat(fDates);
      // 历史蜡烛（实体颜色：红涨绿跌），预测蜡烛（灰色渐变区分）
      var histSeries={{
        name:'历史K线',type:'candlestick',
        data:hCand,
        itemStyle:{{color:'#ef4444',color0:'#10b981',borderColor:'#ef4444',borderColor0:'#10b981'}},
        markArea:{{silent:true,data:[[{{xAxis:hDates[0]}},{{xAxis:hDates[hDates.length-1]}}]]}}
      }};
      var foreSeries={{
        name:'预测走势',type:'candlestick',
        data:Array(hDates.length).fill(null).concat(fCand),
        itemStyle:{{color:'rgba(168,85,247,0.8)',color0:'rgba(99,102,241,0.8)',
                   borderColor:'#7c3aed',borderColor0:'#4f46e5'}},
      }};
      var hMa5=hist.map(function(r){{return r.ma5||null}});
      var hMa20=hist.map(function(r){{return r.ma20||null}});
      echarts.init(el).setOption({{
        backgroundColor:'#fff',
        tooltip:{{trigger:'axis',axisPointer:{{type:'cross'}}}},
        legend:{{data:['历史K线','预测走势','MA5','MA20'],top:2,textStyle:{{fontSize:11}}}},
        grid:{{left:8,right:8,top:30,bottom:24,containLabel:true}},
        xAxis:{{
          type:'category',data:allDates,
          axisLabel:{{fontSize:9,color:'#9ca3af'}},
          axisLine:{{lineStyle:{{color:'#e5e7eb'}}}},
          splitLine:{{show:false}},
          // 预测区用浅色背景
          axisPointer:{{label:{{backgroundColor:'#7c3aed'}}}}
        }},
        yAxis:{{scale:true,axisLabel:{{fontSize:9,color:'#9ca3af'}},splitLine:{{lineStyle:{{color:'#f3f4f6'}}}}}},
        visualMap:[{{
          show:false,seriesIndex:0,
          pieces:[{{gt:-999999,lte:0,color:'#10b981'}},{{gt:0,color:'#ef4444'}}]
        }}],
        series:[
          histSeries,
          foreSeries,
          {{name:'MA5',type:'line',data:hMa5.concat(Array(fDates.length).fill(null)),
            symbol:'none',lineStyle:{{color:'#f97316',width:1}}}},
          {{name:'MA20',type:'line',data:hMa20.concat(Array(fDates.length).fill(null)),
            symbol:'none',lineStyle:{{color:'#8b5cf6',width:1}}}}
        ]
      }});
    }})();
    """

    note_items = "".join(
        f'<span style="margin-right:12px">D+{w.get("day","?")}：{_esc(str(w.get("note","")))}</span>'
        for w in forecast_candles if w.get("note")
    )

    html = f"""
    <div class="card" style="padding:0">
      <div class="dim-hd">
        <span>📈 走势预测</span>
        <span style="font-size:11px;color:#9ca3af;font-weight:400">LLM 情景模拟，仅供参考</span>
      </div>
      {meta_html}
      {patterns_html}
      <div style="padding:8px 14px 4px">
        <div id="{chart_id}" style="height:280px;width:100%"></div>
      </div>
      {('<div class="forecast-note">📝 ' + note_items + '</div>') if note_items else ''}
    </div>
    """
    return html, js


# ── 个股报告 ──────────────────────────────────────────────────
def render_stock_report(report: dict, llm_notes: Optional[dict] = None) -> Path:
    llm_notes = llm_notes or report.get("llm_notes") or {}
    code = str(report.get("stock_code", "")).strip() or "unknown"
    name = report.get("stock_name", code)
    score = report.get("overall_score", 50)
    signal = report.get("overall_signal", "hold")
    sig = SIGNAL_CFG.get(signal, SIGNAL_CFG["hold"])
    date_disp = _now_cn().strftime("%Y-%m-%d %H:%M")
    conclusion = _esc(report.get("conclusion", ""))
    agent_review_html = _md_to_html(report.get("agent_review", "")) if report.get("agent_review") else ""

    dims = report.get("dimensions", [])
    # 按 技术面 → 基本面 → 产业链 顺序单列排列（与网页端维度顺序一致）
    _order = ["technical", "fundamental", "industry"]
    ordered_dims = (
        [d for k in _order for d in dims if d.get("dimension") == k]
        + [d for d in dims if d.get("dimension") not in _order]
    )

    def _dim_block(d):
        dsig = SIGNAL_CFG.get(d.get("signal", "hold"), SIGNAL_CFG["hold"])
        secs = _render_sections_html(d.get("sections", []), llm_notes)
        summ = f'<div class="sec small">{_esc(d.get("summary",""))}</div>' if d.get("summary") else ""
        return (
            f'<div class="card" style="padding:0">'
            f'<div class="dim-hd"><span>{_dim_icon(d.get("dimension"))} {_esc(d.get("name"))}</span>'
            f'<span class="badge" style="background:{dsig["bg"]};color:{dsig["text"]}">{dsig["label"]} · {d.get("score",50)}</span></div>'
            f'{summ}{secs}</div>'
        )

    all_blocks = "".join(_dim_block(d) for d in ordered_dims)

    kline = report.get("kline_data", []) or []
    radar_scores = {d.get("dimension"): d.get("score", 0) for d in dims}

    # ── 走势预测模块 ──────────────────────────────────────────
    forecast_html, forecast_js = _render_forecast_html(
        report.get("price_forecast") or {},
        kline,
        "forecast_chart",
    )

    init_js = f"""
    (function(){{
      if(typeof echarts==='undefined')return;
      var kl={json.dumps(kline, ensure_ascii=False)};
      var rs={json.dumps(radar_scores, ensure_ascii=False)};
      // 雷达图
      var rEl=document.getElementById('radar');
      if(rEl){{echarts.init(rEl).setOption({{
        tooltip:{{}},
        radar:{{indicator:[{{name:'技术面',max:100}},{{name:'基本面',max:100}},{{name:'产业链',max:100}}],radius:'65%',axisName:{{fontSize:12,color:'#374151'}}}},
        series:[{{type:'radar',data:[{{value:[rs.technical||0,rs.fundamental||0,rs.industry||0],areaStyle:{{color:'rgba(59,130,246,.18)'}},lineStyle:{{color:'#3b82f6',width:2}},itemStyle:{{color:'#3b82f6'}}}}]}}]
      }});}}
      // K线蜡烛图
      var kEl=document.getElementById('kline');
      if(kEl&&kl.length){{
        var d=kl.slice(-90);
        var dates=d.map(function(x){{return (x.date||'').slice(5)}});
        var cand=d.map(function(x){{return [x.open,x.close,x.low,x.high]}});
        var hasOHLC=d[0]&&d[0].open!=null;
        var ma5=d.map(function(x){{return x.ma5}}),ma20=d.map(function(x){{return x.ma20}}),ma250=d.map(function(x){{return x.ma250||null}});
        var series=[];
        if(hasOHLC){{series.push({{name:'K线',type:'candlestick',data:cand,itemStyle:{{color:'#ef4444',color0:'#10b981',borderColor:'#ef4444',borderColor0:'#10b981'}}}});}}
        else{{series.push({{name:'收盘',type:'line',data:d.map(function(x){{return x.close}}),symbol:'none',lineStyle:{{color:'#3b82f6',width:2}}}});}}
        series.push({{name:'MA5',type:'line',data:ma5,symbol:'none',lineStyle:{{color:'#f97316',width:1}}}});
        series.push({{name:'MA20',type:'line',data:ma20,symbol:'none',lineStyle:{{color:'#8b5cf6',width:1}}}});
        series.push({{name:'MA250',type:'line',data:ma250,symbol:'none',lineStyle:{{color:'#dc2626',width:1.5,type:'dashed'}}}});
        echarts.init(kEl).setOption({{
          tooltip:{{trigger:'axis',axisPointer:{{type:'cross'}}}},
          legend:{{data:series.map(function(s){{return s.name}}),top:2,textStyle:{{fontSize:11}}}},
          grid:{{left:8,right:8,top:30,bottom:20,containLabel:true}},
          xAxis:{{type:'category',data:dates,axisLabel:{{fontSize:9,color:'#9ca3af'}}}},
          yAxis:{{scale:true,axisLabel:{{fontSize:9,color:'#9ca3af'}},splitLine:{{lineStyle:{{color:'#f3f4f6'}}}}}},
          series:series
        }});
      }}
    }})();
    {forecast_js}
    """

    body = f"""
    <div class="wrap">
      <div class="card hd">
        <div><span class="title">{_esc(name)}</span><span class="code">{_esc(code)}</span>
          <div class="date">📅 报告日期：{date_disp}（最新交易日数据）</div></div>
        <div style="text-align:right">
          <div class="ring" style="color:{_score_color(score)}">{score}<span style="font-size:14px;color:#9ca3af">/100</span></div>
          <span class="badge" style="background:{sig['bg']};color:{sig['text']}">{sig['label']}</span>
        </div>
      </div>
      {'<div class="concl">🎯 <b>综合结论</b>：' + conclusion + '</div>' if conclusion else ''}
      {'<div class="concl" style="border-left-color:#3b82f6;background:#eff6ff">🤖 <b>Agent 综合研判</b><div style="margin-top:6px">' + agent_review_html + '</div></div>' if agent_review_html else ''}
      {forecast_html}
      <div class="grid2">
        <div class="card"><div class="small" style="margin-bottom:6px">📡 维度评分雷达</div><div id="radar" class="chart" style="height:260px"></div></div>
        <div class="card"><div class="small" style="margin-bottom:6px">📈 K线走势（近90日）</div><div id="kline" class="chart" style="height:260px"></div></div>
      </div>
      <div>{all_blocks}</div>
      <div class="foot">本报告由本地量化系统自动生成，仅供研究参考，不构成投资建议。生成时间 {date_disp}</div>
    </div>
    """

    doc = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(name)} {_esc(code)} 分析报告 {_today_str()}</title>
<script src="{ECHARTS_CDN}"></script>
<style>{_BASE_CSS}</style></head>
<body>{body}<script>{init_js}</script></body></html>"""

    _purge_old(code)
    path = _reports_dir() / f"{code}_{_today_str()}.html"
    path.write_text(doc, encoding="utf-8")
    logger.info("[report_html] 个股报告已生成: %s", path)
    return path


# ── 大盘报告（双指数并排，第二套模板）─────────────────────────
def render_market_report(index_results: list, overall_summary: str = "") -> Path:
    date_disp = _now_cn().strftime("%Y-%m-%d %H:%M")

    cols = []
    init_blocks = []
    for i, idx in enumerate(index_results):
        symbol = idx.get("symbol", "")
        name = idx.get("name", symbol)
        score = idx.get("score", 50)
        signal = idx.get("signal", "hold")
        sig = SIGNAL_CFG.get(signal, SIGNAL_CFG["hold"])
        secs = _render_sections_html(idx.get("sections", []), {})
        kline = idx.get("kline_data", []) or []
        last_close = kline[-1].get("close") if kline else None
        chart_id = f"kl{i}"

        cols.append(
            f'<div class="card" style="padding:0">'
            f'<div class="dim-hd"><span>📊 {_esc(name)} <span class="small">{_esc(symbol)}</span></span>'
            f'<span class="badge" style="background:{sig["bg"]};color:{sig["text"]}">{sig["label"]} · {score}</span></div>'
            f'<div class="sec small">最新收盘：{last_close if last_close is not None else "-"}　{_esc(idx.get("summary",""))}</div>'
            f'<div style="padding:8px"><div id="{chart_id}" class="chart" style="height:240px"></div></div>'
            f'{secs}</div>'
        )

        init_blocks.append(f"""
        (function(){{
          var kl={json.dumps(kline, ensure_ascii=False)};
          var el=document.getElementById('{chart_id}');
          if(!el||!kl.length||typeof echarts==='undefined')return;
          var d=kl.slice(-120);
          var dates=d.map(function(x){{return (x.date||'').slice(5)}});
          var cand=d.map(function(x){{return [x.open,x.close,x.low,x.high]}});
          echarts.init(el).setOption({{
            tooltip:{{trigger:'axis',axisPointer:{{type:'cross'}}}},
            legend:{{data:['K线','MA20','MA60','MA250年线'],top:2,textStyle:{{fontSize:10}}}},
            grid:{{left:8,right:8,top:28,bottom:20,containLabel:true}},
            xAxis:{{type:'category',data:dates,axisLabel:{{fontSize:9,color:'#9ca3af'}}}},
            yAxis:{{scale:true,axisLabel:{{fontSize:9,color:'#9ca3af'}},splitLine:{{lineStyle:{{color:'#f3f4f6'}}}}}},
            series:[
              {{name:'K线',type:'candlestick',data:cand,itemStyle:{{color:'#ef4444',color0:'#10b981',borderColor:'#ef4444',borderColor0:'#10b981'}}}},
              {{name:'MA20',type:'line',data:d.map(function(x){{return x.ma20}}),symbol:'none',lineStyle:{{color:'#8b5cf6',width:1}}}},
              {{name:'MA60',type:'line',data:d.map(function(x){{return x.ma60}}),symbol:'none',lineStyle:{{color:'#f97316',width:1}}}},
              {{name:'MA250年线',type:'line',data:d.map(function(x){{return x.ma250}}),symbol:'none',lineStyle:{{color:'#dc2626',width:1.5,type:'dashed'}}}}
            ]
          }});
        }})();
        """)

    body = f"""
    <div class="wrap">
      <div class="card hd">
        <div><span class="title">🌐 大盘复盘</span>
          <div class="date">📅 {date_disp}（以最新交易日数据为准，不判断交易日）</div></div>
      </div>
      {'<div class="concl">🎯 <b>大盘整体研判</b>：' + _md_to_html(overall_summary) + '</div>' if overall_summary else ''}
      <div class="grid2">{''.join(cols)}</div>
      <div class="foot">本报告由本地量化系统自动生成，仅供研究参考，不构成投资建议。生成时间 {date_disp}</div>
    </div>
    """

    doc = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>大盘复盘 {_today_str()}</title>
<script src="{ECHARTS_CDN}"></script>
<style>{_BASE_CSS}</style></head>
<body>{body}<script>{''.join(init_blocks)}</script></body></html>"""

    _purge_old("market")
    path = _reports_dir() / f"market_{_today_str()}.html"
    path.write_text(doc, encoding="utf-8")
    logger.info("[report_html] 大盘报告已生成: %s", path)
    return path


# ── 打开 / 查找 ───────────────────────────────────────────────
def open_in_browser(path: Path, log=None) -> bool:
    """用默认浏览器打开 HTML。无头/远程环境失败时返回 False，不抛异常。"""
    try:
        ok = webbrowser.open(Path(path).as_uri())
        msg = f"🌐 已在浏览器打开报告：{path}" if ok else f"⚠️ 浏览器打开失败，报告已生成于：{path}"
        if log:
            log(msg)
        else:
            logger.info(msg)
        return bool(ok)
    except Exception as e:
        msg = f"⚠️ 无法自动打开浏览器（{e}），报告已生成于：{path}"
        if log:
            log(msg)
        else:
            logger.warning(msg)
        return False


def find_latest_stock_html(code: str) -> Optional[Path]:
    files = sorted(_reports_dir().glob(f"{code}_*.html"), reverse=True)
    return files[0] if files else None


def find_latest_market_html() -> Optional[Path]:
    files = sorted(_reports_dir().glob("market_*.html"), reverse=True)
    return files[0] if files else None
