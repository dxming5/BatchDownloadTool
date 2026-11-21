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

    Returns:
        tuple: (entries, unknown_types)
            entries 每项自带原始详情与命名需要的字段
    """
    entries = []
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

        kind = parser.classify(note)
        seen.add(note_id)
        entries.append({'id': note_id, 'author': author, 'kind': kind,
                        'create_time': parser.created_at(note),
                        # 图片格式要到下载时才知道（地址不带后缀），ext 留空待探测
                        'ext': 'mp4' if kind == 'video' else '',
                        'raw': note})

    return entries, {}


def download(result, store, on_progress=None, should_stop=None,
             media_label=ID_RECORD_LABEL):
    """
    逐条下载小红书笔记，进度只报「已处理条数 / 总条数」

    地址按小红书详情结构现场解析；图片地址不带后缀，走 .part 探测扩展名。
    """
    entries, unknown = collect(result)
    total = len(entries)
    state = {'done': 0, 'stopped': False}
    failures = []
    request_headers = headers()

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

    # 一条都没挑出来：交给编排层报错，不发任何下载事件
    if not total:
        return {'done': 0, 'total': 0, 'stopped': False,
                'failures': [], 'unknown_types': unknown}

    # 按作者分组，保持条目出现顺序
    groups = {}
    order = []
    for entry in entries:
        author = entry['author']
        if author not in groups:
            groups[author] = []
            order.append(author)
        groups[author].append(entry)

    no_time_total = 0

    for author in order:
        if cancelled():
            state['stopped'] = True
            break

        blogger_dir = store.ensure(author)
        if not blogger_dir:
            notice('博主名不可用，跳过：{}'.format(author))
            continue
        done_ids = store.read_ids(author)

        group = groups[author]
        video_total = sum(1 for entry in group if entry['kind'] == 'video')
        image_total = sum(1 for entry in group if entry['kind'] == 'image')

        notify({'stage': 'start', 'blogger': author,
                'video_total': video_total, 'picture_total': image_total,
                'video_items': video_total, 'picture_items': image_total})

        written = {}        # 本次运行内 目标路径 -> 条目 ID，仅用于撞名提示
        new_ids = []

        for entry in group:
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

            all_ok = True
            final_path = ''

            for index, url in enumerate(urls):
                if cancelled():
                    state['stopped'] = True
                    all_ok = False
                    break

                # 扩展名未知时先落 .part，下载完成后改名
                ext = entry['ext'] or downloader.PART_EXT
                target = naming.target_path(blogger_dir, entry['kind'],
                                            create_time, index, ext)
                owner = written.get(target)
                if owner and owner != item_id:
                    notice('文件名冲突：{} 已属于 {}，本次将被 {} 覆盖'.format(
                        os.path.basename(target), owner, item_id))
                written[target] = item_id

                ok, final_path, error = downloader.fetch_media(
                    url, target, request_headers)
                if not ok:
                    failures.append({'url': url, 'file': os.path.basename(target),
                                     'error': error})
                    all_ok = False
                    continue

                # 小红书专有：HEIC 落盘后转 JPG
                try:
                    final_path = post_process(final_path) or final_path
                except Exception as exc:
                    notice('落盘后处理失败（{}）：{}'.format(
                        os.path.basename(final_path), exc))

            # 取消发生在条内：这一条不算完成，直接结束
            if state['stopped']:
                break

            if all_ok:
                new_ids.append(item_id)
                done_ids.add(item_id)

            report(entry, all_ok, create_time=create_time,
                   file_name=os.path.basename(final_path or ''))

        # 每个博主处理完立即落盘，中途崩溃也不会丢掉已完成的记录
        if new_ids:
            store.append_ids(author, new_ids)

        if state['stopped']:
            break

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
