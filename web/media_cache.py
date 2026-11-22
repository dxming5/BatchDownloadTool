# -*- coding: utf-8 -*-
"""
媒体缓存：缩略图与视频封面

缓存放在博主目录内部（<平台>/<博主>/.cache），删除博主时随目录一并清掉，
也不会混进博主的图片列表。只被 web 层（预览接口）使用。
"""

import hashlib
import os

from core import registry
from core.utils import make_dir

CACHE_DIR_NAME = '.cache'
THUMB_DIR_NAME = 'thumbs'
COVER_DIR_NAME = 'covers'


def cache_root(root, target):
    """
    媒体文件对应的预览缓存根目录

    下载根下有两级含义（平台目录 / 博主目录），不能只认一级，
    否则会退化成「平台级公共缓存」。

    Returns:
        tuple: (缓存根目录, 媒体文件相对博主目录的路径)
    """
    root_path = os.path.realpath(root)
    rel = os.path.relpath(os.path.realpath(target), root_path).replace('\\', '/')
    parts = rel.split('/')

    # 目录结构：<平台>/<博主>/...
    if len(parts) > 2 and registry.is_platform_dir(parts[0]):
        return os.path.join(root_path, parts[0], parts[1], CACHE_DIR_NAME), '/'.join(parts[2:])

    return os.path.join(root_path, CACHE_DIR_NAME), rel


def cover_path(root, target, mtime=None):
    """
    视频封面的缓存路径

    key 由「相对博主目录的路径 + 修改时间」决定：视频被重新下载或覆盖后 key 变化，
    旧封面自然失效。mtime 由调用方传入可省掉一次 stat。
    """
    base, inner = cache_root(root, target)
    if mtime is None:
        try:
            mtime = int(os.path.getmtime(target))
        except OSError:
            mtime = 0
    key = hashlib.md5('{}|{}'.format(inner, mtime).encode('utf-8')).hexdigest()
    return os.path.join(base, COVER_DIR_NAME, key + '.jpg')


def thumb_path(root, target, size):
    """缩略图缓存路径：同一文件内容（路径 + 修改时间 + 尺寸）对应一个 key"""
    base, inner = cache_root(root, target)
    try:
        mtime = os.path.getmtime(target)
    except OSError:
        mtime = 0
    stamp = '{}|{}|{}'.format(inner, mtime, size)
    key = hashlib.md5(stamp.encode('utf-8')).hexdigest()
    return os.path.join(base, THUMB_DIR_NAME, key + '.jpg')


def make_thumb(root, target, size):
    """生成缩略图并缓存；已存在直接复用"""
    from PIL import Image

    output = thumb_path(root, target, size)
    if os.path.exists(output):
        return output
    make_dir(os.path.dirname(output))
    with Image.open(target) as image:
        image = image.convert('RGB')
        image.thumbnail((size, size))
        image.save(output, 'JPEG', quality=85)
    return output
