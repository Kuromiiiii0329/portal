# -*- coding: utf-8 -*-
"""
portal/premarket/__main__.py — 命令行入口

    python -m portal.premarket generate [--plan-date YYYYMMDD] [--trade-date YYYYMMDD]
                                        [--no-cache] [--no-fallback] [--json]
    python -m portal.premarket review   [--date YYYYMMDD]
    python -m portal.premarket status
    python -m portal.premarket tables
"""
from __future__ import annotations

import argparse
import json
import sys

from . import account as account_mod
from . import paths
from . import pipeline


def _print_status() -> int:
    st = pipeline.status()
    print(json.dumps(st, ensure_ascii=False, indent=2))
    return 0


def _print_tables() -> int:
    from .store import Tables
    snap = Tables().snapshot()
    for name in ("pool", "positions", "infos", "lessons"):
        rows = snap[name]
        print(f"\n=== {name}（{len(rows)} 条）===")
        for r in rows:
            brief = {k: v for k, v in r.items()
                     if k in ("code", "name", "tier", "status", "content", "lesson",
                              "alloc_pct", "entry_price", "level", "result",
                              "expires_on", "added_date", "entry_date")}
            print("  ", json.dumps(brief, ensure_ascii=False))
        if not rows:
            print("   （空）")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m portal.premarket")
    sub = ap.add_subparsers(dest="cmd")

    d = sub.add_parser("daily", help="盘后一条龙：结算+预案+复盘（推荐）")
    d.add_argument("--date", default=None, help="数据日 YYYYMMDD（默认最新交易日）")
    d.add_argument("--plan-date", default=None)
    d.add_argument("--no-cache", action="store_true")
    d.add_argument("--no-html", action="store_true",
                   help="不写网页版 HTML（内容走邮件正文；门户按钮不受影响）")
    d.add_argument("--mail", default=None, metavar="PATH",
                   help="同时把完整报告写成邮件正文 HTML 到该路径")
    d.add_argument("--json", action="store_true")

    g = sub.add_parser("generate", help="只生成预案（不结算昨日计划）")
    g.add_argument("--plan-date", default=None)
    g.add_argument("--trade-date", default=None)
    g.add_argument("--no-cache", action="store_true", help="不读采集缓存")
    g.add_argument("--no-fallback", action="store_true", help="没有 LLM 就直接失败")
    g.add_argument("--no-html", action="store_true")
    g.add_argument("--mail", default=None, metavar="PATH")
    g.add_argument("--json", action="store_true", help="输出机器可读 JSON")

    m = sub.add_parser("midday", help="午间报告（半日行情 + 午后判断）")
    m.add_argument("--force", action="store_true",
                   help="跳过时段校验（仅调试用，会污染午报口径）")
    m.add_argument("--no-html", action="store_true",
                   help="不写网页版 HTML（内容走邮件正文）")
    m.add_argument("--mail", default=None, metavar="PATH",
                   help="同时把完整报告写成邮件正文 HTML 到该路径")
    m.add_argument("--json", action="store_true")

    r = sub.add_parser("review", help="盘后复盘（按收盘价结算计划）")
    r.add_argument("--date", default=None)

    sub.add_parser("status", help="查看状态")
    sub.add_parser("tables", help="打印四张表")

    args = ap.parse_args(argv)
    paths.ensure_dirs()

    if args.cmd in (None, "status"):
        return _print_status()
    if args.cmd == "tables":
        return _print_tables()

    if args.cmd in ("daily", "generate"):
        if args.cmd == "daily":
            try:
                out = pipeline.daily(
                    date8=args.date, plan_date=args.plan_date,
                    use_cache=not args.no_cache, write=True,
                    emit_html=not args.no_html, mail_path=args.mail)
            except Exception as exc:
                print(json.dumps({"ok": False,
                                  "error": f"{type(exc).__name__}: {exc}"},
                                 ensure_ascii=False))
                return 1
        else:
            try:
                out = pipeline.generate(
                    plan_date=args.plan_date, trade_date=args.trade_date,
                    settle=False,
                    use_cache=not args.no_cache,
                    allow_fallback=not args.no_fallback, write=True,
                    emit_html=not args.no_html, mail_path=args.mail)
            except Exception as exc:
                print(json.dumps({"ok": False,
                                  "error": f"{type(exc).__name__}: {exc}"},
                                 ensure_ascii=False))
                return 1
        if args.json:
            print(json.dumps({
                "ok": out["ok"], "plan_path": out.get("plan_path"),
                "report_path": out.get("report_path"),
                "report_bytes": out.get("report_bytes"),
                "proposal_source": out.get("proposal_source"),
                "plan_date": out["plan"]["plan_date"],
                "trade_date": out["plan"]["trade_date"],
                "anchor": out["plan"]["anchor"]["sha256"],
                "mail_path": out.get("mail_path"),
                "mail_bytes": out.get("mail_bytes"),
                "mail_subject": out.get("mail_subject"),
            }, ensure_ascii=False))
        else:
            print(f"✅ 预案：{out.get('report_path')}")
            print(json.dumps({k: out["plan"][k] for k in
                              ("plan_date", "trade_date", "proposal_source",
                               "tables_after")}, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "midday":
        from portal.premarket import midday
        try:
            out = midday.run(write=True, emit_html=not args.no_html,
                             mail_path=args.mail)
        except midday.SessionError as exc:
            print(json.dumps({"ok": False, "error": str(exc),
                              "hint": "午报只在 11:25–13:15 且官方快照是今天 11:30 时可生成"},
                             ensure_ascii=False))
            return 2
        except Exception as exc:
            print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                             ensure_ascii=False))
            return 1
        print(json.dumps({
            "ok": out["ok"], "report_path": out.get("report_path"),
            "report_bytes": out.get("report_bytes"),
            "proposal_source": out.get("proposal_source"),
            "executed": len((out.get("midday") or {}).get("executed") or []),
            "mail_path": out.get("mail_path"),
            "mail_bytes": out.get("mail_bytes"),
            "mail_subject": out.get("mail_subject"),
        }, ensure_ascii=False))
        return 0

    if args.cmd == "review":
        out = pipeline.review(date8=args.date)
        print(json.dumps({"ok": out["ok"],
                          "applied": len(out.get("applied") or []),
                          "skipped": len(out.get("skipped") or []),
                          "account": {k: out["account"][k] for k in
                                      ("total_pct", "cash_pct",
                                       "position_ratio_pct", "cum_pnl_ratio_pct")}
                          if out.get("account") else None,
                          "error": out.get("error")},
                         ensure_ascii=False))
        return 0 if out.get("ok") else 1

    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
