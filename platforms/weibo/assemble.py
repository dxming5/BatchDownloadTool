# -*- coding: utf-8 -*-
"""
微博下载组装

把抓取结果组装成「下什么、从哪下」，再交给 core.downloader 统一执行。

每篇（一条微博）按内容类型走两条链路，取址与命名各不相同，落盘统一交给
downloader 并发执行：
    图片  mblog.pics[].large.url（缺失时回退 bmiddle / thumbnail / url）
          微博图床的无水印版恒为最长边 690，这里固定取原图以保住最高画质
    视频  page_info 里的一个清晰度地址，落成 {发布时间}.mp4
          视频角标是转码时烧录的，所有直链都带，无法去水印
类型由 classify 判定：先看视频（视频微博的 pics 只是一张封面）。
两类都没有的篇（纯文字、头条文章、转发别人内容）跳过并计数提示。

进度按实际媒体地址（每个图片 / 视频文件）统计，由 downloader.fetch_all 统一
计算并上报；已下载的篇按其媒体数计入已完成，缺时间 / 无地址的篇由本模块在
挑选时跳过。ID 记录仍按篇：一条微博一个 mblog_id，全部媒体成功才写入
aweme_id.txt，任一失败则整篇不记录，下次整篇重下。
"""

import os

from conf import settings
from core import downloader, store
from core.utils import safe_name
from platforms.weibo import scraper

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


# ---------------- 类型判定与取址 ----------------

# page_info.type / object_type 等于这些值就是视频微博
VIDEO_PAGE_TYPES = ('video',)

# 同一条视频接口会给多个清晰度，只能挑一个下载。
# 前四个键名来自 page_info.urls，stream_url_hd / stream_url 来自 media_info；
# 高清排前面，标清退后面，取不到的键直接跳过
VIDEO_QUALITY_ORDER = ('mp4_1080p_mp4', 'mp4_720p_mp4', 'mp4_hd_mp4',
                       'mp4_hd_url', 'stream_url_hd',
                       'mp4_sd_url', 'mp4_ld_mp4', 'mp4_ld_url', 'stream_url')

# 播放列表不是媒体文件，落盘只会得到一份文本，直接排除
PLAYLIST_SUFFIXES = ('.m3u8', '.m3u')


def _pic_url(pic):
    """取一张图的最优地址：large > bmiddle > thumbnail > 顶层 url"""
    for key in ('large', 'bmiddle', 'thumbnail'):
        node = pic.get(key)
        if isinstance(node, dict) and node.get('url'):
            return str(node['url']).replace('\\/', '/')
    value = pic.get('url')
    return str(value).replace('\\/', '/') if isinstance(value, str) else ''


def image_items(mblog):
    """
    取出微博里的图片

    pics 有两种形态：列表，或以 "0"/"1" 为键的字典（旧接口）。
    序号用图片在原微博里的位置（从 1 开始），与落盘命名 `- {序号}` 对齐。

    地址统一取接口给的原图（large，最长边可达 4000+）。
    微博图床的无水印版恒为最长边 690，与最高画质不可兼得，
    因此这里不换前缀，固定保留最高画质。

    Returns:
        list[dict]: [{'index': 1, 'url': 'https://...'}]
    """
    pics = (mblog or {}).get('pics')
    images = []

    if isinstance(pics, list):
        for position, pic in enumerate(pics, 1):
            if not isinstance(pic, dict):
                continue
            url = _pic_url(pic)
            if url:
                images.append({'index': position, 'url': url})
        return images

    if isinstance(pics, dict):
        def order(key):
            return int(key) if str(key).isdigit() else 0

        for position, key in enumerate(sorted(pics, key=order), 1):
            pic = pics[key]
            if not isinstance(pic, dict):
                continue
            url = _pic_url(pic)
            if not url:
                continue
            number = int(key) + 1 if str(key).isdigit() else position
            images.append({'index': number, 'url': url})

    return images


def _page_info(mblog):
    """page_info 节点；缺失或类型不对时返回空 dict"""
    info = (mblog or {}).get('page_info')
    return info if isinstance(info, dict) else {}


def _clean_url(value):
    """接口里的地址可能被转义，且只接受 http(s)"""
    if not isinstance(value, str):
        return ''
    value = value.replace('\\/', '/').strip()
    return value if value.startswith('http') else ''


def _playback_urls(info):
    """
    新版接口的清晰度列表（playback_list），按 quality_level 从高到低取地址

    条目形如 {'play_info': {'label': 'mp4_720p', 'quality_level': 2, 'url': '...'}}，
    label 与 urls 的键名不同，因此单独收集后追加在 urls 之后。
    """
    items = []
    for holder in (info, info.get('media_info')):
        if not isinstance(holder, dict):
            continue
        for entry in holder.get('playback_list') or []:
            detail = entry.get('play_info') if isinstance(entry, dict) else None
            if not isinstance(detail, dict) or not detail.get('url'):
                continue
            try:
                level = int(detail.get('quality_level') or 0)
            except (TypeError, ValueError):
                level = 0
            items.append((level, detail.get('url')))
    return [url for _level, url in sorted(items, key=lambda item: item[0],
                                          reverse=True)]


def video_items(mblog):
    """
    取出视频微博的地址（一条视频取一个清晰度）

    地址可能出现在三处，按清晰度从高到低取第一个可用的：
        page_info.urls           多个清晰度，键名如 mp4_720p_mp4
        media_info.playback_list 新版清晰度列表，带 quality_level
        page_info.media_info     stream_url_hd / stream_url

    page_info.page_url 指向微博自己的播放页而不是媒体文件，不能用。

    注意：微博视频的角标（@昵称 + 微博视频）是转码时烧录进画面的，
    所有对外直链都是这份带水印文件的不同清晰度，改地址去不掉；
    这里只挑最高可用清晰度，不做去水印改写。

    Returns:
        list[dict]: [{'index': 1, 'url': 'https://...'}]；取不到时为空列表
    """
    info = _page_info(mblog)
    urls = info.get('urls')
    media = info.get('media_info')

    # 两处的键名不同，先合并成一张表，再按统一优先级取
    candidates = {}
    if isinstance(urls, dict):
        candidates.update(urls)
    if isinstance(media, dict):
        candidates['stream_url_hd'] = media.get('stream_url_hd')
        candidates['stream_url'] = media.get('stream_url')

    ordered = [candidates.get(key) for key in VIDEO_QUALITY_ORDER]
    # playback_list 的 label 不在这张表里，单独按 quality_level 追加
    ordered.extend(_playback_urls(info))
    # 键名变了也别漏：其余值按接口给的顺序兜底
    ordered.extend(value for key, value in candidates.items()
                   if key not in VIDEO_QUALITY_ORDER)

    for value in ordered:
        url = _clean_url(value)
        if url and not url.split('?')[0].lower().endswith(PLAYLIST_SUFFIXES):
            return [{'index': 1, 'url': url}]
    return []


def is_video(mblog):
    """
    是否视频微博

    优先看 page_info 的 type / object_type；两者都缺失时退回看有没有
    media_info：接口改字段时不至于把视频当成「无内容」静默丢掉。
    """
    info = _page_info(mblog)
    for key in ('object_type', 'type'):
        if str(info.get(key) or '').strip().lower() in VIDEO_PAGE_TYPES:
            return True
    media = info.get('media_info')
    return isinstance(media, dict) and bool(
        media.get('stream_url') or media.get('stream_url_hd'))


def classify(mblog):
    """
    判定一条微博的内容类型

    微博没有抖音那种单一 aweme_type 枚举，只能按字段组合判断：
        视频  page_info 标记为 video，或带 media_info
        图片  pics 非空
    两者都没有的（纯文字、头条文章、转发别人内容、无图）不是可下载的「篇」。

    先判视频：视频微博的 pics 通常只有一张封面图，按图片处理会下成封面。

    Returns:
        str|None: 'video' / 'image' / None
    """
    if is_video(mblog):
        return 'video'
    if image_items(mblog):
        return 'image'
    return None


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

        mblog_id = scraper.mblog_id_of(mblog)
        if not mblog_id or mblog_id in seen:
            continue

        author = scraper.author_of(mblog)
        if not author:
            continue

        # 名字对不上：不是目标博主，丢弃；昵称取不到时放弃过滤
        if expected and author != expected:
            dropped[author] = dropped.get(author, 0) + 1
            continue

        kind = classify(mblog)
        # 纯文字、文章、转发等没有可下载的媒体，不参与下载也不占进度
        if kind is None:
            skipped += 1
            continue

        media = (video_items(mblog) if kind == 'video'
                 else image_items(mblog))

        seen.add(mblog_id)
        entries.append({
            'id': mblog_id,
            'author': author,
            'kind': kind,
            'create_time': scraper.created_at(mblog),
            'media': media,
            'raw': mblog,
        })

    return entries, skipped, dropped


def run(result, on_progress=None, should_stop=None,
        media_label=ID_RECORD_LABEL):
    """
    并发下载微博的图片与视频，进度按实际媒体地址数统计

    一次任务只有一个博主：collect 已滤掉作者不一致的微博。
    博主名与已记录的 ID 从 store 取（编排层已 activate）；落盘路径也由
    下载层向 store 要，本模块不碰任何路径。
    先由主线程逐篇挑出要下载的微博，再交给线程池并发拉取；每个文件完成即
    计 1 个进度，记 ID 由下载层按篇聚合。

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

    if not store.name():
        notice('博主名不可用')
        return {'done': 0, 'total': 0, 'stopped': False,
                'failures': [], 'unknown_types': {}}
    done_ids = store.read_ids()

    video_total = sum(1 for entry in entries if entry['kind'] == 'video')
    image_total = total - video_total
    notify({'stage': 'start', 'blogger': store.name(),
            'video_total': video_total, 'picture_total': image_total})

    jobs = []           # (篇 ID, 发布时间, [地址])，交给统一下载层

    # ---------------- 主线程：挑条目、算发布时间 ----------------
    # 命名与撞名判断都交给统一下载层，这里只挑条目、算时间、记归属
    for entry in entries:
        if cancelled():
            state['stopped'] = True
            break

        item_id = entry['id']
        create_time = downloader.format_create_time(entry['create_time'])

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
