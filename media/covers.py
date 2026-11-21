# -*- coding: utf-8 -*-
"""视频封面：前端取首帧后回存到 博主目录/.cache/covers，这里负责定位"""

import hashlib
import os

from media.cache import COVER_DIR_NAME, cache_root


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
