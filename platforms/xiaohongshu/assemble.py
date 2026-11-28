# -*- coding: utf-8 -*-
"""
小红书下载组装

把抓取结果组装成「下什么、从哪下」，再交给 core.downloader 统一执行。

与另外两个平台不同的地方有三处：
1. 请求头要带小红书自己的 Referer，否则 CDN 可能拒
2. 媒体地址不带后缀，扩展名由下载机制按响应头 + 文件头探测
3. HEIC 图片浏览器与 Pillow 都读不了，落盘后转成 JPG 并删原件
"""

import os
import re

from conf import settings
from core import downloader, store
from core.utils import safe_name
from platforms.xiaohongshu import scraper

KEY = 'xiaohongshu'

ID_RECORD_LABEL = '小红书笔记'

# 小红书原图的 CDN 主机；展示链路下发的是带平台水印的压缩档
ORIGIN_HOST = 'https://sns-img-qc.xhscdn.com/'

# HEIC 解码依赖：未安装 pillow-heif 时跳过转码，保留原文件
try:
    from PIL import Image
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIC_SUPPORT = True
except Exception:      # 依赖缺失或平台不支持
    HEIC_SUPPORT = False


def headers():
    """下载请求头：公共头 + 平台段里的 referer / accept"""
    values = dict(settings.HEADER)
    options = settings.platform_settings(KEY)
    if options.get('referer'):
        values['Referer'] = options['referer']
    if options.get('accept'):
        values['Accept'] = options['accept']
    return values


def heic_to_jpeg(path, quality=98):
    """
    把 HEIC 转成 JPG，返回 JPG 路径（失败时返回原路径）

    HEVC → JPEG 属于换编码，用 quality=98 + 无色度抽样做到视觉无损；原 HEIC 不保留。
    """
    if not HEIC_SUPPORT:
        print('    （未安装 pillow-heif，跳过 HEIC 转码）')
        return path

    target = os.path.splitext(path)[0] + '.jpg'
    if os.path.exists(target):
        # 已有转码结果，清理残留的 HEIC
        try:
            os.remove(path)
        except OSError:
            pass
        return target

    try:
        with Image.open(path) as image:
            image.convert('RGB').save(target, 'JPEG', quality=quality,
                                      subsampling=0, optimize=True)
    except Exception as error:
        # 转码失败时保留原 HEIC，绝不删
        print('    HEIC 转码失败：{}'.format(error))
        return path

    try:
        os.remove(path)
    except OSError:
        pass
    return target


def post_process(path):
    """落盘后处理：HEIC/HEIF 转 JPG"""
    value = (path or '').lower()
    if not value.endswith(('.heic', '.heif')):
        return path
    return heic_to_jpeg(path)


# ---------------- 类型判定与取址 ----------------

def is_live_album(note):
    """实况照片合集：type 是 video，但每张图都带 livePhoto 标记"""
    images = scraper.image_list_of(note)
    return bool(images) and all(image.get('livePhoto') for image in images)


def extract_video(note):
    """
    取视频播放地址

    不同版本的流字段名不一致（h264/h265 或 EF4~EF7 等编码档位），
    因此不认具体键名，遍历所有档位按码率（取不到按高度）挑最优一路。
    """
    video = note.get('video') or {}
    streams = (video.get('media') or {}).get('stream') or {}

    best_url = ''
    best_score = -1
    for items in streams.values():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            url = item.get('masterUrl') or item.get('master_url') or ''
            if not url:
                continue
            score = item.get('videoBitrate') or item.get('avgBitrate') or 0
            if not score:
                score = (item.get('height') or 0) * 1000
            if score > best_score:
                best_url, best_score = url, score
    return best_url


def classify(note):
    """
    判定按图片还是视频处理

    图片全部带实况标记         → 图片（实况合集）
    有视频流且图片至多 1 张     → 视频（那 1 张只是封面）
    其余（多图 + 有视频流）     → 图片（平台把多图合成了轮播视频，不能当视频下）
    """
    images = scraper.image_list_of(note)
    if is_live_album(note):
        return 'image'
    if extract_video(note) and len(images) <= 1:
        return 'video'
    return 'image'


def no_watermark_url(url):
    """
    把带水印的展示图地址还原为无水印原图

    展示链路下发的是压缩档（如 !nd_dft），平台水印合成在这份上；
    原图在 sns-img-qc 上直接用「目录/图片ID」寻址，分辨率与体积都最大且无水印。
    转换规则不成立时原样返回，避免把可用地址改坏。
    """
    if not url:
        return url
    clean = url.split('!')[0].rstrip('/')
    parts = clean.rsplit('/', 2)
    if len(parts) < 2:
        return url

    last = parts[-1]
    prev = parts[-2]
    # 上一级可能是哈希目录（32 位 hex）或时间戳目录（纯数字），
    # 这两类只是 CDN 分片前缀，不属于图片路径
    if re.match(r'^([0-9a-f]{32}|\d{8,14})$', prev):
        path = last
    else:
        path = prev + '/' + last

    if not re.match(r'^([0-9a-zA-Z_]+/)?[0-9a-zA-Z]+$', path):
        return url
    return ORIGIN_HOST + path


def extract_images(note, no_watermark=True):
    """
    取出全部图片地址（默认转成无水印原图）

    SSR 的 imageList 每项可能有 urlDefault，也可能只给 infoList：
    WB_DFT 是原图，WB_PRV 是预览图。
    """
    urls = []
    for image in scraper.image_list_of(note):
        url = image.get('urlDefault') or image.get('url_default') or ''
        if not url:
            infos = image.get('infoList') or image.get('info_list') or []
            by_scene = {info.get('imageScene'): info.get('url')
                        for info in infos if info.get('url')}
            url = by_scene.get('WB_DFT') or by_scene.get('WB_PRV') or ''
        if url:
            urls.append(no_watermark_url(url) if no_watermark else url)
    return urls


def collect(result):
    """
    从原始详情数据里挑出可下载的笔记

    作者与目标博主不一致的笔记（合作、改名、接口异常）直接丢弃：
    一次任务只对应一个博主目录。

    Returns:
        tuple: (entries, unknown_types, dropped_authors)
            entries 每项自带原始详情与命名需要的字段；
            dropped_authors 为「作者昵称 -> 条数」，只用于日志提示
    """
    # 昵称基准是本次任务已登记的目标博主；抓取结果里的昵称只作兜底
    current = store.current()
    expected = safe_name((current.nickname if current else '')
                         or result.get('nickname') or '')

    entries = []
    dropped = {}
    seen = set()

    for note in result.get('notes') or []:
        if not isinstance(note, dict):
            continue

        note_id = scraper.note_id_of(note)
        if not note_id:
            continue
        note_id = str(note_id)
        if note_id in seen:
            continue

        author = scraper.author_of(note)
        if not author:
            continue

        # 名字对不上：不是目标博主，丢弃；昵称取不到时放弃过滤
        if expected and author != expected:
            dropped[author] = dropped.get(author, 0) + 1
            continue

        kind = classify(note)
        seen.add(note_id)
        entries.append({'id': note_id, 'author': author, 'kind': kind,
                        'create_time': scraper.created_at(note),
                        'raw': note})

    return entries, {}, dropped


def run(result, on_progress=None, should_stop=None,
        media_label=ID_RECORD_LABEL):
    """
    并发下载小红书笔记，进度按实际媒体地址数统计

    一次任务只有一个博主：collect 已滤掉作者不一致的笔记。
    博主名与已记录的 ID 从 store 取（编排层已 activate）；落盘路径也由
    下载层向 store 要，本模块不碰任何路径。
    先由主线程挑出要下载的笔记，再交给线程池并发拉取，HEIC 转 JPG 也在
    下载线程里做；每个文件完成即计 1 个进度，记 ID 由下载层按篇聚合。
    图片地址不带后缀，走 .part 探测扩展名。
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
        notice('跳过 {} 篇作者与目标博主不一致的笔记：{}'.format(
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

        if entry['kind'] == 'video':
            urls = [extract_video(entry['raw'])]
        else:
            urls = extract_images(entry['raw'])
        urls = [url for url in urls if url]

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
            finish=post_process, log=notice, done_ids=done_ids, on_done=progress,
            record_ids=store.id_recorder(), on_article_done=article_done):
        # 取消后不再处理结果：已成功篇的 ID 由下载层 finally 兜底写出
        if cancelled():
            state['stopped'] = True
            break

        if not ok:
            failures.append({'url': url, 'file': os.path.basename(target),
                             'error': error})
        elif error:
            # 小红书专有：HEIC 转 JPG 在下载线程里做，失败时只提示不判失败
            notice('落盘后处理失败（{}）：{}'.format(
                os.path.basename(final_path or ''), error))

    if no_time_total:
        notice('跳过 {} 篇缺少发布时间、无法命名的笔记'.format(no_time_total))

    notify({'stage': 'finished', 'blogger': '全部',
            'done': state['done'], 'total': state['total'],
            'failures': failures,
            'stopped': state['stopped'] or cancelled(),
            'unknown_types': {}})

    # 返回给编排层的 total 仍是篇数，只用于判断「有没有可下载内容」
    return {'done': state['done'], 'total': total,
            'stopped': state['stopped'], 'failures': failures,
            'unknown_types': unknown}
