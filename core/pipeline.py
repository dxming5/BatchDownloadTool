# -*- coding: utf-8 -*-
"""
任务编排

一次任务固定为：认平台 → 取目标标识 → 抓取 → 定位存储 → 交给平台下载。
抓取与下载的细节都由平台实现承担，这里只做流程与状态衔接。
"""

import time

from core import registry
from core.store import BloggerStore, download_root
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

    # ---------------- 抓取 ----------------
    task.set_phase('scrape')
    result = platform.scrape(target, options,
                             on_progress=task.push,
                             should_stop=task.stop_flag.is_set)

    if task.stop_flag.is_set() or result.get('cancelled'):
        task.mark('cancelled', '抓取已取消')
        return

    # ---------------- 存储定位 ----------------
    folder_name = safe_name(result.get('nickname') or '') or target
    task.set_name(folder_name)

    store = BloggerStore(download_root(), platform.key)
    existed = store.exists(folder_name)
    if not store.ensure(folder_name):
        task.mark('error', '博主目录不可用：{}'.format(folder_name))
        return
    # 登记主页链接，之后点「更新」就不用再粘贴
    store.save_homepage(folder_name, result.get('user_url') or '')

    # ---------------- 下载 ----------------
    task.set_phase('download')
    stats = platform.download(result, store,
                              on_progress=task.push,
                              should_stop=task.stop_flag.is_set,
                              media_label=platform.media_label) or {}

    # 平台一条都没挑出来：撤掉可能刚建的空目录
    if not stats.get('total'):
        if not existed:
            store.delete(folder_name)
        task.mark('error', '没有可下载内容：可能未登录、没有公开作品、已全部下载过，'
                           '或超出所选时间范围')
        return

    if task.stop_flag.is_set():
        task.mark('cancelled', '任务已取消')
    elif task.is_running():
        task.mark('done')
