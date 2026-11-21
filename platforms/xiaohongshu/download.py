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

ID_FLUSH_BATCH = 20      # 每积累多少个 ID 落盘一次

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
    并发下载小红书笔记，进度只报「已处理条数 / 总条数」

    一次任务只有一个博主：collect 已滤掉作者不一致的笔记。
    先由主线程挑出要下载的笔记并算好落盘路径（建目录只在这一步做），
    再交给线程池并发拉取，HEIC 转 JPG 也在下载线程里做；
    结果回到主线程聚合，一条笔记的媒体全部成功才计进度并记 ID。
    图片地址不带后缀，走 .part 探测扩展名。
    """
    entries, unknown, dropped = collect(result)
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
        """一条处理完：累加计数后连同 done/total 一起上报"""
        state['done'] = min(state['done'] + 1, total)
        notify({'stage': 'item', 'done': state['done'], 'total': total,
                'aweme_id': entry['id'], 'create_time': create_time,
                'file': file_name, 'skipped': skipped, 'ok': ok, 'reason': reason})

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
            'video_total': video_total, 'picture_total': image_total,
            'video_items': video_total, 'picture_items': image_total})

    written = {}        # 本次运行内 目标路径 -> 条目 ID，仅用于撞名提示
    new_ids = []
    pending = {}        # 条目 ID -> 剩余地址数 / 是否全部成功 / 最后落盘文件
    jobs = []           # (条目 ID, 地址, 目标路径)，交给线程池并发拉取

    def flush_ids():
        """把缓冲中的 ID 落盘并清空；失败向上抛，由任务层报错"""
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

        # 已下载过：跳过，但仍计入进度
        if item_id in done_ids:
            report(entry, True, skipped=True, reason='done',
                   create_time=create_time or '')
            continue

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

        # 只报真正要下载的笔记：已下载、缺发布时间、无可用地址的都在上方 continue 掉了
        # 并发下载下这条表示「已提交下载」，不代表此刻正在拉取
        notice('下载 {}'.format(create_time))

        # 扩展名未知时先落 .part，下载完成后改名
        ext = entry['ext'] or downloader.PART_EXT
        for index, url in enumerate(urls):
            target = naming.target_path(blogger_dir, entry['kind'],
                                        create_time, index, ext)
            owner = written.get(target)
            if owner and owner != item_id:
                notice('文件名冲突：{} 已属于 {}，本次将被 {} 覆盖'.format(
                    os.path.basename(target), owner, item_id))
            written[target] = item_id
            jobs.append((item_id, url, target))

        pending[item_id] = {'entry': entry, 'create_time': create_time,
                            'remaining': len(urls), 'ok': True, 'file': ''}

    # ---------------- 线程池：并发拉取，结果回主线程聚合 ----------------
    if state['stopped']:
        jobs = []       # 挑选阶段已取消：一个地址都不提交

    for item_id, url, target, ok, final_path, error in downloader.fetch_all(
            jobs, request_headers, workers, cancelled, finish=post_process):
        item = pending.get(item_id)
        if not item:
            continue

        # 取消后不再处理结果：已落盘的 ID 由下方 flush 写掉，这条不记录
        if cancelled():
            state['stopped'] = True
            break

        if ok:
            item['file'] = os.path.basename(final_path or '')
            # 小红书专有：HEIC 转 JPG 在下载线程里做，失败时只提示不判失败
            if error:
                notice('落盘后处理失败（{}）：{}'.format(item['file'], error))
        else:
            failures.append({'url': url, 'file': os.path.basename(target),
                             'error': error})
            item['ok'] = False

        # 一条笔记的媒体全部回来才计进度；全成功才记 ID
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
        notice('跳过 {} 篇缺少发布时间、无法命名的笔记'.format(no_time_total))

    notify({'stage': 'finished', 'blogger': '全部',
            'done': state['done'], 'total': total,
            'failures': failures,
            'stopped': state['stopped'] or cancelled(),
            'unknown_types': {}})

    return {'done': state['done'], 'total': total,
            'stopped': state['stopped'], 'failures': failures,
            'unknown_types': unknown}
