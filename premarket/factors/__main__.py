# -*- coding: utf-8 -*-
"""
portal/premarket/factors/__main__.py — 因子库命令行

    python -m portal.premarket.factors list              列出所有因子
    python -m portal.premarket.factors eval [--date D]   今日截面的因子值（前 N 只）
    python -m portal.premarket.factors playbooks         列出战法与条件
    python -m portal.premarket.factors hit [--date D]    用战法跑一遍，看命中
    python -m portal.premarket.factors validate [--days 20]  统计检验（离线，慢）
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys


def _conn():
    from portal.virtual import marketdb as M
    return M.connect()


def _latest(conn) -> tuple[str, str]:
    from portal.virtual import marketdb as M
    iso = M.latest_trade_date(conn)
    return (iso or ""), (iso or "").replace("-", "")


def _quotes(conn, d8: str) -> list[dict]:
    """离线回放：直接用库里该日的收盘价当"今日行情"。"""
    rows = conn.execute(
        "SELECT code, close, volume, amount FROM day WHERE date = ?", (d8,)
    ).fetchall() if False else None
    iso = f"{d8[:4]}-{d8[4:6]}-{d8[6:8]}"
    bars = conn.execute(
        "SELECT code, open, high, low, close, volume, amount FROM day "
        "WHERE date = ?", (iso,)
    ).fetchall()
    out = []
    for b in bars:
        prev = conn.execute(
            "SELECT close FROM day WHERE code = ? AND date < ? "
            "ORDER BY date DESC LIMIT 1", (b["code"], iso)).fetchone()
        if not prev or not prev["close"]:
            continue
        out.append({"code": b["code"], "open": b["open"], "high": b["high"],
                    "low": b["low"], "close": b["close"],
                    "prev_close": prev["close"], "volume": b["volume"],
                    "amount": b["amount"]})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m portal.premarket.factors")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("list", help="列出因子")
    sub.add_parser("playbooks", help="列出战法")

    e = sub.add_parser("eval", help="今日因子截面")
    e.add_argument("--date", default=None, help="YYYYMMDD（默认库内最新日）")
    e.add_argument("--top", type=int, default=5)

    h = sub.add_parser("hit", help="用战法跑一遍")
    h.add_argument("--date", default=None)
    h.add_argument("--json", action="store_true")

    k = sub.add_parser("check", help="自检因子库：加载/编译/当日命中")
    k.add_argument("--date", default=None)

    d = sub.add_parser("discover", help="开因子会议：提议→批评→回测→入库")
    d.add_argument("--max", type=int, default=3, help="最多提议几个")
    d.add_argument("--days", type=int, default=10, help="回测天数")
    d.add_argument("--dry-run", action="store_true", help="只提议+评审，不落盘")
    d.add_argument("--json", action="store_true")

    w = sub.add_parser("weekly", help="阶段验证：复验已入库因子，退化的下线")
    w.add_argument("--days", type=int, default=5)
    w.add_argument("--json", action="store_true")

    l = sub.add_parser("ledger", help="查看因子会议与入库历史")
    l.add_argument("--tail", type=int, default=20)

    v = sub.add_parser("validate", help="因子统计检验（慢）")
    v.add_argument("--days", type=int, default=20)
    v.add_argument("--id", default=None, help="只测某个因子")
    v.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)

    from . import factor_summary, load_factors

    if args.cmd in (None, "list"):
        rows = factor_summary()
        print(f"{'id':<20}{'名称':<16}{'类型':<8}{'方向':<8}{'来源':<12}说明")
        for r in rows:
            print(f"{r['id']:<20}{r['name']:<16}{r['kind']:<8}"
                  f"{r['direction']:<8}{r['source']:<12}{r['description'][:44]}")
        print(f"\n共 {len(rows)} 个因子")
        return 0

    if args.cmd == "playbooks":
        from . import playbooks as PB
        plays = PB.load_all()
        print(f"共 {len(plays)} 个战法（内置 + 因子文件贡献）\n")
        for p in plays:
            flag = "可买" if p.get("buyable") else "仅观察"
            src = p.get("source") or "内置 playbooks.py"
            print(f"[{p['id']}] {p['name']}（{flag}）")
            print(f"  来源：{src}")
            print(f"  逻辑：{p.get('why')}")
            for c in p.get("conditions") or []:
                print(f"    - {c}")
            print()
        return 0

    conn = _conn()
    if conn is None:
        print(json.dumps({"ok": False, "error": "market.db 不可用"},
                         ensure_ascii=False))
        return 1
    iso, d8 = _latest(conn)
    # validate 没有 --date（它跑多日），别用 args.date 一把抓
    if getattr(args, "date", None):
        d8 = args.date
        iso = f"{d8[:4]}-{d8[4:6]}-{d8[6:8]}"
    if not iso:
        print(json.dumps({"ok": False, "error": "库里没有数据"}, ensure_ascii=False))
        return 1

    if args.cmd == "eval":
        from . import build_panel, evaluate
        quotes = _quotes(conn, d8)
        panel = build_panel(conn, quotes, iso)
        vals = evaluate(panel)
        print(f"截面日期 {iso}，股票 {len(panel.close)} 只，因子 {len(vals)} 个\n")
        for fid, series in vals.items():
            items = [(c, v) for c, v in series.items() if isinstance(v, (int, float))]
            items.sort(key=lambda t: -float(t[1]))
            top = "、".join(f"{panel.names.get(c, c)}({v})" for c, v in items[:3])
            print(f"{fid:<18} 覆盖 {len(items):>5}  最高: {top[:80]}")
        return 0

    if args.cmd == "hit":
        from ..screen import screen
        quotes = _quotes(conn, d8)
        cands, why = screen(conn, quotes, iso, log=lambda m: print(m))
        if why:
            print(f"\n不可用：{why}")
        print(f"\n命中 {len(cands)} 只：")
        for c in cands:
            print(f"  [{c['playbook_cn']}] {c['code']} {c.get('name')} "
                  f"涨{c.get('chg_pct')}% 量比{c['metrics'].get('vol_ratio')}")
            for e in c.get("setup") or []:
                print(f"      · {e}")
        if args.json:
            print(json.dumps(cands, ensure_ascii=False, indent=1)[:4000])
        return 0

    if args.cmd == "discover":
        from .meeting import run_meeting
        conn = _conn()
        if conn is None:
            print(json.dumps({"ok": False, "error": "market.db 不可用"},
                             ensure_ascii=False))
            return 1
        out = run_meeting(conn=conn, max_n=args.max, days=args.days,
                          dry_run=args.dry_run)
        conn.close()
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=1))
        else:
            print(f"\n提议 {out.get('proposed')} 个，入库 {out.get('accepted')} 个")
            for r in out.get("results") or []:
                print(f"  {'✓' if r.get('ok') else '✗'} {r.get('id'):<22}"
                      f"{r.get('stage') or 'gate':<10}{str(r.get('reason'))[:70]}")
        return 0 if out.get("ok") else 1

    if args.cmd == "weekly":
        from .meeting import weekly
        conn = _conn()
        if conn is None:
            print(json.dumps({"ok": False, "error": "market.db 不可用"},
                             ensure_ascii=False))
            return 1
        out = weekly(conn=conn, days=args.days)
        conn.close()
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=1))
        else:
            print(f"复验 {out.get('checked')} 个，下线 {len(out.get('retired') or [])} 个")
            for rid in out.get("retired") or []:
                print(f"  ✗ {rid}")
        return 0 if out.get("ok") else 1

    if args.cmd == "ledger":
        from .meeting import ledger_read
        rows = ledger_read()[-args.tail:]
        for r in rows:
            ev = r.get("event")
            if ev == "proposed":
                ids = [p.get("id") for p in (r.get("proposals") or [])]
                print(f"{r['at']}  提议 {ids}")
            elif ev == "reviewed":
                for k, v in (r.get("reviews") or {}).items():
                    print(f"{r['at']}  评审 {k}: {v.get('verdict')}")
            elif ev == "gated":
                print(f"{r['at']}  {'入库' if r.get('ok') else '淘汰'} "
                      f"{r.get('id')}: {str(r.get('reason'))[:60]}")
            elif ev == "weekly":
                print(f"{r['at']}  周检 复验{r.get('checked')} "
                      f"下线{r.get('retired')}")
            else:
                print(f"{r['at']}  {ev} {json.dumps(r, ensure_ascii=False)[:90]}")
        return 0

    if args.cmd == "check":
        from . import load_factors
        from . import playbooks as PB
        from ..screen import screen
        from .. import collect as CM

        ok = True
        factors = load_factors(refresh=True)
        print(f"因子 {len(factors)} 个：")
        for f in factors.values():
            src = f.meta.get("source") or "builtin"
            print(f"  {f.id:<18} {f.meta.get('name'):<14} {f.kind:<8} {src}")
        plays = PB.load_all()
        print(f"\n战法 {len(plays)} 个：")
        try:
            exprs = PB.compiled(factor_ids=set(factors), plays=plays)
        except Exception as exc:
            print(f"  ✗ 条件编译失败：{exc}")
            return 1
        for p in plays:
            src = "因子文件贡献" if p.get("source") else "内置"
            n = len(exprs.get(p["id"], []))
            flag = "可买" if p.get("buyable") else "仅观察"
            print(f"  {p['id']:<22} {p.get('name'):<18} {flag:<6} "
                  f"{n} 条条件  [{src}]")
            try:
                PB.check_no_chase(p)
            except Exception as exc:
                print(f"    ✗ {exc}")
                ok = False

        # 当日命中
        conn = _conn()
        if conn is not None:
            iso, d8 = _latest(conn)
            if args.date:
                d8 = args.date
                iso = f"{d8[:4]}-{d8[4:6]}-{d8[6:8]}"
            quotes = _quotes(conn, d8)
            cands, why = screen(conn, quotes, iso, log=lambda m: print(m))
            print(f"\n{iso} 命中：{len(cands)} 只"
                  f"（可买 {sum(1 for c in cands if c.get('buyable'))}）")
            if why:
                print(f"  ! {why}")
            from collections import Counter
            for pid, n in Counter(c["playbook"] for c in cands).items():
                print(f"  {pid:<24} {n} 只")
            for c in cands[:5]:
                print(f"    ★ {c['code']} {c.get('name')} {c.get('chg_pct')}% "
                      f"[{c.get('playbook_cn')}] {'；'.join(c.get('setup') or [])[:80]}")
            conn.close()
        print("\n自检结果：", "通过" if ok else "有失败项")
        return 0 if ok else 1

    if args.cmd == "validate":
        from .validate import run_all
        res = run_all(days=args.days, conn=conn,
                      ids=[args.id] if args.id else None)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=1))
        else:
            print(f"\n{'因子':<20}{'IC':>8}{'IR':>8}{'正日占比':>10}"
                  f"{'覆盖率':>8}{'多空%':>9}  结论")
            for r in res:
                print(f"{r['id']:<20}{r.get('ic_mean') or 0:>8.4f}"
                      f"{r.get('ic_ir') or 0:>8.3f}{r.get('ic_pos_ratio') or 0:>10.2f}"
                      f"{r.get('coverage') or 0:>8.2f}"
                      f"{(r.get('long_short_pct') or 0):>9.3f}  "
                      f"{'✓' if r.get('ok') else r.get('reason', '')[:40]}")
        conn.close()
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
