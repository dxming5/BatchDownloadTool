# -*- coding: utf-8 -*-
"""
命名与落盘布局

布局与旧版保持一致（阶段一不允许改变文件位置与命名）：

    视频  博主目录/{发布时间}.mp4
    图片  博主目录/picture/{发布时间} - {序号}.webp

扩展名由调用方给出，缺省时视频 mp4、图片 webp；
小红书等平台的图片实际格式在下载时才确定，由下载机制回填。
"""

import os
from datetime import datetime

from core.store import PICTURE_DIR_NAME

DEFAULT_VIDEO_EXT = 'mp4'
DEFAULT_IMAGE_EXT = 'webp'


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


def normalize_ext(ext, fallback):
    """统一扩展名写法：去点、小写"""
    value = (ext or '').strip().lstrip('.').lower()
    return value or fallback


def picture_dir(blogger_dir, create=False):
    path = os.path.join(blogger_dir, PICTURE_DIR_NAME)
    if create and not os.path.isdir(path):
        os.makedirs(path)
    return path


def target_path(blogger_dir, kind, create_time, index=0, ext=''):
    """
    生成落盘路径

    Args:
        blogger_dir: 博主目录
        kind:        'image' / 'video'
        create_time: 文件名前缀（已格式化）
        index:       图片序号，从 0 开始
        ext:         实际扩展名，可空
    """
    if kind == 'image':
        suffix = normalize_ext(ext, DEFAULT_IMAGE_EXT)
        return os.path.join(picture_dir(blogger_dir, create=True),
                            '{} - {}.{}'.format(create_time, index + 1, suffix))
    return os.path.join(blogger_dir,
                        '{}.{}'.format(create_time, normalize_ext(ext, DEFAULT_VIDEO_EXT)))
