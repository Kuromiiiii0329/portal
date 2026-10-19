"""
portal/analyzers/industry/sectors.py
板块分析子领域 —— 基于真实板块数据（efinance/同花顺），不依赖 LLM。

子模块：sector_membership（所属板块）/ sector_momentum（板块景气/相对强弱）/
        sector_fund_flow（板块资金/轮动）。

从原 IndustryAnalyzer 的 _analyze_sectors / _stock_today_pct /
_sector_momentum_section / _sector_fund_section 逐字节搬迁，改为模块级纯函数。
"""
from __future__ import annotations

import logging

from ..base import Section

logger = logging.getLogger(__name__)


def analyze_sectors(stock_code, stock_name, df, sector_keys) -> list:
    """
    基于真实板块数据（efinance/同花顺）生成板块 Section。
    核心接口 get_belong_board 稳定；板块K线/全市场快照为可选增强（带超时保护）。
    """
    sections = []
    try:
        from portal.analyzers.sector import SectorData
        sd = SectorData()
    except Exception as e:
        logger.warning("SectorData 加载失败: %s", e)
        sections.append(Section(key="sector_membership", title="所属板块",
                                content="板块数据模块加载失败，跳过板块分析。",
                                score=50, signal="hold"))
        return sections

    # Step 1：个股所属板块（稳定核心接口）
    boards = sd.get_stock_boards(stock_code)
    if not boards:
        sections.append(Section(key="sector_membership", title="所属板块",
                                content="⚠️ 未获取到板块归属数据（数据源不可用），板块分析降级跳过。",
                                score=50, signal="hold"))
        return sections

    primary = sd.pick_primary_boards(boards, top=6)
    primary_names = [b["name"] for b in primary]

    # 个股当日涨幅（从 K 线 df 末行取，用于 alpha）
    stock_pct = _stock_today_pct(df)

    # ── 子模块 1：所属板块 ────────────────────────────────
    if "sector_membership" in sector_keys:
        lines = ["**核心题材板块**（按相关性）："]
        for b in primary:
            pct = b.get("pct")
            tag = f"（当日 {pct:+.2f}%）" if isinstance(pct, (int, float)) else ""
            lines.append(f"- {b['name']} {tag}")
        other_names = [b["name"] for b in boards if b["name"] not in primary_names][:8]
        if other_names:
            lines.append("\n**其他归属**：" + "、".join(other_names))
        sections.append(Section(
            key="sector_membership", title="所属板块（真实数据）",
            content="\n".join(lines),
            data={"primary": primary_names, "all": [b["name"] for b in boards]},
            score=50, signal="hold"))

    # ── 子模块 2：板块景气 / 个股相对强弱 ─────────────────
    if "sector_momentum" in sector_keys:
        sections.append(_sector_momentum_section(sd, primary, stock_pct, stock_name))

    # ── 子模块 3：板块资金 / 轮动 ─────────────────────────
    if "sector_fund_flow" in sector_keys:
        sections.append(_sector_fund_section(sd, primary))

    return sections


def _stock_today_pct(df):
    """从 K 线 DataFrame 末行取当日涨跌幅（%）。"""
    if df is None or (hasattr(df, "empty") and df.empty):
        return None
    try:
        cols = {c.lower(): c for c in df.columns}
        if "pct_chg" in cols:
            v = df[cols["pct_chg"]].iloc[-1]
            return round(float(v), 2)
        if "close" in cols and len(df) >= 2:
            c = df[cols["close"]].astype(float)
            return round((c.iloc[-1] / c.iloc[-2] - 1) * 100, 2)
    except Exception:
        pass
    return None


def _sector_momentum_section(sd, primary, stock_pct, stock_name) -> Section:
    """板块景气 + 个股 alpha（个股涨幅 − 板块涨幅）。"""
    if not primary:
        return Section(key="sector_momentum", title="板块景气/相对强弱",
                       content="无核心题材板块，跳过景气分析。", score=50, signal="hold")

    # 用板块当日涨幅（get_belong_board 已含，稳定）算个股 alpha
    board_pcts = [b["pct"] for b in primary if isinstance(b.get("pct"), (int, float))]
    avg_board_pct = round(sum(board_pcts) / len(board_pcts), 2) if board_pcts else None

    lines = []
    score, signal = 50, "hold"

    if stock_pct is not None and avg_board_pct is not None:
        alpha = round(stock_pct - avg_board_pct, 2)
        strength = "跑赢板块 💪" if alpha > 0.5 else ("跑输板块 📉" if alpha < -0.5 else "与板块同步")
        lines.append(f"个股当日 {stock_pct:+.2f}% vs 核心板块均值 {avg_board_pct:+.2f}%，"
                     f"**超额 α = {alpha:+.2f}%（{strength}）**")
        # alpha 映射评分：跑赢偏强
        if alpha > 1.0:   score, signal = 66, "watch"
        elif alpha > 0.3: score, signal = 58, "watch"
        elif alpha < -1.0: score, signal = 38, "hold"
        elif alpha < -0.3: score, signal = 46, "hold"
    elif stock_pct is not None:
        lines.append(f"个股当日涨幅 {stock_pct:+.2f}%（板块涨幅数据缺失，无法算超额）")
    else:
        lines.append("个股/板块涨幅数据不足，暂无法计算相对强弱。")

    # 板块景气：正涨幅板块占比
    if board_pcts:
        up = sum(1 for p in board_pcts if p > 0)
        lines.append(f"核心题材中 {up}/{len(board_pcts)} 个板块当日上涨"
                     f"（{'题材整体活跃' if up > len(board_pcts)/2 else '题材偏弱'}）")

    # 可选：拉主板块 K 线算近 20 日走势（带超时保护，拿不到就跳过）
    try:
        b0 = primary[0]
        kl = sd.get_sector_kline(b0["bk"], b0["name"], days=60)
        if kl is not None and not kl.empty and "close" in kl.columns and len(kl) >= 20:
            c = kl["close"].astype(float)
            ret20 = round((c.iloc[-1] / c.iloc[-20] - 1) * 100, 1)
            lines.append(f"主板块「{b0['name']}」近 20 交易日 {ret20:+.1f}%"
                         f"（{'上升趋势' if ret20 > 0 else '下行趋势'}）")
    except Exception:
        pass

    return Section(key="sector_momentum", title="板块景气/相对强弱",
                   content="\n".join(lines),
                   data={"stock_pct": stock_pct, "avg_board_pct": avg_board_pct},
                   score=score, signal=signal)


def _sector_fund_section(sd, primary) -> Section:
    """板块资金/轮动：用全市场快照给主板块在全市场的涨幅排名。"""
    snap = sd.get_market_snapshot()
    if snap is None or (hasattr(snap, "empty") and snap.empty):
        # 快照不可用 → 用 get_belong_board 的板块涨幅做简单强弱判断
        board_pcts = [(b["name"], b["pct"]) for b in primary if isinstance(b.get("pct"), (int, float))]
        if board_pcts:
            board_pcts.sort(key=lambda x: x[1], reverse=True)
            top = board_pcts[0]
            content = (f"（全市场快照不可用，基于板块当日涨幅）\n"
                       f"核心题材中最强：**{top[0]} {top[1]:+.2f}%**")
            return Section(key="sector_fund_flow", title="板块资金/轮动",
                           content=content, score=50, signal="hold")
        return Section(key="sector_fund_flow", title="板块资金/轮动",
                       content="板块行情快照与涨幅数据均不可用，跳过。", score=50, signal="hold")

    try:
        total = len(snap)
        names = set(b["name"] for b in primary)
        hit = snap[snap["name"].isin(names)] if "name" in snap.columns else snap.iloc[0:0]
        lines = [f"全市场共 {total} 个概念板块。核心题材板块表现："]
        score, signal = 50, "hold"
        if not hit.empty and "pct_chg" in hit.columns:
            ranked = snap.sort_values("pct_chg", ascending=False).reset_index(drop=True) \
                if "pct_chg" in snap.columns else snap
            for _, r in hit.iterrows():
                nm = r["name"]; pc = r.get("pct_chg")
                rank = ranked.index[ranked["name"] == nm].tolist()
                rank_txt = f"全市场第 {rank[0]+1}/{total}" if rank else ""
                tr = r.get("turnover"); vr = r.get("volume_ratio")
                extra = []
                if isinstance(tr, (int, float)): extra.append(f"换手{tr:.1f}%")
                if isinstance(vr, (int, float)): extra.append(f"量比{vr:.2f}")
                lines.append(f"- {nm} {pc:+.2f}% {rank_txt} {' '.join(extra)}")
            # 主板块排名靠前 → 偏强
            best_rank = min((ranked.index[ranked["name"] == n].tolist() or [total])[0] for n in names)
            if best_rank < total * 0.1:   score, signal = 64, "watch"
            elif best_rank < total * 0.3: score, signal = 56, "watch"
            elif best_rank > total * 0.7: score, signal = 42, "hold"
        else:
            lines.append("（核心题材未在快照中匹配到）")
        return Section(key="sector_fund_flow", title="板块资金/轮动",
                       content="\n".join(lines), score=score, signal=signal)
    except Exception as e:
        logger.warning("sector_fund_section error: %s", e)
        return Section(key="sector_fund_flow", title="板块资金/轮动",
                       content="板块资金分析异常，跳过。", score=50, signal="hold")
