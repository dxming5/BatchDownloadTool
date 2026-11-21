# -*- coding: utf-8 -*-
"""
存储模块

唯一掌握磁盘布局的对象，取代原来的全局路径变量：

    下载根/
    └── <平台 key>/
        └── <博主名>/
            ├── homepage.txt   主页链接
            ├── aweme_id.txt   本博主已下载作品 ID，一篇一行
            ├── *.mp4
            ├── picture/       图片
            └── .cache/        预览缓存（缩略图、视频封面）

下载根来自配置；平台目录名即平台 key；博主目录的判定规则与旧版一致
（有 homepage.txt、有 picture/ 目录、或直接存在视频文件）。
"""

import os

from conf import settings
from core.utils import make_dir, make_txt, read_txt, append_list_txt, rm_dir, safe_name


HOMEPAGE_NAME = 'homepage.txt'
ID_RECORD_NAME = 'aweme_id.txt'      # 保留原名，避免已有下载记录失效
CACHE_DIR_NAME = '.cache'
PICTURE_DIR_NAME = 'picture'


def download_root():
    """当前生效的下载根目录（不存在时创建）"""
    root = settings.CONFIGURED_DOWNLOAD_DIR
    make_dir(root)
    return os.path.abspath(root)


def _resolve_safely(base, name):
    """把 name 解析为 base 下的绝对路径；越界或非法名返回 None"""
    parts = (name or '').replace('\\', '/').split('/')
    if not name or any(part.startswith('.') for part in parts):
        return None
    root = os.path.realpath(base)
    target = os.path.realpath(os.path.join(root, name))
    if target == root or not target.startswith(root + os.sep):
        return None
    return target


class BloggerStore(object):
    """某个平台下的博主存储（实例状态，不做模块级共享）"""

    def __init__(self, root, platform_key):
        self.root = os.path.abspath(root)
        self.platform_key = platform_key

    # ---------------- 目录 ----------------

    def platform_root(self, create=False):
        path = os.path.join(self.root, self.platform_key)
        if create:
            make_dir(path)
        return path

    def blogger_dir(self, name, create=False):
        """博主目录绝对路径；名字非法或越界返回 None"""
        base = self.platform_root(create=create)
        target = _resolve_safely(base, safe_name(name))
        if not target:
            return None
        if create:
            make_dir(target)
        return target if os.path.isdir(target) else None

    def ensure(self, name):
        """创建博主目录与 ID 记录文件，返回目录路径"""
        target = self.blogger_dir(name, create=True)
        if target:
            make_txt(os.path.join(target, ID_RECORD_NAME))
        return target

    def exists(self, name):
        return self.blogger_dir(name) is not None

    def delete(self, name):
        """删除博主目录（连同 .cache）"""
        target = self.blogger_dir(name)
        if not target:
            return False
        rm_dir(target)
        return True

    # ---------------- 去重记录 ----------------

    def ids_path(self, name):
        target = self.blogger_dir(name, create=True)
        return os.path.join(target, ID_RECORD_NAME) if target else None

    def read_ids(self, name):
        path = self.ids_path(name)
        return set(read_txt(path)) if path else set()

    def append_ids(self, name, ids):
        path = self.ids_path(name)
        if path and ids:
            append_list_txt(path, list(ids))

    # ---------------- 主页链接 ----------------

    def homepage_path(self, name):
        target = self.blogger_dir(name, create=True)
        return os.path.join(target, HOMEPAGE_NAME) if target else None

    def save_homepage(self, name, url):
        path = self.homepage_path(name)
        if not path or not (url or '').strip():
            return ''
        with open(path, mode='w', encoding='utf-8') as handle:
            handle.write(url.strip() + '\n')
        return path

    def read_homepage(self, name):
        target = self.blogger_dir(name)
        if not target:
            return ''
        path = os.path.join(target, HOMEPAGE_NAME)
        if not os.path.isfile(path):
            return ''
        try:
            with open(path, mode='r', encoding='utf-8') as handle:
                return handle.read().strip()
        except OSError:
            return ''

    # ---------------- 列举 ----------------

    def list_bloggers(self, video_exts=('.mp4',)):
        """平台目录下的博主名列表；判定规则与旧版一致"""
        base = self.platform_root()
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            return []

        names = []
        for entry in entries:
            if entry.startswith('.'):
                continue
            full = os.path.join(base, entry)
            if not os.path.isdir(full):
                continue
            if os.path.isfile(os.path.join(full, HOMEPAGE_NAME)):
                names.append(entry)
                continue
            if os.path.isdir(os.path.join(full, PICTURE_DIR_NAME)):
                names.append(entry)
                continue
            try:
                has_video = any(item.lower().endswith(video_exts)
                                for item in os.listdir(full))
            except OSError:
                has_video = False
            if has_video:
                names.append(entry)
        return names

    def blogger_item(self, name):
        """博主列表项：目录名即博主名，附带登记过的主页链接"""
        return {'name': name, 'homepage': self.read_homepage(name)}
