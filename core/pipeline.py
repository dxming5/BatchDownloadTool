# -*- coding: utf-8 -*-
"""
任务编排

一次任务固定为：认平台 → 取目标标识 → 抓取 → 定位存储 → 交给平台下载。
抓取与下载的细节都由平台实现承担，这里只做流程与状态衔接。
"""

import time

from core import registry, store
from core.session import DownloadSession


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

    # ---------------- 流式会话 ----------------
    # 抓取与下载交错进行，阶段固定为 stream，避免界面在 scrape / download 间抖动
    task.set_phase('stream')
    session = DownloadSession(platform, task, target=target,
                              media_label=platform.media_label)
    scrape_result = {}
    scrape_error = ''
    login_error = ''
    try:
        try:
            scrape_result = platform.scrape_stream(
                target, options, session,
                on_progress=task.push,
                should_stop=task.stop_flag.is_set) or {}
        except RuntimeError as error:
            # 登录闸门超时等：平台抛带「未登录」的 RuntimeError
            message = str(error)
            scrape_error = message
            if '未登录' in message:
                login_error = message
        except Exception as error:
            scrape_error = '抓取失败：{}'.format(error)
    finally:
        # 无论成败都清目标；close 幂等，未 meta 时也能安全调用
        stats = session.close()

    # 平台归一化阶段的未知类型统计并入会话汇总
    unknown = dict(stats['unknown_types'])
    for key, value in (scrape_result.get('unknown_types') or {}).items():
        try:
            unknown[key] = unknown.get(key, 0) + int(value)
        except (TypeError, ValueError):
            pass
    stats['unknown_types'] = unknown

    if scrape_result.get('error'):
        message = str(scrape_result['error'])
        scrape_error = scrape_error or message
        if '未登录' in message:
            login_error = login_error or message

    # ---------------- 收口：状态与进度只有这一个出口 ----------------
    # 身份失败 / 登录超时都会置 stop_flag，语义却是 error，必须先于取消判定；
    # 用户取消由平台回报 cancelled 且置位 stop_flag，这里两者都认。
    cancelled = bool(scrape_result.get('cancelled')) or task.stop_flag.is_set()
    candidates = stats['candidates']
    files = stats['files']
    failed = stats['failed_files']

    if session.meta_error:
        status, message = 'error', session.meta_error
    elif login_error:
        status, message = 'error', login_error
    elif cancelled:
        status, message = 'cancelled', '任务已取消'
    elif scrape_error:
        status, message = 'error', scrape_error
    elif not session.meta_ok:
        status, message = 'error', '身份信息不可用，已中止，未下载任何内容'
    elif candidates == 0:
        status, message = 'error', (
            '没有可下载内容：可能未登录、没有公开作品、已全部下载过，'
            '或超出所选时间范围')
    elif not stats['downloaded'] and stats['skipped_known'] == candidates:
        status, message = 'done', '已全部下载过，无需重复下载'
    elif not files and failed:
        status, message = 'error', '全部下载失败：失败 {} 个文件'.format(failed)
    else:
        status, message = 'done', '任务完成，共处理 {} 篇、{} 个文件'.format(
            candidates, files)

    # 会话 / 平台可能已 mark 过终态，不重复写日志
    if task.status == 'running':
        task.mark(status, message)
    task.push({'stage': 'finished',
               'done': files,
               'total': files + failed,
               'failures': session.failures,
               'unknown_types': unknown,
               'processed': stats['processed'],
               'stopped': status == 'cancelled'})


def mark_downloaded(platform, users):
    """给关注列表每项标注是否已有下载目录（只读 homepage.txt，不建目录）"""
    existing = set()
    for blogger in store.platform_bloggers(platform.key):
        homepage = blogger.read_homepage()
        uid = platform.user_id_of(homepage) if homepage else ''
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
    不创建任何博主目录，也不写 homepage.txt。
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
