# -*- coding: utf-8 -*-
"""命名规则：时间戳转文件名前缀；落盘路径见 core.downloader.final_target"""

from datetime import datetime


def format_create_time(timestamp):
    """
    把秒级时间戳转成文件名前缀

    Returns:
        str|None: 形如 2026-01-01 10.00.00；缺失或非法时返回 None
    """
    try:
        return datetime.fromtimestamp(int(timestamp)).strftime('%Y-%m-%d %H.%M.%S')
    except (TypeError, ValueError, OSError, OverflowError):
        return None
