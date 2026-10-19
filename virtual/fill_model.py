# -*- coding: utf-8 -*-
"""
portal/virtual/fill_model.py — 成交判定与成本模型

打板最大的自欺是「假设涨停价一定能买到」。现实是一字板你排在几万手后面，
根本轮不到。所以成交必须**建模**，不能拍概率。

三个部件
--------
1. `judge_auction()`  —— 集合竞价买入的 R0–R4 判定（SKILL §7.2 原样保留）
2. `QueueFill`        —— 涨停价排队撮合（移植打板工具包 `paper.py` 的核心洞察）
3. `buy_fee/sell_fee` —— 费用模型；佣金有**单笔最低 5 元**，分批越碎实际费率越高

排队撮合为什么比「拍概率」诚实
------------------------------
一对一错很难自证，但机制可以：

    挂单时你在涨停价排第 `queue_pos` 手（= 当时的买一量），
    同时记录此刻的累计成交量 `vol_at_place`。
    只有当「封板之后新成交的量」累计超过你的排队位置，你的单才会被吃掉：

        成交条件 = (当前累计成交量 − vol_at_place) >= queue_pos

    一字板封死 → 后面没有新成交量 → 永远轮不到你（物理正确）；
    炸板放量   → 新成交量迅速超过排队量 → 自然成交（物理正确）。

这个模型不需要「假设成交概率」，只需要两个可观测量。

费用口径（SKILL §3.6）
----------------------
    买入 = max(金额 × 万2.5, 5) + 金额 × 0.001%（过户费）
    卖出 = max(金额 × 万2.5, 5) + 金额 × 0.001%（过户费） + 金额 × 0.05%（印花税）

**佣金最低 5 元**这一条很要命：单笔 1 万元时万 2.5 只有 2.5 元，被抬到 5 元，
实际费率翻倍。方案里必须把这笔成本算出来，而不是假设「万 2.5」。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_FEE = {
    "commission_rate": 0.00025,   # 万 2.5
    "commission_min": 5.0,        # 单笔最低 5 元
    "stamp_rate": 0.0005,         # 印花税 0.05%（仅卖出）
    "transfer_rate": 0.00001,     # 过户费 0.001%（双向）
    "lot_size": 100,              # 沪深主板/创业板买入须 100 股整数倍
}


# ─────────────────────────────────────────────────────────────
# 费用
# ─────────────────────────────────────────────────────────────
def buy_fee(amount: float, fee: dict | None = None) -> float:
    f = {**DEFAULT_FEE, **(fee or {})}
    a = max(0.0, float(amount))
    return max(a * f["commission_rate"], f["commission_min"]) + a * f["transfer_rate"]


def sell_fee(amount: float, fee: dict | None = None) -> float:
    f = {**DEFAULT_FEE, **(fee or {})}
    a = max(0.0, float(amount))
    return (max(a * f["commission_rate"], f["commission_min"])
            + a * f["transfer_rate"] + a * f["stamp_rate"])


def round_lots(target_amount: float, price: float, fee: dict | None = None) -> int:
    """按目标金额算股数，向下取整到 100 股的整数倍。"""
    f = {**DEFAULT_FEE, **(fee or {})}
    lot = int(f["lot_size"])
    if price is None or price <= 0 or target_amount is None or target_amount <= 0:
        return 0
    raw = int(float(target_amount) / float(price))
    return (raw // lot) * lot


# ─────────────────────────────────────────────────────────────
# 集合竞价判定（R0–R4）
# ─────────────────────────────────────────────────────────────
@dataclass
class AuctionVerdict:
    ok: bool
    rule: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "rule": self.rule, "reason": self.reason}


def judge_auction(
    auction_pct: float | None,
    *,
    max_chase_pct: float = 7.0,
    sealed_pct: float = 9.5,
    order_amount: float | None = None,
    auction_amount: float | None = None,
    liquidity_max_ratio: float = 0.05,
) -> AuctionVerdict:
    """按 R0–R4 判定集合竞价能否成交。

    Args:
        auction_pct: 竞价涨幅%（相对昨收）
        max_chase_pct: 追高上限（R4）
        sealed_pct: 视为「竞价即涨停」的阈值（R3）
        order_amount: 我的委托额
        auction_amount: 竞价成交额（用于 R0 流动性判定）

    判定顺序：R0（流动性）→ R2（承接失败）→ R3（封死）→ R4（不追高）→ R1（成交）。
    """
    if auction_pct is None:
        return AuctionVerdict(False, "NODATA", "取不到竞价价，无法判定（不猜）")

    # R0：委托额占竞价成交额太高 → 排不上 / 冲击成本高
    if (order_amount and auction_amount and auction_amount > 0
            and order_amount / auction_amount > liquidity_max_ratio):
        return AuctionVerdict(
            False, "R0",
            f"委托额/竞价成交额 = {order_amount / auction_amount:.1%} > "
            f"{liquidity_max_ratio:.0%}，竞价盘子太薄")

    if auction_pct <= 0:
        return AuctionVerdict(False, "R2", f"竞价 {auction_pct:+.2f}% ≤ 0%，承接失败")
    if auction_pct >= sealed_pct:
        return AuctionVerdict(False, "R3", f"竞价 {auction_pct:+.2f}% ≥ {sealed_pct}%，排在封单后面")
    if auction_pct > max_chase_pct:
        return AuctionVerdict(False, "R4", f"竞价 {auction_pct:+.2f}% > {max_chase_pct}%，不追高")
    return AuctionVerdict(True, "R1", f"竞价 {auction_pct:+.2f}% ∈ (0%, {max_chase_pct}%]，成交")


# ─────────────────────────────────────────────────────────────
# 排队撮合
# ─────────────────────────────────────────────────────────────
@dataclass
class PendingOrder:
    """涨停价挂单。记录排队位置，等后续成交量把自己吃掉。"""
    code: str
    name: str
    price: float
    shares: int
    placed_at: str = ""
    queue_pos: float = 0.0       # 挂单时的买一量（手）
    vol_at_place: float = 0.0    # 挂单时的累计成交量（手）
    filled: bool = False
    fill_at: str | None = None
    note: str = ""

    def amount(self) -> float:
        return round(self.price * self.shares, 2)

    def can_fill(self, cum_volume: float) -> bool:
        """成交条件：(当前累计成交量 − 挂单时累计) >= 排队位置。"""
        if self.filled or cum_volume is None:
            return False
        return (float(cum_volume) - self.vol_at_place) >= self.queue_pos


@dataclass
class QueueFillBook:
    """一组排队挂单的撮合簿。喂成交量，出成交。"""
    orders: dict[str, PendingOrder] = field(default_factory=dict)

    def place(self, order: PendingOrder) -> PendingOrder:
        self.orders[order.code] = order
        return order

    def feed(self, code: str, cum_volume: float, at: str = "") -> bool:
        """喂一次累计成交量；成交返回 True（只成交一次）。"""
        o = self.orders.get(code)
        if o is None or o.filled:
            return False
        if o.can_fill(cum_volume):
            o.filled = True
            o.fill_at = at or None
            return True
        return False

    def unfilled(self) -> list[PendingOrder]:
        return [o for o in self.orders.values() if not o.filled]

    def reset(self, code: str) -> None:
        self.orders.pop(code, None)


def simulate_limit_up_queue(
    *,
    code: str,
    name: str,
    limit_price: float,
    shares: int,
    tape: list[tuple[str, float, float]],
    queue_pos: float,
) -> PendingOrder:
    """用分时/逐笔喂一个涨停排队单，返回最终状态。

    Args:
        tape: [(时间, 价格, 累计成交量), ...] 按时间升序
              —— 累计成交量指该票当日的累计成交量（手）
        queue_pos: 挂单时刻的买一量（手）

    Returns:
        PendingOrder（`filled` 表示是否成交，`fill_at` 是成交时刻）
    """
    order = PendingOrder(
        code=code, name=name, price=float(limit_price), shares=int(shares),
        queue_pos=float(queue_pos),
        vol_at_place=float(tape[0][2]) if tape else 0.0,
        placed_at=tape[0][0] if tape else "",
    )
    book = QueueFillBook()
    book.place(order)
    for t, _price, cum in tape:
        if book.feed(code, cum, at=t):
            break
    return order


__all__ = [
    "DEFAULT_FEE",
    "AuctionVerdict",
    "PendingOrder",
    "QueueFillBook",
    "buy_fee",
    "sell_fee",
    "round_lots",
    "judge_auction",
    "simulate_limit_up_queue",
]
