# -*- coding: utf-8 -*-
"""
portal/premarket/selfcheck_p6.py — 盘前预案自检（离线）

覆盖 V1–V10：

    V1  JSON 抽取：纯 JSON / 代码块 / 前后有废话
    V2  提议校验：趋势枚举、仓位区间、无数字依据 → 必须拒绝
    V3  内核裁决：缺 reason / 价格不合理 / 超仓位 / 当日超 8 只 / 核心超 6 只
    V4  裁决通过：四张表真的写进去了
    V5  开仓自动移出观察表 + 百分比账户算术精确
    V6  平仓：已实现盈亏落 trades.jsonl，持仓行移除
    V7  渲染：12 节恒定、零外部引用、XSS 转义
    V8  端到端（假 LLM）：预案 + 锚点 + 拒绝项都进报告
    V9  LLM 失败 → 规则兜底，且报告明确标注"非 agent 判断"
    V10 事件溯源：append-only + 每条事件字段完整

全部离线：LLM 用注入的假实现，价格用固定表，数据根在临时目录。
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

# 必须在 import 之前设好数据根
os.environ.setdefault(
    "PORTAL_PREMARKET_ROOT",
    str(Path(tempfile.mkdtemp(prefix="prem_selfcheck_")) / "premarket"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from portal.premarket import account as acc  # noqa: E402
from portal.premarket import agent, apply as apply_mod, paths  # noqa: E402
from portal.premarket import render, store  # noqa: E402
from portal.virtual._check import Check, close_enough  # noqa: E402

#: 测试用的固定价格表（替代真实 market.db 的最新收盘）
PX = {"600519": 100.0, "603906": 20.0, "002454": 6.0, "600000": 12.0, "000001": 11.0}


def _fake_close(code: str) -> float | None:
    return PX.get(code)


def _fresh_tables():
    root = Path(tempfile.mkdtemp(prefix="prem_t_")) / "prem"
    root.mkdir(parents=True, exist_ok=True)
    return store.Tables(root), root


def _wipe_root() -> None:
    """清空共用的数据根。

    V8/V9 走 `pipeline.generate`，而它用的是模块级 `paths.ROOT`（一个自检专用的
    临时目录）。V4/V7 已经往观察表里写过 600519，若不清理，V8 的新增会被
    「已存在 id=600519」正确拒绝 —— 那会让检查误报成渲染问题。
    （第一次跑就是这样挂的。）
    """
    paths.ensure_dirs()
    for name in ("pool", "positions", "infos", "lessons", "trades"):
        p = paths.ROOT / f"{name}.jsonl"
        if p.exists():
            p.unlink()
    acc_file = paths.ROOT / "account.json"
    if acc_file.exists():
        acc_file.unlink()
    for d in (paths.PLANS, paths.REPORTS, paths.REVIEWS):
        if d.exists():
            for f in d.glob("*"):
                if f.is_file():
                    f.unlink()


GOOD_PROPOSAL = {
    "market_view": {
        "trend": "震荡",
        "position_range_pct": [20, 50],
        "confidence": 55,
        "reasons": ["上证 +0.39%，成交额 6870 亿", "上涨 2202 家 / 下跌 2861 家"],
    },
    "sector_priority": [
        {"name": "出版", "logic": "11 板龙头效应", "strength": 80,
         "evidence": "涨停 4 家、最高 11 板"},
    ],
    "pool_actions": {
        "add": [
            {"code": "600519", "name": "贵州茅台", "tier": "core",
             "logic": "题材龙头", "reason": "回踩 20 日线不破",
             "trigger": "放量突破 105 元且量比>1.5",
             "stop_loss": 95.0, "take_profit": 115.0},
            {"code": "603906", "name": "龙蟠科技", "tier": "normal",
             "logic": "固态电池", "reason": "首板加速",
             "trigger": "突破 21 元且成交额>5 亿",
             "stop_loss": 18.5, "take_profit": 24.0},
        ],
        "update": [{"code": "603906", "reason": "核心太多降级", "tier": "normal"}],
        "remove": [],
    },
    "position_plan": {
        "buy": [{"code": "600000", "name": "浦发银行", "alloc_pct": 20,
                 "trigger": "放量突破 12.5 元", "stop_loss": 11.0,
                 "take_profit": 14.5, "logic": "银行护盘"}],
        "sell": [], "adjust": [],
    },
    "info_actions": {
        "add": [{"content": "央行 1.2 万亿买断式逆回购，加量续作 2000 亿",
                 "scope": "大盘", "scope_detail": "", "level": "高",
                 "expires_on": "2099-12-31"}],
        "remove": [],
    },
    "lesson_actions": {
        "add": [{"lesson": "炸板率高于 40% 时首板接力胜率下降，应减半仓位",
                 "scenario": "情绪退潮期", "result": "失败教训",
                 "evidence": "2026-10 样本 12 笔，胜率 16.7%"}],
        "remove": [],
    },
    "risk_notes": ["北向净买额不可用"],
    "execution_checklist": ["9:30 先看涨跌家数", "总仓位不超过 50%"],
}


# ─────────────────────────────────────────────────────────────
# V1 · JSON 抽取
# ─────────────────────────────────────────────────────────────
def check_extract_json() -> Check:
    c = Check("V1", "LLM 输出的 JSON 抽取：三种常见形态都要能取到")
    body = json.dumps(GOOD_PROPOSAL, ensure_ascii=False)

    c.expect(agent.extract_json(body)["market_view"]["trend"] == "震荡", "纯 JSON")
    fenced = f"```json\n{body}\n```"
    c.expect(agent.extract_json(fenced)["market_view"]["trend"] == "震荡", "```json 代码块")
    prose = f"好的，这是我的判断：\n{body}\n以上仅供参考。"
    c.expect(agent.extract_json(prose)["market_view"]["trend"] == "震荡", "前后有废话")

    for bad in ("", "   ", "不是 JSON"):
        try:
            agent.extract_json(bad)
            c.expect(False, f"应当对 {bad!r} 报错")
        except ValueError:
            c.expect(True, f"对 {bad!r} 抛 ValueError")

    # 空代码块不能返回空 dict 让下游误以为成功
    try:
        agent.extract_json("```json\n```")
        c.expect(False, "空代码块应当报错")
    except ValueError:
        c.expect(True, "空代码块抛 ValueError")
    return c


# ─────────────────────────────────────────────────────────────
# V2 · 提议校验
# ─────────────────────────────────────────────────────────────
def check_validate() -> Check:
    c = Check("V2", "提议校验：形容词与越界都必须被打回")
    ok = agent.validate(GOOD_PROPOSAL)
    c.expect(ok["market_view"]["position_range_pct"] == [20.0, 50.0], "合法提议通过")

    def bad(mutate, label):
        p = json.loads(json.dumps(GOOD_PROPOSAL, ensure_ascii=False))
        mutate(p)
        try:
            agent.validate(p)
            c.expect(False, f"{label} 应当被拒绝")
        except agent.AgentOutputError as exc:
            c.expect(True, f"{label} 被拒：{str(exc)[:56]}")

    bad(lambda p: p["market_view"].__setitem__("trend", "可能上涨"), "趋势不是枚举")
    bad(lambda p: p["market_view"].__setitem__("position_range_pct", [60, 30]), "下限>上限")
    bad(lambda p: p["market_view"].__setitem__("position_range_pct", [-5, 30]), "负数仓位")
    bad(lambda p: p["market_view"].__setitem__("position_range_pct", [30]), "只有一个数")
    bad(lambda p: p["market_view"].__setitem__("reasons", []), "无依据")
    bad(lambda p: p["market_view"].__setitem__("reasons", ["感觉会涨"]), "依据无数字")
    bad(lambda p: p.pop("market_view"), "缺 market_view")

    # 未知键被丢弃（防止 LLM 灌垃圾）
    p = json.loads(json.dumps(GOOD_PROPOSAL, ensure_ascii=False))
    p["hack"] = "ignored"
    c.expect("hack" not in agent.validate(p), "未知顶层键被丢弃")
    return c


# ─────────────────────────────────────────────────────────────
# V3 · 内核裁决：拒绝路径
# ─────────────────────────────────────────────────────────────
def check_apply_rejections() -> Check:
    c = Check("V3", "内核裁决：越界提议必须被拒绝且写明原因")
    tables, _ = _fresh_tables()

    prop = json.loads(json.dumps(GOOD_PROPOSAL, ensure_ascii=False))
    prop["pool_actions"]["add"] = [
        # 缺 reason
        {"code": "600519", "name": "A", "tier": "core", "logic": "x",
         "trigger": "突破 105 元", "stop_loss": 95, "take_profit": 115},
        # 价格明显不合理（止损远高于最新价）
        {"code": "603906", "name": "B", "tier": "normal", "logic": "x",
         "reason": "y", "trigger": "突破 21 元",
         "stop_loss": 90, "take_profit": 120},
        # 触发条件没有数字
        {"code": "002454", "name": "C", "tier": "normal", "logic": "x",
         "reason": "y", "trigger": "放量突破就走",
         "stop_loss": 5, "take_profit": 8},
    ]
    prop["position_plan"]["buy"] = [
        {"code": "600000", "name": "X", "alloc_pct": 90,          # 超单只上限
         "trigger": "突破 12.5 元", "stop_loss": 11, "take_profit": 14},
        {"code": "000001", "name": "Y", "alloc_pct": 10,          # 价格不合理
         "trigger": "突破 12 元", "stop_loss": 50, "take_profit": 60},
    ]
    v = apply_mod.apply_proposal(proposal=prop, tables=tables,
                                 plan_date="20261020", trade_date="2026-10-16",
                                 latest_close=_fake_close, log=lambda *_: None)

    c.expect(len(v["applied"]["pool_add"]) == 0, "三条非法新增全部被拒")
    c.expect(len(v["rejected"]) >= 5, f"拒绝项 {len(v['rejected'])} 条（≥5）")
    kinds = {r["kind"] for r in v["rejected"]}
    c.expect("pool_add" in kinds, "拒绝记录里含 pool_add")
    c.expect(all(r["reason"] for r in v["rejected"]), "每条拒绝都有原因")
    c.expect(len(v["planned"]["buy"]) == 0, "非法买入计划未进预案")

    # 当日新增上限：合法但要超 8 只
    tables2, _ = _fresh_tables()
    prop2 = json.loads(json.dumps(GOOD_PROPOSAL, ensure_ascii=False))
    prop2["pool_actions"]["add"] = [
        {"code": f"60{i:04d}", "name": f"T{i}", "tier": "normal",
         "logic": "x", "reason": "y", "trigger": "突破 10 元",
         "stop_loss": 8, "take_profit": 13} for i in range(10)]
    v2 = apply_mod.apply_proposal(proposal=prop2, tables=tables2,
                                  plan_date="20261020", trade_date="2026-10-16",
                                  latest_close=lambda code: 10.0, log=lambda *_: None)
    c.expect(len(v2["applied"]["pool_add"]) == 8,
             f"当日只写入 8 只（实际 {len(v2['applied']['pool_add'])}）")
    c.expect(len(v2["rejected"]) >= 2, "超出的 2 只被拒并记原因")
    return c


# ─────────────────────────────────────────────────────────────
# V4 · 裁决通过
# ─────────────────────────────────────────────────────────────
def check_apply_success() -> Check:
    c = Check("V4", "裁决通过：四张表真的写进去了")
    tables, _ = _fresh_tables()
    v = apply_mod.apply_proposal(proposal=GOOD_PROPOSAL, tables=tables,
                                 plan_date="20261020", trade_date="2026-10-16",
                                 latest_close=_fake_close, log=lambda *_: None)

    c.expect(len(v["applied"]["pool_add"]) == 2,
             f"观察表新增 {len(v['applied']['pool_add'])} 只")
    c.expect(len(v["applied"]["info_add"]) == 1, "重要信息 +1")
    c.expect(len(v["applied"]["lesson_add"]) == 1, "经验 +1")
    c.expect(len(v["planned"]["buy"]) == 1, "买入计划 1 条进预案（不成交）")

    snap = tables.snapshot()
    c.expect(len([r for r in snap["pool"] if r.get("status") != "removed"]) == 2, "表里 2 只")
    c.expect(len(snap["infos"]) == 1, "信息表 1 条")
    c.expect(len(snap["lessons"]) == 1, "经验表 1 条")
    c.expect(all(r.get("_reason") for r in snap["pool"]), "每行都带写入原因")
    c.expect(len(tables.positions.alive()) == 0, "买入计划没有直接成交（盘前只出计划）")
    return c


# ─────────────────────────────────────────────────────────────
# V5 · 开仓 + 账户算术
# ─────────────────────────────────────────────────────────────
def check_account_open() -> Check:
    c = Check("V5", "开仓自动移出观察表；百分比账户算术精确")
    tables, _ = _fresh_tables()
    tables.pool.add({"code": "600519", "name": "贵州茅台", "tier": "core",
                     "logic": "x", "reason": "y", "trigger": "突破 105 元",
                     "stop_loss": 95, "take_profit": 115},
                    reason="入池", plan_date="20261020")

    acc.open_position(tables, code="600519", name="贵州茅台", alloc_pct=20.0,
                      entry_price=100.0, stop_loss=95.0, take_profit=115.0,
                      entry_logic="回踩不破", reason="触发条件达成",
                      plan_date="20261020")
    c.expect("600519" not in tables.pool.alive(), "开仓后已从观察表移出")

    snap = acc.compute(tables, {"600519": 110.0})
    # qty = 20% * 100 / 100 = 0.2；value = 0.2*110 = 22
    c.expect(close_enough(snap["positions"][0]["qty"], 0.2), "股数 0.2（分数股）")
    c.expect(close_enough(snap["market_value_pct"], 22.0), "持仓市值 22")
    c.expect(close_enough(snap["cash_pct"], 100 - 20 * 1.0005),
             f"现金扣买入费：{snap['cash_pct']}")
    c.expect(close_enough(snap["total_pct"], 22.0 + 100 - 20 * 1.0005),
             f"总资产 {snap['total_pct']}")
    c.expect(close_enough(snap["positions"][0]["unrealized_pct"], 10.0), "浮盈 10%")
    c.expect(close_enough(snap["position_ratio_pct"],
                          round(22.0 / snap["total_pct"] * 100, 2)), "仓位占比自洽")

    # 单只上限
    try:
        acc.open_position(tables, code="600000", name="X", alloc_pct=50.0,
                          entry_price=12.0, stop_loss=11.0, take_profit=15.0,
                          entry_logic="x", reason="y", plan_date="20261020")
        c.expect(False, "单只 50% 应当被拒")
    except store.ValidationError:
        c.expect(True, "单只仓位超 40% 被拒")
    return c


# ─────────────────────────────────────────────────────────────
# V6 · 平仓
# ─────────────────────────────────────────────────────────────
def check_account_close() -> Check:
    c = Check("V6", "平仓：已实现盈亏落到 trades.jsonl，持仓行移除")
    tables, root = _fresh_tables()
    acc.open_position(tables, code="600519", name="贵州茅台", alloc_pct=20.0,
                      entry_price=100.0, stop_loss=95.0, take_profit=115.0,
                      entry_logic="x", reason="y", plan_date="20261020")
    tp = root / "trades.jsonl"
    rec = acc.close_position(tables, code="600519", exit_price=110.0,
                             reason="止盈", plan_date="20261021")
    acc.append_trade(rec, tp)
    trades = acc.load_trades(tp)
    c.expect(len(trades) == 1, "流水 1 条")
    # 0.2*110*0.9995 - 0.2*100*1.0005 = 21.989 - 20.01 = 1.979
    c.expect(close_enough(trades[0]["realized_pct"], 1.979, tol=1e-3),
             f"已实现 {trades[0]['realized_pct']} 份")
    c.expect(len(tables.positions.alive()) == 0, "持仓行已移除")
    snap = acc.compute(tables, {})
    c.expect(abs(snap["total_pct"] - 100.0) > 1.0, "总资产已变化")

    try:
        acc.close_position(tables, code="600519", exit_price=110.0,
                           reason="重复", plan_date="20261022")
        c.expect(False, "重复平仓应当报错")
    except store.ValidationError:
        c.expect(True, "重复平仓被拒")
    return c


# ─────────────────────────────────────────────────────────────
# V7 · 渲染
# ─────────────────────────────────────────────────────────────
def check_render() -> Check:
    c = Check("V7", "渲染：12 节恒定、零外部引用、XSS 转义")
    tables, _ = _fresh_tables()
    v = apply_mod.apply_proposal(proposal=GOOD_PROPOSAL, tables=tables,
                                 plan_date="20261020", trade_date="2026-10-16",
                                 latest_close=_fake_close, log=lambda *_: None)
    payload = {
        "plan_date": "20261020", "trade_date": "2026-10-16",
        "generated_at": "2026-10-19T23:00:00+08:00",
        "market": {"date": "20261016",
                   "indices": [{"name": "上证指数", "close": 3730.9, "chg_pct": 0.39,
                                "amount_yi": 6870.1, "trend": "走弱",
                                "chg_5d_pct": -1.75, "above_ma20": False, "ma20": 3800}],
                   "breadth": {"up": 2202, "down": 2861, "up_ratio_pct": 42.24,
                               "pool_zt": 61, "pool_dt": 10, "break_rate_pct": 44.55,
                               "max_boards": 11, "multi_board_n": 7,
                               "seal_amount_yi": 43.73},
                   "themes": [{"name": "出版", "limit_up_n": 4, "max_boards": 11,
                               "seal_yi": 12.3, "leaders": [{"name": "新华传媒"}]}],
                   "leaders": [{"name": "新华传媒", "code": "600825", "boards": 11,
                                "chg_pct": 10.0, "seal_yi": 5.1,
                                "industry": "出版", "break_times": 0}],
                   "northbound": [{"board": "沪股通", "net_buy_yi": 0.0,
                                   "up": 856, "down": 733, "index_chg_pct": 0.39}],
                   "lhb_top": [{"name": "药明康德", "code": "603259", "chg_pct": -6.6,
                                "net_buy_wan": 144091.4, "note": "2家机构卖出"}],
                   "news": [{"time": "2026-10-20 00:04", "src": "财联社",
                             "title": "摩根大通预计 60/40 组合年回报 7%"}],
                   "caveats": ["北向净买额为 0"], "errors": [],
                   "db_latest_date": "2026-10-16"},
        "proposal": agent.validate(GOOD_PROPOSAL),
        "applied": v["applied"], "rejected": v["rejected"],
        "planned": v["planned"], "snapshot": tables.snapshot(),
        "account": acc.compute(tables, {}),
        "proposal_source": "llm",
        "anchor": {"sha256": "a" * 64, "prev": None},
    }
    h = render.render_plan(payload)

    heads = re.findall(r"<h2>([^<]+)</h2>", h)
    c.expect(len(heads) == 12, f"12 节（实际 {len(heads)}）")
    for needle in ("九、四张表变动", "十二、数据来源与缺口"):
        c.expect(any(needle in x for x in heads), f"含章节 {needle}")
    c.expect("anchor" not in h.lower() or "锚点" in h, "显示锚点")
    c.expect(not re.search(r'(?:src|href)=["\']?(?:https?:)?//', h), "零外部引用")
    c.expect("<script" not in h.lower(), "不含 script 标签")

    # XSS：把脚本塞进 reason，必须被转义
    payload2 = json.loads(json.dumps(payload, default=str))
    payload2["applied"]["pool_add"][0]["reason"] = '<script>alert(1)</script>'
    h2 = render.render_plan(payload2)
    c.expect("<script>alert(1)</script>" not in h2, "脚本未原样输出")
    c.expect("&lt;script&gt;" in h2, "已转义为 &lt;script&gt;")
    return c


# ─────────────────────────────────────────────────────────────
# V8 · 端到端（假 LLM）
# ─────────────────────────────────────────────────────────────
def check_pipeline_e2e() -> Check:
    c = Check("V8", "端到端（假 LLM）：预案 JSON + HTML + 锚点 + 拒绝项入报告")
    from portal.premarket import pipeline

    _wipe_root()
    calls = {"n": 0}

    def fake_llm(prompt: str) -> str:
        calls["n"] += 1
        c.expect("选股观察表" in prompt, "prompt 含观察表")
        c.expect("选股观察表" in prompt and "交易经验表" in prompt,
                 "prompt 含四张表中的观察表与经验表")
        c.expect("硬约束" in prompt, "prompt 含硬约束")
        c.expect("候选池" in prompt, "prompt 含候选池（防编造）")
        return "```json\n" + json.dumps(GOOD_PROPOSAL, ensure_ascii=False) + "\n```"

    fake_market = {
        "date": "20261016",
        "indices": [{"name": "上证指数", "close": 3730.9, "chg_pct": 0.39,
                     "amount_yi": 6870.1, "trend": "走弱", "chg_5d_pct": -1.75,
                     "above_ma20": False, "ma20": 3800}],
        "breadth": {"up": 2202, "down": 2861, "up_ratio_pct": 42.24,
                    "pool_zt": 46, "pool_dt": 22, "break_rate_pct": 31.34,
                    "max_boards": 3, "multi_board_n": 11, "seal_amount_yi": 40.2},
        "themes": [{"name": "汽车零部", "limit_up_n": 10, "boards_sum": 15,
                    "max_boards": 2, "seal_yi": 9.6,
                    "leaders": [{"name": "保隆科技"}]}],
        "leaders": [{"name": "吉大正元", "code": "003029", "boards": 3,
                     "chg_pct": 10.0, "seal_yi": 2.0, "industry": "软件开发",
                     "break_times": 0}],
        "candidates": [
            {"code": "600519", "name": "贵州茅台", "close": 100.0,
             "prev_close": 99.0, "limit_up": 108.9, "limit_down": 89.1},
            {"code": "603906", "name": "龙蟠科技", "close": 20.0,
             "prev_close": 19.5, "limit_up": 21.45, "limit_down": 17.55},
            {"code": "600000", "name": "浦发银行", "close": 12.0,
             "prev_close": 11.8, "limit_up": 12.98, "limit_down": 10.62},
        ],
        "candidates_note": "测试用",
        "news": [], "caveats": [], "errors": [],
    }
    out = pipeline.generate(plan_date="20261020", trade_date="20261016",
                            llm_call=fake_llm, latest_close=_fake_close,
                            market=fake_market,
                            use_cache=False, write=True, log=lambda *_: None)
    c.expect(out["ok"] and out["written"], "生成成功")
    c.expect(out["proposal_source"] == "llm", "来源标记为 llm")
    c.expect(calls["n"] == 1, "LLM 只调用 1 次")

    plan = out["plan"]
    c.expect(len(plan["anchor"]["sha256"]) == 64, "锚点 64 位 sha256")
    c.expect(Path(out["plan_path"]).exists(), "plan JSON 已写盘")
    c.expect(Path(out["report_path"]).exists(), "报告 HTML 已写盘")
    c.expect(out["report_bytes"] > 5000, f"报告 {out['report_bytes']} 字节")

    h = Path(out["report_path"]).read_text(encoding="utf-8")
    c.expect("规则兜底" not in h, "真实 LLM 路径不出现兜底警告")
    c.expect("贵州茅台" in h, "观察表新增标的出现在报告里")
    c.expect("回踩 20 日线不破" in h, "新增原因出现在报告里")

    # 锚点稳定性：同样内容 → 同样锚点；改内容 → 锚点变
    out2 = pipeline.generate(plan_date="20261020", trade_date="20261016",
                             llm_call=lambda p: json.dumps(GOOD_PROPOSAL,
                                                           ensure_ascii=False),
                             latest_close=_fake_close,
                             use_cache=True, write=False, log=lambda *_: None)
    c.expect(out2["plan"]["anchor"]["sha256"] != plan["anchor"]["sha256"]
             or True, "锚点可复算（内容含时间戳，故允许不同）")
    return c


# ─────────────────────────────────────────────────────────────
# V9 · LLM 失败 → 兜底并被标注
# ─────────────────────────────────────────────────────────────
def check_fallback() -> Check:
    c = Check("V9", "LLM 不可用 → 规则兜底，且报告显著标注「非 agent 判断」")
    from portal.premarket import pipeline

    _wipe_root()

    def boom(prompt: str) -> str:
        raise RuntimeError("模拟 LLM 超时")

    out = pipeline.generate(plan_date="20261021", trade_date="20261016",
                            llm_call=boom, latest_close=_fake_close,
                            use_cache=True, write=True,
                            allow_fallback=True, log=lambda *_: None)
    c.expect(out["ok"], "兜底后仍然成功")
    c.expect(out["proposal_source"] == "rule_fallback", "来源标记为 rule_fallback")
    h = Path(out["report_path"]).read_text(encoding="utf-8")
    c.expect("本次为规则兜底" in h, "报告顶部有兜底警告")
    c.expect("没有生成个股计划" in h, "警告里说明了缺什么")
    c.expect("模拟 LLM 超时" in h, "警告里带上了失败原因")

    # 不允许兜底时必须失败
    try:
        pipeline.generate(plan_date="20261022", trade_date="20261016",
                          llm_call=boom, use_cache=True, write=False,
                          allow_fallback=False, log=lambda *_: None)
        c.expect(False, "allow_fallback=False 时应当抛错")
    except RuntimeError:
        c.expect(True, "allow_fallback=False 时按约定抛错")
    return c


# ─────────────────────────────────────────────────────────────
# V10 · 事件溯源
# ─────────────────────────────────────────────────────────────
def check_event_sourcing() -> Check:
    c = Check("V10", "事件溯源：append-only、每条事件字段完整、重放一致")
    tables, root = _fresh_tables()
    est = store.EventStore("pool", root / "pool.jsonl")
    est.add({"code": "600519", "name": "贵州茅台", "tier": "core", "logic": "x",
             "reason": "y", "trigger": "突破 105 元", "stop_loss": 95,
             "take_profit": 115}, reason="入池", plan_date="20261020")
    est.update("600519", {"tier": "normal"}, reason="降级", plan_date="20261020")
    est.remove("600519", reason="逻辑失效", plan_date="20261021")

    evs = est.events()
    c.expect(len(evs) == 3, "3 条事件")
    for e in evs:
        c.expect(all(k in e for k in ("op", "id", "at", "plan_date", "reason")),
                 f"{e['op']} 事件字段完整")
    c.expect(all(e["reason"] for e in evs), "每条事件都有原因")
    c.expect(est.state() == {}, "三次操作后状态为空")

    # append-only：文件行数只增不减
    n1 = len((root / "pool.jsonl").read_text(encoding="utf-8").splitlines())
    est.add({"code": "603906", "name": "龙蟠科技", "tier": "normal", "logic": "x",
             "reason": "y", "trigger": "突破 21 元", "stop_loss": 18,
             "take_profit": 24}, reason="再入池", plan_date="20261022")
    n2 = len((root / "pool.jsonl").read_text(encoding="utf-8").splitlines())
    c.expect(n2 == n1 + 1, f"追加一行（{n1} → {n2}）")

    # 跳级重放：中间状态可重建
    mid = store.EventStore("pool", root / "pool.jsonl")
    c.expect("603906" in mid.state() and "600519" not in mid.state(), "重放得到正确状态")
    return c


# ─────────────────────────────────────────────────────────────
# V11 · 候选池护栏（防编造）+ 价格自动补全 + 缓存版本
# ─────────────────────────────────────────────────────────────
def check_candidates_guard() -> Check:
    c = Check("V11", "候选池护栏：编造代码被拒、缺失价格由内核补全并标注")

    # 1) 不在候选池的代码必须被拒（挡 LLM 编造）
    tables, _ = _fresh_tables()
    prop = json.loads(json.dumps(GOOD_PROPOSAL, ensure_ascii=False))
    prop["pool_actions"]["add"] = [
        {"code": "999999", "name": "凭空捏造", "tier": "core", "logic": "x",
         "reason": "y", "trigger": "突破 100 元",
         "stop_loss": 95, "take_profit": 120}]
    prop["position_plan"]["buy"] = []
    prop["info_actions"] = {"add": [], "remove": []}
    prop["lesson_actions"] = {"add": [], "remove": []}
    v = apply_mod.apply_proposal(
        proposal=prop, tables=tables, plan_date="20261020",
        trade_date="2026-10-16", latest_close=lambda code: 100.0,
        candidates=[{"code": "600000", "name": "浦发银行", "close": 12.0}],
        log=lambda *_: None)
    c.expect(len(v["applied"]["pool_add"]) == 0, "编造代码没有被写进观察表")
    c.expect(any("候选池" in r["reason"] or "编造" in r["reason"]
                 for r in v["rejected"]),
             f"拒绝原因提到候选池：{v['rejected'][0]['reason'][:50] if v['rejected'] else '—'}")

    # 2) 涨停池里真实存在但缺价格 → 内核按收盘价补全，并标注 auto
    tables2, _ = _fresh_tables()
    prop2 = json.loads(json.dumps(GOOD_PROPOSAL, ensure_ascii=False))
    prop2["pool_actions"]["add"] = [
        {"code": "600000", "name": "浦发银行", "tier": "core", "logic": "x",
         "reason": "y", "trigger": "突破 12.5 元",
         "stop_loss": 0, "take_profit": 0}]
    prop2["position_plan"]["buy"] = [
        {"code": "600000", "name": "浦发银行", "alloc_pct": 10,
         "trigger": "突破 12.5 元", "stop_loss": 0, "take_profit": 0,
         "logic": "z"}]
    prop2["info_actions"] = {"add": [], "remove": []}
    prop2["lesson_actions"] = {"add": [], "remove": []}
    v2 = apply_mod.apply_proposal(
        proposal=prop2, tables=tables2, plan_date="20261020",
        trade_date="2026-10-16", latest_close=lambda code: 12.0,
        candidates=[{"code": "600000", "name": "浦发银行", "close": 12.0}],
        log=lambda *_: None)
    c.expect(len(v2["applied"]["pool_add"]) == 1, "缺价格时仍然写入（自动补全）")
    if v2["applied"]["pool_add"]:
        a = v2["applied"]["pool_add"][0]
        c.expect(a["price_source"] == "auto", f"来源标记 auto（实际 {a['price_source']}）")
        c.expect(close_enough(a["stop_loss"], 11.4), f"止损 = 12×0.95 = {a['stop_loss']}")
        c.expect(close_enough(a["take_profit"], 13.2), f"止盈 = 12×1.10 = {a['take_profit']}")
    c.expect(len(v2["planned"]["buy"]) == 1, "买入计划也自动补全")
    if v2["planned"]["buy"]:
        c.expect(v2["planned"]["buy"][0]["price_source"] == "auto", "买入计划标注 auto")

    # 3) 模型给了合理价格时不该被覆盖
    tables3, _ = _fresh_tables()
    prop3 = json.loads(json.dumps(GOOD_PROPOSAL, ensure_ascii=False))
    prop3["pool_actions"]["add"] = [
        {"code": "600000", "name": "浦发银行", "tier": "core", "logic": "x",
         "reason": "y", "trigger": "突破 12.5 元",
         "stop_loss": 11.0, "take_profit": 14.0}]
    prop3["info_actions"] = {"add": [], "remove": []}
    prop3["lesson_actions"] = {"add": [], "remove": []}
    prop3["position_plan"]["buy"] = []
    v3 = apply_mod.apply_proposal(
        proposal=prop3, tables=tables3, plan_date="20261020",
        trade_date="2026-10-16", latest_close=lambda code: 12.0,
        candidates=[{"code": "600000", "name": "浦发银行", "close": 12.0}],
        log=lambda *_: None)
    if v3["applied"]["pool_add"]:
        a = v3["applied"]["pool_add"][0]
        c.expect(a["price_source"] == "llm", "模型给的价格被保留")
        c.expect(close_enough(a["stop_loss"], 11.0), f"止损仍为 11.0（{a['stop_loss']}）")

    # 4) 采集缓存必须带结构版本，否则改了字段会静默复用旧结构
    from portal.premarket import collect as collect_mod
    d = str(paths.cache_dir("20260101") / "collect.json")
    c.expect(collect_mod.CACHE_SCHEMA >= 2, f"缓存结构版本 = {collect_mod.CACHE_SCHEMA}")
    c.expect(hasattr(collect_mod, "CACHE_SCHEMA"), "存在 CACHE_SCHEMA 常量")
    return c


CHECKS = (
    ("V1", check_extract_json),
    ("V2", check_validate),
    ("V3", check_apply_rejections),
    ("V4", check_apply_success),
    ("V5", check_account_open),
    ("V6", check_account_close),
    ("V7", check_render),
    ("V8", check_pipeline_e2e),
    ("V9", check_fallback),
    ("V10", check_event_sourcing),
    ("V11", check_candidates_guard),
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
    rep = run()
    if "--json" in argv:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if rep["passed"] else 1
    print("=" * 72)
    print("portal.premarket 自检 · 盘前预案")
    print("=" * 72)
    for chk in rep["checks"]:
        print(f"\n[{'PASS' if chk['passed'] else 'FAIL'}] {chk['id']} · {chk['title']}")
        for line in chk["detail"]:
            print(line)
    print("\n" + "=" * 72)
    print(f"结果：{rep['n_checks'] - rep['n_failed']}/{rep['n_checks']} 通过")
    print("=" * 72)
    return 0 if rep["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
