# -*- coding: utf-8 -*-
"""
portal/analyzers/yuzi.py — 「游资 / 情绪」分析维度

把「无名游资」包里真正可复用的三样东西接进 portal 现有分析流水线：

    1. 涨停池定位（连板数 / 首封时间 / 炸板次数 / 封单÷流通）
    2. 可买性分档（封单÷流通 的四档判据）
    3. 情绪刹车读数（**只用于仓位，禁止进入个股评分**）

一条硬约束，写进代码而不是只写进文档
------------------------------------
情绪类指标（涨停家数/连板高度/炸板率）与走势是**同步**的，没有领先性
（华泰金工对 21 个情绪指标的统一回测）。所以：

    emotion_brake 这一节
        ✅ 进 section.data，给「仓位档位」
        ❌ 不进 score —— `_score_from_stock_level()` 只读个股级事实

如果哪天有人把情绪分加进 `score`，`selfcheck` 的注释与本节 content 都会对不上，
这是故意的：让越界在代码审查里显眼。
"""
from __future__ import annotations

import logging
from typing import Any

from .base import BaseAnalyzer, DimensionResult, Section

logger = logging.getLogger(__name__)


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _clamp(x: float, lo: int = 0, hi: int = 100) -> int:
    return int(max(lo, min(hi, round(x))))


def _score_from_stock_level(*, seal_ratio_pct: float | None, boards: float | None,
                            lhb_net: float | None, in_pool: bool) -> int:
    """**只从个股级事实算分。** 情绪读数不在这里 —— 见模块 docstring。"""
    score = 50.0

    if in_pool:
        score += 4.0                       # 在涨停池里：短线关注度确认
    if boards is not None:
        if boards <= 1:
            score += 6.0                   # 首板：位置相对干净
        elif boards >= 3:
            score -= 8.0 * min(boards - 2, 3)   # 高位接力风险递增

    if seal_ratio_pct is not None:
        # 封单/流通 越小越买得到；买不到的好票对收益没有意义
        if seal_ratio_pct < 1.0:
            score += 10.0
        elif seal_ratio_pct < 3.0:
            score += 2.0
        elif seal_ratio_pct < 10.0:
            score -= 8.0
        else:
            score -= 15.0

    if lhb_net is not None:
        score += 6.0 if lhb_net > 0 else -6.0

    return _clamp(score)


def _buyability_bucket(r: float | None) -> tuple[str, str]:
    """(档位, 人话)。与 virtual/pools.py 的四档保持一致。"""
    if r is None:
        return "unknown", "取不到封单/流通数据"
    if r < 1.0:
        return "buyable", "可买（次日大概率能成交）"
    if r < 3.0:
        return "auction_decide", "需在 9:20—9:25 竞价窗口判断"
    if r < 10.0:
        return "likely_sealed", "大概率一字板，基本买不到"
    return "unbuyable", "完全买不到（连续一字）"


class YuziAnalyzer(BaseAnalyzer):
    name = "游资情绪"
    dimension = "yuzi"
    description = "涨停池定位 / 封单可买性 / 龙虎榜 / 情绪刹车读数"

    MODULES = {
        "limit_up_status": "涨停池定位（连板/首封/炸板）",
        "buyability":      "可买性（封单÷流通，四档）",
        "emotion_brake":   "情绪刹车读数（仅仓位，不进评分）",
        "dragon_tiger":    "龙虎榜净买与席位",
    }
    DEFAULT_MODULES = ["limit_up_status", "buyability", "emotion_brake"]

    # ── 数据获取（可在测试里替换）─────────────────────────────
    def _fetch_pools(self, date8: str | None) -> dict:
        from portal.virtual import pools as P
        import datetime
        d = date8 or datetime.date.today().strftime("%Y%m%d")
        return P.fetch_pools(d, kinds=("zt", "zb", "dt"), strict=False)

    def _fetch_dragon_tiger(self, stock_code: str) -> dict:
        try:
            from data_provider.base import DataFetcherManager
        except ImportError:
            from portal.lib.data_provider.base import DataFetcherManager  # type: ignore
        mgr = DataFetcherManager()
        return mgr.get_dragon_tiger_context(stock_code) or {}

    # ── 主流程 ────────────────────────────────────────────────
    def analyze(self, stock_code, stock_name, df, modules, llm_call, search, log=None):
        result = DimensionResult(dimension=self.dimension, name=self.name)

        def _log(msg: str) -> None:
            if log:
                log(msg)

        # 数据层可能整个不可用：导入失败也要继续，只是降级成「无数据」
        pools_mod = None
        try:
            from portal.virtual import pools as pools_mod  # type: ignore
        except Exception as exc:
            _log(f"⚠️ [游资情绪] 无法加载 virtual.pools：{exc}")

        # 1) 涨停池
        pool_doc: dict = {}
        if pools_mod is not None:
            try:
                pool_doc = self._fetch_pools(None)
            except Exception as exc:
                _log(f"⚠️ [游资情绪] 涨停池获取失败：{exc}")

        zt = pool_doc.get("zt") or []
        zb = pool_doc.get("zb") or []
        dt = pool_doc.get("dt") or []
        row = next((x for x in zt if str(x.get("code")) == str(stock_code)), None)
        in_pool = row is not None

        seal_ratio = _num((row or {}).get("seal_ratio_pct"))
        boards = _num((row or {}).get("boards"))

        sections: list[Section] = []

        # ── 涨停池定位
        if "limit_up_status" in modules:
            if row:
                content = (
                    f"**在涨停池中**｜连板 {int(boards or 0)}｜首封 {row.get('first_seal') or '—'}"
                    f"｜炸板 {int(_num(row.get('break_count')) or 0)} 次"
                    f"｜封单 {(_num(row.get('seal_amount')) or 0)/1e8:.2f} 亿"
                    f"｜流通 {(_num(row.get('float_cap')) or 0)/1e8:.1f} 亿"
                )
                sig = "buy" if (boards or 1) <= 1 else ("watch" if (boards or 1) == 2 else "hold")
            else:
                content = ("**不在今日涨停池中**｜说明今日未封板（可能未涨停、或已炸板）。"
                           "本维度只对涨停股有信息量。")
                sig = "hold"
            sections.append(Section(
                key="limit_up_status", title="涨停池定位", content=content,
                data={"in_pool": in_pool, "boards": boards,
                      "first_seal": (row or {}).get("first_seal"),
                      "break_count": (row or {}).get("break_count"),
                      "seal_amount": (row or {}).get("seal_amount"),
                      "float_cap": (row or {}).get("float_cap"),
                      "zt_n": len(zt), "zb_n": len(zb), "dt_n": len(dt)},
                signal=sig, score=60 if in_pool else 50,
            ))

        # ── 可买性
        if "buyability" in modules:
            bucket, human = _buyability_bucket(seal_ratio)
            if seal_ratio is not None:
                content = (f"封单/流通 = **{seal_ratio:.3f}%** → {human}。"
                           f"这是判断次日能否成交的唯一有效量化判据："
                           f"涨停时卖盘稀少，要成交必须有卖单落在我前面。")
            else:
                content = "取不到封单/流通数据，可买性未知（**不猜**）。"
            sections.append(Section(
                key="buyability", title="可买性（封单÷流通）", content=content,
                data={"seal_ratio_pct": seal_ratio, "bucket": bucket,
                      "thresholds": {"buyable": "<1%", "auction_decide": "1–3%",
                                     "likely_sealed": "3–10%", "unbuyable": ">10%"}},
                signal={"buyable": "buy", "auction_decide": "watch"}.get(bucket, "hold"),
                score={"buyable": 72, "auction_decide": 58}.get(bucket, 42),
            ))

        # ── 情绪刹车（不进评分）
        snap: dict | None = None
        if "emotion_brake" in modules:
            if pools_mod is not None:
                snap = pools_mod.emotion_snapshot(zt, zb, dt)
            content = (
                f"涨停 {snap['zt_n'] if snap else '—'}｜跌停 {snap['dt_n'] if snap else '—'}"
                f"｜炸板 {snap['zb_n'] if snap else '—'}"
                f"（炸板率 {(snap or {}).get('zb_rate', '—')}）"
                f"｜最高 {(snap or {}).get('max_lbc', '—')} 板"
                f"｜情绪分 {(snap or {}).get('emotion_score', '—')}（自定义，待校准）。\n\n"
                f"**用途约束（硬性）**：这是**同步指标**，没有领先性。"
                f"只能用于「仓位档位」与「今天做不做」，"
                f"**禁止**进入个股评分、禁止当作买入信号、禁止写成「明天会涨」。"
            )
            sections.append(Section(
                key="emotion_brake", title="情绪刹车读数（仅仓位）", content=content,
                data={**(snap or {}), "usage": "brake_only"},
                # signal 恒为 hold：它不表达方向，只表达强度
                signal="hold", score=50,
            ))

        # ── 龙虎榜
        lhb_net: float | None = None
        if "dragon_tiger" in modules:
            try:
                lhb = self._fetch_dragon_tiger(stock_code)
                lhb_net = _num(lhb.get("net_amount") or lhb.get("net_buy"))
                seats = lhb.get("seats") or []
                if lhb_net is None:
                    content = "未取得龙虎榜净买数据（可能当日未上榜，或数据源不可用）。"
                else:
                    content = (f"龙虎榜净买 {lhb_net/1e4:+.0f} 万元｜席位 {len(seats)} 条。"
                               f"买方若含机构专用/深沪股通/一线游资中的至少两类，正向信号更强。")
            except Exception as exc:
                logger.debug("dragon tiger failed: %s", exc)
                content = "龙虎榜数据源不可用（已降级，不猜）。"
            sections.append(Section(
                key="dragon_tiger", title="龙虎榜", content=content,
                data={"net": lhb_net},
                signal="buy" if (lhb_net or 0) > 0 else "hold",
                score=60 if (lhb_net or 0) > 0 else 50,
            ))

        result.sections = sections
        # ★ 评分只吃个股级事实（seal_ratio / boards / lhb），情绪不进分
        score = _score_from_stock_level(
            seal_ratio_pct=seal_ratio, boards=boards, lhb_net=lhb_net, in_pool=in_pool)
        result.score = score
        result.signal = "buy" if score >= 70 else ("watch" if score >= 55 else "hold")
        result.summary = (
            f"{'在涨停池' if in_pool else '不在涨停池'}"
            f"｜可买性 {_buyability_bucket(seal_ratio)[0]}"
            + (f"｜情绪分（仅仓位参考）{snap['emotion_score']}" if snap else "")
        )
        return result


__all__ = ["YuziAnalyzer"]
