# -*- coding: utf-8 -*-
"""
portal/virtual — 虚拟盘内核（数据 / 特征 / 规则 / 锚点 / 结算 / 闸门）

分层见 UPGRADE_PLAN.md §3.3：

    L0 数据层   minute_data · ths_reason · pools
    L1 特征层   tagging（pre/post 两阶段，防时序泄漏）
    L2 规则层   expr（安全求值）· rules（四态生命周期）
    L3 裁决层   anchor（防作弊）· gate（P3）· settle（P2）

**本 __init__ 故意不 import 任何子模块。**
原因：`paths` 在 import 时就读取 `PORTAL_VIRTUAL_ROOT` 环境变量。若包初始化阶段
就把 paths 拉进来，测试/多账户场景下就再也改不了数据根了。所以子模块一律按需
显式导入：

    from portal.virtual import rules, tagging
    from portal.virtual.anchor import freeze, verify

自检：python -m portal.virtual.selfcheck
"""

__all__ = [
    "paths",
    "expr",
    "rules",
    "anchor",
    "tagging",
    "pools",
    "minute_data",
    "ths_reason",
]
