# -*- coding: utf-8 -*-
"""
缓存位置计算

缓存放在博主目录内部（<平台>/<博主>/.cache），删除博主时随目录一并清掉，
也不会混进博主的图片列表。

注意：下载根下有两级含义（平台目录 / 博主目录），不能只认一级，
否则会退化成「平台级公共缓存」。
"""

import os

from core import registry

CACHE_DIR_NAME = '.cache'
THUMB_DIR_NAME = 'thumbs'
COVER_DIR_NAME = 'covers'


def cache_root(root, target):
    """
    媒体文件对应的预览缓存根目录

    Returns:
        tuple: (缓存根目录, 媒体文件相对博主目录的路径)
    """
    root_path = os.path.realpath(root)
    rel = os.path.relpath(os.path.realpath(target), root_path).replace('\\', '/')
    parts = rel.split('/')

    # 新结构：<平台>/<博主>/...
    if len(parts) > 2 and registry.is_platform_dir(parts[0]):
        return os.path.join(root_path, parts[0], parts[1], CACHE_DIR_NAME), '/'.join(parts[2:])

    # 兼容旧结构：<博主>/...
    if len(parts) > 1 and not parts[0].startswith('.'):
        return os.path.join(root_path, parts[0], CACHE_DIR_NAME), '/'.join(parts[1:])

    return os.path.join(root_path, CACHE_DIR_NAME), rel


def cache_dir(root, target, kind):
    """缩略图或封面目录"""
    base, _inner = cache_root(root, target)
    return os.path.join(base, kind)
