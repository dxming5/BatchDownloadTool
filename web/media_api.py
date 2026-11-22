# -*- coding: utf-8 -*-
"""
媒体响应

负责把下载目录里的文件按字节区间回给浏览器（支持拖动进度条），
以及缩略图、视频封面的响应。与平台无关。
"""

import os

from flask import abort, send_file

from core.downloader import (
    IMAGE_ALIASES,
    VIDEO_ALIASES,
    IMAGE_EXTS as _IMAGE_EXTS,
    VIDEO_EXTS as _VIDEO_EXTS,
)
from web.media_cache import make_thumb

# 浏览范围与下载层共用同一份后缀清单，别名（jpeg / tiff 等）一并列出，
# 历史文件也能预览；带点前缀是给 os.path.splitext / endswith 用的
IMAGE_EXTS = tuple('.' + ext for ext in _IMAGE_EXTS + tuple(IMAGE_ALIASES))
VIDEO_EXTS = tuple('.' + ext for ext in _VIDEO_EXTS + tuple(VIDEO_ALIASES))
MEDIA_EXTS = IMAGE_EXTS + VIDEO_EXTS


def resolve_media(root, rel_path):
    """把相对路径解析为下载目录内的绝对路径；越界或不存在返回 None"""
    root_path = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root_path, (rel_path or '').strip()))
    if target != root_path and not target.startswith(root_path + os.sep):
        return None
    if not os.path.isfile(target):
        return None
    return target


def media_response(root, rel_path, thumb=None):
    """媒体文件响应：图片可带 thumb 参数走缩略图缓存，其余按区间返回原文件"""
    target = resolve_media(root, rel_path)
    if not target:
        abort(404)

    if thumb and target.lower().endswith(IMAGE_EXTS):
        try:
            size = max(64, min(1024, int(thumb)))
            return send_file(make_thumb(root, target, size), mimetype='image/jpeg')
        except (ValueError, TypeError):
            pass
        except Exception:
            # 缩略图失败（例如格式不受 Pillow 支持）时回退原图，不阻断预览
            pass

    return send_file(target, conditional=True)
