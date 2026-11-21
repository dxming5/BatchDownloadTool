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

进度按「篇」（一条微博）统计：一篇的全部媒体处理完才计 1 篇，与抖音、
小红书一致；上报给任务层的 done/total 是篇数，不是图片张数或视频个数。

ID 记录同样按篇（一条微博一个 mblog_id），与抖音 aweme_id、小红书 note_id
一致：一篇的全部媒体都成功才写入 aweme_id.txt，任一失败则整篇不记录，
下次整篇重下。这样断点续传、进度、去重三者都是同一个粒度，行为可预期。
"""

import os

from conf import settings
from core import downloader, naming
from core.utils import safe_name
from platforms.weibo import parser

KEY = 'weibo'

ID_RECORD_LABEL = '微博作品'

ID_FLUSH_BATCH = 20      # 每积累多少个篇 ID 落盘一次


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
    并发下载微博的图片与视频，进度只报「已处理篇数 / 总篇数」

    一次任务只有一个博主：collect 已滤掉作者不一致的微博。
    先由主线程逐篇算好落盘路径（建目录只在这一步做），再交给线程池并发拉取；
    结果回到主线程按篇聚合，一篇微博的全部媒体都处理完才计 1 篇进度。

    ID 记录与抖音、小红书一致：一条微博的全部媒体成功才把 mblog_id 写入
    aweme_id.txt，任一失败则整篇不记录，下次整篇重下；已记录的篇直接跳过。

    图片固定取接口原图（large，最长边可达 4000+）：微博图床的无水印版恒为
    最长边 690，与最高画质不可兼得。视频角标由微博转码时烧录，所有直链都带，
    无法去除，这里只取最高可用清晰度。
    """
    entries, skipped_total, dropped = collect(result)
    total = len(entries)
    state = {'done': 0, 'stopped': False}
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

    def report(entry, ok, skipped=False, reason='', create_time='', file_name=''):
        """一篇处理完：累加计数后连同 done/total 一起上报"""
        state['done'] = min(state['done'] + 1, total)
        notify({'stage': 'item', 'done': state['done'], 'total': total,
                'aweme_id': entry['id'], 'create_time': create_time,
                'file': file_name, 'skipped': skipped, 'ok': ok, 'reason': reason})

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
            'video_total': video_total, 'picture_total': image_total,
            'video_items': video_total, 'picture_items': image_total})

    written = {}        # 本次运行内 目标路径 -> 篇 ID，仅用于撞名提示
    new_ids = []
    pending = {}        # 篇 ID -> 剩余媒体数 / 是否全部成功 / 最后落盘文件
    jobs = []           # ((篇 ID, 媒体序号), 地址, 目标路径)，交给线程池并发拉取

    def flush_ids():
        """把缓冲中的篇 ID 落盘并清空；失败向上抛，由任务层报错"""
        if not new_ids:
            return
        store.append_ids(author, new_ids)
        del new_ids[:]

    # ---------------- 主线程：挑条目、算路径 ----------------
    # 建目录与撞名判断都只在这里做，线程池只管下载本身
    for entry in entries:
        if cancelled():
            state['stopped'] = True
            break

        item_id = entry['id']
        create_time = naming.format_create_time(entry['create_time'])

        # 已下载过：整篇跳过，但仍计 1 篇进度
        if item_id in done_ids:
            report(entry, True, skipped=True, reason='done',
                   create_time=create_time or '')
            continue

        # 没有发布时间就无法命名，整篇跳过且不记录
        if not create_time:
            no_time_total += 1
            report(entry, False, skipped=True, reason='no_time')
            continue

        downloadable = [item for item in entry['media'] if item.get('url')]

        # 没有可用地址：本篇记失败，不记录 ID
        # 视频地址缺失多半是链接失效或需要登录，按类型说明比笼统的「媒体」更好排查
        if not downloadable:
            kind_label = '视频' if entry['kind'] == 'video' else '图片'
            failures.append({'url': '', 'file': item_id,
                             'error': '没有可下载的{}地址（{}）'.format(
                                 kind_label, media_label)})
            report(entry, False, skipped=True, reason='no_media',
                   create_time=create_time)
            continue

        # 只报真正要下载的篇：已下载、缺发布时间、无可用地址的都在上方 continue 掉了
        # 并发下载下这条表示「已提交下载」，不代表此刻正在拉取
        # 日志固定为「下载 {发布时间}」，一篇一条，不逐媒体打印
        notice('下载 {}'.format(create_time))

        # 视频与图片落盘位置不同（视频在博主目录、图片在 picture/ 下），
        # 由 naming.target_path 按 kind 区分，这里只管算路径与扩展名
        for item in downloadable:
            # 扩展名未知时先落 .part，下载完成后按响应头/文件头改名
            ext = parser.ext_of(item['url']) or downloader.PART_EXT
            target = naming.target_path(blogger_dir, entry['kind'],
                                        create_time, item['index'] - 1, ext)
            owner = written.get(target)
            if owner and owner != item_id:
                notice('文件名冲突：{} 已属于 {}，本次将被 {} 覆盖'.format(
                    os.path.basename(target), owner, item_id))
            written[target] = item_id
            jobs.append(((item_id, item['index']), item['url'], target))

        pending[item_id] = {'entry': entry, 'create_time': create_time,
                            'remaining': len(downloadable), 'ok': True,
                            'file': ''}

    # ---------------- 线程池：并发拉取，结果回主线程按篇聚合 ----------------
    if state['stopped']:
        jobs = []       # 挑选阶段已取消：一个地址都不提交

    for (item_id, _index), url, target, ok, final_path, error in downloader.fetch_all(
            jobs, request_headers, workers, cancelled):
        item = pending.get(item_id)
        if not item:
            continue

        # 取消后不再处理结果：已落盘的 ID 由下方 flush 写掉，这条不计进度
        if cancelled():
            state['stopped'] = True
            break

        if ok:
            item['file'] = os.path.basename(final_path or '')
        else:
            failures.append({'url': url, 'file': os.path.basename(target),
                             'error': error})
            item['ok'] = False

        # 一篇微博的媒体全部回来才计进度；全成功才记篇 ID
        item['remaining'] -= 1
        if item['remaining'] > 0:
            continue

        if item['ok']:
            new_ids.append(item_id)
            done_ids.add(item_id)
            if len(new_ids) >= ID_FLUSH_BATCH:
                flush_ids()

        report(item['entry'], item['ok'], create_time=item['create_time'],
               file_name=item['file'])

    # 余数与取消时的兜底：把还没落盘的 ID 写掉
    flush_ids()

    if no_time_total:
        notice('跳过 {} 篇缺少发布时间、无法命名的微博'.format(no_time_total))

    notify({'stage': 'finished', 'blogger': '全部',
            'done': state['done'], 'total': total,
            'failures': failures,
            'stopped': state['stopped'] or cancelled(),
            'unknown_types': {}})

    return {'done': state['done'], 'total': total,
            'stopped': state['stopped'], 'failures': failures,
            'unknown_types': {}}
