# -*- coding: utf-8 -*-
"""
portal/virtual/settle.py — 结算引擎（机械回放）

整套系统的价值都压在这个文件上：**成交由算法判定，不许人工叙述。**

流程
----
    plan（已冻结、已锚定）
        ↓ ① 锚点校验（不一致直接中止）
        ↓ ② 日期护栏（分时日期 != 交易日 直接中止）
        ↓ ③ 开仓前提打分 gate（不达标 → 空仓，盈亏记 0）
        ↓ ④ 逐只按 R0–R4 判竞价成交
        ↓ ⑤ 三把尺子（大盘 / 基准组合 / 影子组合）
        ↓ ⑥ 写 fills + 滚动账本

三把零自由度尺子（缺一个整套系统就失去意义，SKILL §7.2.1）
--------------------------------------------------------
| 尺子 | 成分 | 回答 |
|---|---|---|
| 大盘 | 沪深300 当日 | 有没有跑赢「什么都不做」 |
| 基准组合 | T 日**全部**涨停股，次日开盘等权 | 选股有超越「无脑买昨日涨停」吗 |
| 影子组合 | **自己选的三只**，不做 gate、不做 R 过滤 | 执行规则是加分还是减分 |

口径必须统一到**投入资金**（用总资产当分母去比影子是错的，分母不同没法比）：

    执行贡献 = 实际 − 影子
    选股贡献 = 影子 − 基准

必须主动堵的作弊口
------------------
1. 亏损时用「不成交」出口逃避 → 全部由算法判定，不许人工叙述
2. 参数按结果调优 → params_hash + 外置锚点链
3. 空仓两面讨好 → gate 机械判定 + 空仓也算机会成本
4. 时序泄漏 → 涨停池/龙虎榜/封单额是盘后数据，只能用于选 T+1
5. 已知不可消除的偏差要如实标注（回放用最终开盘价，实操用 9:24 虚拟价）
"""
from __future__ import annotations

import datetime
import json
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import anchor, fill_model, minute_data, paths, plan as plan_mod

DEFAULT_TIMEOUT = 12

_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://gu.qq.com/",
}


class SettleAbort(RuntimeError):
    """结算必须中止（锚点不符 / 日期不符 / 数据缺失）。**不写账本。**"""


# ─────────────────────────────────────────────────────────────
# 数据源（默认走腾讯；测试可注入假数据）
# ─────────────────────────────────────────────────────────────
class Source(Protocol):
    def quote(self, code: str, *, is_index: bool = False) -> dict: ...
    def minute(self, code: str, *, is_index: bool = False) -> dict: ...


@dataclass
class LiveSource:
    """真实数据源：实时行情（qt.gtimg.cn）+ 分时（web.ifzq.gtimg.cn）。"""
    timeout: int = DEFAULT_TIMEOUT

    def quote(self, code: str, *, is_index: bool = False) -> dict:
        sym = minute_data.tencent_symbol(code, is_index=is_index)
        url = f"https://qt.gtimg.cn/q={sym}"
        req = urllib.request.Request(url, headers=_UA)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except Exception as exc:
            raise SettleAbort(f"行情请求失败 {sym}：{exc}") from exc

        text = None
        for enc in ("gbk", "utf-8"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None or "=" not in text:
            raise SettleAbort(f"行情响应无法解析：{sym}")

        payload = text.split("=", 1)[1].strip().strip('";')
        f = payload.split("~")
        if len(f) < 6:
            raise SettleAbort(f"行情字段不足（{len(f)} 个）：{sym}")

        def num(i: int) -> float | None:
            try:
                v = float(f[i])
                return v if v == v else None
            except (ValueError, IndexError):
                return None

        return {
            "code": code, "symbol": sym, "name": f[1],
            "last": num(3), "prev_close": num(4), "open": num(5),
        }

    def minute(self, code: str, *, is_index: bool = False) -> dict:
        return minute_data.fetch_minute(code, is_index=is_index, timeout=self.timeout)


@dataclass
class DictSource:
    """测试用的固定数据源。`quotes` / `minutes` 都是 code → 数据。"""
    quotes: dict[str, dict] = field(default_factory=dict)
    minutes: dict[str, dict] = field(default_factory=dict)

    def quote(self, code: str, *, is_index: bool = False) -> dict:
        return self.quotes.get(str(code), {})

    def minute(self, code: str, *, is_index: bool = False) -> dict:
        d = self.minutes.get(str(code))
        if d is None:
            raise SettleAbort(f"没有 {code} 的分时数据")
        return d


# ─────────────────────────────────────────────────────────────
# 日期护栏
# ─────────────────────────────────────────────────────────────
def guard_dates(plan: dict, source: Source) -> None:
    """分时日期必须等于交易日，否则中止（不写账本）。"""
    want = str(plan["trade_date"]).replace("-", "")
    picks = plan.get("picks") or []
    probe_code = picks[0]["code"] if picks else None
    if not probe_code:
        return
    try:
        probe = source.minute(probe_code)
    except Exception as exc:
        raise SettleAbort(f"日期护栏：取不到 {probe_code} 的分时（{exc}）") from exc
    got = str(probe.get("date") or "")
    if got != want:
        raise SettleAbort(
            f"日期护栏：分时日期是 {got or '(空)'}，交易日是 {want}。"
            f"说明该日行情还没生成（或接口滞后）。空跑会污染账本，故不做任何判定。"
        )


# ─────────────────────────────────────────────────────────────
# 前提打分
# ─────────────────────────────────────────────────────────────
def score_gate(plan: dict, source: Source) -> dict[str, Any]:
    """逐条判 gate。返回 {"passed": n, "total": n, "min_pass": n, "detail": [...]}。"""
    gate = plan.get("open_gate") or {}
    conds = gate.get("conditions") or []
    detail: list[dict[str, Any]] = []
    passed = 0

    # 竞价价：三只票的 open 只取一次，供 B 项复用
    pick_auction: dict[str, float | None] = {}
    for p in plan.get("picks") or []:
        try:
            q = source.quote(p["code"])
        except Exception:
            q = {}
        pick_auction[p["code"]] = q.get("open")

    for c in conds:
        cid = c.get("id")
        ctype = c.get("type")
        ok = False
        desc = c.get("desc", "")

        if ctype == "index_auction":
            code = c.get("code", "000001")
            try:
                q = source.quote(code, is_index=True)
            except Exception:
                q = {}
            open_px, prev = q.get("open"), q.get("prev_close")
            if open_px and prev:
                pct = (open_px / prev - 1) * 100
                ok = pct > float(c.get("threshold_pct", -1.0))
                desc = f"{desc}｜实测 {pct:+.2f}%"
            else:
                desc = f"{desc}｜取不到竞价价"

        elif ctype == "pick_auction_band":
            lo, hi = float(c.get("lo_pct", 0)), float(c.get("hi_pct", 100))
            min_count = int(c.get("min_count", 1))
            cnt = 0
            parts = []
            for p in plan.get("picks") or []:
                open_px = pick_auction.get(p["code"])
                prev = p.get("prev_close")
                if open_px and prev:
                    pct = (open_px / prev - 1) * 100
                    if lo <= pct <= hi:
                        cnt += 1
                    parts.append(f"{p.get('name') or p['code']} {pct:+.2f}%")
                else:
                    parts.append(f"{p.get('name') or p['code']} 无数据")
            ok = cnt >= min_count
            desc = f"{desc}｜落在区间 {cnt} 只（需≥{min_count}）：{' / '.join(parts)}"

        else:  # manual
            ok = bool(c.get("default", True))
            desc = f"{desc}｜人工项，采用默认值 {ok}（必须人工复核并留痕）"

        passed += 1 if ok else 0
        detail.append({"id": cid, "type": ctype, "ok": ok, "desc": desc})

    total = len(conds)
    min_pass = int(gate.get("min_pass", total))
    return {"passed": passed, "total": total, "min_pass": min_pass, "detail": detail,
            "gate_ok": passed >= min_pass}


# ─────────────────────────────────────────────────────────────
# 三把尺子
# ─────────────────────────────────────────────────────────────
def _ret_pct(open_px: float | None, close_px: float | None,
             cost: float) -> float | None:
    if not open_px or not close_px or open_px <= 0:
        return None
    return (close_px / open_px - 1) * 100 - cost * 100


def shadow_ruler(plan: dict, source: Source, cost: float) -> dict:
    """影子组合：**自己选的三只**，不做 gate、不做 R 过滤，一律开盘价买入。

    只排除「开盘即涨停」（物理上买不进），其余全部计入 —— 否则它就不再是零自由度了。
    """
    sealed_pct = float((plan.get("fill_rule") or {}).get("sealed_pct", 9.5))
    rows, sealed = [], []
    for p in plan.get("picks") or []:
        try:
            q = source.quote(p["code"])
        except Exception:
            q = {}
        open_px, last, prev = q.get("open"), q.get("last"), q.get("prev_close")
        if not open_px or not last or not prev:
            rows.append({"code": p["code"], "name": p.get("name"), "status": "nodata"})
            continue
        pct = (open_px / prev - 1) * 100
        if pct >= sealed_pct:
            sealed.append({"code": p["code"], "name": p.get("name"),
                           "status": "sealed", "auction_pct": round(pct, 2)})
            continue
        r = _ret_pct(open_px, last, cost)
        rows.append({"code": p["code"], "name": p.get("name"), "status": "buy",
                     "auction_pct": round(pct, 2), "open": open_px, "close": last,
                     "ret_pct": round(r, 3) if r is not None else None})
    bought = [r for r in rows if r.get("status") == "buy" and r.get("ret_pct") is not None]
    return {
        "detail": rows + sealed,
        "ret_pct": round(sum(r["ret_pct"] for r in bought) / len(bought), 3) if bought else None,
        "n": len(bought),
        "sealed_out": len(sealed),
    }


def baseline_ruler(plan: dict, source: Source, cost: float) -> dict:
    """基准组合：T 日**全部**涨停股，次日开盘等权买，开盘涨停的剔除（买不进）。

    这是「无脑买昨日所有涨停股」的零自由度对照。跑不过它，选股就没有 alpha。
    """
    bl = plan.get("baseline") or {}
    codes = bl.get("codes") or []
    names = {str(d.get("code")): d.get("name") for d in (bl.get("detail") or [])}
    sealed_pct = float((plan.get("fill_rule") or {}).get("sealed_pct", 9.5))
    rows, sealed = [], 0
    for code in codes:
        try:
            q = source.quote(str(code))
        except Exception:
            continue
        open_px, last, prev = q.get("open"), q.get("last"), q.get("prev_close")
        if not open_px or not last or not prev:
            continue
        if (open_px / prev - 1) * 100 >= sealed_pct:
            sealed += 1
            continue
        r = _ret_pct(open_px, last, cost)
        if r is None:
            continue
        rows.append({"code": code, "name": names.get(str(code)) or q.get("name"),
                     "ret_pct": round(r, 3)})
    return {
        "detail": rows,
        "ret_pct": round(sum(r["ret_pct"] for r in rows) / len(rows), 3) if rows else None,
        "n": len(rows),
        "sealed_out": sealed,
    }


def index_ruler(plan: dict, source: Source) -> dict:
    """大盘：沪深300 当日涨跌幅。"""
    try:
        q = source.quote("000300", is_index=True)
    except Exception:
        return {"code": "000300", "ret_pct": None}
    prev, last = q.get("prev_close"), q.get("last")
    r = (last / prev - 1) * 100 if prev and last else None
    return {"code": "000300", "name": "沪深300",
            "ret_pct": round(r, 3) if r is not None else None}


# ─────────────────────────────────────────────────────────────
# 买入日结算
# ─────────────────────────────────────────────────────────────
def settle_buy(plan_id: str, *, source: Source | None = None,
               dry: bool = False, write: bool = True) -> dict[str, Any]:
    """买入日结算：用真实竞价判成交、算当日盈亏与三把尺子。"""
    plan = plan_mod.load(plan_id)
    if plan is None:
        raise SettleAbort(f"找不到 plan {plan_id}")

    ok, msg = anchor.verify(plan_id, plan)
    if not ok:
        raise SettleAbort(
            f"锚点校验未通过，结算中止：{msg}\n"
            f"规则变更后不许原地重算 —— 必须用新 plan_id 开新一轮，否则账本不可比。"
        )

    source = source or LiveSource()
    guard_dates(plan, source)

    gate = score_gate(plan, source)
    fr = plan.get("fill_rule") or {}
    fee = fr.get("fee") or {}
    cost_rt = (fill_model.sell_fee(10_000, fee) + fill_model.buy_fee(10_000, fee)) / 10_000

    per_pick, total_pct = plan_mod.position_for_gate(plan, gate["passed"], gate["total"])

    fills: list[dict[str, Any]] = []
    cash = float(plan.get("initial_asset") or 0.0)
    invested = 0.0

    if not gate["gate_ok"]:
        # 空仓是合法结果，不是失败。仍要算机会成本（影子/基准照算）。
        pass
    else:
        for p in plan.get("picks") or []:
            code = str(p["code"])
            try:
                q = source.quote(code)
            except Exception:
                q = {}
            open_px = q.get("open")
            prev = p.get("prev_close")
            if not open_px or not prev:
                fills.append({"code": code, "name": p.get("name"), "status": "nodata",
                              "rule": "NODATA", "reason": "取不到竞价价"})
                continue
            pct = (open_px / prev - 1) * 100

            # R0 需要竞价成交额：分时的 0930 那行
            auction_amount = None
            try:
                m = source.minute(code)
                rows = m.get("rows") or []
                if rows and rows[0].get("time") == "0930":
                    auction_amount = rows[0].get("amount")
            except Exception:
                auction_amount = None

            target = float(p.get("target_amount") or per_pick or 0)
            verdict = fill_model.judge_auction(
                pct,
                max_chase_pct=float(fr.get("max_chase_pct", 7.0)),
                order_amount=target,
                auction_amount=auction_amount,
                liquidity_max_ratio=float(fr.get("liquidity_max_ratio", 0.05)),
            )
            if not verdict.ok:
                fills.append({"code": code, "name": p.get("name"), "status": "rejected",
                              "rule": verdict.rule, "reason": verdict.reason,
                              "auction_pct": round(pct, 2)})
                continue

            shares = fill_model.round_lots(target, open_px, fee)
            if shares <= 0:
                fills.append({"code": code, "name": p.get("name"), "status": "rejected",
                              "rule": "LOT", "reason": f"目标金额 {target} 不够一手",
                              "auction_pct": round(pct, 2)})
                continue
            amount = round(open_px * shares, 2)
            bfee = round(fill_model.buy_fee(amount, fee), 2)
            if amount + bfee > cash:
                fills.append({"code": code, "name": p.get("name"), "status": "rejected",
                              "rule": "CASH", "reason": "现金不足",
                              "auction_pct": round(pct, 2)})
                continue

            cash -= amount + bfee
            invested += amount + bfee
            close_px = q.get("last")
            mv = round(close_px * shares, 2) if close_px else None
            # 涨跌停价必须随持仓一起落盘：平仓日的 Z1（收盘涨停顺延）与
            # L3（跌停封死 forced_hold）都要读它们。少了这两个字段，
            # 那两条规则永远不会触发 —— 而且是**静默**不触发。
            up, down = plan_mod.limit_prices(prev, code)
            fills.append({
                "code": code, "name": p.get("name"), "status": "filled",
                "rule": "R1", "reason": verdict.reason,
                "auction_pct": round(pct, 2), "fill_price": open_px,
                "shares": shares, "amount": amount, "buy_fee": bfee,
                "close": close_px, "market_value": mv,
                "unrealized": round(mv - amount - bfee, 2) if mv else None,
                "prev_close": prev, "limit_up": up, "limit_down": down,
                "stop": p.get("stop"), "tp1": p.get("tp1"), "tp2": p.get("tp2"),
            })

    filled = [f for f in fills if f.get("status") == "filled"]
    mv_total = sum(f.get("market_value") or 0 for f in filled)
    unrealized = sum(f.get("unrealized") or 0 for f in filled)
    total_asset = round(cash + mv_total, 2)

    rulers = {
        "index": index_ruler(plan, source),
        "shadow": shadow_ruler(plan, source, cost_rt),
        "baseline": baseline_ruler(plan, source, cost_rt),
    }
    actual_pct = round(unrealized / invested * 100, 3) if invested else 0.0
    sh = rulers["shadow"]["ret_pct"]
    bl = rulers["baseline"]["ret_pct"]
    rulers["attribution"] = {
        "actual_pct": actual_pct,
        "shadow_pct": sh,
        "baseline_pct": bl,
        "execution_pp": round(actual_pct - sh, 3) if sh is not None else None,
        "selection_pp": round(sh - bl, 3) if (sh is not None and bl is not None) else None,
        "basis": "统一到「投入资金」口径；用总资产当分母会因分母不同而不可比",
    }

    result = {
        "plan_id": plan_id,
        "trade_date": plan["trade_date"],
        "action": "buy",
        "settled_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "anchored": True,
        "gate": gate,
        "position": {"per_pick": per_pick, "total_pct": total_pct, "invested": round(invested, 2)},
        "fills": fills,
        "cash": round(cash, 2),
        "market_value": round(mv_total, 2),
        "unrealized": round(unrealized, 2),
        "total_asset": total_asset,
        "rulers": rulers,
        "params_version": plan.get("params_version"),
        "params_hash": plan.get("params_hash"),
        "known_bias": plan.get("known_bias") or [],
    }

    if not dry and write:
        _write_fills(plan_id, result, suffix="")
        _append_ledger(plan, result)
    return result


# ─────────────────────────────────────────────────────────────
# 平仓日结算
# ─────────────────────────────────────────────────────────────
def settle_exit(plan_id: str, *, source: Source | None = None,
                dry: bool = False, write: bool = True) -> dict[str, Any]:
    """平仓日结算：按 exit_rule 机械执行（止损层 / 收盘平仓 / Z1 顺延 / forced_hold）。"""
    plan = plan_mod.load(plan_id)
    if plan is None:
        raise SettleAbort(f"找不到 plan {plan_id}")
    ok, msg = anchor.verify(plan_id, plan)
    if not ok:
        raise SettleAbort(f"锚点校验未通过，结算中止：{msg}")

    buy = read_fills(plan_id, suffix="")
    if buy is None:
        raise SettleAbort(f"没有 {plan_id} 的买入日 fills，先跑 settle_buy")

    source = source or LiveSource()
    er = plan.get("exit_rule") or {}
    price_basis = er.get("price_basis", "close")
    use_stop = bool(er.get("use_stop", False))
    z1 = bool(er.get("z1", False))
    fr = plan.get("fill_rule") or {}
    fee = fr.get("fee") or {}

    positions = [f for f in (buy.get("fills") or []) if f.get("status") == "filled"]
    rows: list[dict[str, Any]] = []
    cash_out = 0.0
    realized = 0.0

    for pos in positions:
        code = str(pos["code"])
        shares = int(pos["shares"])
        buy_amount = float(pos["amount"])
        buy_fee_ = float(pos["buy_fee"])
        try:
            q = source.quote(code)
        except Exception:
            q = {}
        last, prev = q.get("last"), q.get("prev_close")

        # 出场价：与回测口径统一（回测 = T+1 收盘）
        exit_px = last
        basis_used = "close"
        forced_hold = False
        if price_basis == "1450":
            try:
                m = source.minute(code)
                px = minute_data.at_time(m.get("rows") or [], "1450")
                if px:
                    exit_px, basis_used = px, "1450"
            except Exception:
                pass

        # 止损层（默认关闭：41,655 笔回测证明机械止损是负期望，见 SKILL §9.7）
        stop = pos.get("stop")
        if use_stop and stop and last is not None and last <= float(stop):
            exit_px, basis_used = float(stop), "stop"
            try:
                m = source.minute(code)
                rows_m = m.get("rows") or []
                open_px = minute_data.at_time(rows_m, "0930")
                if open_px is not None and open_px <= float(stop):
                    exit_px, basis_used = open_px, "stop_gap_open"
            except Exception:
                pass

        # Z1：平仓日收盘涨停 → 不卖，顺延一次
        limit_up = pos.get("limit_up") or pos.get("limit_up_next")
        sealed_today = (last is not None and limit_up is not None
                        and abs(float(last) - float(limit_up)) < 0.005)
        if z1 and sealed_today and basis_used == "close":
            rows.append({
                "code": code, "name": pos.get("name"), "status": "z1_defer",
                "close": last, "limit_up": limit_up, "shares": shares,
                "reason": "收盘涨停，Z1 顺延下一交易日",
            })
            continue

        # L3 卖不出：跌停封死
        limit_down = pos.get("limit_down")
        if last is not None and limit_down is not None and abs(float(last) - float(limit_down)) < 0.005:
            forced_hold = True

        if forced_hold:
            rows.append({
                "code": code, "name": pos.get("name"), "status": "forced_hold",
                "close": last, "shares": shares,
                "reason": "跌停封死，卖不出；账本如实记 forced_hold，下一可交易日开盘一律卖出",
            })
            continue

        if exit_px is None:
            rows.append({"code": code, "name": pos.get("name"), "status": "nodata",
                         "reason": "取不到出场价"})
            continue

        gross = round(exit_px * shares, 2)
        sfee = round(fill_model.sell_fee(gross, fee), 2)
        net = round(gross - sfee, 2)
        pnl = round(net - buy_amount - buy_fee_, 2)
        cash_out += net
        realized += pnl
        rows.append({
            "code": code, "name": pos.get("name"), "status": "closed",
            "basis": basis_used, "exit_price": exit_px, "shares": shares,
            "gross": gross, "sell_fee": sfee, "net": net,
            "pnl": pnl,
            "pnl_pct": round(pnl / (buy_amount + buy_fee_) * 100, 3) if buy_amount else None,
        })

    invested = float((buy.get("position") or {}).get("invested") or 0.0)
    exit_fills = {
        "plan_id": plan_id,
        "trade_date": plan["trade_date"],
        "action": "exit",
        "settled_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "basis": price_basis,
        "rows": rows,
        "realized": round(realized, 2),
        "realized_pct": round(realized / invested * 100, 3) if invested else None,
        "invested": round(invested, 2),
        "close_total_asset": round(float(buy.get("total_asset") or 0) + realized, 2),
        "params_version": plan.get("params_version"),
        "params_hash": plan.get("params_hash"),
    }

    if not dry and write:
        _write_fills(plan_id, exit_fills, suffix="_exit")
    return exit_fills


# ─────────────────────────────────────────────────────────────
# 落盘
# ─────────────────────────────────────────────────────────────
def _write_fills(plan_id: str, payload: dict, *, suffix: str = "") -> None:
    paths.ensure_dirs()
    p = paths.FILLS_DIR / f"{plan_id}{suffix}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def read_fills(plan_id: str, *, suffix: str = "") -> dict | None:
    p = paths.FILLS_DIR / f"{plan_id}{suffix}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _append_ledger(plan: dict, result: dict) -> None:
    """滚动账本：人类可读的追加式流水（**只许追加**）。"""
    paths.ensure_dirs()
    lines = []
    if not paths.LEDGER.exists():
        lines.append("# 虚拟盘账本\n")
        lines.append(f"> 初始本金 {plan.get('initial_asset', 0):,.2f} 元。"
                     f"规则只在**前一晚**改；盘后改动会被锚点校验抓到并中止结算。\n")
        lines.append("| 日期 | 计划 | 动作 | gate | 投入 | 浮动/已实现 | 期末总资产 |")
        lines.append("|---|---|---|---|---|---|---|")
    r = result
    lines.append(
        f"| {r.get('trade_date')} | {r.get('plan_id')} | {r.get('action')} "
        f"| {r.get('gate', {}).get('passed')}/{r.get('gate', {}).get('total')} "
        f"| {r.get('position', {}).get('invested', 0):,.2f} "
        f"| {r.get('unrealized', 0):,.2f} "
        f"| {r.get('total_asset', 0):,.2f} |"
    )
    with paths.LEDGER.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────
def _print_buy(r: dict) -> None:
    g = r["gate"]
    print(f"\n=== 结算 {r['trade_date']} · plan {r['plan_id']} · 买入日 ===")
    print(f"锚点：通过")
    print(f"前提打分：{g['passed']}/{g['total']}（需 ≥{g['min_pass']}）"
          f" → {'建仓' if g['gate_ok'] else '空仓'}")
    for d in g["detail"]:
        print(f"   {'✓' if d['ok'] else '✗'} {d['id']} {d['desc']}")
    print("\n成交明细：")
    for f in r["fills"]:
        if f["status"] == "filled":
            print(f"   买入 {f['name']}({f['code']}) {f['fill_price']} × {f['shares']} 股 "
                  f"= {f['amount']:,.2f}（费 {f['buy_fee']}） 竞价 {f['auction_pct']:+.2f}%")
        else:
            print(f"   {f['status']:8} {f['name']}({f['code']}) [{f['rule']}] {f['reason']}")
    pos = r["position"]
    print(f"\n投入 {pos['invested']:,.2f}（仓位 {pos['total_pct']}%） 现金 {r['cash']:,.2f} "
          f"市值 {r['market_value']:,.2f} 浮动 {r['unrealized']:+,.2f} "
          f"总资产 {r['total_asset']:,.2f}")
    a = r["rulers"]["attribution"]
    print(f"三把尺子（按投入资金）：实际 {a['actual_pct']:+.3f}% / "
          f"影子 {a['shadow_pct']}% / 基准 {a['baseline_pct']}% / "
          f"大盘 {r['rulers']['index']['ret_pct']}%")
    print(f"   执行贡献 {a['execution_pp']}pp   选股贡献 {a['selection_pp']}pp")


def _print_exit(r: dict) -> None:
    print(f"\n=== 结算 {r['trade_date']} · plan {r['plan_id']} · 平仓日"
          f"（出场口径 {r['basis']}）===")
    for row in r["rows"]:
        if row["status"] == "closed":
            print(f"   卖出 {row['name']} {row['exit_price']} × {row['shares']} 股 = "
                  f"{row['gross']:,.2f}（费 {row['sell_fee']}） 盈亏 {row['pnl']:+,.2f}"
                  f"（{row['pnl_pct']}%）")
        else:
            print(f"   {row['status']:12} {row['name']} — {row['reason']}")
    print(f"\n已实现 {r['realized']:+,.2f}（{r['realized_pct']}%） "
          f"期末总资产 {r['close_total_asset']:,.2f}")


def main(argv: list[str] | None = None) -> int:
    """python -m portal.virtual.settle <plan_id> [--exit] [--dry]"""
    import sys

    args = argv if argv is not None else sys.argv[1:]
    flags = {a for a in args if a.startswith("--")}
    pos = [a for a in args if not a.startswith("--")]
    if not pos:
        print(__doc__)
        print("\n用法：python -m portal.virtual.settle <plan_id> [--exit] [--dry]")
        return 2

    plan_id = pos[0]
    dry = "--dry" in flags
    try:
        if "--exit" in flags:
            _print_exit(settle_exit(plan_id, dry=dry))
        else:
            _print_buy(settle_buy(plan_id, dry=dry))
    except SettleAbort as exc:
        print(f"\n!! 结算中止：{exc}")
        print("!! 本次没有写入任何账本。")
        return 1
    if dry:
        print("\n（--dry：已打印判定过程，未写入账本）")
    return 0


__all__ = [
    "SettleAbort",
    "Source",
    "LiveSource",
    "DictSource",
    "guard_dates",
    "score_gate",
    "shadow_ruler",
    "baseline_ruler",
    "index_ruler",
    "settle_buy",
    "settle_exit",
    "read_fills",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
