# -*- coding: utf-8 -*-
"""
小红书下载

差异有三处：
1. 请求头要带小红书自己的 Referer，否则 CDN 可能拒
2. 媒体地址不带后缀，扩展名由下载机制按响应头 + 文件头探测
3. HEIC 图片浏览器与 Pillow 都读不了，落盘后转成 JPG 并删原件
"""

import os

from conf import settings
from core import downloader, naming
from core.utils import safe_name
from platforms.xiaohongshu import parser

KEY = 'xiaohongshu'

ID_RECORD_LABEL = '小红书笔记'

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


def media_urls(note, kind):
    """
    小红书取址规则

    视频：遍历编码档位挑最优一路；
    图片：还原为无水印原图。
    """
    if kind == 'video':
        return [parser.extract_video(note)]
    return parser.extract_images(note)


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
    expected = safe_name(result.get('nickname') or '')

    entries = []
    dropped = {}
    seen = set()

    for note in result.get('notes') or []:
        if not isinstance(note, dict):
            continue

        note_id = parser.note_id_of(note)
        if not note_id:
            continue
        note_id = str(note_id)
        if note_id in seen:
            continue

        author = parser.author_of(note)
        if not author:
            continue

        # 名字对不上：不是目标博主，丢弃；昵称取不到时放弃过滤
        if expected and author != expected:
            dropped[author] = dropped.get(author, 0) + 1
            continue

        kind = parser.classify(note)
        seen.add(note_id)
        entries.append({'id': note_id, 'author': author, 'kind': kind,
                        'create_time': parser.created_at(note),
                        # 图片格式要到下载时才知道（地址不带后缀），ext 留空待探测
                        'ext': 'mp4' if kind == 'video' else '',
                        'raw': note})

    return entries, {}, dropped


def download(result, store, on_progress=None, should_stop=None,
             media_label=ID_RECORD_LABEL):
    """
    并发下载小红书笔记，进度按实际媒体地址数统计

    一次任务只有一个博主：collect 已滤掉作者不一致的笔记。
    先由主线程挑出要下载的笔记并算好落盘路径（建目录只在这一步做），
    再交给线程池并发拉取，HEIC 转 JPG 也在下载线程里做；
    每个文件完成即计 1 个进度，记 ID 由下载层按篇聚合。
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

    author = entries[0]['author']
    blogger_dir = store.ensure(author)
    if not blogger_dir:
        notice('博主名不可用：{}'.format(author))
        return {'done': 0, 'total': 0, 'stopped': False,
                'failures': [], 'unknown_types': unknown}
    done_ids = store.read_ids(author)

    video_total = sum(1 for entry in entries if entry['kind'] == 'video')
    image_total = sum(1 for entry in entries if entry['kind'] == 'image')
    notify({'stage': 'start', 'blogger': author,
            'video_total': video_total, 'picture_total': image_total})

    jobs = []           # (条目 ID, 发布时间, [地址])，交给统一下载层

    # ---------------- 主线程：挑条目、算发布时间 ----------------
    # 命名与撞名判断都交给统一下载层，这里只挑条目、算时间、记归属
    for entry in entries:
        if cancelled():
            state['stopped'] = True
            break

        item_id = entry['id']
        create_time = naming.format_create_time(entry['create_time'])

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

    def record_ids(batch):
        store.append_ids(author, batch)

    def article_done(item_id, ok, create_time, file_name):
        report(entry_by_id[item_id], ok, create_time=create_time,
               file_name=file_name)

    for item_id, url, target, ok, final_path, error in downloader.fetch_all(
            jobs, blogger_dir, request_headers, workers, cancelled,
            finish=post_process, log=notice, done_ids=done_ids, on_done=progress,
            record_ids=record_ids, on_article_done=article_done):
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
