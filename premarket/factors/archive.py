# -*- coding: utf-8 -*-
"""
portal/premarket/factors/archive.py — 因子归档（废弃不删除）

为什么是归档而不是删除
--------------------
一个因子被淘汰，往往不是因为"它没算出来"，而是因为**它的假设在最近这段数据上
不成立**。这类信息对以后很有价值：改天市场风格切换，同一个因子可能重新有效；
而且"当初为什么淘汰它"本身是经验来源。所以一律移动到 `factors/_archive/`，
并写一份 `<id>.meta.json` 记下时间、原因、当时的回测数字与评分。

目录
----
    factors/
        f_*.py                 活跃因子
        discovered/f_*.py      agent 产出、已入库
        _archive/              归档（不再被加载，但完整保留）
            f_xxx.py
            f_xxx.meta.json

上限
----
`MAX_FACTORS = 15`（写在 meeting.py，用户可改）。
超过上限时按"评分最低且没有战法依赖"的顺序归档；如果所有因子都被战法依赖，
则**拒绝新增**而不是硬删 —— 硬删会让战法编译失败。
"""
from __future__ import annotations

import datetime
import json
import shutil
from pathlib import Path
from typing import Any

_DIR = Path(__file__).parent
DISCOVERED = _DIR / "discovered"
ARCHIVE = _DIR / "_archive"


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _read(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def active_factor_files() -> dict[str, Path]:
    """{factor_id: 文件路径}，只含活跃因子（不含 _archive）。"""
    out: dict[str, Path] = {}
    for p in _DIR.glob("f_*.py"):
        out[p.stem[2:]] = p
    if DISCOVERED.is_dir():
        for p in DISCOVERED.glob("f_*.py"):
            out[p.stem[2:]] = p
    return out


def archive(fid: str, *, reason: str, stats: dict | None = None,
            score: float | None = None, log=print) -> dict[str, Any]:
    """把一个因子移入归档目录，并写下原因与当时的数字。"""
    files = active_factor_files()
    src = files.get(fid)
    if src is None:
        return {"ok": False, "reason": f"没有活跃因子 {fid}"}
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    dst = ARCHIVE / src.name
    try:
        shutil.move(str(src), str(dst))
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    meta = {"id": fid, "archived_at": _now(), "reason": reason,
            "from": str(src.relative_to(_DIR)), "stats": stats or {},
            "score": score}
    (ARCHIVE / f"{fid}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"   📦 归档 {fid}：{reason}")
    return {"ok": True, "path": str(dst), "meta": meta}


def archived() -> list[dict]:
    """归档清单（按归档时间倒序）。"""
    out = []
    if not ARCHIVE.is_dir():
        return out
    for m in ARCHIVE.glob("*.meta.json"):
        d = _read(m)
        if d:
            out.append(d)
    out.sort(key=lambda x: x.get("archived_at") or "", reverse=True)
    return out


def migrate_legacy(log=print) -> list[str]:
    """把旧的 discovered/_retired/ 迁移到统一的 _archive/。"""
    legacy = DISCOVERED / "_retired"
    moved = []
    if not legacy.is_dir():
        return moved
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    for p in list(legacy.glob("f_*.py")):
        fid = p.stem[2:]
        dst = ARCHIVE / p.name
        shutil.move(str(p), str(dst))
        (ARCHIVE / f"{fid}.meta.json").write_text(
            json.dumps({"id": fid, "archived_at": _now(),
                        "reason": "历史淘汰（迁移自 discovered/_retired）",
                        "from": f"discovered/_retired/{p.name}"},
                       ensure_ascii=False, indent=1), encoding="utf-8")
        moved.append(fid)
    for p in legacy.glob("*.json"):
        p.unlink(missing_ok=True)
    try:
        legacy.rmdir()
    except OSError:
        pass
    if moved:
        log(f"   📦 迁移历史归档 {len(moved)} 个：{moved}")
    return moved


def _used_by_plays() -> tuple[set[str], dict[str, float]]:
    """哪些因子被活跃战法引用；以及每个因子的评分。"""
    from . import load_factors
    from . import playbooks as PB
    from .catalog import load_stats, score as calc_score

    factors = load_factors()
    stats = load_stats()
    used: set[str] = set()
    for p in PB.load_all():
        for cond in p.get("conditions") or []:
            for fid in factors:
                if fid in cond:
                    used.add(fid)
        for fid in p.get("evidence") or []:
            used.add(fid)
    scores = {fid: calc_score(stats.get(fid)) for fid in factors}
    return used, scores


def ensure_room(cap: int, *, log=print) -> dict[str, Any]:
    """保证活跃因子数不超过 cap。返回 {ok, archived, reason}。

    挑选顺序：
      1. 未被任何战法引用、评分最低的（优先 discovered，因为它们本来就是实验品）
      2. 若全都还被战法引用 → 拒绝，而不是硬删（硬删会让战法编译失败）
    """
    from . import load_factors
    migrate_legacy(log=log)
    n = len(active_factor_files())
    if n < cap:
        return {"ok": True, "archived": [], "n": n,
                "reason": f"{n} < 上限 {cap}"}

    to_archive: list[str] = []
    while n > cap - 1:      # 腾出至少 1 个位置
        used, scores = _used_by_plays()
        factors = load_factors(refresh=True)
        pool = [fid for fid in factors if fid not in used]
        if not pool:
            return {"ok": False, "archived": to_archive, "n": n,
                    "reason": f"所有 {len(factors)} 个因子都被战法引用，"
                              f"无法腾出位置（上限 {cap}）"}
        # discovered 优先、评分低的优先
        pool.sort(key=lambda fid: (
            0 if (factors[fid].meta.get("source") == "discovered") else 1,
            scores.get(fid, 0.0)))
        victim = pool[0]
        st = load_factors(refresh=True)[victim]
        r = archive(victim, reason=f"因子数超上限 {cap}，评分最低且无战法依赖",
                    stats=None, score=scores.get(victim), log=log)
        if not r.get("ok"):
            return {"ok": False, "archived": to_archive, "n": n,
                    "reason": r.get("reason")}
        to_archive.append(victim)
        n = len(active_factor_files())
    return {"ok": True, "archived": to_archive, "n": n,
            "reason": f"已归档 {len(to_archive)} 个，剩余 {n} 个"}


__all__ = ["ARCHIVE", "active_factor_files", "archive", "archived",
           "migrate_legacy", "ensure_room"]
