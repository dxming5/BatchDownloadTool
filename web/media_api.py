# -*- coding: utf-8 -*-
"""
媒体响应与预览图

负责把下载目录里的文件按字节区间回给浏览器（支持拖动进度条），以及预览图的
生成与缓存。预览图与媒体文件放在一起：图片的缩略图在 `picture/.preview/`，
视频的取帧封面在 `video/.preview/`；隐藏目录，删除博主时随目录一起清掉，
也不会混进博主的媒体列表。

预览图位置由博主身份（平台 key + 博主名）与媒体类型给出，不从文件路径反推。
"""

import hashlib
import os

from flask import abort, send_file

from core import paths, store
from core.downloader import (
    IMAGE_ALIASES,
    VIDEO_ALIASES,
    IMAGE_EXTS as _IMAGE_EXTS,
    VIDEO_EXTS as _VIDEO_EXTS,
)
from core.utils import make_dir

# 浏览范围与下载层共用同一份后缀清单，别名（jpeg / tiff 等）一并列出，
# 历史文件也能预览；带点前缀是给 os.path.splitext / endswith 用的
IMAGE_EXTS = tuple('.' + ext for ext in _IMAGE_EXTS + tuple(IMAGE_ALIASES))
VIDEO_EXTS = tuple('.' + ext for ext in _VIDEO_EXTS + tuple(VIDEO_ALIASES))
MEDIA_EXTS = IMAGE_EXTS + VIDEO_EXTS


def _inner(blogger, target):
    """媒体文件相对博主目录的路径，作为预览图 key 的一部分（由 core.paths 计算）"""
    return paths.inner_path(blogger.key, blogger.name, target)


def _mtime(target):
    try:
        return int(os.path.getmtime(target))
    except OSError:
        return 0


def cover_path(blogger, target, mtime=None):
    """
    视频预览图（取首帧封面）路径：<博主>/video/.preview/<hash>.jpg

    key 由「相对博主目录的路径 + 修改时间」决定：视频被重新下载或覆盖后 key
    变化，旧预览图自然失效。mtime 由调用方传入可省掉一次 stat。
    """
    stamp = '{}|{}'.format(_inner(blogger, target),
                           _mtime(target) if mtime is None else mtime)
    key = hashlib.md5(stamp.encode('utf-8')).hexdigest()
    return paths.preview_file(blogger.key, blogger.name, True, key)


def thumb_path(blogger, target, size):
    """
    图片缩略图路径：<博主>/picture/.preview/<hash>.jpg

    key 由「路径 + 修改时间 + 尺寸」决定，同一张图不同尺寸各存一份。
    """
    try:
        mtime = os.path.getmtime(target)
    except OSError:
        mtime = 0
    stamp = '{}|{}|{}'.format(_inner(blogger, target), mtime, size)
    key = hashlib.md5(stamp.encode('utf-8')).hexdigest()
    return paths.preview_file(blogger.key, blogger.name, False, key)


def make_thumb(blogger, target, size):
    """生成图片缩略图并写入预览目录；已存在直接复用"""
    from PIL import Image

    output = thumb_path(blogger, target, size)
    if os.path.exists(output):
        return output
    make_dir(os.path.dirname(output))
    with Image.open(target) as image:
        image = image.convert('RGB')
        image.thumbnail((size, size))
        image.save(output, 'JPEG', quality=85)
    return output


def media_response(platform_key, name, rel_path, thumb=None):
    """媒体文件响应：图片可带 thumb 参数走缩略图，其余按区间返回原文件"""
    target = paths.resolve(rel_path)
    if not target:
        abort(404)

    if thumb and target.lower().endswith(IMAGE_EXTS):
        blogger = store.blogger(platform_key, name)
        try:
            size = max(64, min(1024, int(thumb)))
            if blogger:
                return send_file(make_thumb(blogger, target, size),
                                 mimetype='image/jpeg')
        except (ValueError, TypeError):
            pass
        except Exception:
            # 缩略图失败（例如格式不受 Pillow 支持）时回退原图，不阻断预览
            pass

    return send_file(target, conditional=True)
