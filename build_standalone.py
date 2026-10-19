#!/usr/bin/env python3
"""
portal/build_standalone.py
把 portal/js/ 下所有模块内联到 index-standalone.html，
使其可以通过 file:// 协议直接打开，无需 HTTP 服务器。

用法：
    cd C:\\Users\\I762120\\Desktop\\incident\\daily
    python portal/build_standalone.py
"""
import re
from pathlib import Path

PORTAL = Path(__file__).parent

ORDER_WITH_PREFIX = [
    ("js/config.js",                 None),
    ("js/components/toast.js",       None),
    ("js/components/modal.js",       "MODAL"),
    ("js/components/report-view.js", None),
    ("js/store.js",                  None),
    ("js/tabs/watchlist.js",         "WL"),
    ("js/tabs/settings.js",          "SETTINGS"),
    ("js/tabs/guide.js",             None),
    ("js/tabs/run.js",               "RUN"),
    ("js/tabs/chat.js",              "CHAT"),
    ("js/tabs/virtual.js",           "VIRTUAL"),
    ("js/app.js",                    "APP"),
]

# 顶层 const 名在合并后会冲突，按模块重命名。
# 注：SERVER 原是唯一冲突常量，现已统一到 js/config.js（排在最前、只声明一次），
#     各文件改为 import 后 build 会删掉 import 行，标准合并即无重复声明，故 COLLIDING 清空。
COLLIDING = set()

# 新增模块时**必须**同时做三件事（否则构建产物会静默坏掉）：
#   1) 加进上面的 ORDER_WITH_PREFIX，且排在使用者之前；
#   2) 确认该文件顶层 const/class/function 名全局唯一
#      （单文件产物是同一作用域，重名会抛 `Identifier already declared`，
#       而本脚本**不会报错**，只会产出一个坏文件）；
#   3) 构建后跑 `node --check index-standalone.html` 之外，还要用
#      `python portal/build_standalone.py --verify` 做一次引用完整性检查。



def build():
    parts = []
    missing = []
    for path, prefix in ORDER_WITH_PREFIX:
        p = PORTAL / path
        if not p.exists():
            missing.append(path)
            continue
        content = p.read_text(encoding="utf-8")
        lines = []
        for line in content.splitlines():
            stripped = line.strip()
            # 去掉 import 语句
            if stripped.startswith("import ") and "from " in stripped:
                continue
            # 去掉 export 前缀
            for kw in ("export const ", "export class ", "export function "):
                if stripped.startswith(kw):
                    line = line.replace(kw, kw[7:], 1)
                    break
            # 重命名冲突常量
            if prefix:
                for name in COLLIDING:
                    line = re.sub(rf'\bconst {name}\b', f'const {name}_{prefix}', line)
                    line = re.sub(rf'(?<!const ){name}(?!_[A-Z])\b', f'{name}_{prefix}', line)
            lines.append(line)
        parts.append(f"\n// ── {path} ──────────────────────────────")
        parts.append("\n".join(lines))

    if missing:
        print("❌ ORDER_WITH_PREFIX 里这些文件不存在：" + ", ".join(missing))
        return 1

    js_block = "\n".join(parts)

    # 注入到 index.html
    html = (PORTAL / "index.html").read_text(encoding="utf-8")
    OLD_TAG = '<script type="module" src="js/app.js"></script>'
    if OLD_TAG not in html:
        print("❌ 找不到 script 标签，请检查 index.html")
        return 1

    new_html = html.replace(OLD_TAG, f'<script>\n{js_block}\n</script>')
    out = PORTAL / "index-standalone.html"
    out.write_text(new_html, encoding="utf-8")
    size_kb = out.stat().st_size / 1024
    print(f"✅ 生成 {out.name}  ({size_kb:.1f} KB)")
    print(f"   直接双击 {out} 即可使用，无需任何服务器")

    problems = verify(new_html, js_block)
    warns = audit_dead_code(js_block)
    if warns:
        print("\n⚠️  告警（不阻断）：")
        for w in warns:
            print("   -", w)
    if problems:
        print("\n❌ 产物校验未通过：")
        for p in problems:
            print("   -", p)
        return 1
    print("✅ 产物校验通过（无 import/export 残留、无顶层重名、TABS 类均已定义）")
    return 0


#: 这些标记出现在产物里说明模块没被正确内联
_BAD_PATTERNS = (
    (re.compile(r"^\s*import\s+.*\bfrom\s+['\"]", re.M), "残留 import 语句"),
    (re.compile(r"^\s*export\s+(const|class|function|default)\b", re.M), "残留 export 前缀"),
)

#: 顶层声明（用于查重名）。产物是单一作用域，重名会抛 Identifier already declared
_DECL_RE = re.compile(r"^(?:const|let|var|class|function)\s+([A-Za-z_$][\w$]*)", re.M)


def _strip_comments(js: str) -> str:
    """去掉块注释与行注释，避免 Doc 注释把「引用次数」虚抬。

    不这么做的话，`class ResearchTab` 只在标题注释里出现一次、在声明里出现一次，
    计数 = 2，死代码检测就永远不报 —— 而它确实是死代码。实测踩到过。
    """
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    js = re.sub(r"(?m)^\s*//.*$", "", js)
    return js


def audit_dead_code(js_block: str) -> list[str]:
    """找出「定义了但从未被引用」的类（只告警，不阻断构建）。

    这一条是踩出来的：提交进仓库的 `index-standalone.html` 里躺着一个 36KB 的
    `ResearchTab` 类，而 `js/` 里早就没有它了 —— 产物与源码漂移，而旧构建脚本
    不做任何校验，所以没人发现。
    """
    code = _strip_comments(js_block)
    warns: list[str] = []
    for name in re.findall(r"^class\s+([A-Za-z_$][\w$]*)", code, re.M):
        if len(re.findall(rf"\b{name}\b", code)) <= 1:
            warns.append(f"死代码：class {name} 已定义但从未被引用")
    return warns


def verify(new_html: str, js_block: str) -> list[str]:
    """校验构建产物。返回问题清单（空 = 通过）。

    为什么必须有：`build_standalone.py` 出坏文件时**不会报错**，
    只会静默产出一个在浏览器里崩掉的 html。这是 README 里点名过的坑。
    """
    problems: list[str] = []
    for pat, label in _BAD_PATTERNS:
        hits = pat.findall(js_block)
        if hits:
            problems.append(f"{label}：{len(hits)} 处，例如 {hits[0]!r}")

    # 重名
    names = _DECL_RE.findall(js_block)
    seen: dict[str, int] = {}
    for n in names:
        seen[n] = seen.get(n, 0) + 1
    dupes = {k: v for k, v in seen.items() if v > 1}
    if dupes:
        problems.append(f"顶层重名 {len(dupes)} 个：" +
                        ", ".join(f"{k}×{v}" for k, v in list(dupes.items())[:6]))

    # 每个 TAB 的 Cls 必须在产物里被定义
    tabs = re.findall(r"id:\s*'([\w-]+)'.*?Cls:\s*([A-Za-z_$][\w$]*)", new_html, re.S)
    defined = set(seen)
    for tid, cls in tabs:
        if cls not in defined:
            problems.append(f"TAB '{tid}' 用的 {cls} 在产物里没有定义")

    # 基础结构
    if "<script>" not in new_html:
        problems.append("没有内联 <script> 块")
    if 'type="module"' in new_html:
        problems.append("仍存在 type=\"module\" 脚本标签（file:// 下无法工作）")
    return problems


def self_test() -> int:
    """证明校验器「能失败」—— 塞一个已知坏样本进去看它报不报。

    原则同《回测避坑手册》§5.6：改完判据必须塞一个已知答案进去确认它能报出来，
    否则「0 命中」分不清是「真没有」还是「探针坏了」。
    """
    ok = True
    probe = "class __ProbeDead {}\nconst x = 1;\n"
    warns = audit_dead_code(probe)
    if any("__ProbeDead" in w for w in warns):
        print("✅ 死代码检测：注入 __ProbeDead 后成功报出")
    else:
        print("❌ 死代码检测：注入 __ProbeDead 后没有报出（探针坏了）")
        ok = False

    bad_html = "<script>\nimport x from 'y';\n</script>"
    probs = verify(bad_html, "import x from 'y';")
    if probs:
        print(f"✅ 产物校验：残留 import 被报出（{len(probs)} 条）")
    else:
        print("❌ 产物校验：残留 import 未被报出（探针坏了）")
        ok = False

    dup = "const A = 1;\nconst A = 2;\n"
    if verify("<script>\n</script>", dup):
        print("✅ 重名检测：重复 const 被报出")
    else:
        print("❌ 重名检测：重复 const 未被报出（探针坏了）")
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        raise SystemExit(self_test())
    raise SystemExit(build() or 0)
