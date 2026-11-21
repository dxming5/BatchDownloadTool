# -*- coding: utf-8 -*-
"""图片缩略图：用 Pillow 生成并缓存到 博主目录/.cache/thumbs"""

import hashlib
import os

from core.utils import make_dir
from media.cache import THUMB_DIR_NAME, cache_root


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
