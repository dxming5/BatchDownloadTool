# -*- coding: utf-8 -*-
"""
抖音下载组装

把抓取结果组装成「下什么、从哪下」，再交给 core.downloader 统一执行。

请求头需要带抖音的 Referer。先按抖音接口结构挑出可下载的作品
（作者与目标博主不一致的直接丢弃），再逐条落盘：
目录与去重名单只读一次，全部媒体成功才记录 ID，ID 每满一批落盘一次。
"""

import os

from conf import settings
from core import downloader, store
from core.utils import safe_name

KEY = 'douyin'

ID_RECORD_LABEL = '抖音作品'


def headers():
    """下载请求头：公共头 + 平台段里的 referer / accept"""
    values = dict(settings.HEADER)
    options = settings.platform_settings(KEY)
    if options.get('referer'):
        values['Referer'] = options['referer']
    if options.get('accept'):
        values['Accept'] = options['accept']
    return values


def classify(aweme, image_types, video_types):
    """按配置判定作品类型；不属于任何已配置类型时返回 None"""
    aweme_type = aweme.get('aweme_type')
    if aweme_type in image_types:
        return 'image'
    if aweme_type in video_types:
        return 'video'
    return None


def media_urls(aweme, kind):
    """
    抖音取址规则

    图片：每张图取 url_list 里的第一个；
    视频：取 play_addr.url_list 的第一个（同一文件的多条地址是备用 CDN，不能都下）。
    """
    if kind == 'image':
        urls = []
        for image in aweme.get('images') or []:
            candidates = (image or {}).get('url_list') or []
            if candidates:
                urls.append(candidates[0])
        return urls
    play_addr = (aweme.get('video') or {}).get('play_addr') or {}
    return (play_addr.get('url_list') or [])[:1]


def collect(result):
    """
    从原始接口数据里挑出可下载的作品

    作者与目标博主不一致的作品（合作、改名、接口异常）直接丢弃：
    一次任务只对应一个博主目录。

    Returns:
        tuple: (entries, unknown_types, dropped_authors)
            entries 每项自带原始 dict 与命名需要的字段；
            dropped_authors 为「作者昵称 -> 条数」，只用于日志提示
    """
    values = settings.platform_settings(KEY)
    image_types = values.get('image_types') or ()
    video_types = values.get('video_types') or ()

    expected = safe_name(result.get('nickname') or '')

    entries = []
    unknown = {}
    dropped = {}
    seen = set()

    for aweme in result.get('aweme_list') or []:
        if not isinstance(aweme, dict):
            continue

        aweme_id = aweme.get('aweme_id')
        if not aweme_id:
            continue
        aweme_id = str(aweme_id)
        if aweme_id in seen:
            continue

        author = safe_name((aweme.get('author') or {}).get('nickname') or '')
        if not author:
            continue

        # 名字对不上：不是目标博主，丢弃；昵称取不到时放弃过滤
        if expected and author != expected:
            dropped[author] = dropped.get(author, 0) + 1
            continue

        kind = classify(aweme, image_types, video_types)
        if kind is None:
            aweme_type = aweme.get('aweme_type')
            label = '缺失' if aweme_type is None else str(aweme_type)
            unknown[label] = unknown.get(label, 0) + 1
            continue

        seen.add(aweme_id)
        entries.append({'id': aweme_id, 'author': author, 'kind': kind,
                        'create_time': aweme.get('create_time') or 0,
                        'raw': aweme})

    return entries, unknown, dropped


def run(result, on_progress=None, should_stop=None,
        media_label=ID_RECORD_LABEL):
    """
    并发下载抖音作品，进度按实际媒体地址数统计

    一次任务只有一个博主：collect 已滤掉作者不一致的作品。
    博主名与已记录的 ID 从 store 取（编排层已 activate）；落盘路径也由
    下载层向 store 要，本模块不碰任何路径。
    先由主线程挑出要下载的作品（命名与落盘交给下载层），再交给线程池并发
    拉取；每个文件完成即计 1 个进度，记 ID 由下载层按篇聚合（全部媒体成功
    才记，满一批落盘一次），本模块只上报条目事件。
    """
    entries, unknown, dropped = collect(result)
    total = len(entries)    # 篇数，仅交给编排层判断「有没有可下载内容」
    state = {'done': 0, 'total': 0, 'stopped': False}
    failures = []
    request_headers = headers()
    no_time_total = 0
    workers = settings.DOWNLOAD_WORKERS

    def cancelled():
        return bool(should_stop and should_stop())

    def notify(event):
        """安全上报：界面层异常不中断下载"""
        if not on_progress:
            return
        try:
            on_progress(event)
        except Exception:
            pass

    def notice(message):
        notify({'stage': 'notice', 'message': message})

    def progress(done, total_urls):
        """fetch_all 每个地址完成后的进度回调：只发事件，不写日志"""
        state['done'] = done
        state['total'] = total_urls
        notify({'stage': 'notice', 'done': done, 'total': total_urls})

    def report(entry, ok, skipped=False, reason='', create_time='', file_name=''):
        """一条处理完：只上报条目事件，进度由 fetch_all 统一负责"""
        notify({'stage': 'item', 'aweme_id': entry['id'],
                'create_time': create_time, 'file': file_name,
                'skipped': skipped, 'ok': ok, 'reason': reason})

    if dropped:
        notice('跳过 {} 篇作者与目标博主不一致的作品：{}'.format(
            sum(dropped.values()),
            ', '.join('{}={}'.format(name, count)
                      for name, count in sorted(dropped.items()))))

    # 一条都没挑出来：交给编排层报错，不发任何下载事件
    if not total:
        return {'done': 0, 'total': 0, 'stopped': False,
                'failures': [], 'unknown_types': unknown}

    if not store.name():
        notice('博主名不可用')
        return {'done': 0, 'total': 0, 'stopped': False,
                'failures': [], 'unknown_types': unknown}
    done_ids = store.read_ids()

    video_total = sum(1 for entry in entries if entry['kind'] == 'video')
    image_total = sum(1 for entry in entries if entry['kind'] == 'image')
    notify({'stage': 'start', 'blogger': store.name(),
            'video_total': video_total, 'picture_total': image_total})

    jobs = []           # (条目 ID, 发布时间, [地址])，交给统一下载层

    # ---------------- 主线程：挑条目、算发布时间 ----------------
    # 命名与撞名判断都交给统一下载层，这里只挑条目、算时间、记归属
    for entry in entries:
        if cancelled():
            state['stopped'] = True
            break

        item_id = entry['id']
        create_time = downloader.format_create_time(entry['create_time'])

        # 没有发布时间就无法命名，跳过且不记录
        if not create_time:
            no_time_total += 1
            report(entry, False, skipped=True, reason='no_time')
            continue

        urls = [url for url in media_urls(entry['raw'], entry['kind']) if url]

        # 没有可用地址：记失败，不记录 ID
        if not urls:
            failures.append({'url': '', 'file': item_id,
                             'error': '没有可下载的媒体地址（{}）'.format(media_label)})
            report(entry, False, skipped=True, reason='no_media',
                   create_time=create_time)
            continue

        # 已下载的篇也照常进 jobs，由 fetch_all 统一校验跳过并计入进度
        jobs.append((item_id, create_time, urls))

    # ---------------- 线程池：并发拉取，结果回主线程聚合 ----------------
    if state['stopped']:
        jobs = []       # 挑选阶段已取消：一个地址都不提交

    entry_by_id = {entry['id']: entry for entry in entries}

    def article_done(item_id, ok, create_time, file_name):
        report(entry_by_id[item_id], ok, create_time=create_time,
               file_name=file_name)

    for item_id, url, target, ok, final_path, error in downloader.fetch_all(
            jobs, request_headers, workers, cancelled,
            log=notice, done_ids=done_ids, on_done=progress,
            record_ids=store.id_recorder(), on_article_done=article_done):
        # 取消后不再处理结果：已成功篇的 ID 由下载层 finally 兜底写出
        if cancelled():
            state['stopped'] = True
            break

        if not ok:
            failures.append({'url': url, 'file': os.path.basename(target),
                             'error': error})

    if unknown:
        notice('跳过未知类型共 {} 篇：{}'.format(
            sum(unknown.values()),
            ', '.join('{}={}'.format(key, value)
                      for key, value in sorted(unknown.items()))))
    if no_time_total:
        notice('跳过 {} 篇缺少发布时间、无法命名的作品'.format(no_time_total))

    notify({'stage': 'finished', 'blogger': '全部',
            'done': state['done'], 'total': state['total'],
            'failures': failures,
            'stopped': state['stopped'] or cancelled(),
            'unknown_types': dict(sorted(unknown.items()))})

    # 返回给编排层的 total 仍是篇数，只用于判断「有没有可下载内容」
    return {'done': state['done'], 'total': total,
            'stopped': state['stopped'], 'failures': failures,
            'unknown_types': unknown}
