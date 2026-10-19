# -*- coding: utf-8 -*-
"""portal/premarket/selfcheck.py — 统一入口别名

`python -m portal.premarket.selfcheck` 等同于 `python -m portal.premarket.selfcheck_p6`。
（与 `portal.virtual.selfcheck` 的命名保持一致，便于 CI 里一条命令跑全套。）
"""
from __future__ import annotations

import sys

from .selfcheck_p6 import CHECKS, main, run  # noqa: F401

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
