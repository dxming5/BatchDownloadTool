# -*- coding: utf-8 -*-
"""
媒体响应

负责把下载目录里的文件按字节区间回给浏览器（支持拖动进度条），
以及缩略图、视频封面的响应。与平台无关。
"""

import os

from flask import abort, send_file

from media.covers import cover_path
from media.thumbs import make_thumb

# 浏览范围：抖音产出 jpg/png/webp/mp4；小红书还会产出 avif/gif，
# 未装 pillow-heif 时可能残留 heic（前端不一定能显示，但至少要列出来）
IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.webp', '.avif', '.gif', '.heic', '.heif')
VIDEO_EXTS = ('.mp4',)
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


def cover_rel_path(root, target, mtime=None):
    """视频封面在下载根下的相对路径，供接口拼 URL"""
    cover = cover_path(root, target, mtime)
    return os.path.relpath(cover, os.path.realpath(root)).replace('\\', '/')
