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
    ("js/app.js",                    "APP"),
]

# 顶层 const 名在合并后会冲突，按模块重命名。
# 注：SERVER 原是唯一冲突常量，现已统一到 js/config.js（排在最前、只声明一次），
#     各文件改为 import 后 build 会删掉 import 行，标准合并即无重复声明，故 COLLIDING 清空。
COLLIDING = set()



def build():
    parts = []
    for path, prefix in ORDER_WITH_PREFIX:
        content = (PORTAL / path).read_text(encoding="utf-8")
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

    js_block = "\n".join(parts)

    # 注入到 index.html
    html = (PORTAL / "index.html").read_text(encoding="utf-8")
    OLD_TAG = '<script type="module" src="js/app.js"></script>'
    if OLD_TAG not in html:
        print("❌ 找不到 script 标签，请检查 index.html")
        return

    new_html = html.replace(OLD_TAG, f'<script>\n{js_block}\n</script>')
    out = PORTAL / "index-standalone.html"
    out.write_text(new_html, encoding="utf-8")
    size_kb = out.stat().st_size / 1024
    print(f"✅ 生成 {out.name}  ({size_kb:.1f} KB)")
    print(f"   直接双击 {out} 即可使用，无需任何服务器")


if __name__ == "__main__":
    build()
