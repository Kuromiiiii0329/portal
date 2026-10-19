"""
portal/srv/_config.py
服务基础配置层 —— 常量、路径注入、日志设置。

⚠️ 本模块被 srv 包内其他模块最先导入，其顶层的路径注入（sys.path）是所有
   analyzer 裸导入（from data_provider import ...）的前提，必须在导入任何
   业务模块之前完成。常量值与原 server.py 顶部逐字节一致。
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import timezone, timedelta
from pathlib import Path

PORT = int(os.environ.get("PORTAL_SERVER_PORT", 7788))

ALLOWED_ENV_KEYS = {
    "GEMINI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "LITELLM_MODEL",
    "HAI_BASE_URL", "HAI_API_KEY", "HAI_MODEL",
    "TUSHARE_TOKEN", "BOCHA_API_KEYS", "TAVILY_API_KEYS", "SERPAPI_API_KEYS",
    "EMAIL_SENDER", "EMAIL_PASSWORD", "EMAIL_RECEIVERS", "EMAIL_SENDER_NAME",
}
# __file__ 在 portal/srv/ 下，PORTAL_DIR 需上溯两级
PORTAL_DIR   = Path(__file__).parent.parent   # portal/
PROJECT_ROOT = PORTAL_DIR.parent              # daily/  (may not exist if running standalone)
LIB_DIR      = PORTAL_DIR / "lib"             # portal/lib/ — bundled dependencies
CONFIG_PATH  = PORTAL_DIR.parent / "config" / "watchlist.json"
TZ_CN = timezone(timedelta(hours=8))

# ── 路径注入：portal/lib/ 优先，其次项目根（向后兼容）─────────
for _p in [str(LIB_DIR), str(PROJECT_ROOT)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("portal.server")

# ── 压制第三方库噪音日志 ───────────────────────────────────
# litellm 在解析 provider/model 时会打印 "Provider List: https://..." 等对用户无用的行，
# 统一把 litellm 及其底层 httpx/openai 日志级别调到 ERROR，只保留真正的错误。
for _noisy in ("LiteLLM", "litellm", "httpx", "httpcore", "openai"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)
try:
    import os as _os
    _os.environ.setdefault("LITELLM_LOG", "ERROR")  # litellm 新版用此环境变量控制日志
except Exception:
    pass
