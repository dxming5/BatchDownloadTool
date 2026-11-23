# -*- coding: utf-8 -*-
"""
存储模块

博主身份、记录文件与「当前任务目标」。磁盘布局全部在 core.paths，本模块
不做任何路径运算：

    Blogger：只持「平台 key + 博主名」，记录文件（homepage.txt / aweme_id.txt）
        由它读写，位置一律问 core.paths；
    当前任务目标：编排层 activate、结束 clear，下载层与平台层据此向 store
        取博主名、ID 记录与落盘路径，彼此之间不再传递博主对象。
"""

from core import paths
from core.utils import append_list_txt, make_txt, read_txt, rm_dir


class Blogger(object):
    """一位博主：身份是「平台标识 + 博主名」，位置与记录文件都由 paths 给出"""

    def __init__(self, key, name):
        self.key = key
        self.name = paths.blogger_name(name)

    def __str__(self):
        return self.name

    # ---------------- 目录 ----------------

    def _existing(self, create=False):
        """校验后的博主目录；名字非法、越界或不存在（未要求创建）返回 None"""
        return paths.existing_dir(self.key, self.name, create)

    def exists(self):
        return self._existing() is not None

    def create(self):
        """创建博主目录与 ID 记录文件，成功返回自身，失败返回 None"""
        if not self._existing(create=True):
            return None
        make_txt(paths.ids_path(self.key, self.name))
        return self

    def delete(self):
        """删除博主目录（连同缓存目录）"""
        target = self._existing()
        if not target:
            return False
        rm_dir(target)
        return True

    # ---------------- 去重记录 ----------------

    def read_ids(self):
        if not self._existing():
            return set()
        return set(read_txt(paths.ids_path(self.key, self.name)))

    def append_ids(self, ids):
        if self._existing() and ids:
            append_list_txt(paths.ids_path(self.key, self.name), list(ids))

    # ---------------- 主页链接 ----------------

    def save_homepage(self, url):
        url = (url or '').strip()
        if not url or not self._existing(create=True):
            return ''
        path = paths.homepage_path(self.key, self.name)
        with open(path, mode='w', encoding='utf-8') as handle:
            handle.write(url + '\n')
        return path

    def read_homepage(self):
        if not self._existing():
            return ''
        try:
            with open(paths.homepage_path(self.key, self.name),
                      mode='r', encoding='utf-8') as handle:
                return handle.read().strip()
        except OSError:
            return ''

    def item(self):
        """列表项：目录名即博主名，附带登记过的主页链接"""
        return {'name': self.name, 'homepage': self.read_homepage()}


# ---------------- 入口 ----------------

def blogger(key, name):
    """按 (平台标识, 博主名) 取已存在的博主；非法或不存在返回 None"""
    candidate = Blogger(key, name)
    return candidate if candidate.exists() else None


def ensure(key, name):
    """创建博主目录与 ID 记录文件，返回博主对象；名字不可用返回 None"""
    return Blogger(key, name).create()


def platform_bloggers(key, video_exts=('.mp4',)):
    """平台目录下的博主列表；判定规则见 core.paths"""
    return [Blogger(key, name)
            for name in paths.blogger_names(key, video_exts)]


# ---------------- 当前任务目标 ----------------
#
# 一次任务只有一个博主：编排层定位目录后 activate，结束时 clear。
# 下载层与平台层据此直接向 store 取博主名、ID 记录与落盘路径，
# 不再接收博主对象，也不接收任何目录参数。单任务是本模块的前提。

_current = None


def activate(target):
    """登记本次任务的目标博主；重复登记视为编排层出错"""
    global _current
    if _current is not None:
        raise RuntimeError('上一任务未清理目标：{}'.format(_current.name))
    _current = target


def clear():
    """任务结束时清空目标"""
    global _current
    _current = None


def current():
    """当前任务目标；未激活时返回 None"""
    return _current


def _active(action):
    if _current is None:
        raise RuntimeError('下载目标未激活：编排层未调用 store.activate()')
    return _current


def name():
    """当前任务目标的博主名；未激活返回空串"""
    return _current.name if _current else ''


def read_ids():
    """当前任务目标已记录的篇 ID；未激活返回空集合"""
    return _current.read_ids() if _current else set()


def id_recorder():
    """记 ID 回调：绑定当前目标，任务清理后仍能安全写入（取消兜底）"""
    return _active('记 ID').append_ids


def part_path(create_time, index):
    """下载中的临时文件路径（当前任务目标）"""
    target = _active('取临时路径')
    return paths.part_path(target.key, target.name, create_time, index)


def media_path(create_time, index, ext, is_video):
    """最终落盘路径（当前任务目标）"""
    target = _active('取落盘路径')
    return paths.media_path(target.key, target.name, create_time, index,
                            ext, is_video)
