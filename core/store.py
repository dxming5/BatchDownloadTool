# -*- coding: utf-8 -*-
"""
存储模块

博主身份、记录文件与「当前任务目标」。磁盘布局全部在 core.paths，本模块
不做任何路径运算：

    Blogger：持「平台 key + 目录名」，目录名形如 <昵称> uid=<uid>，记录文件
        （author.json / downloaded_ids.txt）由它读写，位置一律问 core.paths；博主
        改名时由 attach 按 uid 定位旧目录并原地重命名，不按新昵称另建目录；
    当前任务目标：编排层 activate、结束 clear，下载层与平台层据此向 store
        取博主名、ID 记录与落盘路径，彼此之间不再传递博主对象。
"""

import json
import os
import re

from pypinyin import lazy_pinyin

from core import paths
from core.utils import append_list_txt, make_txt, read_txt, rm_dir

# 记录文件名（即平台用户 ID）允许的字符：抖音 sec_user_id / 微博数字 uid /
# 小红书 user_id 都落在其中；同时挡住路径分隔符与 .. 这类非法文件名
UID_RE = re.compile(r'^[A-Za-z0-9_-]{1,128}$')


def _same_dir(first, second):
    """两个路径是否指向同一个目录（Windows / macOS 不区分大小写）"""
    return (os.path.normcase(os.path.abspath(first))
            == os.path.normcase(os.path.abspath(second)))


def _rename_dir(src, dst):
    """目录改名：一般一次 rename 到位，失败即放弃，绝不覆盖目标

    只改大小写时（Windows / macOS 不区分大小写）系统不承认这种移动，需
    先挪到同级的隐藏临时名再改到目标名；第二步失败就把临时目录改回原名，
    内容始终只在一处。
    """
    if not _same_dir(src, dst):
        try:
            os.rename(src, dst)
        except OSError:
            return False
        return True

    temp = os.path.join(os.path.dirname(dst),
                        '.rename-{}'.format(os.urandom(4).hex()))
    try:
        os.rename(src, temp)
    except OSError:
        return False
    try:
        os.rename(temp, dst)
    except OSError:
        try:
            os.rename(temp, src)
        except OSError:
            pass
        return False
    return True


class Blogger(object):
    """一位博主：身份是「平台标识 + uid」，目录名形如 <昵称> uid=<uid>"""

    def __init__(self, key, name):
        self.key = key
        self.name = paths.blogger_name(name)      # 完整目录名（含 uid 后缀）

    def __str__(self):
        return self.name

    @property
    def nickname(self):
        """目录名里的昵称部分（展示用）"""
        return paths.dir_nickname(self.name)

    @property
    def uid(self):
        """目录名里的 uid（定位与去重用）；没有后缀返回空串"""
        return paths.dir_uid(self.name)

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
        """删除博主目录（连同 video/、picture/ 里的预览图）"""
        target = self._existing()
        if not target:
            return False
        rm_dir(target)
        return True

    def rename(self, new_name):
        """把博主目录名改成新的 <昵称> uid=<uid>，目录内全部内容原样保留

        只用于「按 uid 定位到旧目录」后的改名：位置一律由「平台 key + 目录名」
        现算，改名后不需要再更新任何配置、链接或索引；author.json 与
        downloaded_ids.txt、媒体、预览图都在目录内，随目录一起走。

        目标名被别的目录占用、名字不可用或系统调用失败时保持原样返回 False，
        不覆盖、不合并，也不留下第二份目录。成功后本对象指向新目录，并把
        author.json 里的昵称同步成新名字。
        """
        clean = paths.blogger_name(new_name)
        if not clean or clean == self.name:
            return False
        src = self._existing()
        if not src:
            return False
        dst = paths.resolve_safely(paths.platform_dir(self.key), clean)
        if not dst:
            return False
        # 目标名已有别的东西（可能是另一位博主的目录）：宁可留着旧目录
        if not _same_dir(src, dst) and os.path.exists(dst):
            return False
        if not _rename_dir(src, dst):
            return False

        self.name = clean
        record = self.read_record()
        if record['url']:
            self.save_record(record['url'], record['uid'], record['fans'],
                             paths.dir_nickname(clean))
        return True

    # ---------------- 去重记录 ----------------

    def read_ids(self):
        if not self._existing():
            return set()
        return set(read_txt(paths.ids_path(self.key, self.name)))

    def append_ids(self, ids):
        if self._existing() and ids:
            append_list_txt(paths.ids_path(self.key, self.name), list(ids))

    # ---------------- 记录文件（uid / 昵称 / 主页链接 / 粉丝数） ----------------

    def record_file(self):
        """记录文件绝对路径：<博主目录>/author.json"""
        return paths.record_path(self.key, self.name)

    def read_record(self):
        """
        读取 author.json

        uid 与昵称优先取文件内容，缺失时回落到目录名（目录名里带 uid），
        这样手动拷进来的目录也能正常展示与更新。

        Returns:
            dict: {'uid': 平台用户 ID, 'nickname': 昵称,
                   'url': 主页链接, 'fans': 粉丝数}
                  没有记录时 url 为空串、fans 为 0
        """
        record = {'uid': self.uid, 'nickname': self.nickname,
                  'url': '', 'fans': 0}
        path = paths.record_path(self.key, self.name)
        if not path or not os.path.isfile(path):
            return record
        try:
            with open(path, mode='r', encoding='utf-8') as handle:
                text = handle.read().strip()
        except OSError:
            return record

        try:
            data = json.loads(text or '{}')
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}

        uid = (data.get('uid') or '').strip()
        if uid:
            record['uid'] = uid
        nickname = (data.get('nickname') or '').strip()
        if nickname:
            record['nickname'] = nickname
        record['url'] = (data.get('url') or '').strip()
        try:
            record['fans'] = max(0, int(data.get('fans') or 0))
        except (TypeError, ValueError):
            record['fans'] = 0
        return record

    def read_homepage(self):
        """登记过的主页链接；没有记录返回空串"""
        return self.read_record()['url']

    def save_record(self, url, uid, fans=None, nickname=''):
        """
        写 author.json（uid + 昵称 + 主页链接 + 粉丝数）

        uid 不可用时直接返回空串（没有 uid 就无法定位博主）；
        昵称留空时用目录名里的昵称；fans 为空或 0 时保留已有粉丝数，
        避免未登录的一轮把数据抹掉。

        Returns:
            str: 实际写入的文件路径；失败返回空串
        """
        url = (url or '').strip()
        clean = (uid or '').strip() or self.uid
        if not url or not UID_RE.match(clean) or not self._existing(create=True):
            return ''

        try:
            fans_value = max(0, int(fans)) if fans is not None else 0
        except (TypeError, ValueError):
            fans_value = 0
        if not fans_value:
            fans_value = self.read_record()['fans']

        path = paths.record_path(self.key, self.name)
        payload = {'uid': clean,
                   'nickname': (nickname or '').strip() or self.nickname,
                   'url': url,
                   'fans': fans_value}
        try:
            with open(path, mode='w', encoding='utf-8') as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write('\n')
        except OSError:
            return ''
        return path

    def item(self):
        """列表项：目录名用于定位，nickname 用于展示"""
        record = self.read_record()
        return {'name': self.name,
                'label': record['nickname'],
                'uid': record['uid'],
                'homepage': record['url'],
                'fans': record['fans']}


# ---------------- 入口 ----------------

def blogger(key, name):
    """按 (平台标识, 博主名) 取已存在的博主；非法或不存在返回 None"""
    candidate = Blogger(key, name)
    return candidate if candidate.exists() else None


def ensure(key, name):
    """创建博主目录与 ID 记录文件，返回博主对象；名字不可用返回 None"""
    return Blogger(key, name).create()


def find_by_uid(key, uid, prefer=''):
    """按 uid 定位平台目录下的已有博主；没有对应目录时返回 None

    uid 写在目录名后缀（` uid=<uid>`）里，目录改什么昵称都能找到。
    prefer 是期望的目录名：同 uid 出现多个目录（异常情况）时优先取它。
    """
    names = paths.uid_dirs(key, uid)
    if not names:
        return None
    clean = paths.blogger_name(prefer)
    return Blogger(key, clean if clean in names else names[0])


def attach(key, name, uid):
    """按 uid 认领博主目录：已有目录原地改名，没有才新建

    目录名固定为 <昵称> uid=<uid>，uid 是唯一标识：改名时把旧目录整体
    重命名为最新昵称（author.json、媒体、预览图随目录一起走），而不是按
    新昵称另建目录；改名不成功（目标名被占用、名字不可用）时继续用 uid
    对应的旧目录，绝不留两份数据。同一 uid 的目录名天然唯一，因此正常
    情况下不存在撞名。

    Returns:
        tuple: (blogger, created, renamed)
            blogger 为 None 表示名字不可用；
            created 表示本次任务新建了目录；
            renamed 为 (旧名, 新名)，没有改名时为 None
    """
    clean = paths.blogger_name(name)
    if not clean:
        return None, False, None

    token = (uid or '').strip()
    if not UID_RE.match(token):
        # 没有可用 uid 就只能按名字来：同名目录当作同一位博主沿用
        created = not Blogger(key, clean).exists()
        blogger = ensure(key, clean)
        return blogger, bool(blogger) and created, None

    target = paths.dir_name(clean, token)
    existing = find_by_uid(key, token, target)
    if existing is not None:
        old = existing.name
        if old == target:
            return existing, False, None
        if existing.rename(target):
            return Blogger(key, target), False, (old, target)
        return existing, False, None

    blogger = ensure(key, target)
    return blogger, bool(blogger), None


def blogger_order(blogger):
    """博主展示排序键：符号最前，其次数字，最后字母与汉字按拼音升序

    按昵称排序（不含目录名里的 uid 后缀），首字符决定分组（符号/标点/
    emoji → 数字 → 字母与汉字），组内用 lazy_pinyin 转换后比较并忽略
    大小写；这样「“符”LL」「🥑」这类以符号开头的名字不会因为码点大而
    被甩到列表末尾。昵称相同时用 uid 兜底，保证顺序稳定不跳动。
    """
    text = (blogger.nickname or '').lower()
    head = text[:1]
    if not head or not head.isalnum():
        group = 0                       # 符号、标点、emoji
    elif head.isdigit():
        group = 1                       # 数字
    else:
        group = 2                       # 字母与汉字
    return group, lazy_pinyin(text), blogger.uid


def platform_bloggers(key):
    """平台目录下的博主列表：仅有下载内容的目录，按 blogger_order 排序

    列举平台目录属于集合语义，由本模块做；「一个目录算不算博主目录」
    仍交给 core.paths 判定，路径与布局规则不在本模块重复。
    """
    try:
        entries = os.listdir(paths.platform_dir(key))
    except OSError:
        return []
    bloggers = [Blogger(key, name) for name in entries
                if not name.startswith('.')
                and paths.is_blogger_dir(key, name)]
    bloggers.sort(key=blogger_order)
    return bloggers


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


def part_path(create_time, index, is_video):
    """下载中的临时文件路径（当前任务目标；与最终文件同目录）"""
    target = _active('取临时路径')
    return paths.part_path(target.key, target.name, create_time, index,
                           is_video)


def media_path(create_time, index, ext, is_video):
    """最终落盘路径（当前任务目标）"""
    target = _active('取落盘路径')
    return paths.media_path(target.key, target.name, create_time, index,
                            ext, is_video)
