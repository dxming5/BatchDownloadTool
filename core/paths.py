# -*- coding: utf-8 -*-
"""
路径模块

磁盘布局的唯一出处。只做路径运算与布局判定，不读不写记录，也不认识
「当前任务」：

    <下载根>/
    └── <平台 key>/
        └── <博主名>/
            ├── homepage.txt   主页链接
            ├── aweme_id.txt   已下载篇 ID
            ├── *.mp4          视频
            ├── picture/       图片
            └── .cache/        预览缓存（thumbs/ 缩略图，covers/ 视频封面）

博主由「平台 key + 博主名」标识，所有位置都按这两个值现算，不保存路径。
调用方不接收目录参数，也不在别处拼目录；下载中的临时文件、最终落盘文件、
缓存文件的文件名规则同样在这里，下载层只提供时间前缀、序号、扩展名与
「这是不是视频」。
"""

import os

from conf import settings
from core.utils import make_dir, safe_name


HOMEPAGE_NAME = 'homepage.txt'
ID_RECORD_NAME = 'aweme_id.txt'      # 保留原名，避免已有下载记录失效
PICTURE_DIR_NAME = 'picture'
CACHE_DIR_NAME = '.cache'
THUMB_DIR_NAME = 'thumbs'
COVER_DIR_NAME = 'covers'
PART_EXT = 'part'                    # 下载中的临时文件后缀


# ---------------- 下载根 / 平台 ----------------

def download_root():
    """当前生效的下载根目录（不存在时创建）"""
    root = settings.CONFIGURED_DOWNLOAD_DIR
    make_dir(root)
    return os.path.abspath(root)


def platform_dir(key):
    """某个平台的目录：<下载根>/<平台 key>"""
    return os.path.join(download_root(), key)


def relative(path):
    """绝对路径 → 下载根内的相对路径（统一用 / 分隔，给前端用）"""
    return os.path.relpath(path, download_root()).replace('\\', '/')


def resolve(rel_path):
    """请求里的相对路径 → 下载根内的绝对文件路径；越界或不存在返回 None"""
    root = os.path.realpath(download_root())
    target = os.path.realpath(os.path.join(root, (rel_path or '').strip()))
    if target != root and not target.startswith(root + os.sep):
        return None
    return target if os.path.isfile(target) else None


def resolve_safely(base, name):
    """把 name 解析为 base 下的绝对路径；越界或非法名返回 None"""
    parts = (name or '').replace('\\', '/').split('/')
    if not name or any(part.startswith('.') for part in parts):
        return None
    root = os.path.realpath(base)
    target = os.path.realpath(os.path.join(root, name))
    if target == root or not target.startswith(root + os.sep):
        return None
    return target


# ---------------- 博主位置 ----------------

def blogger_name(name):
    """博主目录用的名字（剔除非法字符），不可用时返回空串"""
    return safe_name(name or '')


def blogger_dir(key, name):
    """博主目录：<平台目录>/<博主名>；名字不可用时返回空串"""
    clean = blogger_name(name)
    if not clean:
        return ''
    return os.path.join(platform_dir(key), clean)


def existing_dir(key, name, create=False):
    """校验后的博主目录；名字非法、越界或不存在（未要求创建）返回 None"""
    clean = blogger_name(name)
    if not clean:
        return None
    target = resolve_safely(platform_dir(key), clean)
    if not target:
        return None
    if create:
        make_dir(target)
    return target if os.path.isdir(target) else None


def _child(key, name, *parts):
    base = blogger_dir(key, name)
    return os.path.join(base, *parts) if base else ''


def picture_dir(key, name):
    return _child(key, name, PICTURE_DIR_NAME)


def cache_dir(key, name):
    return _child(key, name, CACHE_DIR_NAME)


def thumb_dir(key, name):
    return _child(key, name, CACHE_DIR_NAME, THUMB_DIR_NAME)


def cover_dir(key, name):
    return _child(key, name, CACHE_DIR_NAME, COVER_DIR_NAME)


def ids_path(key, name):
    return _child(key, name, ID_RECORD_NAME)


def homepage_path(key, name):
    return _child(key, name, HOMEPAGE_NAME)


# ---------------- 落盘与缓存文件 ----------------

def part_path(key, name, create_time, index):
    """下载中的临时文件：博主目录下 {时间} - {序号}.part"""
    return _child(key, name,
                  '{} - {}.{}'.format(create_time, index + 1, PART_EXT))


def media_path(key, name, create_time, index, ext, is_video):
    """最终落盘路径：视频进博主目录，图片进 picture/"""
    if is_video:
        return _child(key, name, '{}.{}'.format(create_time, ext))
    return _child(key, name, PICTURE_DIR_NAME,
                  '{} - {}.{}'.format(create_time, index + 1, ext))


def cover_file(key, name, key_hash):
    """视频封面缓存文件：<博主>/.cache/covers/<hash>.jpg"""
    return _child(key, name, CACHE_DIR_NAME, COVER_DIR_NAME, key_hash + '.jpg')


def thumb_file(key, name, key_hash):
    """缩略图缓存文件：<博主>/.cache/thumbs/<hash>.jpg"""
    return _child(key, name, CACHE_DIR_NAME, THUMB_DIR_NAME, key_hash + '.jpg')


# ---------------- 判定 ----------------

def inner_path(key, name, path):
    """绝对路径相对博主目录的形式（缓存 key 用，统一 / 分隔）"""
    return os.path.relpath(path, blogger_dir(key, name)).replace('\\', '/')


def is_inside(key, name, path):
    """path 是否位于该博主目录内（真实路径比较，用于越界校验）"""
    base = blogger_dir(key, name)
    if not base or not path:
        return False
    base = os.path.realpath(base)
    target = os.path.realpath(path)
    return target != base and target.startswith(base + os.sep)


def is_cover(rel_path):
    """相对路径是否指向视频封面缓存：<平台>/<博主>/.cache/covers/*.jpg"""
    parts = [part for part in (rel_path or '').replace('\\', '/').split('/')
             if part]
    return len(parts) >= 3 and parts[-3:-1] == [CACHE_DIR_NAME, COVER_DIR_NAME]


def media_files(key, name, exts):
    """博主目录下的媒体文件；隐藏目录（如 .cache）不参与遍历"""
    base = blogger_dir(key, name)
    if not base or not os.path.isdir(base):
        return []

    exts = tuple(ext.lower() for ext in exts)
    found = []
    for current, dirs, files in os.walk(base):
        dirs[:] = [entry for entry in dirs if not entry.startswith('.')]
        for file_name in files:
            if file_name.lower().endswith(exts):
                found.append(os.path.join(current, file_name))
    return found


def is_blogger_dir(key, name, video_exts=('.mp4',)):
    """该目录是否符合博主目录特征：有主页链接、有图片目录或有视频文件

    只看一级目录内容，不递归；「有哪些博主」由业务层列举平台目录后
    逐个调用本函数判定，本模块不承担集合语义。
    """
    full = blogger_dir(key, name)
    if not full or not os.path.isdir(full):
        return False
    if os.path.isfile(os.path.join(full, HOMEPAGE_NAME)):
        return True
    if os.path.isdir(os.path.join(full, PICTURE_DIR_NAME)):
        return True
    exts = tuple(ext.lower() for ext in video_exts)
    try:
        return any(entry.lower().endswith(exts) for entry in os.listdir(full))
    except OSError:
        return False
