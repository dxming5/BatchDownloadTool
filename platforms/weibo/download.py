# -*- coding: utf-8 -*-
"""
微博下载

每篇（一条微博）按内容类型走两条链路，取址与命名各不相同，落盘统一交给
downloader 并发执行：
    图片  mblog.pics[].large.url（缺失时回退 bmiddle / thumbnail / url）
          微博图床的无水印版恒为最长边 690，这里固定取原图以保住最高画质
    视频  page_info 里的一个清晰度地址，落成 {发布时间}.mp4
          视频角标是转码时烧录的，所有直链都带，无法去水印
类型由 parser.classify 判定：先看视频（视频微博的 pics 只是一张封面）。
两类都没有的篇（纯文字、头条文章、转发别人内容）跳过并计数提示。

进度按实际媒体地址（每个图片 / 视频文件）统计，由 downloader.fetch_all 统一
计算并上报；已下载的篇按其媒体数计入已完成，缺时间 / 无地址的篇由本模块在
挑选时跳过。ID 记录仍按篇：一条微博一个 mblog_id，全部媒体成功才写入
aweme_id.txt，任一失败则整篇不记录，下次整篇重下。
"""

import os

from conf import settings
from core import downloader, naming
from core.utils import safe_name
from platforms.weibo import parser

KEY = 'weibo'

ID_RECORD_LABEL = '微博作品'


def headers():
    """下载请求头：公共头 + 平台段里的 referer / accept"""
    values = dict(settings.HEADER)
    options = settings.platform_settings(KEY)
    if options.get('referer'):
        values['Referer'] = options['referer']
    if options.get('accept'):
        values['Accept'] = options['accept']
    return values


def collect(result):
    """
    从原始微博数据里挑出可下载的「篇」（一条微博 = 一篇）

    作者与目标博主不一致的微博（合作、改名、接口异常）直接丢弃：
    一次任务只对应一个博主目录。
    既没有图片也没有视频的微博（纯文字、头条文章、转发别人内容）没有可下载
    内容，直接丢弃，不占进度，只计数用于日志提示。

    Returns:
        tuple: (entries, skipped, dropped_authors)
            entries 每项是一篇微博，kind 为 image / video，media 为该篇的媒体；
            skipped 为「没有可下载内容」的篇数，只用于日志提示；
            dropped_authors 为「作者昵称 -> 篇数」，只用于日志提示
    """
    expected = safe_name(result.get('nickname') or '')

    entries = []
    dropped = {}
    seen = set()
    skipped = 0

    for mblog in result.get('mblogs') or []:
        if not isinstance(mblog, dict):
            continue

        mblog_id = parser.mblog_id_of(mblog)
        if not mblog_id or mblog_id in seen:
            continue

        author = parser.author_of(mblog)
        if not author:
            continue

        # 名字对不上：不是目标博主，丢弃；昵称取不到时放弃过滤
        if expected and author != expected:
            dropped[author] = dropped.get(author, 0) + 1
            continue

        kind = parser.classify(mblog)
        # 纯文字、文章、转发等没有可下载的媒体，不参与下载也不占进度
        if kind is None:
            skipped += 1
            continue

        media = (parser.video_items(mblog) if kind == 'video'
                 else parser.image_items(mblog))

        seen.add(mblog_id)
        entries.append({
            'id': mblog_id,
            'author': author,
            'kind': kind,
            'create_time': parser.created_at(mblog),
            'media': media,
            'raw': mblog,
        })

    return entries, skipped, dropped


def download(result, store, on_progress=None, should_stop=None,
             media_label=ID_RECORD_LABEL):
    """
    并发下载微博的图片与视频，进度按实际媒体地址数统计

    一次任务只有一个博主：collect 已滤掉作者不一致的微博。
    先由主线程逐篇算好落盘路径（建目录只在这一步做），再交给线程池并发拉取；
    每个文件完成即计 1 个进度，记 ID 由下载层按篇聚合。

    ID 记录与抖音、小红书一致：一条微博的全部媒体成功才把 mblog_id 写入
    aweme_id.txt，任一失败则整篇不记录，下次整篇重下；已记录的篇直接跳过。

    图片固定取接口原图（large，最长边可达 4000+）：微博图床的无水印版恒为
    最长边 690，与最高画质不可兼得。视频角标由微博转码时烧录，所有直链都带，
    无法去除，这里只取最高可用清晰度。
    """
    entries, skipped_total, dropped = collect(result)
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
        """一篇处理完：只上报条目事件，进度由 fetch_all 统一负责"""
        notify({'stage': 'item', 'aweme_id': entry['id'],
                'create_time': create_time, 'file': file_name,
                'skipped': skipped, 'ok': ok, 'reason': reason})

    if dropped:
        notice('跳过 {} 篇作者与目标博主不一致的微博：{}'.format(
            sum(dropped.values()),
            ', '.join('{}={}'.format(name, count)
                      for name, count in sorted(dropped.items()))))

    # 抓到的条数与下载篇数的差额都摊在这里，避免用户以为「抓少了」
    if skipped_total:
        notice('跳过 {} 篇没有图片、也没有视频的微博'
               '（纯文字、头条文章、转发他人内容）'.format(skipped_total))

    # 一篇都没挑出来：交给编排层报错，不发任何下载事件
    if not total:
        return {'done': 0, 'total': 0, 'stopped': False,
                'failures': [], 'unknown_types': {}}

    author = entries[0]['author']
    blogger_dir = store.ensure(author)
    if not blogger_dir:
        notice('博主名不可用：{}'.format(author))
        return {'done': 0, 'total': 0, 'stopped': False,
                'failures': [], 'unknown_types': {}}
    done_ids = store.read_ids(author)

    video_total = sum(1 for entry in entries if entry['kind'] == 'video')
    image_total = total - video_total
    notify({'stage': 'start', 'blogger': author,
            'video_total': video_total, 'picture_total': image_total})

    jobs = []           # (篇 ID, 发布时间, [地址])，交给统一下载层

    # ---------------- 主线程：挑条目、算发布时间 ----------------
    # 命名与撞名判断都交给统一下载层，这里只挑条目、算时间、记归属
    for entry in entries:
        if cancelled():
            state['stopped'] = True
            break

        item_id = entry['id']
        create_time = naming.format_create_time(entry['create_time'])

        # 没有发布时间就无法命名，整篇跳过且不记录
        if not create_time:
            no_time_total += 1
            report(entry, False, skipped=True, reason='no_time')
            continue

        urls = [item['url'] for item in entry['media'] if item.get('url')]

        # 没有可用地址：本篇记失败，不记录 ID
        # 视频地址缺失多半是链接失效或需要登录，按类型说明比笼统的「媒体」更好排查
        if not urls:
            kind_label = '视频' if entry['kind'] == 'video' else '图片'
            failures.append({'url': '', 'file': item_id,
                             'error': '没有可下载的{}地址（{}）'.format(
                                 kind_label, media_label)})
            report(entry, False, skipped=True, reason='no_media',
                   create_time=create_time)
            continue

        # 已下载的篇也照常进 jobs，由 fetch_all 统一校验跳过并计入进度
        jobs.append((item_id, create_time, urls))

    # ---------------- 线程池：并发拉取，结果回主线程按篇聚合 ----------------
    if state['stopped']:
        jobs = []       # 挑选阶段已取消：一个地址都不提交

    entry_by_id = {entry['id']: entry for entry in entries}

    def record_ids(batch):
        store.append_ids(author, batch)

    def article_done(item_id, ok, create_time, file_name):
        report(entry_by_id[item_id], ok, create_time=create_time,
               file_name=file_name)

    for item_id, url, target, ok, final_path, error in downloader.fetch_all(
            jobs, blogger_dir, request_headers, workers, cancelled,
            log=notice, done_ids=done_ids, on_done=progress,
            record_ids=record_ids, on_article_done=article_done):
        # 取消后不再处理结果：已成功篇的 ID 由下载层 finally 兜底写出
        if cancelled():
            state['stopped'] = True
            break

        if not ok:
            failures.append({'url': url, 'file': os.path.basename(target),
                             'error': error})

    if no_time_total:
        notice('跳过 {} 篇缺少发布时间、无法命名的微博'.format(no_time_total))

    notify({'stage': 'finished', 'blogger': '全部',
            'done': state['done'], 'total': state['total'],
            'failures': failures,
            'stopped': state['stopped'] or cancelled(),
            'unknown_types': {}})

    # 返回给编排层的 total 仍是篇数，只用于判断「有没有可下载内容」
    return {'done': state['done'], 'total': total,
            'stopped': state['stopped'], 'failures': failures,
            'unknown_types': {}}
