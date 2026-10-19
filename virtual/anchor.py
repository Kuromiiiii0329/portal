# -*- coding: utf-8 -*-
"""
portal/virtual/anchor.py — 参数锚点链（防作弊）

移植自「无名游资」包的 `anchor.py`（210 行，逻辑几乎原样保留）。

为什么需要它
------------
`plan_*.json` 里原本只有 `params_hash` / `params_frozen_at` —— 它们写在 plan **自己**
里面。谁改参数谁顺手重算哈希，验证链闭合，锁不住任何东西。

    **自我认证 = 没有认证。**

本模块把哈希**外置**到 `anchors.json`，并用链把所有历史锚点串起来：
改任何一条记录，后续所有链哈希都对不上。

约定（不许违反）
----------------
1. 本文件只被 anchor 自己写，结算/回测**只读不写**。
2. 结算前必须 `verify()`，不一致就中止 —— 不是警告，是中止。
3. 规则变更**不许原地改**，必须用新 `plan_id` 开新一轮，否则账本不可比。

诚实标注
--------
若锚定时刻晚于 plan 声明的冻结时刻，只能证明「此后未改」，**不能**证明「当时没改」。
这个限制会打在回执里，不许藏。

用法
----
    python -m portal.virtual.anchor freeze 20261008
    python -m portal.virtual.anchor verify 20261008
    python -m portal.virtual.anchor verify-all
    python -m portal.virtual.anchor list
"""
from __future__ import annotations

import datetime
import hashlib
import json
from typing import Any

from . import paths

GENESIS = "0" * 64


# ─────────────────────────────────────────────────────────────
# 基础
# ─────────────────────────────────────────────────────────────
def _sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha_file(p) -> str:
    return _sha_bytes(p.read_bytes())


def _payload(rec: dict) -> str:
    """链哈希的输入：锁住所有关键字段，按固定顺序拼接。"""
    return "|".join([
        str(rec.get("plan_id", "")),
        str(rec.get("plan_sha256", "")),
        str(rec.get("params_hash", "")),
        str(rec.get("params_version", "")),
        str(rec.get("params_frozen_at", "")),
        str(rec.get("anchored_at", "")),
        str(rec.get("prev", "")),
    ])


def chain_hash(rec: dict) -> str:
    return _sha_bytes(_payload(rec).encode("utf-8"))


def load() -> dict:
    p = paths.ANCHORS
    if not p.exists():
        return {"version": 1, "genesis": GENESIS, "order": [], "records": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def save(d: dict) -> None:
    paths.ensure_dirs()
    paths.ANCHORS.write_text(
        json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8"
    )


# ─────────────────────────────────────────────────────────────
# 冻结
# ─────────────────────────────────────────────────────────────
def freeze(plan_id: str, note: str = "") -> dict:
    """把 plan 当前状态的哈希追加进锚点链。已锚定且内容未变则幂等返回。"""
    pf = paths.plan_path(plan_id)
    if not pf.exists():
        raise FileNotFoundError(f"找不到 {pf}（先把 plan 写到 data/virtual/plans/）")

    plan = json.loads(pf.read_text(encoding="utf-8"))
    d = load()

    if plan_id in d["records"]:
        old = d["records"][plan_id]
        new_sha = sha_file(pf)
        if old["plan_sha256"] == new_sha:
            return old
        raise RuntimeError(
            f"!! {plan_id} 已有锚点，但 plan 内容已变。\n"
            f"   锚定时 sha256 = {old['plan_sha256']}\n"
            f"   当前   sha256 = {new_sha}\n"
            f"   规则变更必须用新 plan_id 重开一轮，不许原地改。"
        )

    prev = d["order"][-1] if d["order"] else GENESIS
    prev_h = d["records"][prev]["chain_sha256"] if d["order"] else GENESIS
    rec: dict[str, Any] = {
        "plan_id": plan_id,
        "plan_file": pf.name,
        "plan_sha256": sha_file(pf),
        "params_hash": plan.get("params_hash"),
        "params_version": plan.get("params_version"),
        "params_frozen_at": plan.get("params_frozen_at"),
        "anchored_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "prev": prev_h,
        "note": note,
    }
    rec["chain_sha256"] = chain_hash(rec)
    d["records"][plan_id] = rec
    d["order"].append(plan_id)
    d["head"] = rec["chain_sha256"]
    save(d)
    return rec


# ─────────────────────────────────────────────────────────────
# 校验
# ─────────────────────────────────────────────────────────────
def verify_chain(d: dict | None = None) -> list[str]:
    """校验链完整性。返回问题列表（空 = 完整）。"""
    d = d or load()
    bad: list[str] = []
    prev = d.get("genesis", GENESIS)
    for pid in d.get("order", []):
        rec = d["records"].get(pid)
        if not rec:
            bad.append(f"{pid}: 在 order 中但缺记录")
            continue
        if rec.get("prev") != prev:
            bad.append(f"{pid}: prev 断链（记录 {str(rec.get('prev'))[:12]}… vs 应为 {prev[:12]}…）")
        if chain_hash(rec) != rec.get("chain_sha256"):
            bad.append(f"{pid}: 记录内容被改（chain_sha256 不匹配）")
        prev = rec.get("chain_sha256", "")
    if d.get("head") and d.get("order") and d["head"] != d["records"][d["order"][-1]]["chain_sha256"]:
        bad.append("head 与链尾不一致")
    return bad


def verify(plan_id: str, plan: dict | None = None, strict: bool = True) -> tuple[bool, str]:
    """校验单个 plan。返回 (是否通过, 人类可读说明)。

    与移植前相比的改动：原版用 print + bool，这里改成返回 (bool, msg)，
    让调用方（结算引擎）能把失败原因写进日志/报告，而不是只往 stdout 丢一行。
    """
    d = load()
    rec = d["records"].get(plan_id)
    if not rec:
        msg = f"{plan_id} 没有任何锚点 —— 无法证明参数在冻结后未被修改。"
        if strict:
            msg += f" 请先运行：python -m portal.virtual.anchor freeze {plan_id}"
            return False, msg
        return True, f"{plan_id} 未锚定（strict=False 放行）"

    bad = verify_chain(d)
    if bad:
        return False, "锚点链已损坏，整个账本不可信：" + "；".join(bad)

    pf = paths.PLANS_DIR / rec["plan_file"]
    if not pf.exists():
        return False, f"锚点指向的 {rec['plan_file']} 不存在"

    cur = sha_file(pf)
    if cur != rec["plan_sha256"]:
        return False, (
            f"{plan_id} 的 plan 文件在锚定后被修改过："
            f"锚定 {rec['plan_sha256'][:16]}… 当前 {cur[:16]}…"
        )

    if plan is not None:
        for k in ("params_hash", "params_version"):
            if plan.get(k) != rec.get(k):
                return False, (
                    f"{plan_id} 的 {k} 与锚点不符：plan={plan.get(k)} 锚点={rec.get(k)}"
                )

    msg = (f"{plan_id} 通过：plan 未被改动，"
           f"params {rec['params_version']}/{rec['params_hash']}")
    lag = _freeze_lag(rec)
    if lag:
        msg += "；" + lag
    return True, msg


def _freeze_lag(rec: dict) -> str:
    """若锚定时刻晚于声明的冻结时刻，如实提示。"""
    declared = rec.get("params_frozen_at")
    anchored = rec.get("anchored_at")
    if not declared or not anchored:
        return ""
    try:
        d_dt = datetime.datetime.strptime(str(declared)[:16], "%Y-%m-%d %H:%M")
        a_dt = datetime.datetime.strptime(str(anchored)[:16], "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return ""
    if a_dt > d_dt:
        return (f"注意：锚定（{anchored}）晚于声明的冻结时刻（{declared}），"
                f"只能证明『此后未改』，不能证明『当时没改』")
    return ""


def head() -> str:
    return str(load().get("head") or GENESIS)


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    import sys

    args = [a for a in (argv if argv is not None else sys.argv[1:]) if not a.startswith("-")]
    cmd = args[0] if args else "list"

    if cmd == "freeze":
        if len(args) < 2:
            print("用法：python -m portal.virtual.anchor freeze <plan_id>")
            return 2
        rec = freeze(args[1])
        print(f"已锚定 {rec['plan_id']}：plan_sha256={rec['plan_sha256'][:16]}… "
              f"链哈希={rec['chain_sha256'][:16]}…")
        lag = _freeze_lag(rec)
        if lag:
            print("  " + lag)
        return 0

    if cmd == "verify":
        if len(args) < 2:
            print("用法：python -m portal.virtual.anchor verify <plan_id>")
            return 2
        ok, msg = verify(args[1])
        print(("[锚点] " if ok else "[失败] ") + msg)
        return 0 if ok else 1

    if cmd == "verify-all":
        d = load()
        bad = verify_chain(d)
        print(f"链完整性：{'损坏' if bad else '完整'}（共 {len(d.get('order', []))} 条）")
        for b in bad:
            print("  -", b)
        n_ok = 0
        for pid in d.get("order", []):
            pf = paths.PLANS_DIR / d["records"][pid]["plan_file"]
            ok = pf.exists() and sha_file(pf) == d["records"][pid]["plan_sha256"]
            print(f"  {pid}: {'OK' if ok else 'FAIL ← plan 文件已变'}")
            n_ok += bool(ok)
        print(f"通过 {n_ok}/{len(d.get('order', []))}")
        return 0 if (not bad and n_ok == len(d.get("order", []))) else 1

    d = load()
    print(f"锚点数量：{len(d.get('order', []))}   链 head：{head()[:16]}…")
    for pid in d.get("order", []):
        r = d["records"][pid]
        print(f"  {pid}  params {r['params_version']}/{r['params_hash']}  "
              f"plan_sha {r['plan_sha256'][:12]}…  锚定于 {r['anchored_at']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
