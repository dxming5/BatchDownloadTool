# -*- coding: utf-8 -*-
"""
路径模块

磁盘布局的唯一出处。只做路径运算与布局判定，不读不写记录，也不认识
「当前任务」：

    <下载根>/
    └── <平台 key>/
        └── <昵称> uid=<uid>/     目录名带 uid，重名与改名都不影响定位
            ├── author.json   uid、昵称、主页链接与粉丝数
            ├── aweme_id.txt   已下载篇 ID
            ├── video/         视频
            │   └── .preview/  视频预览图（取首帧封面）
            └── picture/       图片
                └── .preview/  图片预览图（缩略图）

媒体只落在 video/ 与 picture/，两类目录下的预览图统一放同名的 .preview/
子目录（隐藏目录，不参与媒体遍历）。下载中的临时文件（.part）同样落在
这两类目录里，与最终文件同目录，完成后直接改名即可。

博主由「平台 key + uid」标识：uid 写进目录名后缀（` uid=<uid>`）与记录
文件，目录名其余部分只是昵称，改名时整目录重命名即可。所有位置都按
「平台 key + 目录名」现算，不保存路径；调用方不接收目录参数，也不在别处
拼目录。下载中的临时文件、最终落盘文件、预览文件的文件名规则同样在这里，
下载层只提供时间前缀、序号、扩展名与「这是不是视频」。
"""

import os

from core import settings
from core.utils import make_dir, safe_name


RECORD_NAME = 'author.json'          # 博主记录文件：uid / 昵称 / 主页链接 / 粉丝数
UID_SEP = ' uid='                    # 目录名里的 uid 分隔：<昵称> uid=<uid>
ID_RECORD_NAME = 'aweme_id.txt'      # 保留原名，避免已有下载记录失效
VIDEO_DIR_NAME = 'video'
PICTURE_DIR_NAME = 'picture'
PREVIEW_DIR_NAME = '.preview'        # 预览图子目录：video/ 与 picture/ 下同名
PREVIEW_EXT = '.jpg'
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


def dir_name(nickname, uid):
    """博主目录名：<昵称> uid=<uid>；没有 uid 时退化为纯昵称

    昵称被剔空时用 blogger 兜底，保证目录名里始终有 uid，重名不会撞车。
    """
    nick = blogger_name(nickname) or 'blogger'
    token = (uid or '').strip()
    return '{}{}{}'.format(nick, UID_SEP, token) if token else nick


def split_dir_name(name):
    """目录名 → (昵称, uid)；没有 uid= 后缀时 uid 为空串

    用 rpartition 取最后一个分隔，昵称本身含「 uid=」也不会误切。
    """
    clean = blogger_name(name)
    if UID_SEP in clean:
        nick, _, token = clean.rpartition(UID_SEP)
        if nick and token:
            return nick, token
    return clean, ''


def dir_nickname(name):
    """目录名里的昵称部分（展示用）"""
    return split_dir_name(name)[0]


def dir_uid(name):
    """目录名里的 uid（定位与去重用）；没有后缀返回空串"""
    return split_dir_name(name)[1]


def blogger_dir(key, name):
    """博主目录：<平台目录>/<目录名>；名字不可用时返回空串"""
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


def video_dir(key, name):
    return _child(key, name, VIDEO_DIR_NAME)


def picture_dir(key, name):
    return _child(key, name, PICTURE_DIR_NAME)


def media_dir(key, name, is_video):
    """媒体目录：视频 video/，图片 picture/"""
    return _child(key, name, VIDEO_DIR_NAME if is_video else PICTURE_DIR_NAME)


def preview_dir(key, name, is_video):
    """预览图目录：<博主>/video/.preview 或 <博主>/picture/.preview"""
    return _child(key, name,
                  VIDEO_DIR_NAME if is_video else PICTURE_DIR_NAME,
                  PREVIEW_DIR_NAME)


def ids_path(key, name):
    return _child(key, name, ID_RECORD_NAME)


def record_path(key, name):
    """博主记录文件：<博主目录>/author.json"""
    return _child(key, name, RECORD_NAME)


def has_record(key, name):
    """博主目录里是否已有记录文件（author.json）"""
    path = record_path(key, name)
    return bool(path) and os.path.isfile(path)


def uid_dirs(key, uid):
    """平台目录下目录名以「 uid=<uid>」结尾的目录（改名时按 uid 定位旧目录）

    uid 直接写在目录名后缀里，所以「这个 uid 有没有目录」只需比较目录名，
    不必打开记录文件。返回目录名（不是路径），排序后返回便于调用方稳定
    选择；uid 含路径分隔符或为 . / .. 时直接不认。
    """
    clean = (uid or '').strip()
    if not clean or clean in ('.', '..') or '/' in clean or '\\' in clean:
        return []
    suffix = UID_SEP + clean
    base = platform_dir(key)
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return []
    return [entry for entry in entries
            if not entry.startswith('.')
            and entry.endswith(suffix)
            and os.path.isdir(os.path.join(base, entry))]


# ---------------- 落盘与预览文件 ----------------

def part_path(key, name, create_time, index, is_video):
    """下载中的临时文件：与最终文件同目录（video/ 或 picture/）"""
    if is_video:
        return os.path.join(media_dir(key, name, True),
                            '{}.{}'.format(create_time, PART_EXT))
    return os.path.join(media_dir(key, name, False),
                        '{} - {}.{}'.format(create_time, index + 1, PART_EXT))


def media_path(key, name, create_time, index, ext, is_video):
    """最终落盘路径：视频进 video/，图片进 picture/"""
    if is_video:
        return os.path.join(media_dir(key, name, True),
                            '{}.{}'.format(create_time, ext))
    return os.path.join(media_dir(key, name, False),
                        '{} - {}.{}'.format(create_time, index + 1, ext))


def preview_file(key, name, is_video, key_hash):
    """预览图文件：video/.preview/<hash>.jpg 或 picture/.preview/<hash>.jpg"""
    return os.path.join(preview_dir(key, name, is_video),
                        key_hash + PREVIEW_EXT)


# ---------------- 判定 ----------------

def inner_path(key, name, path):
    """绝对路径相对博主目录的形式（预览图 key 用，统一 / 分隔）"""
    return os.path.relpath(path, blogger_dir(key, name)).replace('\\', '/')


def is_inside(key, name, path):
    """path 是否位于该博主目录内（真实路径比较，用于越界校验）"""
    base = blogger_dir(key, name)
    if not base or not path:
        return False
    base = os.path.realpath(base)
    target = os.path.realpath(path)
    return target != base and target.startswith(base + os.sep)


def is_preview(rel_path):
    """相对路径是否指向预览图：<平台>/<博主>/(video|picture)/.preview/*.jpg"""
    parts = [part for part in (rel_path or '').replace('\\', '/').split('/')
             if part]
    return (len(parts) >= 4
            and parts[-2] == PREVIEW_DIR_NAME
            and parts[-3] in (VIDEO_DIR_NAME, PICTURE_DIR_NAME))


def media_files(key, name, exts):
    """博主目录下的媒体文件：只遍历 video/ 与 picture/，隐藏目录不参与"""
    base = blogger_dir(key, name)
    if not base or not os.path.isdir(base):
        return []

    exts = tuple(ext.lower() for ext in exts)
    found = []
    for folder in (VIDEO_DIR_NAME, PICTURE_DIR_NAME):
        for current, dirs, files in os.walk(os.path.join(base, folder)):
            dirs[:] = [entry for entry in dirs if not entry.startswith('.')]
            for file_name in files:
                if file_name.lower().endswith(exts):
                    found.append(os.path.join(current, file_name))
    return found


def is_blogger_dir(key, name):
    """该目录是否符合博主目录特征：有记录文件、有视频目录或有图片目录

    只看一级目录内容，不递归；「有哪些博主」由业务层列举平台目录后
    逐个调用本函数判定，本模块不承担集合语义。记录文件为 author.json
    （内容校验由 core.store 负责）。媒体一律在 video/ 与 picture/ 下，
    博主目录根上的零散文件不参与判定。
    """
    full = blogger_dir(key, name)
    if not full or not os.path.isdir(full):
        return False
    if has_record(key, name):
        return True
    for folder in (VIDEO_DIR_NAME, PICTURE_DIR_NAME):
        if os.path.isdir(os.path.join(full, folder)):
            return True
    return False
