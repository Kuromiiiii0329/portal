# -*- coding: utf-8 -*-
"""
portal/premarket/factors/portfolio.py — 策略级组合回测（真回测）

与 profile.py / validate.py 的区别
---------------------------------
| | IC 检验 | 前向画像 | **本模块** |
|---|---|---|---|
| 持仓 | 无 | 无 | 有（受限仓位） |
| 成交价 | 无 | 信号日收盘 | **次日开盘** |
| 涨跌停 | 无 | 无 | **开盘即涨停买不进** |
| 成本 | 无 | 无 | 佣金+印花+滑点 |
| 卖出规则 | 无 | 固定持有 | **四种策略可选** |
| 输出 | IC/IR | 胜率/涨幅 | **净值、年化、每年收益、最大回撤、夏普、卡玛、胜率、盈亏比、换手** |

四种卖出策略
-----------
    fixed(H)     买入后第 H 个交易日开盘卖出（基准）
    rebalance    每日重选，被挤出前 N 名就卖（跟势、换手高）
    risk         止损/止盈/移动止损，盘中用 high/low 触发（控回撤）
    decay        战法条件不再成立就卖（逻辑最一致）

执行口径（A 股现实约束）
----------------------
- 信号在 **收盘** 产生，**次日开盘** 成交（T+1）
- 次日开盘价 ≥ 涨停价 → 买不进，跳过
- 买入：`open × (1 + 滑点)`，再扣佣金
- 卖出：`open × (1 - 滑点)`，再扣佣金 + 印花税
- 风险止损在**盘中**按止损价成交（用当日 low 判断是否触及）
- 分数股（不凑 100 股整手）—— 这是为了精确表达仓位比例，执行层再取整

性能
----
**单遍扫描、多组合并行**：一次遍历 2430 个交易日，因子值只算一遍，
所有配置共用；每个配置只做自己的账本。所以 84 个配置 ≈ 一遍的代价。
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Callable

#: 成本（基点 = 万分之一）
COST_COMMISSION_BP = 2.5      # 佣金，双边
COST_STAMP_BP = 10.0          # 印花税，仅卖出
COST_SLIP_BP = 10.0           # 滑点，双边
#: 一年按多少交易日折算
DAYS_PER_YEAR = 243


@dataclass
class Config:
    id: str
    playbooks: list[str] | None = None      # None = 全部可买战法
    sell: str = "fixed"                     # fixed|rebalance|risk|decay
    hold: int = 5
    max_pos: int = 5
    stop_pct: float = 8.0                   # risk 用
    take_pct: float = 15.0
    trail_pct: float | None = None
    max_hold: int = 20                      # risk/decay 的兜底持有上限
    #: 成本缩放。0 = 零成本对照，用来区分「选股有没有 alpha」与「成本吃掉多少」
    cost_scale: float = 1.0


@dataclass
class Position:
    code: str
    shares: float
    entry_price: float           # 含滑点的成交价
    entry_idx: int
    playbook: str
    peak_close: float = 0.0


@dataclass
class Book:
    """一个配置的账本。"""
    cfg: Config
    cash: float = 1_000_000.0
    positions: dict[str, Position] = field(default_factory=dict)
    pending_buy: list[dict] = field(default_factory=list)
    pending_sell: list[str] = field(default_factory=list)
    equity: list[tuple[str, float]] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    traded_notional: float = 0.0

    def value(self, close: dict[str, float]) -> float:
        v = self.cash
        for code, p in self.positions.items():
            px = close.get(code) or p.entry_price
            v += p.shares * px
        return v


def _buy(book: Book, code: str, price: float, playbook: str, idx: int,
         notional: float) -> None:
    """按目标金额买入。price 已含滑点。"""
    if notional <= 0 or price <= 0:
        return
    shares = notional / price
    fee = notional * COST_COMMISSION_BP / 1e4 * book.cfg.cost_scale
    if shares <= 0 or book.cash < notional + fee:
        return
    book.cash -= notional + fee
    book.traded_notional += notional
    book.positions[code] = Position(code, shares, price, idx, playbook,
                                    peak_close=price)


def _sell(book: Book, code: str, price: float, idx: int, reason: str) -> None:
    p = book.positions.pop(code, None)
    if not p or price <= 0:
        return
    gross = p.shares * price
    fee = gross * (COST_COMMISSION_BP + COST_STAMP_BP) / 1e4 * book.cfg.cost_scale
    book.cash += gross - fee
    book.traded_notional += gross
    book.trades.append({
        "code": code, "playbook": p.playbook, "reason": reason,
        "entry_idx": p.entry_idx, "exit_idx": idx,
        "hold_days": idx - p.entry_idx,
        "entry_price": round(p.entry_price, 4),
        "exit_price": round(price, 4),
        "ret_pct": round((gross - fee) / (p.shares * p.entry_price) * 100 - 100, 4),
    })


def _metrics(book: Book) -> dict[str, Any]:
    eq = [v for _, v in book.equity]
    if len(eq) < 30:
        return {"ok": False, "reason": "净值序列太短"}
    total = eq[-1] / eq[0] - 1
    n = len(eq)
    years = n / DAYS_PER_YEAR
    cagr = (eq[-1] / eq[0]) ** (1 / years) - 1 if years > 0 and eq[-1] > 0 else None
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, n) if eq[i - 1] > 0]
    vol = statistics.pstdev(rets) * math.sqrt(DAYS_PER_YEAR) if len(rets) > 2 else 0.0
    sharpe = ((statistics.fmean(rets) * DAYS_PER_YEAR) / vol) if vol > 1e-9 else None
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1)
    # 每年收益
    by_year: dict[str, float] = {}
    year_first: dict[str, float] = {}
    for d, v in book.equity:
        y = d[:4]
        year_first.setdefault(y, v)
        by_year[y] = v
    yearly = {}
    prev = None
    for y in sorted(by_year):
        base = prev if prev is not None else year_first[y]
        yearly[y] = round((by_year[y] / base - 1) * 100, 2) if base else None
        prev = by_year[y]
    # 交易统计
    wins = [t["ret_pct"] for t in book.trades if t["ret_pct"] > 0]
    losses = [t["ret_pct"] for t in book.trades if t["ret_pct"] <= 0]
    win_rate = len(wins) / len(book.trades) if book.trades else None
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else None
    avg_hold = (statistics.fmean([t["hold_days"] for t in book.trades])
                if book.trades else None)
    turnover = (book.traded_notional / statistics.fmean(eq) / years
                if years > 0 else None)
    return {
        "ok": True,
        "total_pct": round(total * 100, 2),
        "cagr_pct": round(cagr * 100, 2) if cagr is not None else None,
        "max_dd_pct": round(mdd * 100, 2),
        "sharpe": round(sharpe, 3) if sharpe is not None else None,
        "calmar": (round(cagr / abs(mdd), 3)
                   if cagr is not None and mdd < 0 else None),
        "vol_pct": round(vol * 100, 2),
        "trades": len(book.trades),
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "profit_factor": round(pf, 3) if pf is not None else None,
        "avg_hold_days": round(avg_hold, 2) if avg_hold is not None else None,
        "turnover_x": round(turnover, 2) if turnover is not None else None,
        "yearly": yearly,
        "years": round(years, 2),
    }


def run_sweep(conn=None, *, configs: list[Config],
              max_days: int | None = None,
              log: Callable[[str], None] = print) -> dict[str, Any]:
    """单遍扫描，多组合并行。返回每个配置的绩效。"""
    from portal.virtual import marketdb as M
    from . import load_factors
    from . import playbooks as PB
    from .panel_iter import iter_panels, trading_dates
    from ..screen import select_panel

    own = conn is None
    if conn is None:
        conn = M.connect()
    if conn is None:
        return {"ok": False, "error": "market.db 不可用"}
    try:
        factors = load_factors()
        play_defs = {p["id"]: p for p in PB.load_all()}
        buyable = [p for p in PB.buyable_ids()]
        books: list[Book] = []
        for cfg in configs:
            if cfg.playbooks is None:
                cfg = Config(**{**cfg.__dict__, "playbooks": buyable})
            books.append(Book(cfg))
        dates = trading_dates(conn, limit=max_days)
        log(f"组合回测：{len(books)} 个配置 × {len(dates)} 个交易日")

        idx = 0
        bench_eq: list[tuple[str, float]] = []
        bench_val = 1.0
        prev_close_map: dict[str, float] = {}
        for d, panel, close_map in iter_panels(conn, dates, log=None):
            # 基准：全市场等权、每日再平衡、**零成本**（这是"什么都不选"的对照）
            if prev_close_map:
                rets = [close_map[c] / prev_close_map[c] - 1
                        for c in close_map
                        if c in prev_close_map and prev_close_map[c]]
                if rets:
                    bench_val *= 1 + statistics.fmean(rets)
            bench_eq.append((d, bench_val))
            prev_close_map = close_map
            # ── 1) 当日信号：只算一遍，所有配置共用 ──
            cands = select_panel(panel, factors=factors,
                                 plays=PB.load_all(), cap=None)
            by_play: dict[str, list[dict]] = {}
            for c in cands:
                by_play.setdefault(c["playbook"], []).append(c)

            limit_up_px = {}
            for code, pc in panel.prev_close.items():
                lp = panel.limit_pct.get(code) or 10.0
                limit_up_px[code] = round(pc * (1 + lp / 100), 2)

            for bk in books:
                cfg = bk.cfg
                # ── 2) 盘中风控（risk 策略）：用今日 low/high 判断触发 ──
                if cfg.sell == "risk":
                    for code in list(bk.positions):
                        pos = bk.positions[code]
                        lo = panel.low_today.get(code)
                        hi = panel.high_today.get(code)
                        cl = close_map.get(code)
                        if cl:
                            pos.peak_close = max(pos.peak_close, cl)
                        stop = pos.entry_price * (1 - cfg.stop_pct / 100)
                        if cfg.trail_pct:
                            stop = max(stop, pos.peak_close
                                       * (1 - cfg.trail_pct / 100))
                        take = pos.entry_price * (1 + cfg.take_pct / 100)
                        if lo is not None and lo <= stop:
                            _sell(bk, code, stop, idx, "stop")
                        elif hi is not None and hi >= take:
                            _sell(bk, code, take, idx, "take")

                # ── 3) 执行昨天的委托：今开成交 ──
                for code in bk.pending_sell:
                    if code in bk.positions:
                        op = panel.open_today.get(code)
                        if op:
                            _sell(bk, code,
                                  op * (1 - COST_SLIP_BP / 1e4 * cfg.cost_scale),
                                  idx, "rule")
                bk.pending_sell = []
                for order in bk.pending_buy:
                    code = order["code"]
                    if code in bk.positions or len(bk.positions) >= cfg.max_pos:
                        continue
                    op = panel.open_today.get(code)
                    if not op:
                        continue
                    lup = limit_up_px.get(code)
                    if lup and op >= lup - 0.005:
                        continue          # 开盘即涨停，买不进
                    equity = bk.value(close_map)
                    slots = max(1, cfg.max_pos - len(bk.positions))
                    notional = min(bk.cash, equity / cfg.max_pos)
                    notional = min(notional, bk.cash / slots)
                    _buy(bk, code,
                         op * (1 + COST_SLIP_BP / 1e4 * cfg.cost_scale),
                         order["playbook"], idx, notional)
                bk.pending_buy = []

                # ── 4) 收盘估值 ──
                bk.equity.append((d, bk.value(close_map)))

                # ── 5) 生成明日委托 ──
                picks = []
                for pid in (cfg.playbooks or []):
                    picks.extend(by_play.get(pid) or [])
                held = set(bk.positions)

                # rebalance 按**排名**保留前 N 名；decay 只要**条件仍成立**就留。
                # 这两者在实现上必须分开，否则两种策略会跑出一样的结果（我第一版就是这样）。
                if cfg.sell == "rebalance":
                    keep = {c["code"] for c in picks[:cfg.max_pos]}
                elif cfg.sell == "decay":
                    keep = {c["code"] for c in picks}
                else:
                    keep = set(held)

                if cfg.sell == "fixed":
                    for code, pos in list(bk.positions.items()):
                        if idx - pos.entry_idx >= cfg.hold:
                            bk.pending_sell.append(code)
                elif cfg.sell in ("rebalance", "decay"):
                    for code, pos in list(bk.positions.items()):
                        if code not in keep:
                            bk.pending_sell.append(code)
                        elif idx - pos.entry_idx >= cfg.max_hold:
                            bk.pending_sell.append(code)
                elif cfg.sell == "risk":
                    for code, pos in list(bk.positions.items()):
                        if idx - pos.entry_idx >= cfg.max_hold:
                            bk.pending_sell.append(code)

                # 买入：按战法排序依次补位（去重，同一只票可能被多个战法选中）
                need = cfg.max_pos - (len(held) - len(set(bk.pending_sell)))
                seen: set[str] = set()
                for c in picks:
                    if need <= 0:
                        break
                    code = c["code"]
                    if code in held or code in seen:
                        continue
                    seen.add(code)
                    bk.pending_buy.append({"code": code,
                                           "playbook": c["playbook"]})
                    need -= 1
            idx += 1
            if idx % 200 == 0:
                log(f"   …已处理 {idx}/{len(dates)} 天")

        # 基准指标（用同一个 _metrics 口径，但账本是等权指数）
        bench_book = Book(Config(id="__bench__"))
        bench_book.equity = bench_eq
        bench = _metrics(bench_book)
        out = {}
        for bk in books:
            m = _metrics(bk)
            if m.get("ok") and bench.get("ok"):
                m["excess_cagr_pct"] = (round((m["cagr_pct"] or 0)
                                              - (bench["cagr_pct"] or 0), 2))
                m["excess_total_pct"] = (round(m["total_pct"]
                                               - bench["total_pct"], 2))
            out[bk.cfg.id] = {**m, "config": bk.cfg.__dict__,
                              "equity": [(d, round(v, 2))
                                         for d, v in bk.equity[::20]]}
        return {"ok": True, "days": idx, "results": out,
                "benchmark": bench}
    finally:
        if own and conn is not None:
            conn.close()


def default_configs(*, playbooks: list[str] | None = None,
                    max_pos: int = 5) -> list[Config]:
    """默认扫描矩阵：4 种卖出策略 × 若干持有期。"""
    cfgs = []
    for h in (3, 5, 10, 20):
        cfgs.append(Config(id=f"fixed{h}", playbooks=playbooks, sell="fixed",
                           hold=h, max_pos=max_pos))
    cfgs.append(Config(id="rebalance", playbooks=playbooks, sell="rebalance",
                       max_pos=max_pos, max_hold=20))
    cfgs.append(Config(id="decay", playbooks=playbooks, sell="decay",
                       max_pos=max_pos, max_hold=20))
    cfgs.append(Config(id="risk8_15", playbooks=playbooks, sell="risk",
                       max_pos=max_pos, stop_pct=8.0, take_pct=15.0,
                       max_hold=20))
    cfgs.append(Config(id="risk5_10", playbooks=playbooks, sell="risk",
                       max_pos=max_pos, stop_pct=5.0, take_pct=10.0,
                       max_hold=10))
    return cfgs


def zero_cost_configs(*, playbooks: list[str] | None = None,
                      max_pos: int = 5) -> list[Config]:
    """零成本对照：同样选股、同样规则，只去掉费用。

    用途：把「选股 alpha」和「成本拖累」分开。若零成本下仍跑输基准，
    说明选股本身没有 alpha；若零成本下明显跑赢、净结果却亏，
    说明问题在换手，方向应该是降低交易频率而不是换因子。
    """
    out = []
    for c in default_configs(playbooks=playbooks, max_pos=max_pos):
        if c.sell not in ("fixed", "rebalance"):
            continue
        out.append(Config(id=f"{c.id}_nocost", playbooks=playbooks,
                          sell=c.sell, hold=c.hold, max_pos=max_pos,
                          stop_pct=c.stop_pct, take_pct=c.take_pct,
                          trail_pct=c.trail_pct, max_hold=c.max_hold,
                          cost_scale=0.0))
    return out


def render_markdown(res: dict[str, Any], *, title: str) -> str:
    rows = res.get("results") or {}
    b = res.get("benchmark") or {}
    out = [f"# {title}", "",
           f"> 交易日 {res.get('days')} 个　成本：佣金 {COST_COMMISSION_BP}bp + "
           f"印花 {COST_STAMP_BP}bp（卖出）+ 滑点 {COST_SLIP_BP}bp　"
           f"次日开盘成交、涨停买不进", "",
           f"> **基准（全市场等权、每日再平衡、零成本）**："
           f"总收益 {b.get('total_pct')}%　年化 {b.get('cagr_pct')}%　"
           f"最大回撤 {b.get('max_dd_pct')}%　夏普 {b.get('sharpe')}", ""]
    out += ["| 配置 | 卖出 | 持有 | 总收益 | 年化 | **超额年化** | 最大回撤 | "
            "夏普 | 卡玛 | 胜率 | 盈亏比 | 交易数 | 平均持有 | 换手 |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    def key(kv):
        v = kv[1].get("cagr_pct")
        return -(v if v is not None else -999)
    for cid, r in sorted(rows.items(), key=key):
        if not r.get("ok"):
            out.append(f"| {cid} | - | - | 数据不足 | | | | | | | | | |")
            continue
        c = r["config"]
        out.append(
            f"| {cid} | {c['sell']} | {c.get('hold') or c.get('max_hold')} | "
            f"{r['total_pct']:+.1f}% | {r['cagr_pct']}% | "
            f"**{r.get('excess_cagr_pct')}%** | "
            f"{r['max_dd_pct']}% | {r['sharpe']} | {r['calmar']} | "
            f"{(r['win_rate'] or 0):.1%} | {r['profit_factor']} | "
            f"{r['trades']} | {r['avg_hold_days']} | {r['turnover_x']}x |")
    # 每年收益
    years = sorted({y for r in rows.values() if r.get("ok")
                    for y in (r.get("yearly") or {})})
    if years:
        out += ["", "## 每年收益（%）", "",
                "| 配置 | " + " | ".join(years) + " |",
                "|---" * (len(years) + 1) + "|"]
        for cid, r in sorted(rows.items(), key=key):
            if not r.get("ok"):
                continue
            cells = [(r.get("yearly") or {}).get(y) for y in years]
            out.append(f"| {cid} | " + " | ".join(
                "—" if v is None else f"{v:+.1f}" for v in cells) + " |")
    out.append("")
    return "\n".join(out)


__all__ = ["Config", "run_sweep", "default_configs", "render_markdown",
           "COST_COMMISSION_BP", "COST_STAMP_BP", "COST_SLIP_BP",
           "DAYS_PER_YEAR"]
