# -*- coding: utf-8 -*-
"""
portal/virtual/selfcheck_p2.py — P2 结算闭环自检（全部离线）

覆盖 T1–T12：

    T1  计划 schema         —— 4 只票 / 止盈超涨停价 / 止损不低于昨收 / 缺 exit 开关
    T2  params_hash         —— 稳定性 + 参数一变就变
    T3  冻结 + 锚点         —— 先落盘再锚定；改动后结算必须中止
    T4  费用模型            —— 佣金最低 5 元的抬升、印花税只在卖出
    T5  股数取整            —— 100 股整数倍
    T6  竞价判定 R0–R4      —— 边界 0% / 7% / 9.5%，R0 流动性
    T7  排队撮合            —— 一字板永远轮不到；放量则成交
    T8  前提打分 gate       —— A/B/C 三项与 min_pass
    T9  买入结算            —— gate 失败必须空仓；成交时现金/市值/费用恒等式
    T10 平仓结算            —— 收盘平仓 / Z1 顺延 / 跌停 forced_hold
    T11 锚点篡改            —— 改一个空格后 settle 必须抛 SettleAbort
    T12 日期护栏            —— 分时日期不符必须中止，不写账本

运行：由 `python -m portal.virtual.selfcheck` 统一调用；
也可单独跑 `python -m portal.virtual.selfcheck_p2`。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault(
    "PORTAL_VIRTUAL_ROOT",
    str(Path(tempfile.mkdtemp(prefix="portal_virtual_p2_")) / "virtual"),
)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from portal.virtual import fill_model, paths, plan as plan_mod, settle  # noqa: E402
from portal.virtual._check import Check, close_enough  # noqa: E402

FEE = {
    "commission_rate": 0.00025,
    "commission_min": 5.0,
    "stamp_rate": 0.0005,
    "transfer_rate": 0.00001,
    "lot_size": 100,
}


# ─────────────────────────────────────────────────────────────
# 构造工具
# ─────────────────────────────────────────────────────────────
def _pick(code: str, name: str, prev: float, target: float = 10000.0,
          **extra) -> dict:
    up, down = plan_mod.limit_prices(prev, code)
    p = {"code": code, "name": name, "prev_close": prev,
         "limit_up": up, "limit_down": down, "target_amount": target,
         "reason": "测试用"}
    p.update(extra)
    return p


def _plan(plan_id: str = "TEST1", *, picks=None, min_pass: int = 2,
          trade_date: str = "20261008", tiers=None, exit_rule=None,
          baseline_codes=None) -> dict:
    picks = picks if picks is not None else [_pick("600001", "甲", 10.0),
                                             _pick("600002", "乙", 10.0)]
    return plan_mod.build_plan(
        plan_id=plan_id,
        trade_date=trade_date,
        data_date="20261007",
        initial_asset=100000.0,
        picks=picks,
        open_gate={
            "min_pass": min_pass,
            "conditions": [
                {"id": "A", "type": "index_auction", "code": "000300",
                 "threshold_pct": -1.0, "desc": "沪深300 竞价 > -1%"},
                {"id": "B", "type": "pick_auction_band", "min_count": 2,
                 "lo_pct": 1.0, "hi_pct": 7.0, "desc": "主选 ≥2 只落在 +1%~+7%"},
                {"id": "C", "type": "manual", "default": True, "desc": "外围无新增利空"},
            ],
        },
        fill_rule={"fill_method": "集合竞价买入", "max_chase_pct": 7.0,
                   "sealed_pct": 9.5, "liquidity_max_ratio": 0.05, "fee": dict(FEE)},
        exit_rule=exit_rule or {"price_basis": "close", "use_stop": False, "z1": False},
        position_sizing={"tiers": tiers or [
            {"gate_pass": "3/3", "per_pick": 10000, "total_pct": 30.0},
            {"gate_pass": "2/3", "per_pick": 7000, "total_pct": 21.0},
        ]},
        baseline={"codes": baseline_codes or [], "detail": []},
    )


def _minute(code: str, date: str = "20261008", price: float = 10.0) -> dict:
    return {"code": code, "symbol": "sh" + code, "date": date, "rows": [
        {"time": "0930", "price": price, "volume": 1000.0, "amount": 5_000_000.0},
        {"time": "1000", "price": price, "volume": 1200.0, "amount": 6_000_000.0},
        {"time": "1450", "price": price, "volume": 1500.0, "amount": 7_500_000.0},
        {"time": "1500", "price": price, "volume": 1600.0, "amount": 8_000_000.0},
    ]}


def _source(quotes: dict, date: str = "20261008") -> settle.DictSource:
    minutes = {c: _minute(c, date, q.get("open") or 10.0) for c, q in quotes.items()}
    return settle.DictSource(quotes=quotes, minutes=minutes)


def _clean(plan_id: str) -> None:
    for p in (paths.plan_path(plan_id),
              paths.FILLS_DIR / f"{plan_id}.json",
              paths.FILLS_DIR / f"{plan_id}_exit.json"):
        if p.exists():
            p.unlink()
    d = __import__("portal.virtual.anchor", fromlist=["load", "save"]).load()
    if plan_id in d.get("records", {}):
        d["records"].pop(plan_id)
        if plan_id in d["order"]:
            d["order"].remove(plan_id)
        d["head"] = d["records"][d["order"][-1]]["chain_sha256"] if d["order"] else "0" * 64
        __import__("portal.virtual.anchor", fromlist=["save"]).save(d)


# ─────────────────────────────────────────────────────────────
# T1 · 计划 schema
# ─────────────────────────────────────────────────────────────
def check_plan_schema() -> Check:
    c = Check("T1", "计划 schema：最多 3 只 / 止盈不超涨停 / 止损不低于昨收 / exit 开关写全")

    good = _plan("T1-OK")
    c.expect(plan_mod.validate(good) == [], "正常计划零问题")

    four = _plan("T1-4")
    four["picks"] = four["picks"] + [_pick("600003", "丙", 10.0), _pick("600004", "丁", 10.0)]
    c.expect(any("最多 3 只" in p for p in plan_mod.validate(four)), "超过 3 只被拒")

    # 止盈档高于涨停价 → 当天不可能成交
    bad_tp = _plan("T1-TP")
    bad_tp["picks"][0]["tp1"] = bad_tp["picks"][0]["limit_up"] + 1.0
    c.expect(any("高于涨停价" in p for p in plan_mod.validate(bad_tp)),
             "止盈档高于涨停价被拒（否则止损止盈是幻觉）")

    # 止损位 >= 昨收 → 买入即触发
    bad_stop = _plan("T1-STOP")
    bad_stop["picks"][0]["stop"] = bad_stop["picks"][0]["prev_close"]
    c.expect(any("买入即触发止损" in p for p in plan_mod.validate(bad_stop)),
             "止损位不低于昨收被拒")

    # 涨停价算错 → 后面所有档位都失效
    bad_lu = _plan("T1-LU")
    bad_lu["picks"][0]["limit_up"] = 11.9          # 10.0 主板应为 11.0
    c.expect(any("与 昨收×幅度" in p for p in plan_mod.validate(bad_lu)),
             "涨停价与板块幅度不符被拒")

    # exit_rule 缺开关：默认值不是中性选项，它是隐形的规则
    partial = dict(good)
    partial["exit_rule"] = {"z1": False}
    c.expect(any("exit_rule 缺少显式开关" in p for p in plan_mod.validate(partial)),
             "exit_rule 缺 price_basis/use_stop 被拒（不许依赖默认值）")

    # 空 gate 不许：必须有空仓出口
    no_gate = dict(good)
    no_gate["open_gate"] = {"conditions": []}
    c.expect(any("空仓出口" in p for p in plan_mod.validate(no_gate)),
             "缺少 gate 条件被拒（空仓出口必须显式）")

    # 手工项必须声明 default
    mg = dict(good)
    mg["open_gate"] = {"min_pass": 1, "conditions": [{"id": "C", "type": "manual"}]}
    c.expect(any("未声明 default" in p for p in plan_mod.validate(mg)),
             "人工项未声明 default 被拒（静默默认通过等于没有过滤）")
    return c


# ─────────────────────────────────────────────────────────────
# T2 · params_hash
# ─────────────────────────────────────────────────────────────
def check_params_hash() -> Check:
    c = Check("T2", "params_hash：同参数稳定、改参数必变")

    a = _plan("T2-A")
    b = _plan("T2-A")                     # 同 plan_id 同参数
    c.expect(a["params_hash"] == b["params_hash"], "同参数两次构造 hash 相同")

    changed = _plan("T2-A")
    changed["fill_rule"]["max_chase_pct"] = 6.0
    c.expect(plan_mod.params_hash(changed) != a["params_hash"],
             "改 fill_rule 一个参数 → hash 变")

    changed2 = _plan("T2-A")
    changed2["picks"][0]["target_amount"] = 9000
    c.expect(plan_mod.params_hash(changed2) != a["params_hash"],
             "改仓位 → hash 变")

    # hash 不覆盖 plan_id / 时间戳（那属于元数据），但**也不构成防作弊**
    c.expect("sha256" not in a["params_hash"].lower() and len(a["params_hash"]) == 16,
             "hash 是 16 位前缀")
    c.expect("自我认证" in (plan_mod.params_hash.__doc__ or ""),
             "docstring 里写明它只是自我认证，防作弊靠外置锚点链")
    return c


# ─────────────────────────────────────────────────────────────
# T3 · 费用模型
# ─────────────────────────────────────────────────────────────
def check_fees() -> Check:
    c = Check("T3", "费用模型：佣金最低 5 元的抬升、印花税只在卖出")

    small = 1_000.0
    bf = fill_model.buy_fee(small, FEE)
    c.expect(close_enough(bf, 5.0 + 0.01, 1e-6),
             f"1,000 元买入佣金被抬到 5 元（实际 {bf:.4f}）；按万2.5 只有 0.25 元")
    c.expect(bf / small > 0.005, f"实际费率 {bf/small:.3%} 远高于万2.5 —— 分批越碎越贵")

    big = 100_000.0
    c.expect(close_enough(fill_model.buy_fee(big, FEE), 25.0 + 1.0, 1e-6),
             "10 万元买入按万2.5 计算（25 元 + 过户费 1 元）")

    sell_small = fill_model.sell_fee(small, FEE)
    c.expect(close_enough(sell_small, 5.0 + 0.01 + 0.5, 1e-6),
             f"卖出多收印花税 0.05%（实际 {sell_small:.4f}）")
    c.expect(sell_small > bf, "卖出费用恒大于买入（印花税只在卖出）")

    c.expect(fill_model.buy_fee(0, FEE) == 5.0, "0 元也收最低 5 元（不豁免）")
    return c


# ─────────────────────────────────────────────────────────────
# T4 · 股数取整
# ─────────────────────────────────────────────────────────────
def check_lots() -> Check:
    c = Check("T4", "股数取整：100 股整数倍，向下取整")
    c.expect(fill_model.round_lots(10000, 10.2, FEE) == 900, "10000/10.2=980.4 → 900 股")
    c.expect(fill_model.round_lots(10000, 10.0, FEE) == 1000, "整除时为整千")
    c.expect(fill_model.round_lots(500, 10.0, FEE) == 0, "500 元不够一手 → 0 股")
    c.expect(fill_model.round_lots(10000, 0, FEE) == 0, "价格为 0 → 0 股（不除零）")
    c.expect(fill_model.round_lots(10000, None, FEE) == 0, "价格缺失 → 0 股")
    return c


# ─────────────────────────────────────────────────────────────
# T5 · 竞价判定 R0–R4
# ─────────────────────────────────────────────────────────────
def check_auction_rules() -> Check:
    c = Check("T5", "竞价判定 R0–R4：边界与顺序")

    def j(pct, **kw):
        return fill_model.judge_auction(pct, **kw)

    c.expect(j(3.0).ok and j(3.0).rule == "R1", "3% → R1 成交")
    c.expect(j(7.0).ok, "7.0% 恰好上界 → 成交（区间是闭的）")
    c.expect(not j(7.01).ok and j(7.01).rule == "R4", "7.01% → R4 不追高")
    c.expect(not j(0.0).ok and j(0.0).rule == "R2", "0% → R2 承接失败")
    c.expect(not j(-1.5).ok and j(-1.5).rule == "R2", "-1.5% → R2")
    c.expect(not j(9.5).ok and j(9.5).rule == "R3", "9.5% → R3 排不上队")
    c.expect(not j(9.49).ok and j(9.49).rule == "R4", "9.49% 仍 >7% → R4")
    c.expect(not j(None).ok and j(None).rule == "NODATA", "缺数据 → NODATA（不猜）")

    # R0 优先级最高：即使涨幅在区间内，流动性不够也不成交
    r0 = j(3.0, order_amount=600_000, auction_amount=1_000_000)
    c.expect(not r0.ok and r0.rule == "R0", f"委托占竞价成交额 60% → R0：{r0.reason}")
    ok0 = j(3.0, order_amount=10_000, auction_amount=1_000_000)
    c.expect(ok0.ok, "委托占 1% → 仍可成交")
    return c


# ─────────────────────────────────────────────────────────────
# T6 · 排队撮合
# ─────────────────────────────────────────────────────────────
def check_queue_fill() -> Check:
    c = Check("T6", "排队撮合：一字板永远轮不到，放量则成交")

    # 一字板：挂单后累计成交量不再增长 → 永远不成交
    flat = [("0930", 11.0, 5000.0), ("1000", 11.0, 5000.0), ("1500", 11.0, 5000.0)]
    o1 = fill_model.simulate_limit_up_queue(
        code="600001", name="甲", limit_price=11.0, shares=900, tape=flat, queue_pos=800)
    c.expect(not o1.filled, "一字板封死（之后无新增成交量）→ 不成交")

    # 炸板放量：新增成交量超过排队量 → 成交
    surge = [("0930", 11.0, 5000.0), ("1000", 10.9, 5100.0),
             ("1300", 11.0, 6100.0), ("1450", 11.0, 7000.0)]
    o2 = fill_model.simulate_limit_up_queue(
        code="600002", name="乙", limit_price=11.0, shares=900, tape=surge, queue_pos=800)
    c.expect(o2.filled, f"炸板放量 1100 手 > 排队 800 手 → 成交（fill_at={o2.fill_at}）")
    c.expect(o2.fill_at == "1300", f"成交时刻是量够的那一刻，不是最后一根：{o2.fill_at}")

    # 边界：新增量恰好等于排队量 → 成交（>=）
    exact = [("0930", 11.0, 1000.0), ("1000", 11.0, 1800.0)]
    o3 = fill_model.simulate_limit_up_queue(
        code="600003", name="丙", limit_price=11.0, shares=100, tape=exact, queue_pos=800)
    c.expect(o3.filled, "新增量恰好等于排队位置 → 成交（条件用 >=）")

    # 不足：新增 700 < 800 → 不成交
    short = [("0930", 11.0, 1000.0), ("1000", 11.0, 1700.0)]
    o4 = fill_model.simulate_limit_up_queue(
        code="600004", name="丁", limit_price=11.0, shares=100, tape=short, queue_pos=800)
    c.expect(not o4.filled, "新增 700 < 排队 800 → 不成交")
    return c


# ─────────────────────────────────────────────────────────────
# T7 · 前提打分
# ─────────────────────────────────────────────────────────────
def check_gate() -> Check:
    c = Check("T7", "前提打分：A/B/C 三项阈值与 min_pass")

    p = _plan("T7", min_pass=2)
    src = _source({
        "000300": {"open": 4000.0, "last": 4040.0, "prev_close": 4000.0},   # 0% > -1%
        "600001": {"open": 10.2, "last": 10.4, "prev_close": 10.0},          # +2%
        "600002": {"open": 10.5, "last": 10.3, "prev_close": 10.0},          # +5%
    })
    g = settle.score_gate(p, src)
    c.expect(g["passed"] == 3 and g["gate_ok"], f"3/3 通过（实际 {g['passed']}）")
    c.expect(all(d["id"] in ("A", "B", "C") for d in g["detail"]), "三项都有明细")
    c.expect(any("实测" in d["desc"] for d in g["detail"]), "明细里带实测数值，可回溯")

    # 指数竞价跌破 -1% → A 失败
    src2 = _source({
        "000300": {"open": 3950.0, "last": 3960.0, "prev_close": 4000.0},   # -1.25%
        "600001": {"open": 10.2, "last": 10.4, "prev_close": 10.0},
        "600002": {"open": 10.5, "last": 10.3, "prev_close": 10.0},
    })
    g2 = settle.score_gate(p, src2)
    c.expect(g2["passed"] == 2, f"指数 -1.25% → A 失败，3 变 {g2['passed']}")
    c.expect(g2["gate_ok"], "min_pass=2 仍达标")

    # 两只都低开 → B 失败；再破指数 → 只剩 C，min_pass=3 时必须空仓
    p3 = _plan("T7-3", min_pass=3)
    src3 = _source({
        "000300": {"open": 3950.0, "last": 3960.0, "prev_close": 4000.0},
        "600001": {"open": 9.9, "last": 9.8, "prev_close": 10.0},    # -1%
        "600002": {"open": 9.8, "last": 9.7, "prev_close": 10.0},    # -2%
    })
    g3 = settle.score_gate(p3, src3)
    c.expect(g3["passed"] == 1, f"只有手动项通过（实际 {g3['passed']}/3）")
    c.expect(not g3["gate_ok"], "min_pass=3 时 gate 不达标 → 必须空仓")

    # 缺失行情：不许当通过
    src4 = settle.DictSource(quotes={}, minutes={})
    g4 = settle.score_gate(p, src4)
    c.expect(not g4["gate_ok"], f"全部取不到数据 → 不达标（实际 {g4['passed']}/3，不猜）")
    return c


# ─────────────────────────────────────────────────────────────
# T8 · 买入结算
# ─────────────────────────────────────────────────────────────
def check_settle_buy() -> Check:
    c = Check("T8", "买入结算：gate 失败必须空仓；成交时现金/市值/费用恒等式")
    _clean("T8-OK")
    _clean("T8-EMPTY")

    p = _plan("T8-OK", min_pass=2)
    plan_mod.freeze(p)
    src = _source({
        "000300": {"open": 4000.0, "last": 4040.0, "prev_close": 4000.0},
        "600001": {"open": 10.2, "last": 10.4, "prev_close": 10.0},
        "600002": {"open": 10.5, "last": 10.3, "prev_close": 10.0},
        "600003": {"open": 10.1, "last": 10.0, "prev_close": 10.0},
        "600004": {"open": 10.0, "last": 10.0, "prev_close": 10.0},
    })
    r = settle.settle_buy("T8-OK", source=src, write=False)

    filled = [f for f in r["fills"] if f["status"] == "filled"]
    c.expect(len(filled) == 2, f"两只竞价落在区间 → 成交 2 笔（实际 {len(filled)}）")
    c.expect(filled[0]["shares"] == 900, f"10000/10.2 → 900 股（实际 {filled[0]['shares']}）")
    c.expect(close_enough(filled[0]["buy_fee"], 5.09, 0.01),
             f"买入费用 5.09（佣金被最低 5 元抬升）：{filled[0]['buy_fee']}")

    invested = sum(f["amount"] + f["buy_fee"] for f in filled)
    mv = sum(f["market_value"] for f in filled)
    c.expect(close_enough(r["position"]["invested"], round(invested, 2), 0.01),
             "投入 = Σ(成交额+买入费)")
    c.expect(close_enough(r["total_asset"], round(r["cash"] + mv, 2), 0.01),
             f"总资产 = 现金 {r['cash']} + 市值 {mv}")
    c.expect(close_enough(r["unrealized"],
                          round(sum(f["unrealized"] for f in filled), 2), 0.01),
             "浮动盈亏 = Σ(市值−成交额−买入费)")

    ru = r["rulers"]
    c.expect(ru["index"]["ret_pct"] is not None, f"大盘尺子：沪深300 {ru['index']['ret_pct']}%")
    c.expect(ru["shadow"]["ret_pct"] is not None, f"影子尺子：{ru['shadow']['ret_pct']}%"
             f"（n={ru['shadow']['n']}）")
    c.expect(ru["attribution"]["execution_pp"] is not None, "执行贡献可算")
    c.expect("投入资金" in ru["attribution"]["basis"], "口径声明写明按投入资金计算")

    # gate 不达标 → 空仓（合法结果），但三把尺子照算（机会成本）
    p2 = _plan("T8-EMPTY", min_pass=3)
    plan_mod.freeze(p2)
    src2 = _source({
        "000300": {"open": 3950.0, "last": 3960.0, "prev_close": 4000.0},
        "600001": {"open": 9.9, "last": 9.8, "prev_close": 10.0},
        "600002": {"open": 9.8, "last": 9.7, "prev_close": 10.0},
    })
    r2 = settle.settle_buy("T8-EMPTY", source=src2, write=False)
    c.expect(not r2["gate"]["gate_ok"], "gate 不达标")
    c.expect(r2["position"]["invested"] == 0 and r2["cash"] == 100000.0,
             "空仓：投入 0、现金不变")
    c.expect(r2["total_asset"] == 100000.0, "空仓总资产不变（不追责、不补票）")
    c.expect(r2["rulers"]["shadow"]["n"] > 0, "空仓仍算影子组合（机会成本要记账）")

    # R2 拒绝路径必须留下理由，不许静默跳过
    p3 = _plan("T8-EMPTY", min_pass=1)
    _clean("T8-EMPTY")
    plan_mod.freeze(p3)
    r3 = settle.settle_buy("T8-EMPTY", source=src2, write=False)
    rej = [f for f in r3["fills"] if f["status"] == "rejected"]
    c.expect(len(rej) == 2 and all(f["rule"] == "R2" for f in rej),
             f"两只低开都被 R2 拒绝并记录理由（实际 {[f['rule'] for f in rej]}）")
    return c


# ─────────────────────────────────────────────────────────────
# T9 · 平仓结算
# ─────────────────────────────────────────────────────────────
def check_settle_exit() -> Check:
    c = Check("T9", "平仓结算：收盘价平仓 / Z1 顺延 / 跌停 forced_hold")

    # 先造一笔买入
    _clean("T9-A")
    p = _plan("T9-A", min_pass=1)
    plan_mod.freeze(p)
    buy_src = _source({
        "000300": {"open": 4000.0, "last": 4040.0, "prev_close": 4000.0},
        "600001": {"open": 10.2, "last": 10.4, "prev_close": 10.0},
        "600002": {"open": 10.5, "last": 10.3, "prev_close": 10.0},
    })
    settle.settle_buy("T9-A", source=buy_src, write=True)
    buy = settle.read_fills("T9-A")
    c.expect(buy is not None and buy["fills"], "买入 fills 已落盘")
    pos0 = next(f for f in buy["fills"] if f["status"] == "filled")
    c.expect("limit_up" in pos0 and "limit_down" in pos0,
             "持仓里带 limit_up/limit_down（否则 Z1 与 L3 永远静默不触发）")

    # 收盘平仓
    exit_src = _source({
        "600001": {"open": 10.2, "last": 11.0, "prev_close": 10.5},
        "600002": {"open": 10.5, "last": 11.0, "prev_close": 10.5},
    })
    r = settle.settle_exit("T9-A", source=exit_src, write=False)
    closed = [x for x in r["rows"] if x["status"] == "closed"]
    c.expect(len(closed) == len([f for f in buy["fills"] if f["status"] == "filled"]),
             f"全部平仓（{len(closed)} 笔）")
    if closed:
        row = closed[0]
        gross = round(row["exit_price"] * row["shares"], 2)
        c.expect(close_enough(row["gross"], gross, 0.01), "卖出金额 = 价 × 股数")
        c.expect(close_enough(row["net"], round(gross - row["sell_fee"], 2), 0.01),
                 "净额 = 金额 − 卖出费")
        c.expect(row["basis"] == "close", "出场价口径 = 收盘（与回测统一）")
    c.expect(r["realized"] != 0, f"已实现盈亏非零：{r['realized']}")

    # Z1：收盘涨停 → 不卖，顺延
    _clean("T9-Z1")
    pz = _plan("T9-Z1", min_pass=1,
               exit_rule={"price_basis": "close", "use_stop": False, "z1": True})
    plan_mod.freeze(pz)
    settle.settle_buy("T9-Z1", source=buy_src, write=True)
    zbuy = settle.read_fills("T9-Z1")
    zpos = next(f for f in zbuy["fills"] if f["status"] == "filled")
    lu = zpos["limit_up"]
    zsrc = _source({
        "600001": {"open": 10.2, "last": lu, "prev_close": 10.5},
        "600002": {"open": 10.5, "last": lu, "prev_close": 10.5},
    })
    rz = settle.settle_exit("T9-Z1", source=zsrc, write=False)
    c.expect(any(x["status"] == "z1_defer" for x in rz["rows"]),
             f"收盘价 == 涨停价 {lu} → Z1 顺延（z1=True）")

    # z1=False 时同一情形必须平仓（证明开关真的在起作用）
    _clean("T9-NOZ")
    pn = _plan("T9-NOZ", min_pass=1,
               exit_rule={"price_basis": "close", "use_stop": False, "z1": False})
    plan_mod.freeze(pn)
    settle.settle_buy("T9-NOZ", source=buy_src, write=True)
    rn = settle.settle_exit("T9-NOZ", source=zsrc, write=False)
    c.expect(not any(x["status"] == "z1_defer" for x in rn["rows"]),
             "z1=False 时同情形不平仓顺延 → 开关有效")

    # 跌停封死 → forced_hold
    _clean("T9-FH")
    pfh = _plan("T9-FH", min_pass=1)
    plan_mod.freeze(pfh)
    settle.settle_buy("T9-FH", source=buy_src, write=True)
    fbuy = settle.read_fills("T9-FH")
    fpos = next(f for f in fbuy["fills"] if f["status"] == "filled")
    ld = fpos["limit_down"]
    fsrc = _source({
        "600001": {"open": 10.2, "last": ld, "prev_close": 10.5},
        "600002": {"open": 10.5, "last": ld, "prev_close": 10.5},
    })
    rf = settle.settle_exit("T9-FH", source=fsrc, write=False)
    c.expect(any(x["status"] == "forced_hold" for x in rf["rows"]),
             f"收盘价 == 跌停价 {ld} → 记 forced_hold，不许假装卖掉了")
    return c


# ─────────────────────────────────────────────────────────────
# T10 · 锚点篡改 / 日期护栏
# ─────────────────────────────────────────────────────────────
def check_settle_guards() -> Check:
    c = Check("T10", "结算护栏：锚点被改 / 日期不符 → 必须中止，不写账本")

    # 锚点篡改：改一个空格
    _clean("T10-A")
    p = _plan("T10-A", min_pass=1)
    plan_mod.freeze(p)
    src = _source({
        "000300": {"open": 4000.0, "last": 4040.0, "prev_close": 4000.0},
        "600001": {"open": 10.2, "last": 10.4, "prev_close": 10.0},
        "600002": {"open": 10.5, "last": 10.3, "prev_close": 10.0},
    })
    pf = paths.plan_path("T10-A")
    original = pf.read_text(encoding="utf-8")
    pf.write_text(original.replace('"测试用"', '"测试用" '), encoding="utf-8")
    c.raises(settle.SettleAbort,
             lambda: settle.settle_buy("T10-A", source=src, write=False),
             "plan 被加了一个空格 → settle 抛 SettleAbort")
    c.expect(settle.read_fills("T10-A") is None, "中止后没有写出 fills（账本未被污染）")
    pf.write_text(original, encoding="utf-8")

    # 日期护栏
    _clean("T10-B")
    p2 = _plan("T10-B", min_pass=1, trade_date="20261008")
    plan_mod.freeze(p2)
    stale = _source({
        "000300": {"open": 4000.0, "last": 4040.0, "prev_close": 4000.0},
        "600001": {"open": 10.2, "last": 10.4, "prev_close": 10.0},
        "600002": {"open": 10.5, "last": 10.3, "prev_close": 10.0},
    }, date="20261009")                     # 分时日期错了一天
    c.raises(settle.SettleAbort,
             lambda: settle.settle_buy("T10-B", source=stale, write=False),
             "分时日期 != 交易日 → 抛 SettleAbort（不拿前一天数据污染账本）")

    # 缺失 plan
    c.raises(settle.SettleAbort,
             lambda: settle.settle_buy("NOPE", source=src, write=False),
             "plan 不存在 → 中止")
    return c


CHECKS = (
    ("T1", check_plan_schema),
    ("T2", check_params_hash),
    ("T3", check_fees),
    ("T4", check_lots),
    ("T5", check_auction_rules),
    ("T6", check_queue_fill),
    ("T7", check_gate),
    ("T8", check_settle_buy),
    ("T9", check_settle_exit),
    ("T10", check_settle_guards),
)


def run() -> dict:
    results = []
    for _, fn in CHECKS:
        try:
            results.append(fn())
        except Exception as exc:
            import traceback
            chk = Check("ERR", f"{fn.__name__} 执行异常")
            chk.expect(False, f"{type(exc).__name__}: {exc}")
            chk.note(traceback.format_exc().splitlines()[-1])
            results.append(chk)
    passed = all(c.passed for c in results)
    return {"passed": passed, "n_checks": len(results),
            "n_failed": sum(1 for c in results if not c.passed),
            "checks": [c.to_dict() for c in results]}


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    report = run()
    if "--json" in argv:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["passed"] else 1
    print("=" * 72)
    print("portal.virtual 自检 · P2 结算闭环")
    print(f"数据根（隔离）：{paths.VIRTUAL_ROOT}")
    print("=" * 72)
    for chk in report["checks"]:
        print(f"\n[{'PASS' if chk['passed'] else 'FAIL'}] {chk['id']} · {chk['title']}")
        for line in chk["detail"]:
            print(line)
    print("\n" + "=" * 72)
    print(f"结果：{report['n_checks'] - report['n_failed']}/{report['n_checks']} 通过")
    print("=" * 72)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
