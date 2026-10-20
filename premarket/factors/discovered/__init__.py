# -*- coding: utf-8 -*-
"""agent 讨论产出、经内核回测通过后入库的因子（由 discover.py 写入）。

目录约定
--------
- 本目录下的 `f_*.py` 会被自动发现，和内置因子一视同仁地参与午报/晚报筛选。
- 未通过检验或后续退化的，移动到 `_retired/`（不再被加载，但保留证据）。
- 每次提议/评审/入库/淘汰都记录在 `portal/data/premarket/factors_ledger.jsonl`。
"""
