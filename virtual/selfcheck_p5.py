# -*- coding: utf-8 -*-
"""
portal/virtual/selfcheck_p5.py — 数据层增量与口径自检（离线）

覆盖 W1–W6：

    W1  腾讯字段解析   —— 手→股、万元→元、字段错位（f31/f32 是涨跌额/涨跌%）
    W2  盘中护栏       —— 盘中行情**必须拒绝写库**（否则日K被半天快照污染）
    W3  收盘后写入     —— 幂等 UPSERT；重复跑不产生重复行
    W4  交易所舍入     —— 19.45×1.1 必须得 21.40（Python round 会给 21.39）
    W5  池分类         —— ST / 炸板回封 / 正常封住 三类能区分
    W6  北交所         —— fetch_one 必须标 unsupported，不许反复重试

全部离线：数据源用 monkeypatch 注入，数据库用临时文件。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault(
    "PORTAL_VIRTUAL_ROOT",
    str(Path(tempfile.mkdtemp(prefix="portal_virtual_p5_")) / "virtual"),
)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from portal.virtual import marketdb as M  # noqa: E402
from portal.virtual._check import Check, close_enough  # noqa: E402

#: 构造一条 88 字段的腾讯行情（真实返回就是 88 段）。
#: **必须用构造器而不是手写字符串**：手写时很难数清 f[30] 到底在第几段，
#: 第一版就因为这个把时间戳放错了位置，解析直接返回 None。
QT_FIELDS = 88


def _mk_qt_line(*, code: str = "600519", name: str = "贵州茅台",
                ts: str = "20261019161457", last: str = "1324.97",
                prev: str = "1329.00", open_: str = "1322.50",
                high: str = "1348.60", low: str = "1321.00",
                vol_hand: str = "39729", amt_wan: str = "530894") -> str:
    f = [""] * QT_FIELDS
    f[0] = "1"
    f[1] = name
    f[2] = code
    f[3] = last          # 最新价
    f[4] = prev          # 昨收
    f[5] = open_         # 今开
    f[6] = vol_hand      # 成交量（手）
    f[30] = ts           # 时间戳 YYYYMMDDHHMMSS
    f[31] = "-4.03"      # 涨跌额（注意不是涨跌%）
    f[32] = "-0.30"      # 涨跌%
    f[33] = high
    f[34] = low
    f[35] = f"{last}/{vol_hand}/5308935919"
    f[36] = vol_hand
    f[37] = amt_wan      # 成交额（万元）
    f[38] = "0.32"       # 换手率
    f[39] = "20.34"      # PE
    f[43] = "2.08"       # 振幅
    f[44] = "16563.21"   # 流通市值（亿）
    f[45] = "16563.21"   # 总市值（亿）
    f[47] = "1461.90"    # 涨停价
    f[48] = "1196.10"    # 跌停价
    f[49] = "1.12"       # 量比
    return 'v_' + ("sh" if code[0] in "69" else "sz") + code + '="' + "~".join(f) + '"'


QT_LINE = _mk_qt_line()
#: 盘中版本：时间戳 10:15，成交量只走了一半
QT_LINE_INTRADAY = _mk_qt_line(ts="20261019101500", vol_hand="18000", amt_wan="240000")


def _tmp_conn():
    p = Path(tempfile.mkdtemp(prefix="marketdb_p5_")) / "m.db"
    conn = M.connect(p)
    M.init_schema(conn)
    with conn:
        for code, name, board in (("600519", "贵州茅台", "main"),
                                  ("000001", "平安银行", "main"),
                                  ("300750", "宁德时代", "gem")):
            conn.execute(
                """INSERT OR REPLACE INTO stock(code,name,market,board,limit_pct,is_st)
                   VALUES(?,?,?,?,?,0)""",
                (code, name, "sh" if code[0] in "69" else "sz", board, 0.1))
    return conn


# ─────────────────────────────────────────────────────────────
# W1 · 字段解析与单位
# ─────────────────────────────────────────────────────────────
def check_qt_parse() -> Check:
    c = Check("W1", "腾讯字段解析：单位换算与字段错位")
    r = M.parse_qt_line(QT_LINE)
    c.expect(r is not None, "能解析出记录")
    if not r:
        return c
    c.expect(r["code"] == "600519" and r["name"] == "贵州茅台", "代码/名称正确")
    c.expect(r["date"] == "20261019" and r["hhmm"] == "1614",
             f"时间戳拆分：日期 {r['date']}，时刻 {r['hhmm']}（4 位，供与 '1500' 比较）")
    c.expect(close_enough(r["open"], 1322.50), f"今开 {r['open']}")
    c.expect(close_enough(r["close"], 1324.97), f"最新价 {r['close']}")
    c.expect(close_enough(r["high"], 1348.60) and close_enough(r["low"], 1321.00),
             f"高/低 {r['high']}/{r['low']}")
    # ★ 单位：手 → 股
    c.expect(close_enough(r["volume"], 39_729 * 100),
             f"成交量 39,729 手 → {r['volume']:,.0f} 股（与新浪口径一致）")
    # ★ 单位：万元 → 元
    c.expect(close_enough(r["amount"], 530_894 * 10_000),
             f"成交额 530,894 万 → {r['amount']:,.0f} 元")
    # ★ 字段错位验证：f31 是涨跌额、f32 是涨跌%
    c.expect(close_enough(r["prev_close"], 1329.00), "昨收取 f[4]")
    c.expect(not close_enough(r["close"], 0) and r["close"] > 0, "价格为正")

    c.expect(M.parse_qt_line("garbage") is None, "非行情行返回 None（不猜）")
    c.expect(M.parse_qt_line('v_sh1="1~2~3"') is None, "字段不足返回 None")
    bad_ts = QT_LINE.replace("20261019161457", "abcdefghijkl")
    c.expect(M.parse_qt_line(bad_ts) is None, "时间戳非数字返回 None")
    return c


# ─────────────────────────────────────────────────────────────
# W2 / W3 · 盘中护栏 + 收盘后写入
# ─────────────────────────────────────────────────────────────
def _patch_quotes(quotes):
    """把 fetch_quotes_batch 换成固定返回值。"""
    M._orig_fetch = M.fetch_quotes_batch
    M.fetch_quotes_batch = lambda codes, timeout=15: list(quotes)


def _unpatch():
    if hasattr(M, "_orig_fetch"):
        M.fetch_quotes_batch = M._orig_fetch


def check_intraday_guard() -> Check:
    c = Check("W2", "盘中护栏：半天快照必须拒绝写库")
    conn = _tmp_conn()
    before = conn.execute("SELECT COUNT(*) FROM day").fetchone()[0]

    q = M.parse_qt_line(QT_LINE_INTRADAY)
    c.expect(q is not None and q["hhmm"] == "1015", f"构造出盘中行情 {q and q['hhmm']}")
    _patch_quotes([q, {**q, "code": "000001"}, {**q, "code": "300750"}])
    try:
        stats = M.update_today(conn, sleep=0, progress=None)
    finally:
        _unpatch()

    after = conn.execute("SELECT COUNT(*) FROM day").fetchone()[0]
    c.expect(stats.get("intraday") is True, "识别为盘中数据")
    c.expect(stats.get("written", 0) == 0, f"没有写入任何行（written={stats.get('written')}）")
    c.expect(after == before, f"表行数不变（{before} → {after}）")
    c.expect("盘中" in (stats.get("error") or ""), f"给出明确原因：{stats.get('error', '')[:60]}")

    # 显式允许时才写（并标明 intraday=True）
    _patch_quotes([q, {**q, "code": "000001"}, {**q, "code": "300750"}])
    try:
        stats2 = M.update_today(conn, sleep=0, progress=None, allow_intraday=True)
    finally:
        _unpatch()
    c.expect(stats2.get("written", 0) == 3, f"allow_intraday=True 时写入 {stats2.get('written')} 行")
    c.expect(stats2.get("intraday") is True, "返回里标明 intraday=True")
    conn.close()
    return c


def check_update_today_write() -> Check:
    c = Check("W3", "收盘后写入：数值正确且幂等")
    conn = _tmp_conn()
    q = M.parse_qt_line(QT_LINE)
    quotes = [q, {**q, "code": "000001"}, {**q, "code": "300750"}]

    _patch_quotes(quotes)
    try:
        s1 = M.update_today(conn, sleep=0, progress=None)
    finally:
        _unpatch()
    c.expect(s1.get("written") == 3, f"首跑写入 {s1.get('written')} 行")
    c.expect(s1.get("intraday") is False, "判为收盘后数据")

    row = conn.execute(
        "SELECT open,high,low,close,volume,amount FROM day WHERE code='600519' AND date='2026-10-19'"
    ).fetchone()
    c.expect(row is not None, "行已落库")
    if row:
        c.expect(close_enough(row["close"], 1324.97), f"收盘 {row['close']}")
        c.expect(close_enough(row["volume"], 3_972_900), f"成交量 {row['volume']:,.0f} 股")
        c.expect(close_enough(row["amount"], 5_308_940_000), f"成交额 {row['amount']:,.0f} 元")

    _patch_quotes(quotes)
    try:
        s2 = M.update_today(conn, sleep=0, progress=None)
    finally:
        _unpatch()
    total = conn.execute("SELECT COUNT(*) FROM day").fetchone()[0]
    c.expect(s2.get("written") == 3, "重复跑仍写 3 行（UPSERT）")
    c.expect(total == 3, f"表里仍是 3 行，没有重复（实际 {total}）")
    c.expect("last_update_today_at" in dict(conn.execute("SELECT k,v FROM meta").fetchall()),
             "meta 记录了更新时间")

    # 取不到行情时必须报错而不是静默成功
    _patch_quotes([])
    try:
        s3 = M.update_today(conn, sleep=0, progress=None)
    finally:
        _unpatch()
    c.expect(s3.get("written", 0) == 0 and s3.get("error"),
             f"空行情返回 error：{s3.get('error', '')[:40]}")
    conn.close()
    return c


# ─────────────────────────────────────────────────────────────
# W4 · 交易所舍入
# ─────────────────────────────────────────────────────────────
def check_exchange_rounding() -> Check:
    c = Check("W4", "交易所舍入：19.45×1.1 必须得 21.40，不是 21.39")
    c.expect(M.round_half_up(21.395) == 21.40,
             f"21.395 → {M.round_half_up(21.395)}（Python round 会给 {round(21.395, 2)}）")
    c.expect(M.limit_prices(19.45, "001319")[0] == 21.40, "001319 涨停价 21.40")
    c.expect(M.limit_prices(22.65, "001358")[0] == 24.92, "001358 涨停价 24.92")
    c.expect(M.limit_prices(10.95, "600721")[0] == 12.05, "600721 涨停价 12.05")
    c.expect(M.limit_prices(1.15, "600000")[0] == 1.27, "1.15 → 1.27")
    c.expect(M.limit_prices(4.85, "600000")[0] == 5.34, "4.85 → 5.34")
    # 非边界值不能被改坏
    c.expect(M.limit_prices(6.75, "600000")[0] == 7.43, "6.75 → 7.43")
    c.expect(M.limit_prices(30.34, "600000")[0] == 33.37, "30.34 → 33.37")
    # 板块幅度
    c.expect(M.limit_prices(10.0, "300001")[0] == 12.0, "创业板 ×1.2")
    c.expect(M.limit_prices(10.0, "830001")[0] == 13.0, "北交所 ×1.3")
    # ST
    c.expect(M.limit_prices(10.0, "600000", is_st=True)[0] == 10.5, "ST ×1.05")
    # 创业板 2020-08-24 分界
    c.expect(M.limit_pct_at("300001", "20200821") == 0.10, "创业板改革前 10%")
    c.expect(M.limit_pct_at("300001", "20200824") == 0.20, "创业板改革后 20%")
    return c


# ─────────────────────────────────────────────────────────────
# W5 · 池分类
# ─────────────────────────────────────────────────────────────
def check_pool_classify() -> Check:
    c = Check("W5", "池分类：封住 / 炸板回封 / ST 三类能区分")
    conn = _tmp_conn()
    base = {"date": "2026-10-16", "code": "600519", "name": "贵州茅台",
            "prev_close": 10.0, "is_st": 0, "amount": 1e8, "volume": 1e6}
    from portal.virtual import scan

    sealed = scan._classify({**base, "close": 11.0, "high": 11.0})
    c.expect(sealed and sealed["sealed"], "收盘 11.00 = 涨停价 → sealed")
    c.expect(sealed and not sealed["touch_only"], "不算炸板")

    # 盘中触及 11.00，收 10.60 → 炸板
    zb = scan._classify({**base, "close": 10.60, "high": 11.0})
    c.expect(zb is not None and zb["touch_only"], "触及涨停但未收住 → touch_only")

    # 收 10.60、最高 10.80 → 两边都不是
    none_ = scan._classify({**base, "close": 10.60, "high": 10.80})
    c.expect(none_ is None, "既未封住也未触及 → 不入选")

    # ST：5% 幅度，10.5 是涨停价
    st = scan._classify({**base, "is_st": 1, "close": 10.50, "high": 10.50})
    c.expect(st and st["sealed"] and close_enough(st["limit_pct"], 0.05),
             "ST 用 5% 幅度判定")

    # 跌停
    dt = scan._classify({**base, "close": 9.0, "high": 9.5})
    c.expect(dt is not None and dt["limit_down_sealed"], "收盘 9.00 = 跌停价")
    conn.close()
    return c


# ─────────────────────────────────────────────────────────────
# W6 · 北交所显式不支持
# ─────────────────────────────────────────────────────────────
def check_bse_unsupported() -> Check:
    c = Check("W6", "北交所：标 unsupported，不反复重试")
    r = M.fetch_one("920002")
    c.expect(r["status"] == "unsupported", f"状态 {r['status']}")
    c.expect(r["bars"] == [] and r["factors"] == [], "不返回任何数据")
    c.expect("北交所" in (r.get("err") or ""), f"原因写明：{(r.get('err') or '')[:50]}")
    c.expect(M.board_of("920002") == "bse" and M.board_of("600519") == "main",
             "板块识别正确")
    return c


# ─────────────────────────────────────────────────────────────
# W7 · 后台任务的并发护栏
# ─────────────────────────────────────────────────────────────
def check_job_guard() -> Check:
    c = Check("W7", "后台增量任务：并发与非法参数都要被挡住")

    from portal.srv import virtual_api as api

    # 未知 kind
    r = api.start_update("nonsense")
    c.expect(r["ok"] is False and "未知" in r["error"], f"未知 kind 被拒：{r.get('error')}")

    # 已有任务在跑 → 拒绝（不排队，避免并发写库）
    with api._update_lock:
        api._update_state["running"] = True
        api._update_state["started_at"] = "test"
    try:
        r2 = api.start_update("today")
    finally:
        with api._update_lock:
            api._update_state["running"] = False
            api._update_state["started_at"] = None
    c.expect(r2["ok"] is False and "已有更新" in r2["error"],
             f"并发启动被拒：{r2.get('error')}")
    c.expect("hint" in r2, "给了提示（告诉用户什么时候开始的）")

    # 状态查询在空闲时也要可用
    st = api.update_status()
    c.expect(st["ok"] is True and st["running"] is False, "空闲时状态可查")
    c.expect(isinstance(st.get("lines"), list), "带 lines 列表")
    c.expect(st.get("kind") is None or isinstance(st.get("kind"), str), "kind 字段类型正确")
    return c


CHECKS = (
    ("W1", check_qt_parse),
    ("W2", check_intraday_guard),
    ("W3", check_update_today_write),
    ("W4", check_exchange_rounding),
    ("W5", check_pool_classify),
    ("W6", check_bse_unsupported),
    ("W7", check_job_guard),
)


def run() -> dict:
    results = []
    for _, fn in CHECKS:
        try:
            results.append(fn())
        except Exception as exc:
            import traceback
            chk = Check("ERR", f"{fn.__name__} 执行异常")
            chk.expect(False, f"{type(exc).__name__}: {exc}")
            chk.note(traceback.format_exc().splitlines()[-1])
            results.append(chk)
    passed = all(c.passed for c in results)
    return {"passed": passed, "n_checks": len(results),
            "n_failed": sum(1 for c in results if not c.passed),
            "checks": [c.to_dict() for c in results]}


def main(argv: list[str] | None = None) -> int:
    import json
    argv = argv if argv is not None else sys.argv[1:]
    rep = run()
    if "--json" in argv:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if rep["passed"] else 1
    print("=" * 72)
    print("portal.virtual 自检 · 数据层增量与口径")
    print("=" * 72)
    for chk in rep["checks"]:
        print(f"\n[{'PASS' if chk['passed'] else 'FAIL'}] {chk['id']} · {chk['title']}")
        for line in chk["detail"]:
            print(line)
    print("\n" + "=" * 72)
    print(f"结果：{rep['n_checks'] - rep['n_failed']}/{rep['n_checks']} 通过")
    print("=" * 72)
    return 0 if rep["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
