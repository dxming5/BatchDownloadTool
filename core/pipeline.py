# -*- coding: utf-8 -*-
"""
任务编排

一次任务固定为：认平台 → 取目标标识 → 抓取 → 定位存储 → 交给平台下载。
抓取与下载的细节都由平台实现承担，这里只做流程与状态衔接。
"""

import time

from core import registry, store
from core.utils import safe_name


def resolve_platform(url):
    """按链接取平台；不支持时抛出带提示的 ValueError"""
    platform = registry.resolve(url)
    if not platform:
        raise ValueError('无法识别该链接所属平台，请粘贴博主主页地址')
    return platform


def run(url, month_days, task):
    """
    执行一次「抓取并下载」

    Args:
        url:        博主主页链接
        month_days: 只要最近若干天的作品；0 表示全部
        task:       core.task.Task 实例（进度与事件在这里收口）

    Raises:
        ValueError: 链接无法识别或平台参数非法
        RuntimeError: 抓取失败（无作品、未登录等）
    """
    platform = resolve_platform(url)
    target = platform.parse_target(url)
    task.set_platform(platform.key, platform.label)

    options = platform.options()
    options['since_ts'] = (int(time.time()) - month_days * 86400) if month_days else None

    # ---------------- 主页信息即刻登记 ----------------
    # 平台一拿到昵称 / 主页链接 / uid / 粉丝数就回调这里：博主目录与
    # author.json 立即落盘，不必等抓取结束（中途取消也能留下记录）
    registered = {'blogger': None, 'record': None, 'created': False}

    def announce(raw_name, blogger, renamed):
        """改名与登记都走同一种提示：改名或撞名时把实际目录名说出来"""
        if renamed:
            task.push({'stage': 'notice',
                       'message': '博主已改名：{} → {}（按 uid 重命名原目录）'
                                  .format(renamed[0], renamed[1])})
        elif blogger.nickname != raw_name:
            task.push({'stage': 'notice',
                       'message': '目录名 {} 与已有目录冲突或不可用，实际使用：{}'
                                  .format(raw_name, blogger.name)})
        task.push({'stage': 'notice',
                   'message': '已登记主页信息：{}'.format(blogger.nickname)})

    def register(info):
        """主页信息回调：只有第一次决定目录名，之后只补记录

        一次任务只允许一次别名修改：目录名以「刚拿到的主页信息」为准，
        后续回调（平台可能多次上报、抓取结束也可能再回传昵称）不再触发
        attach，避免同一次任务里目录名被反复改来改去。
        """
        info = info or {}
        folder = safe_name(info.get('nickname') or '') or target
        if not folder:
            return
        uid = (info.get('user_id') or '').strip()
        record = (folder, info.get('user_url') or '', uid,
                  info.get('follower_count'))
        if record == registered['record']:
            return
        if registered['blogger'] is not None:
            # 目录名已定：只刷新记录，昵称沿用目录名里的
            registered['record'] = record
            registered['blogger'].save_record(record[1], uid, record[3])
            return
        task.set_name(folder)
        # uid 是博主唯一标识：已有目录按 uid 认领，改名时原地重命名而不是新建
        blogger, created, renamed = store.attach(platform.key, folder, uid)
        if not blogger:
            return
        registered['created'] = registered['created'] or created
        blogger.save_record(record[1], uid, record[3], folder)
        registered['blogger'] = blogger
        registered['record'] = record
        announce(folder, blogger, renamed)

    # ---------------- 抓取 ----------------
    task.set_phase('scrape')
    result = platform.scrape(target, options,
                             on_progress=task.push,
                             should_stop=task.stop_flag.is_set,
                             on_profile=register)

    if task.stop_flag.is_set() or result.get('cancelled'):
        task.mark('cancelled', '抓取已取消')
        return

    # ---------------- 存储定位 ----------------
    # 目录名在「拿到主页信息」时就已定下：一次任务只改一次名。这里只兜底——
    # 主页信息没上报过（拿不到昵称）才用抓取结果里的昵称建目录，不二次改名。
    blogger = registered['blogger']
    if blogger is None:
        folder_name = safe_name(result.get('nickname') or '') or target
        blogger, created, renamed = store.attach(platform.key, folder_name,
                                                 result.get('user_id') or '')
        registered['created'] = registered['created'] or created
        if blogger:
            announce(folder_name, blogger, renamed)
    if not blogger:
        task.mark('error', '博主目录不可用：{}'.format(
            safe_name(result.get('nickname') or '') or target))
        return
    task.set_name(blogger.nickname or blogger.name)
    # 登记 uid / 昵称 / 主页链接 / 粉丝数（author.json），昵称一律用目录名里的，
    # 不传抓取结果里的昵称，避免旧昵称把刚改好的名字覆盖回去
    blogger.save_record(result.get('user_url') or '',
                        result.get('user_id') or '',
                        result.get('follower_count'))

    # ---------------- 下载组装 ----------------
    task.set_phase('download')
    # 登记本次任务目标：下载层只向 store 要路径，不再接收博主对象
    store.activate(blogger)
    try:
        stats = platform.assemble(result,
                                  on_progress=task.push,
                                  should_stop=task.stop_flag.is_set,
                                  media_label=platform.media_label) or {}
    finally:
        store.clear()

    # 平台一条都没挑出来：撤掉本次任务刚建的空目录
    if not stats.get('total'):
        if registered['created']:
            blogger.delete()
        task.mark('error', '没有可下载内容：可能未登录、没有公开作品、已全部下载过，'
                           '或超出所选时间范围')
        return

    if task.stop_flag.is_set():
        task.mark('cancelled', '任务已取消')
    elif task.is_running():
        task.mark('done')


def mark_downloaded(platform, users):
    """给关注列表每项标注是否已有下载目录（只读记录文件，不建目录）

    记录文件名就是平台用户 ID（抖音关注列表返回的也是 sec_user_id）。
    """
    existing = set()
    for blogger in store.platform_bloggers(platform.key):
        uid = blogger.read_record()['uid']
        if uid:
            existing.add(uid)

    items = []
    for user in users:
        item = dict(user)
        item['downloaded'] = bool(item.get('uid') and item.get('uid') in existing)
        items.append(item)
    return items


def run_following(platform_key, task):
    """
    抓取「当前登录账号」的关注列表

    只产出博主主页链接，交给前端勾选后再走正常抓取下载流程；
    不创建任何博主目录，也不写记录文件。
    """
    platform = registry.get(platform_key)
    if platform is None or not getattr(platform, 'supports_following', False):
        raise ValueError('该平台暂未支持抓取关注列表')

    task.set_platform(platform.key, platform.label)
    task.set_name('我的关注')
    task.set_phase('following')

    result = platform.following(platform.options(),
                                on_progress=task.push,
                                should_stop=task.stop_flag.is_set) or {}

    if task.stop_flag.is_set() or result.get('cancelled'):
        task.mark('cancelled', '已取消，关注列表未入队')
        return

    users = result.get('users') or []
    owner = (result.get('owner') or '').strip()
    if owner:
        task.set_name('{} 的关注'.format(owner))
    if not users:
        task.mark('error', '没有抓到关注列表：可能登录态失效，'
                           '或当前账号没有关注任何人')
        return

    task.set_following(mark_downloaded(platform, users))
    task.mark('done', '关注列表抓取完成，共 {} 位'.format(len(users)))
