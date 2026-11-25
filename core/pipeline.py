# -*- coding: utf-8 -*-
"""
任务编排

一次任务固定为：认平台 → 取目标标识 → 抓取 → 定位存储 → 交给平台下载。
抓取与下载的细节都由平台实现承担；逐篇下载的闭环（去重、跳过、统计、
停止与收尾）也收口在本模块，不再另设会话对象。
"""

import time

from core import config
from core import downloader, location, registry, store


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
    # 目标标识与规范化主页链接一起从链接里取出：平台不再参与身份构造
    target_id, target_url = platform.parse_target(url)
    task.set_platform(platform.key, platform.label)

    options = platform.options()
    options['since_ts'] = (int(time.time()) - month_days * 86400) if month_days else None

    # ---------------- 流式会话 ----------------
    # 抓取与下载交错进行，阶段固定为 stream，避免界面在 scrape / download 间抖动
    task.set_phase('stream')
    stats = {
        'candidates': 0,        # 交到本任务的条目数
        'downloaded': 0,        # 本次全成功的篇数
        'skipped_known': 0,     # 已下载过的篇数
        'skipped_invalid': 0,   # 无时间 / 无媒体地址而跳过的篇数
        'files': 0,             # 成功落盘的文件数
        'failed_files': 0,      # 失败的文件（含无媒体地址的篇）数
        'processed': 0,         # 处理过的文件数
        'unknown_types': {},    # 平台归一化阶段报出的未知类型统计
    }
    failures = []
    state = {
        'identity': None,       # 冻结后的身份，非 None 视为 meta 已完成
        'known_ids': set(),     # 目录里已记录的篇 ID
        'blogger': None,        # 本次任务的目标博主
        'existed': False,       # 目录是否在本次任务前就存在
        'ok': False,            # meta 是否成功
        'error': '',            # meta 失败原因
        'closed': False,        # close 是否已执行
    }

    def on_meta(nickname):
        """
        冻结身份并定位目录；只认第一次调用，重复上报直接忽略

        Args:
            nickname: 平台从页面级数据取到的博主昵称（平台唯一需要提供的东西）

        Returns:
            dict: {'known_ids': set, 'ok': bool}；校验失败时不建目录，
                  已 mark('error') 并置 stop_flag，平台不得再派发任何 article
        """
        if state['identity'] is not None:
            return {'known_ids': set(state['known_ids']), 'ok': True}

        # 身份数据只有这一个构造点：ID 与主页链接来自链接解析，昵称来自页面
        frozen = location.identity(platform, nickname, target_id, target_url)
        nickname = frozen['nickname']

        if not nickname or not frozen['id']:
            state['error'] = '身份信息不完整：取不到博主昵称或 ID，已中止，不下载任何内容'
            task.mark('error', state['error'])
            task.stop_flag.set()
            return {'known_ids': set(), 'ok': False}

        # 定位目录：先按主页链接里的账号标识复用原目录（博主改名也能续上），
        # 再按昵称找，最后才按昵称新建。
        existed = True
        blogger = store.find_by_user_id(platform.key, frozen['id'],
                                        platform.user_id_of)
        if blogger is None:
            existed = store.blogger(platform.key, nickname) is not None
            blogger = store.ensure(platform.key, nickname)
        if not blogger:
            state['error'] = '博主目录不可用：{}'.format(nickname)
            task.mark('error', state['error'])
            task.stop_flag.set()
            return {'known_ids': set(), 'ok': False}

        # 登记主页链接：之后点「更新」不用再粘贴；blogger_id 不参与目录定位
        blogger.save_homepage(frozen['user_url'])
        store.activate(blogger)

        # 改名后按账号标识对上了旧目录：提示一句，避免以为建了新目录
        if blogger.name != nickname:
            task.push({'stage': 'notice',
                       'message': '复用已有目录「{}」（页面昵称：{}）'.format(
                           blogger.name, nickname)})

        state['blogger'] = blogger
        state['existed'] = existed
        state['identity'] = frozen
        state['ok'] = True
        state['known_ids'] = blogger.read_ids()
        task.set_identity(frozen)
        task.push({'stage': 'start', 'blogger': blogger.name or nickname})
        return {'known_ids': set(state['known_ids']), 'ok': True}

    def on_article(article):
        """
        下载一条统一 article：{'id', 'create_time', 'urls', 'kind'}

        create_time 是原始时间戳，这里用 downloader.format_create_time 转成
        文件名的命名前缀；一篇的全部媒体都成功才由下载层记 ID。

        Returns:
            bool: True 继续取下一篇，False 应停止采集
        """
        if (state['closed'] or task.stop_flag.is_set()
                or state['identity'] is None or store.current() is None):
            return False

        stats['candidates'] += 1
        article = article or {}
        article_id = str(article.get('id') or '')
        create_time = downloader.format_create_time(article.get('create_time'))
        urls = [url for url in (article.get('urls') or []) if url]

        # 已下载的篇：不计入本次文件数，保留目录与记录
        if article_id and article_id in state['known_ids']:
            stats['skipped_known'] += 1
            task.push({'stage': 'notice',
                       'message': '    跳过：已下载过（{}）'.format(article_id)})
            return True

        # 没有发布时间就无法命名，按现有语义跳过且不记 ID
        if not create_time:
            stats['skipped_invalid'] += 1
            task.push({'stage': 'item', 'aweme_id': article_id,
                       'reason': 'no_time', 'ok': False})
            return True

        # 没有可用媒体地址：记失败、不记 ID
        if not urls:
            failure = {'url': '', 'file': article_id,
                       'error': '没有可下载的媒体地址（{}）'.format(
                           platform.media_label or '媒体')}
            failures.append(failure)
            stats['skipped_invalid'] += 1
            stats['failed_files'] += 1
            task.push({'stage': 'failure', 'failure': failure})
            task.push({'stage': 'item', 'aweme_id': article_id,
                       'reason': 'no_media', 'ok': False})
            return True

        headers = None
        try:
            headers = platform.headers()
        except Exception:
            pass
        finish = getattr(platform, 'post_process', None)
        if not callable(finish):
            finish = None

        job = {'id': article_id, 'create_time': create_time, 'urls': urls,
               'kind': article.get('kind') or ''}
        result = downloader.fetch_article(
            job, headers, config.DOWNLOAD_WORKERS,
            should_stop=task.stop_flag.is_set,
            finish=finish,
            log=lambda message: task.push({'stage': 'notice',
                                           'message': message}),
            record_ids=store.id_recorder())

        files = result.get('files') or 0
        article_failures = result.get('failures') or []
        stats['files'] += files
        stats['failed_files'] += len(article_failures)
        stats['processed'] += files + len(article_failures)
        failures.extend(article_failures)
        for failure in article_failures:
            task.push({'stage': 'failure', 'failure': failure})
        if result.get('ok'):
            stats['downloaded'] += 1

        file_name = result.get('file') or ''
        if not result.get('ok') and article_failures:
            file_name = article_failures[0].get('file') or file_name
        task.push({'stage': 'item', 'aweme_id': article_id,
                   'ok': result.get('ok'), 'file': file_name})

        return not (result.get('cancelled') or task.stop_flag.is_set())

    def close():
        """
        幂等收尾：删除本次新建的空目录、输出跳过汇总、store.clear()

        Returns:
            dict: stats，供收口时一次性读取
        """
        if state['closed']:
            return stats
        state['closed'] = True
        try:
            # 一条都没交出来：撤掉本次刚建的空目录，保留已有博主目录
            if (stats['candidates'] == 0 and state['blogger']
                    and not state['existed']):
                state['blogger'].delete()
            if stats['skipped_known']:
                task.push({'stage': 'notice',
                           'message': '跳过 {} 篇已下载过的内容'.format(
                               stats['skipped_known'])})
            if stats['skipped_invalid']:
                task.push({'stage': 'notice',
                           'message': '跳过 {} 篇无效条目（无时间或无媒体地址）'.format(
                               stats['skipped_invalid'])})
            unknown = stats['unknown_types']
            if unknown:
                task.push({'stage': 'notice',
                           'message': '跳过未知类型共 {} 篇：{}'.format(
                               sum(unknown.values()),
                               ', '.join('{}={}'.format(key, value)
                                         for key, value
                                         in sorted(unknown.items())))})
        finally:
            store.clear()
        return stats

    scrape_result = {}
    scrape_error = ''
    login_error = ''
    try:
        try:
            scrape_result = platform.scrape_stream(
                target_url, options,
                on_meta=on_meta,
                on_article=on_article,
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
        close()

    # 平台归一化阶段的未知类型统计并入任务汇总
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

    if state['error']:
        status, message = 'error', state['error']
    elif login_error:
        status, message = 'error', login_error
    elif cancelled:
        status, message = 'cancelled', '任务已取消'
    elif scrape_error:
        status, message = 'error', scrape_error
    elif not state['ok']:
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
               'failures': failures,
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
