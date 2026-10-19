# -*- coding: utf-8 -*-
"""
portal/backtester.py — 信号回测入口（薄封装，口径实现在 backtest_core）

本文件曾经自己实现收益计算，存在 6 处「不报错、只出错数」的口径错误
（无 T+1、无成本、无基准、无中位数/t 值、无复权校验、依赖 pct_chg）。
现已全部迁到 `backtest_core.py`，并配了 11 条自检（见 `backtest_audit.py`）。

对外契约**保持不变**：
    run_backtest(df, forward_days=(1,3,5,10,20), code=None) -> dict
    返回包含 signals / total_days / stock_days，且每个信号的 stats[h] 里
    仍有 win_rate / avg_return / valid —— 所以 srv/http_handler.py、
    srv/tasks.py、srv/prompts.py 无需改动即可继续工作。

新增字段（下游可选用）：
    caliber / baseline / data_quality / warnings
    以及每个信号 stats[h] 的 median_return、t_stat、excess_return；信号级的
    count_used、excluded、by_year、same_sign_years、insufficient。

自检：
    python portal/backtest_audit.py --real
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pandas as pd

try:  # 包内导入 `portal.backtester`
    from portal import backtest_core as _core
except ImportError:  # 直接跑脚本 / 独立目录
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import backtest_core as _core  # type: ignore[no-redef]

__all__ = [
    "run_backtest",
    "summarize_for_prompt",
    "is_current_caliber",
    "SIGNAL_NAMES",
    "COST_ROUND_TRIP",
    "CALIBER",
    "CALIBER_VERSION",
]

# 兼容旧名：旧的 SIGNALS 是 [(名称, 单行判定函数)]，外部无人依赖；
# 这里改为只导出名称清单，避免留下一个「看起来还能用」的半成品。
SIGNAL_NAMES: list[str] = _core.signal_names()
COST_ROUND_TRIP: float = _core.COST_ROUND_TRIP
CALIBER: dict[str, Any] = _core.CALIBER
CALIBER_VERSION: int = _core.CALIBER_VERSION

# 旧版内部函数名，保留别名以免外部脚本 import 失败
_compute_indicators = _core.compute_indicators


def is_current_caliber(result: Any) -> bool:
    """判断一份（可能来自磁盘缓存的）回测结果是否用的是当前口径。

    `data_cache` 会把回测结果落盘复用。口径一旦修正，旧缓存如果照读，就会把
    错误的数字继续喂给报告和 LLM —— 所以调用方必须先过这一关，不符就重算。
    """
    if not isinstance(result, dict):
        return False
    return result.get("caliber_version") == CALIBER_VERSION


def run_backtest(
    df: pd.DataFrame,
    forward_days: tuple[int, ...] | list[int] = _core.DEFAULT_HORIZONS,
    code: str | None = None,
) -> dict[str, Any]:
    """对单只股票的日线运行 9 个技术信号的持有期回测。

    Args:
        df: 日线 K 线（date/open/high/low/close/volume...），行序无所谓，内部会排序
        forward_days: 持有交易日数
        code: 股票代码；传入后可按板块推断涨跌停幅度，从而正确剔除「次日开盘涨停」
              这类买不进的样本。不传按主板 10% 处理。

    Returns:
        dict，字段见模块 docstring。口径见 `CALIBER`。
    """
    return _core.run_backtest(df, forward_days=forward_days, code=code)


def summarize_for_prompt(result: dict[str, Any],
                        horizons: tuple[int, ...] | list[int] = _core.DEFAULT_HORIZONS) -> str:
    """把回测结果压成带口径声明的几行文本，供 LLM prompt 使用。"""
    return _core.summarize_for_prompt(result, horizons)


if __name__ == "__main__":  # 手工体验：python portal/backtester.py 002466
    import json

    argv = sys.argv[1:]
    root = Path(__file__).resolve().parent / "data" / "stocks"
    target = argv[0] if argv else (sorted(p.name for p in root.glob("*"))[0] if root.exists() else "")
    csv_path = root / target / "kline.csv"
    if not csv_path.exists():
        print(f"找不到 {csv_path}，请先对该股票跑一次深度分析以建立缓存")
        raise SystemExit(2)

    frame = pd.read_csv(csv_path)
    res = run_backtest(frame, code=target)
    print(summarize_for_prompt(res) or "(无信号统计)")
    print("\n数据体检：", json.dumps(res.get("data_quality"), ensure_ascii=False, indent=2))
