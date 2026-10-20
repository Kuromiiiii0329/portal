# -*- coding: utf-8 -*-
"""
portal/premarket/collect.py — 盘前数据采集

原则
----
1. **每个数据源都可能挂**，所以逐块 try，失败只记进 `errors`，绝不整份预案作废。
2. **能算的不用接口**：题材强弱用「涨停池 + 所属行业」聚合，不依赖已被封的
   东财板块接口（`push2*` 在本机全部 ProxyError，实测见 docs/data-and-ui.md）。
3. **每个数字都带 `source`**，报告里要标出来源，避免「不知道哪来的数」。
4. 采集结果落 `cache/<date>/collect.json`，同一天重复生成预案不再打接口。

本机实测可用性（2026-10）
------------------------
✅ 指数日K stock_zh_index_daily / 指数快照 stock_zh_index_spot_sina
✅ 赚钱效应 stock_market_activity_legu
✅ 涨停/炸板/跌停/强势股池 stock_zt_pool_*_em
✅ 北向 stock_hsgt_fund_flow_summary_em
✅ 龙虎榜 stock_lhb_detail_em
✅ 快讯 stock_info_global_cls / stock_info_global_em
❌ 行业/概念板块 stock_board_*_em（push2 被封）
❌ 个股资金流 stock_individual_fund_flow（push2his 被封）
"""
from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from typing import Any, Callable

from . import paths

#: 主要指数（代码 → 展示名）。顺序即分组顺序。
INDICES = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("sh000688", "科创50"),
    ("sh000016", "上证50"),
    ("sh000905", "中证500"),
]

#: 采集结果的结构版本。**改动采集字段时必须 +1**，否则旧缓存会被静默复用：
#: 实测加了候选池之后，因为缓存命中，模型收到的候选池是空的，于是它严格遵守
#: 「无候选池就不要选股」，整份预案一个个股计划都没有 —— 而且日志里只有一行
#: 「复用缓存」，看不出问题。缓存必须能自证新鲜。
#: v4：候选池加入因子战法（playbook / buyable / setup / metrics），
#:     涨停家族降级为 buyable=False。
CACHE_SCHEMA = 5

#: 新闻保留窗口（小时）与条数上限
NEWS_HOURS = 36
NEWS_MAX = 40

#: 连板梯队只保留 3 板以上
LEADER_MIN_BOARDS = 3


def _today8() -> str:
    return datetime.date.today().strftime("%Y%m%d")


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


# ─────────────────────────────────────────────────────────────
# 小工具
# ─────────────────────────────────────────────────────────────
def _f(v: Any, default: float | None = None) -> float | None:
    try:
        x = float(v)
        return x if x == x else default       # NaN 过滤
    except (TypeError, ValueError):
        return default


def _num(v: Any, digits: int = 2) -> float | None:
    x = _f(v)
    return None if x is None else round(x, digits)


def _pct(a: Any, b: Any) -> float | None:
    x, y = _f(a), _f(b)
    if x is None or y in (None, 0):
        return None
    return round((x / y - 1) * 100, 3)


def _rows(df) -> list[dict]:
    if df is None:
        return []
    try:
        return df.to_dict("records")
    except Exception:
        return []


def _err(out: dict, name: str, exc: Exception) -> None:
    out["errors"].append({"source": name,
                          "error": f"{type(exc).__name__}: {str(exc)[:180]}"})


# ─────────────────────────────────────────────────────────────
# 各数据块
# ─────────────────────────────────────────────────────────────
def index_snapshot(out: dict) -> None:
    """指数：快照（当日涨跌/成交额）+ 日K（均线与趋势）。"""
    import akshare as ak

    snapshot: list[dict] = []
    try:
        spot = ak.stock_zh_index_spot_sina()
        rows = _rows(spot)
        by_code = {str(r.get("代码")): r for r in rows}
        for code, name in INDICES:
            r = by_code.get(code)
            if not r:
                continue
            snapshot.append({
                "code": code, "name": name,
                "close": _num(r.get("最新价"), 2),
                "chg_pct": _num(r.get("涨跌幅"), 3),
                "open": _num(r.get("今开"), 2),
                "high": _num(r.get("最高"), 2),
                "low": _num(r.get("最低"), 2),
                "amount_yi": round(_f(r.get("成交额"), 0) / 1e8, 1) if _f(r.get("成交额")) else None,
                "source": "stock_zh_index_spot_sina",
            })
    except Exception as exc:
        _err(out, "index_spot", exc)

    # 日K → 均线位置与趋势
    for item in snapshot:
        code = item["code"]
        try:
            hist = _rows(ak.stock_zh_index_daily(symbol=code))
            closes = [_f(r.get("close")) for r in hist if _f(r.get("close"))]
            if len(closes) >= 20:
                last = closes[-1]
                ma5 = sum(closes[-5:]) / 5
                ma20 = sum(closes[-20:]) / 20
                ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else None
                item["ma5"] = round(ma5, 2)
                item["ma20"] = round(ma20, 2)
                if ma60:
                    item["ma60"] = round(ma60, 2)
                item["above_ma20"] = last > ma20
                item["above_ma5"] = last > ma5
                item["chg_5d_pct"] = _pct(last, closes[-6]) if len(closes) >= 6 else None
                item["chg_20d_pct"] = _pct(last, closes[-21]) if len(closes) >= 21 else None
                item["trend"] = ("强势" if last > ma5 > ma20 else
                                 "走弱" if last < ma5 < ma20 else "震荡")
                item["source_hist"] = "stock_zh_index_daily"
        except Exception as exc:
            _err(out, f"index_daily:{code}", exc)

    out["indices"] = snapshot


def breadth(out: dict, date8: str) -> None:
    """市场宽度：涨跌家数、涨停/跌停/炸板家数、成交额。"""
    import akshare as ak

    info: dict[str, Any] = {"source": "stock_market_activity_legu"}
    try:
        rows = _rows(ak.stock_market_activity_legu())
        kv = {str(r.get("item")).strip(): _f(r.get("value")) for r in rows}
        info.update({
            "up": int(kv.get("上涨", 0) or 0),
            "down": int(kv.get("下跌", 0) or 0),
            "flat": int(kv.get("平盘", 0) or 0),
            "limit_up": int(kv.get("涨停", 0) or 0),
            "limit_down": int(kv.get("跌停", 0) or 0),
            "real_limit_up": int(kv.get("真实涨停", 0) or 0),
            "real_limit_down": int(kv.get("真实跌停", 0) or 0),
            "activity": _num(kv.get("活跃度")),
            "raw": {k: _num(v) for k, v in kv.items() if k},
        })
    except Exception as exc:
        _err(out, "breadth_legu", exc)

    # 三个池子补充：炸板家数、连板梯队（宽度指标比 legu 更细）
    try:
        zt = _rows(ak.stock_zt_pool_em(date=date8))
        zb = _rows(ak.stock_zt_pool_zbgc_em(date=date8))
        dt = _rows(ak.stock_zt_pool_dtgc_em(date=date8))
        info["pool_zt"] = len(zt)
        info["pool_zb"] = len(zb)
        info["pool_dt"] = len(dt)
        if zt:
            boards = [_f(r.get("连板数")) or 1 for r in zt]
            info["max_boards"] = int(max(boards))
            info["multi_board_n"] = int(sum(1 for b in boards if b >= 2))
            # 炸板率 = 炸板 / (涨停 + 炸板)
            info["break_rate_pct"] = round(
                len(zb) / (len(zt) + len(zb)) * 100, 2) if (len(zt) + len(zb)) else None
            # 封板资金合计（亿）
            seal = sum(_f(r.get("封板资金")) or 0 for r in zt)
            info["seal_amount_yi"] = round(seal / 1e8, 2)
        info["source_pools"] = "stock_zt_pool_em + zbgc + dtgc"
    except Exception as exc:
        _err(out, "breadth_pools", exc)

    # 赚钱效应：上涨占比
    if info.get("up") is not None and info.get("down") is not None:
        total = info["up"] + info["down"] + (info.get("flat") or 0)
        info["up_ratio_pct"] = round(info["up"] / total * 100, 2) if total else None
    out["breadth"] = info


def themes(out: dict, date8: str) -> None:
    """题材强弱：用涨停池的「所属行业」聚合（不依赖被封的板块接口）。

    指标：涨停家数、连板总数、封板资金合计、最高板。
    「涨停家数」是最难造假的题材强度 —— 它是资金真金白银买出来的。
    """
    import akshare as ak

    try:
        zt = _rows(ak.stock_zt_pool_em(date=date8))
    except Exception as exc:
        _err(out, "themes", exc)
        out["themes"] = []
        return

    agg: dict[str, dict] = {}
    for r in zt:
        ind = str(r.get("所属行业") or "未分类").strip()
        a = agg.setdefault(ind, {"name": ind, "limit_up_n": 0, "boards_sum": 0,
                                 "seal_yi": 0.0, "max_boards": 0, "leaders": []})
        boards = int(_f(r.get("连板数")) or 1)
        a["limit_up_n"] += 1
        a["boards_sum"] += boards
        a["seal_yi"] += (_f(r.get("封板资金")) or 0) / 1e8
        a["max_boards"] = max(a["max_boards"], boards)
        if boards >= 2:
            a["leaders"].append({"code": str(r.get("代码")), "name": r.get("名称"),
                                 "boards": boards})

    rows = sorted(agg.values(),
                  key=lambda x: (x["limit_up_n"], x["boards_sum"]), reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i
        r["seal_yi"] = round(r["seal_yi"], 2)
        r["leaders"] = r["leaders"][:5]
    out["themes"] = rows[:20]
    out["themes_source"] = "stock_zt_pool_em.所属行业 聚合（板块接口 push2 不可用）"


def leaders(out: dict, date8: str) -> None:
    """连板梯队：3 板以上个股（情绪温度与接力风险）。"""
    import akshare as ak

    try:
        zt = _rows(ak.stock_zt_pool_em(date=date8))
    except Exception as exc:
        _err(out, "leaders", exc)
        out["leaders"] = []
        return

    rows = []
    for r in zt:
        boards = int(_f(r.get("连板数")) or 1)
        if boards < LEADER_MIN_BOARDS:
            continue
        rows.append({
            "code": str(r.get("代码")), "name": r.get("名称"),
            "boards": boards,
            "chg_pct": _num(r.get("涨跌幅"), 2),
            "seal_yi": round((_f(r.get("封板资金")) or 0) / 1e8, 2),
            "break_times": int(_f(r.get("炸板次数")) or 0),
            "industry": str(r.get("所属行业") or ""),
            "first_seal": str(r.get("首次封板时间") or ""),
        })
    rows.sort(key=lambda x: (-x["boards"], -x["seal_yi"]))
    out["leaders"] = rows
    out["leader_rule"] = f"连板数 >= {LEADER_MIN_BOARDS}"


def northbound(out: dict) -> None:
    """北向资金（沪深港通）。注：北向实时额度已停止披露，成交净买额可能为 0。"""
    import akshare as ak

    try:
        rows = _rows(ak.stock_hsgt_fund_flow_summary_em())
        items = []
        for r in rows:
            if str(r.get("资金方向")) != "北向":
                continue
            items.append({
                "trade_date": str(r.get("交易日")),
                "board": str(r.get("板块")),
                "net_buy_yi": round((_f(r.get("成交净买额")) or 0) / 1e8, 2),
                "net_inflow_yi": round((_f(r.get("资金净流入")) or 0) / 1e8, 2),
                "up": int(_f(r.get("上涨数")) or 0),
                "down": int(_f(r.get("下跌数")) or 0),
                "index_chg_pct": _num(r.get("指数涨跌幅"), 3),
            })
        out["northbound"] = items
        if items and all(abs(i["net_buy_yi"]) < 1e-9 for i in items):
            out.setdefault("caveats", []).append(
                "北向成交净买额为 0：沪深港通实时额度已停止披露，该字段不可用于判断资金方向。")
    except Exception as exc:
        _err(out, "northbound", exc)
        out["northbound"] = []


def lhb(out: dict, date8: str) -> None:
    """龙虎榜：净买额前排 + 机构参与。"""
    import akshare as ak

    try:
        rows = _rows(ak.stock_lhb_detail_em(start_date=date8, end_date=date8))
    except Exception as exc:
        _err(out, "lhb", exc)
        out["lhb"] = []
        return

    items = []
    for r in rows:
        net = _f(r.get("龙虎榜净买额"))
        items.append({
            "code": str(r.get("代码")), "name": r.get("名称"),
            "chg_pct": _num(r.get("涨跌幅"), 2),
            "net_buy_wan": round(net / 1e4, 1) if net is not None else None,
            "note": str(r.get("解读") or "")[:40],
        })
    items = [i for i in items if i["net_buy_wan"] is not None]

    # ★ 同一只票可能因多个上榜原因出现多次，数值完全相同。
    #   直接排会看到同一只票占满前几名（实测出现过 3 次），所以按代码去重，
    #   保留绝对值最大的那次，并把上榜次数记下来。
    merged: dict[str, dict] = {}
    for it in items:
        cur = merged.get(it["code"])
        if cur is None:
            merged[it["code"]] = {**it, "times": 1}
        else:
            cur["times"] += 1
            if abs(it["net_buy_wan"]) > abs(cur["net_buy_wan"]):
                cur["net_buy_wan"] = it["net_buy_wan"]
                cur["note"] = it["note"]

    items = sorted(merged.values(), key=lambda x: -x["net_buy_wan"])
    out["lhb_top"] = items[:10]
    out["lhb_bottom"] = items[-5:] if len(items) > 5 else []
    out["lhb_n"] = len(items)
    out["lhb_note"] = "同一代码多个上榜原因已合并（取绝对值最大的一次净买额）"


def news(out: dict) -> None:
    """财经快讯：财联社 + 东财，按时间窗口过滤、去重、限量。"""
    import akshare as ak

    cutoff = datetime.datetime.now() - datetime.timedelta(hours=NEWS_HOURS)
    items: list[dict] = []

    try:
        for r in _rows(ak.stock_info_global_cls(symbol="全部")):
            ts = f"{r.get('发布日期','')} {r.get('发布时间','')}".strip()
            items.append({"title": str(r.get("标题") or "").strip(),
                          "content": str(r.get("内容") or "").strip(),
                          "time": ts, "src": "财联社"})
    except Exception as exc:
        _err(out, "news_cls", exc)

    try:
        for r in _rows(ak.stock_info_global_em()):
            items.append({"title": str(r.get("标题") or "").strip(),
                          "content": str(r.get("摘要") or "").strip(),
                          "time": str(r.get("发布时间") or "").strip(),
                          "src": "东方财富"})
    except Exception as exc:
        _err(out, "news_em", exc)

    def _parse(ts: str) -> datetime.datetime | None:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.datetime.strptime(ts, fmt)
            except ValueError:
                continue
        return None

    kept, seen = [], set()
    for it in items:
        text = (it["title"] or it["content"])[:40]
        if not text:
            continue
        key = re.sub(r"\s+", "", text)[:24]
        if key in seen:
            continue
        seen.add(key)
        t = _parse(it["time"])
        if t is not None and t < cutoff:
            continue
        it["content"] = it["content"][:220]
        kept.append(it)

    kept.sort(key=lambda x: x["time"], reverse=True)
    out["news"] = kept[:NEWS_MAX]
    out["news_window_hours"] = NEWS_HOURS
    out["news_total_seen"] = len(items)


def _LOG(msg: str) -> None:
    """候选池里战法模块的日志出口（没接日志器时直接打印，绝不静默失败）。"""
    try:
        print(msg)
    except Exception:
        pass


def candidates(out: dict, date8: str) -> None:
    """候选池：**带价格的**可交易标的清单，避免 LLM 编造代码。

    为什么必须有
    ------------
    第一版只给了「题材聚合（汽车零部 10 家涨停）」和「连板梯队前 10 名」，
    结果 LLM 为了选汽车零部的票，**自己编了一个 603197 保隆科技**
    （它确实属于汽车零部件，但当天不在任何池子里，价格也无从得知），
    接着 stop_loss / take_profit 只能填 0，四条计划全被内核拒绝。

    所以这里把「当天真正在池子里的票」连价格一起给它：涨停池 + 强势股池 +
    龙虎榜净买前列，带上前收与涨跌停价，让模型给的止损止盈可被校验。
    """
    import akshare as ak

    from portal.virtual import marketdb as M

    universe: dict[str, dict] = {}

    def add(code, name, close, chg, *, src: str, **extra) -> None:
        code = str(code or "").strip()
        if not (len(code) == 6 and code.isdigit()):
            return
        px = _f(close)
        if px is None or px <= 0:
            return
        rec = universe.get(code)
        if rec is None:
            c = _f(chg)
            prev = round(px / (1 + c / 100), 2) if c not in (None, -100) else None
            try:
                up, down = (M.limit_prices(prev, code, date8) if prev else (None, None))
            except Exception:
                up = down = None
            rec = {"code": code, "name": name, "close": round(px, 2),
                   "chg_pct": _num(chg, 2), "prev_close": prev,
                   "limit_up": up, "limit_down": down,
                   "sources": [], "boards": None, "industry": None,
                   "seal_yi": None, "amount_yi": None, "turnover": None,
                   "lhb_net_wan": None}
            universe[code] = rec
        rec["sources"].append(src)
        for k, v in extra.items():
            if v is not None:
                rec[k] = v

    try:
        for r in _rows(ak.stock_zt_pool_em(date=date8)):
            add(r.get("代码"), r.get("名称"), r.get("最新价"), r.get("涨跌幅"),
                src="涨停", boards=int(_f(r.get("连板数")) or 1),
                industry=str(r.get("所属行业") or "") or None,
                seal_yi=round((_f(r.get("封板资金")) or 0) / 1e8, 2),
                amount_yi=round((_f(r.get("成交额")) or 0) / 1e8, 2),
                turnover=_num(r.get("换手率"), 2))
    except Exception as exc:
        _err(out, "candidates_zt", exc)

    try:
        for r in _rows(ak.stock_zt_pool_strong_em(date=date8)):
            add(r.get("代码"), r.get("名称"), r.get("最新价"), r.get("涨跌幅"),
                src="强势", boards=int(_f(r.get("连板数")) or 0) or None,
                industry=str(r.get("所属行业") or "") or None,
                amount_yi=round((_f(r.get("成交额")) or 0) / 1e8, 2),
                turnover=_num(r.get("换手率"), 2))
    except Exception as exc:
        _err(out, "candidates_strong", exc)

    try:
        rows = _rows(ak.stock_lhb_detail_em(start_date=date8, end_date=date8))
        best: dict[str, float] = {}
        for r in rows:
            code = str(r.get("代码") or "")
            net = _f(r.get("龙虎榜净买额"))
            if code and net is not None:
                best[code] = max(best.get(code, -1e18), net)
        for r in rows:
            code = str(r.get("代码") or "")
            if code in best and best[code] == _f(r.get("龙虎榜净买额")):
                add(code, r.get("名称"), r.get("收盘价"), r.get("涨跌幅"),
                    src="龙虎榜", lhb_net_wan=round(best[code] / 1e4, 1))
    except Exception as exc:
        _err(out, "candidates_lhb", exc)

    rows = sorted(universe.values(),
                  key=lambda x: (-(x.get("boards") or 0), -(x.get("seal_yi") or 0),
                                 -(x.get("amount_yi") or 0)))
    # ── 涨停家族：降级为「只能观察」 ──────────────────────
    # 实测（2026-10-19）这三个来源给出的 40 只票 **40 只都已封涨停**，
    # 而内核禁止打板 → 可买 0 只，账户永远不动。所以这里显式标成
    # buyable=False，只让它们进观察表等回调；真正的买点交给因子战法。
    from .screen import LIMIT_UP_CAP
    for r in rows:
        r["sources"] = sorted(set(r["sources"]))
        r["playbook"] = "limit_up_chase"
        r["playbook_cn"] = "涨停/连板（仅观察）"
        r["buyable"] = False
        r["why"] = "当日已封涨停：只进观察表等回调，内核禁止打板买入"
        r["setup"] = []
        r["metrics"] = {}
    limit_ups = rows[:LIMIT_UP_CAP]

    # ── A/B/C 战法（因子库）────────────────────────────────
    screened: list[dict] = []
    skip_reason: str | None = None
    iso = f"{date8[:4]}-{date8[4:6]}-{date8[6:8]}"
    try:
        from . import screen as screen_mod
        from portal.virtual import marketdb as M2
        conn2 = M2.connect()
        if conn2 is None:
            skip_reason = "本地 market.db 不可用，A/B/C 战法需要历史K线，已跳过"
        else:
            codes = _quote_universe(conn2)
            quotes: list[dict] = []
            for i in range(0, len(codes), 50):
                quotes.extend(M2.fetch_quotes_batch(codes[i:i + 50]))
            screened, skip_reason = screen_mod.screen(
                conn2, quotes, iso, log=lambda m: _LOG(m))
            conn2.close()
    except Exception as exc:
        _err(out, "screen_playbooks", exc)
        skip_reason = f"战法筛选失败：{type(exc).__name__}"

    if skip_reason:
        out.setdefault("caveats", []).append(skip_reason)
        # 战法没跑出来 = 候选池缺了「可买」那部分，这份结果不许进缓存
        out["_degraded"] = True

    for r in screened:
        r["sources"] = sorted(set(r.get("sources") or []))

    # 可买的排前面：模型注意力有限，先看能买的
    out["candidates"] = screened + limit_ups
    out["candidates_n"] = len(universe) + len(screened)
    out["candidates_buyable_n"] = len(screened)
    out["candidates_observe_n"] = len(limit_ups)
    out["candidates_note"] = (
        f"可买 {len(screened)} 只（A/B/C 战法）+ 仅观察 {len(limit_ups)} 只"
        f"（涨停家族，内核禁买）。模型选股必须来自这里。")


def market_stats(out: dict, date8: str) -> None:
    """用本地 market.db 算「涨跌分布 + 量能变化」—— 官方接口没有这两块。

    为什么自己做：用户要的「涨跌情况、量能变化」，`stock_market_activity_legu`
    只给上涨/下跌总数，没有分布，也没有「今日成交额 vs 5 日/20 日均量」的对比。
    market.db 里有全市场逐日行情，这两样都能精确算出来，且不依赖网络。
    """
    try:
        from portal.virtual import marketdb as M
        conn = M.connect()
    except Exception as exc:
        _err(out, "market_stats_import", exc)
        return
    if conn is None:
        out.setdefault("caveats", []).append("本地 market.db 不可用，涨跌分布与量能对比缺失。")
        return

    iso = f"{date8[:4]}-{date8[4:6]}-{date8[6:8]}"
    try:
        rows = conn.execute(
            """WITH w AS (
                   SELECT code, date, close, amount,
                          LAG(close) OVER (PARTITION BY code ORDER BY date) AS prev
                   FROM day
                   WHERE date <= ? AND date >= date(?, '-60 day')
               )
               SELECT code, date, close, prev, amount FROM w
               WHERE date <= ? ORDER BY date""", (iso, iso, iso)).fetchall()
    except Exception as exc:
        _err(out, "market_stats_query", exc)
        conn.close()
        return
    finally:
        pass

    amt_by_date: dict[str, float] = {}
    dist: dict[str, int] = {}
    n_up = n_down = n_flat = 0
    buckets = [(">7%", 7, 999), ("5~7%", 5, 7), ("2~5%", 2, 5), ("0~2%", 0, 2),
               ("-2~0%", -2, 0), ("-5~-2%", -5, -2), ("-7~-5%", -7, -5), ("<-7%", -999, -7)]
    for r in rows:
        amt_by_date[r["date"]] = amt_by_date.get(r["date"], 0.0) + (r["amount"] or 0)
        if r["date"] != iso or not r["prev"]:
            continue
        pct = (r["close"] / r["prev"] - 1) * 100
        if pct > 0:
            n_up += 1
        elif pct < 0:
            n_down += 1
        else:
            n_flat += 1
        for label, lo, hi in buckets:
            if lo <= pct < hi:
                dist[label] = dist.get(label, 0) + 1
                break

    dates = sorted(amt_by_date)
    today_amt = amt_by_date.get(iso, 0.0)
    prev5 = [amt_by_date[d] for d in dates if d < iso][-5:]
    prev20 = [amt_by_date[d] for d in dates if d < iso][-20:]
    tot = n_up + n_down + n_flat
    stats = {
        "date": iso, "n_total": tot,
        "n_up": n_up, "n_down": n_down, "n_flat": n_flat,
        "up_ratio_pct": round(n_up / tot * 100, 2) if tot else None,
        "distribution": dist,
        "amount_yi": round(today_amt / 1e8, 1),
        "amount_ma5_yi": round(sum(prev5) / len(prev5) / 1e8, 1) if prev5 else None,
        "amount_ma20_yi": round(sum(prev20) / len(prev20) / 1e8, 1) if prev20 else None,
        "amount_vs_ma5_pct": round((today_amt / (sum(prev5) / len(prev5)) - 1) * 100, 2)
        if prev5 and sum(prev5) else None,
        "source": "market.db 全市场逐日行情（本地计算）",
    }

    # ★ 库不完整时必须换口径，否则报告会拿半个市场冒充全市场。
    #   实测（GitHub Actions 首次真实运行 2026-10-20）：CI 上没有 market.db 缓存，
    #   `init` 又只拿到沪市 2318 只（深交所列表接口在境外 IP 上失败），
    #   于是涨跌分布只覆盖约四成市场，而报告照样写"全市场 2317 只"——
    #   数字是错的却不显眼，这比报错更危险。
    try:
        n_today_rows = conn.execute(
            "SELECT COUNT(*) FROM day WHERE date = ?", (iso,)).fetchone()[0]
    except Exception:
        n_today_rows = 0
    expected = len(_quote_universe(conn))
    if n_today_rows < max(800, int(expected * 0.6)):
        live = _live_market_stats(expected)
        if live:
            live["amount_ma5_yi"] = stats["amount_ma5_yi"]
            live["amount_ma20_yi"] = stats["amount_ma20_yi"]
            live["amount_vs_ma5_pct"] = (
                round((live["amount_yi"] / stats["amount_ma5_yi"] - 1) * 100, 2)
                if live.get("amount_yi") and stats.get("amount_ma5_yi") else None)
            live["source"] = "腾讯批量行情（库里当日数据不完整，改用实盘重算分布）"
            stats = live
            out.setdefault("caveats", []).append(
                f"本地库当日只覆盖 {n_today_rows}/{expected} 只，"
                f"涨跌分布已改用实时行情重算；5/20 日均量仍取本地库历史完整部分。")
    out["market_stats"] = stats
    conn.close()


def _quote_universe(conn=None, *, floor: int = 4000) -> list[str]:
    """全市场代码表：优先 market.db，不完整时退回 akshare 全市场列表。

    `market.db` 在全新环境（CI 首次运行）里可能只有部分市场；
    `stock_info_a_code_name()`（东财，5575 只）在境外 IP 上更稳。
    """
    try:
        from portal.virtual import marketdb as M
    except Exception:
        return []
    codes: list[str] = []
    if conn is not None:
        try:
            codes = [c for c in M.universe(conn) if M.board_of(c) != "bse"]
        except Exception:
            codes = []
    if len(codes) >= floor:
        return codes
    try:
        import akshare as ak
        got = [str(r.get("code") or "").strip()
               for r in _rows(ak.stock_info_a_code_name())]
        got = [c for c in got if c and M.board_of(c) != "bse"]
        if len(got) > len(codes):
            return got
    except Exception:
        pass
    return codes


def _live_market_stats(codes: list[str] | None = None) -> dict | None:
    """用实时/收盘批量行情算涨跌分布。收盘后调用即等于当日全天口径。"""
    from portal.virtual import marketdb as M
    codes = codes or _quote_universe()
    if not codes:
        return None
    quotes: list[dict] = []
    try:
        for i in range(0, len(codes), 50):
            quotes.extend(M.fetch_quotes_batch(codes[i:i + 50]))
    except Exception:
        return None
    if not quotes:
        return None
    dist = {">7%": 0, "5~7%": 0, "2~5%": 0, "0~2%": 0,
            "-2~0%": 0, "-5~-2%": 0, "-7~-5%": 0, "<-7%": 0}
    bricks = ((">7%", 7, 999), ("5~7%", 5, 7), ("2~5%", 2, 5), ("0~2%", 0, 2),
              ("-2~0%", -2, 0), ("-5~-2%", -5, -2), ("-7~-5%", -7, -5),
              ("<-7%", -999, -7))
    up = down = flat = 0
    amount = 0.0
    for q in quotes:
        amount += q.get("amount") or 0
        p, c = q.get("prev_close"), q.get("close")
        if not p or not c:
            continue
        pct = (c / p - 1) * 100
        if pct > 0:
            up += 1
        elif pct < 0:
            down += 1
        else:
            flat += 1
        for label, lo, hi in bricks:
            if lo <= pct < hi:
                dist[label] += 1
                break
    tot = up + down + flat
    return {
        "date": quotes[0].get("date"),
        "n_total": tot, "n_up": up, "n_down": down, "n_flat": flat,
        "up_ratio_pct": round(up / tot * 100, 2) if tot else None,
        "distribution": dist,
        "amount_yi": round(amount / 1e8, 1),
        "amount_ma5_yi": None, "amount_ma20_yi": None, "amount_vs_ma5_pct": None,
        "source": "腾讯批量行情（实盘）",
    }


def index_detail(out: dict) -> None:
    """主板/创业板的 K 线细节：均线位置、量能倍数、振幅、连续涨跌。

    用户要「主板 K 线、创业板 K 线」。这里给判断趋势的最小集合：
    收盘相对 MA5/10/20/60、5/20 日涨幅、今日振幅、量能相对 5 日均量的倍数、
    连续涨跌天数。这些比一句「走弱」有信息量得多。
    """
    import akshare as ak

    detail = []
    for code, name in (("sh000001", "上证指数（主板）"),
                       ("sz399001", "深证成指"),
                       ("sz399006", "创业板指")):
        try:
            rows = _rows(ak.stock_zh_index_daily(symbol=code))
        except Exception as exc:
            _err(out, f"index_detail:{code}", exc)
            continue
        closes = [_f(r.get("close")) for r in rows if _f(r.get("close"))]
        vols = [_f(r.get("volume")) or 0 for r in rows]
        if len(closes) < 25:
            continue
        last = closes[-1]

        def ma(n: int) -> float | None:
            return round(sum(closes[-n:]) / n, 2) if len(closes) >= n else None

        ma5, ma10, ma20, ma60 = ma(5), ma(10), ma(20), ma(60)
        streak, direction = 0, 0
        for i in range(len(closes) - 1, 0, -1):
            d = 1 if closes[i] > closes[i - 1] else (-1 if closes[i] < closes[i - 1] else 0)
            if d == 0:
                break
            if direction == 0:
                direction, streak = d, 1
            elif d == direction:
                streak += 1
            else:
                break
        vol5 = sum(vols[-6:-1]) / 5 if len(vols) >= 6 and sum(vols[-6:-1]) else None
        detail.append({
            "code": code, "name": name, "close": round(last, 2),
            "chg_pct": round((last / closes[-2] - 1) * 100, 3) if len(closes) > 1 else None,
            "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60,
            "vs_ma5_pct": round((last / ma5 - 1) * 100, 2) if ma5 else None,
            "vs_ma20_pct": round((last / ma20 - 1) * 100, 2) if ma20 else None,
            "vs_ma60_pct": round((last / ma60 - 1) * 100, 2) if ma60 else None,
            "chg_5d_pct": round((last / closes[-6] - 1) * 100, 2) if len(closes) >= 6 else None,
            "chg_20d_pct": round((last / closes[-21] - 1) * 100, 2) if len(closes) >= 21 else None,
            "amplitude_pct": round(((_f(rows[-1].get("high")) - _f(rows[-1].get("low")))
                                    / closes[-2]) * 100, 2) if len(closes) > 1 else None,
            "vol_vs_ma5": round(vols[-1] / vol5, 2) if vol5 else None,
            "streak_days": streak,
            "streak_dir": ("涨" if direction > 0 else "跌" if direction < 0 else "平"),
            "above": {"ma5": last > (ma5 or last), "ma10": last > (ma10 or last),
                      "ma20": last > (ma20 or last), "ma60": last > (ma60 or last)},
        })
    out["index_detail"] = detail


def theme_trend(out: dict, date8: str) -> None:
    """题材多日趋势：近 5 个交易日各行业涨停家数序列。

    只看单日涨停家数，分不清「正在发酵」和「已经兑现」。给 5 日序列后，
    升温 / 降温 / 持续 一目了然 —— 这是判断可持续性最硬的证据。
    """
    import akshare as ak

    d = datetime.datetime.strptime(date8, "%Y%m%d").date()
    days: list[str] = []
    guard = 0
    while len(days) < 5 and guard < 20:
        if d.weekday() < 5:
            days.append(d.strftime("%Y%m%d"))
        d -= datetime.timedelta(days=1)
        guard += 1
    days.reverse()

    series: dict[str, list[int]] = {}
    ok_days: list[str] = []
    for dd in days:
        try:
            rows = _rows(ak.stock_zt_pool_em(date=dd))
        except Exception:
            continue
        if not rows:
            continue
        ok_days.append(dd)
        for r in rows:
            ind = str(r.get("所属行业") or "未分类").strip()
            s = series.setdefault(ind, [0] * len(days))
            s[days.index(dd)] += 1

    out["theme_trend"] = {
        "days": ok_days or days,
        "series": {k: v[-len(ok_days):] if ok_days else v
                   for k, v in series.items() if sum(v) >= 2},
        "note": "各行业近几日涨停家数；连续多日上榜 = 题材仍在发酵",
    }


def market_db_context(out: dict) -> None:
    """用本地 market.db 补充：最新交易日、当日涨停重建对照、成交额合计。

    这一步走本地库，不依赖网络；库不存在就跳过并留 caveat。
    """
    try:
        from portal.virtual import marketdb as M
    except Exception as exc:  # pragma: no cover - 环境问题
        _err(out, "market_db_import", exc)
        return

    try:
        conn = M.connect()
    except Exception as exc:
        _err(out, "market_db_connect", exc)
        return
    if conn is None:
        out.setdefault("caveats", []).append("本地 market.db 不可用，涨停重建未做对照。")
        return
    try:
        latest = M.latest_trade_date(conn)
        out["db_latest_date"] = latest
        if latest:
            row = conn.execute(
                "SELECT COUNT(*) n, SUM(amount) amt FROM day WHERE date=?", (latest,)
            ).fetchone()
            out["db_day"] = {"date": latest,
                             "n_codes": int(row["n"] or 0),
                             "amount_yi": round((row["amt"] or 0) / 1e8, 1)}
    except Exception as exc:
        _err(out, "market_db_query", exc)
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────
BLOCKS: list[tuple[str, Callable[..., None]]] = [
    ("indices", index_snapshot),
    ("breadth", breadth),
    ("themes", themes),
    ("leaders", leaders),
    ("northbound", northbound),
    ("lhb", lhb),
    ("candidates", candidates),
    ("index_detail", index_detail),
    ("market_stats", market_stats),
    ("theme_trend", theme_trend),
    ("news", news),
]


def collect(date8: str | None = None, *, use_cache: bool = True,
            progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """采集当日全部盘前信息。任一数据源失败都只记录，不中断。"""
    d8 = (date8 or _today8()).replace("-", "")[:8]
    cache_file = paths.cache_dir(d8) / "collect.json"

    if use_cache and cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            if data.get("_schema") != CACHE_SCHEMA:
                if progress:
                    progress(f"  缓存结构版本不符（{data.get('_schema')} != "
                             f"{CACHE_SCHEMA}），重新采集")
            else:
                data["_from_cache"] = True
                if progress:
                    progress(f"  复用缓存 {cache_file.name}")
                return data
        except json.JSONDecodeError:
            pass

    out: dict[str, Any] = {
        "date": d8, "collected_at": _now(),
        "errors": [], "caveats": [],
        "_from_cache": False,
        "_schema": CACHE_SCHEMA,
    }

    for name, fn in BLOCKS:
        if progress:
            progress(f"  采集 {name}…")
        try:
            if name in ("breadth", "themes", "leaders", "lhb", "candidates",
                         "market_stats", "theme_trend"):
                fn(out, d8)
            else:
                fn(out)
        except Exception as exc:      # 兜底：任何块崩了都不许拖垮整份预案
            _err(out, name, exc)

    try:
        market_db_context(out)
    except Exception as exc:
        _err(out, "market_db_context", exc)

    # 数据缺口统一说明（报告里要显示，不能假装数据齐全）
    if out["errors"]:
        out["caveats"].append(
            f"{len(out['errors'])} 个数据源失败，相关结论需谨慎："
            + "、".join(sorted({e['source'].split(':')[0] for e in out['errors']})))

    # ★ 采集有硬伤时**不许写缓存**。
    #   实测：某次战法模块抛 NameError，候选池只剩 15 只涨停股，但这份残缺结果被
    #   正常写进了缓存（schema 也对），之后整天都在复用"可买 0 只"的数据。
    #   schema 版本只能挡结构变化，挡不住"采到一半失败"被永久固化。
    degraded = bool(out.get("_degraded")) or bool(out["errors"])
    if degraded:
        out["_cache_skipped"] = True
        if progress:
            progress("  ⚠️  采集不完整（"
                     f"{len(out['errors'])} 个数据源失败），本次不写缓存")
    else:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    return out


def summarize(collect_data: dict) -> dict[str, Any]:
    """给 LLM 看的紧凑摘要（省 token，且只给可验证的数字）。"""
    b = collect_data.get("breadth") or {}
    return {
        "date": collect_data.get("date"),
        "indices": [
            {"name": i["name"], "close": i.get("close"), "chg_pct": i.get("chg_pct"),
             "amount_yi": i.get("amount_yi"), "trend": i.get("trend"),
             "above_ma20": i.get("above_ma20"), "chg_5d_pct": i.get("chg_5d_pct")}
            for i in collect_data.get("indices") or []
        ],
        "breadth": {k: b.get(k) for k in
                    ("up", "down", "flat", "up_ratio_pct", "limit_up", "limit_down",
                     "pool_zt", "pool_zb", "pool_dt", "break_rate_pct",
                     "max_boards", "multi_board_n", "seal_amount_yi", "activity")
                    if b.get(k) is not None},
        "themes_top": [
            {"name": t["name"], "limit_up_n": t["limit_up_n"],
             "boards_sum": t["boards_sum"], "max_boards": t["max_boards"],
             "leaders": [l["name"] for l in t["leaders"][:3]]}
            for t in (collect_data.get("themes") or [])[:10]
        ],
        "leaders": [
            {"name": l["name"], "code": l["code"], "boards": l["boards"],
             "industry": l["industry"]}
            for l in (collect_data.get("leaders") or [])[:10]
        ],
        "northbound": collect_data.get("northbound") or [],
        "lhb_top": (collect_data.get("lhb_top") or [])[:6],
        "index_detail": [
            {k: d.get(k) for k in
             ("name", "close", "chg_pct", "ma5", "ma20", "ma60", "vs_ma20_pct",
              "chg_5d_pct", "chg_20d_pct", "amplitude_pct", "vol_vs_ma5",
              "streak_days", "streak_dir", "above")}
            for d in (collect_data.get("index_detail") or [])
        ],
        "market_stats": collect_data.get("market_stats") or {},
        "theme_trend": collect_data.get("theme_trend") or {},
        "candidates": [
            {"code": c["code"], "name": c["name"], "close": c["close"],
             "prev_close": c.get("prev_close"), "limit_up": c.get("limit_up"),
             "limit_down": c.get("limit_down"), "chg_pct": c.get("chg_pct"),
             "boards": c.get("boards"), "industry": c.get("industry"),
             "seal_yi": c.get("seal_yi"), "sources": c.get("sources"),
             # 战法归属必须留在归档里，否则事后无法回答「今天为什么买/没买它」
             "playbook": c.get("playbook"), "playbook_cn": c.get("playbook_cn"),
             "buyable": c.get("buyable"), "setup": c.get("setup")}
            for c in (collect_data.get("candidates") or [])[:40]
        ],
        "candidates_note": collect_data.get("candidates_note"),
        "candidates_buyable_n": collect_data.get("candidates_buyable_n"),
        "candidates_observe_n": collect_data.get("candidates_observe_n"),
        "news": [
            {"t": n["time"][:16], "src": n["src"],
             "text": (n["title"] or n["content"])[:70]}
            for n in (collect_data.get("news") or [])[:30]
        ],
        "caveats": collect_data.get("caveats") or [],
        "errors": [e["source"] for e in collect_data.get("errors") or []],
    }


__all__ = ["collect", "summarize", "INDICES", "BLOCKS", "NEWS_MAX", "NEWS_HOURS"]
