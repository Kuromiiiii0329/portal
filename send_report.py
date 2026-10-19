#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
portal/send_report.py

分析完成后，读取 reports/ 目录中当天生成的报告文件，
组合成 HTML 邮件正文并通过 EmailSender 发送。

用法（由 portal-daily-analysis.yml 调用）:
    python portal/send_report.py

所需环境变量:
    EMAIL_SENDER        发件人邮箱
    EMAIL_PASSWORD      邮箱授权码
    EMAIL_RECEIVERS     收件人，逗号分隔
    EMAIL_SENDER_NAME   发件人昵称（可选）
"""
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace

# 将项目根目录加入 Python 路径，使 portal/ 子目录也能 import src/
PROJECT_ROOT = Path(__file__).parent.parent
# path already set

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

TZ_CN = timezone(timedelta(hours=8))


def load_watchlist_config() -> dict:
    """读取 config/watchlist.json，返回配置字典；不存在时返回默认值。"""
    config_path = PROJECT_ROOT / "config" / "watchlist.json"
    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("读取 config/watchlist.json 失败: %s，使用默认值", e)
    return {}


def find_report_files(date_str: str) -> dict:
    """
    在 reports/ 目录下查找当天的报告文件。

    返回:
        {
            "stock": Path | None,       # report_{YYYYMMDD}.md
            "market": Path | None,      # market_review_{YYYYMMDD}.md
        }
    """
    reports_dir = PROJECT_ROOT / "reports"
    result = {"stock": None, "market": None}

    if not reports_dir.exists():
        logger.warning("reports/ 目录不存在")
        return result

    stock_file = reports_dir / f"report_{date_str}.md"
    market_file = reports_dir / f"market_review_{date_str}.md"

    if stock_file.exists():
        result["stock"] = stock_file
        logger.info("找到个股报告: %s", stock_file)
    else:
        logger.warning("未找到个股报告: %s", stock_file)

    if market_file.exists():
        result["market"] = market_file
        logger.info("找到大盘复盘: %s", market_file)
    else:
        logger.info("未找到大盘复盘文件（可能未启用）")

    return result


def build_email_body(stock_path: Path | None, market_path: Path | None) -> str:
    """将大盘复盘 + 个股报告拼合成一份 Markdown 正文。"""
    parts = []

    if market_path:
        content = market_path.read_text(encoding="utf-8").strip()
        if content:
            parts.append("# 📈 今日大盘复盘\n\n" + content)

    if stock_path:
        content = stock_path.read_text(encoding="utf-8").strip()
        if content:
            parts.append("# 🚀 个股分析报告\n\n" + content)

    if not parts:
        return ""

    return "\n\n---\n\n".join(parts)


def build_email_config() -> SimpleNamespace:
    """从环境变量构建最小化邮件配置对象（兼容 EmailSender 的接口）。"""
    receivers_raw = os.environ.get("EMAIL_RECEIVERS", "")
    receivers = [r.strip() for r in receivers_raw.split(",") if r.strip()]

    return SimpleNamespace(
        email_sender=os.environ.get("EMAIL_SENDER", ""),
        email_password=os.environ.get("EMAIL_PASSWORD", ""),
        email_receivers=receivers,
        email_sender_name=os.environ.get("EMAIL_SENDER_NAME", "A股智能分析助手"),
        stock_email_groups=[],
    )


def main() -> int:
    date_str = datetime.now(TZ_CN).strftime("%Y%m%d")
    date_display = datetime.now(TZ_CN).strftime("%Y-%m-%d")

    # 1. 读取 watchlist 配置（邮件开关 + 主题前缀）
    wl_config = load_watchlist_config()
    email_cfg_json = wl_config.get("email", {})

    if not email_cfg_json.get("enabled", True):
        logger.info("config/watchlist.json 中 email.enabled=false，跳过邮件发送")
        return 0

    subject_prefix = email_cfg_json.get("subject_prefix", "A股智能分析")
    subject = f"{subject_prefix} {date_display}"

    # 2. 查找报告文件
    files = find_report_files(date_str)
    if not files["stock"] and not files["market"]:
        logger.warning("今日无任何报告文件（可能是非交易日或分析未完成），跳过邮件发送")
        return 0

    # 3. 组合邮件正文
    body = build_email_body(files["stock"], files["market"])
    if not body.strip():
        logger.warning("报告文件内容为空，跳过邮件发送")
        return 0

    # 4. 构建邮件配置
    email_config = build_email_config()
    if not email_config.email_sender or not email_config.email_password:
        logger.error("EMAIL_SENDER 或 EMAIL_PASSWORD 未配置，无法发送邮件")
        return 1
    if not email_config.email_receivers:
        logger.error("EMAIL_RECEIVERS 未配置，无法发送邮件")
        return 1

    # 5. 发送邮件（复用现有 EmailSender）
    try:
        from src.notification_sender.email_sender import EmailSender
        sender = EmailSender(email_config)
        success = sender.send_to_email(body, subject=subject)
        if success:
            logger.info("邮件发送成功，主题: %s，收件人: %s", subject, email_config.email_receivers)
            return 0
        else:
            logger.error("邮件发送失败")
            return 1
    except ImportError as e:
        logger.error("无法导入 EmailSender，请确认在项目根目录运行: %s", e)
        return 1
    except Exception as e:
        logger.exception("发送邮件时发生异常: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
