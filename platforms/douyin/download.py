# -*- coding: utf-8 -*-
"""
抖音下载

请求头需要带抖音的 Referer。
先按抖音接口结构挑出可下载的作品（作者与目标博主不一致的直接丢弃），再逐条落盘：
目录与去重名单只读一次，全部媒体成功才记录 ID，ID 每满一批落盘一次。
"""

import os

from conf import settings
from core import downloader, naming
from core.utils import safe_name
from platforms.douyin import parser

KEY = 'douyin'

ID_RECORD_LABEL = '抖音作品'

ID_FLUSH_BATCH = 20      # 每积累多少个 ID 落盘一次


def headers():
    """下载请求头：公共头 + 平台段里的 referer / accept"""
    values = dict(settings.HEADER)
    options = settings.platform_settings(KEY)
    if options.get('referer'):
        values['Referer'] = options['referer']
    if options.get('accept'):
        values['Accept'] = options['accept']
    return values


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

        kind = parser.classify(aweme, image_types, video_types)
        if kind is None:
            aweme_type = aweme.get('aweme_type')
            label = '缺失' if aweme_type is None else str(aweme_type)
            unknown[label] = unknown.get(label, 0) + 1
            continue

        seen.add(aweme_id)
        entries.append({'id': aweme_id, 'author': author, 'kind': kind,
                        'create_time': aweme.get('create_time') or 0,
                        'ext': 'mp4' if kind == 'video' else 'webp',
                        'raw': aweme})

    return entries, unknown, dropped


def download(result, store, on_progress=None, should_stop=None,
             media_label=ID_RECORD_LABEL):
    """
    逐条下载抖音作品，进度只报「已处理条数 / 总条数」

    一次任务只有一个博主：collect 已滤掉作者不一致的作品，
    这里顺序遍历一遍，ID 每满 ID_FLUSH_BATCH 条落盘一次，结束时再补一次。
    """
    entries, unknown, dropped = collect(result)
    total = len(entries)
    state = {'done': 0, 'stopped': False}
    failures = []
    request_headers = headers()
    no_time_total = 0

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
        notice('跳过 {} 篇作者与目标博主不一致的作品：{}'.format(
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

    def flush_ids():
        """把缓冲中的 ID 落盘并清空；失败向上抛，由任务层报错"""
        if not new_ids:
            return
        store.append_ids(author, new_ids)
        del new_ids[:]

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

        # 只报真正要下载的作品：已下载、缺发布时间、无可用地址的都在上方 continue 掉了
        notice('下载 {}'.format(create_time))

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

        # 取消发生在条内：这一条不算完成，直接结束
        if state['stopped']:
            break

        if all_ok:
            new_ids.append(item_id)
            done_ids.add(item_id)
            if len(new_ids) >= ID_FLUSH_BATCH:
                flush_ids()

        report(entry, all_ok, create_time=create_time,
               file_name=os.path.basename(final_path or ''))

    # 余数与取消时的兜底：把还没落盘的 ID 写掉
    flush_ids()

    if unknown:
        notice('跳过未知类型共 {} 篇：{}'.format(
            sum(unknown.values()),
            ', '.join('{}={}'.format(key, value)
                      for key, value in sorted(unknown.items()))))
    if no_time_total:
        notice('跳过 {} 篇缺少发布时间、无法命名的作品'.format(no_time_total))

    notify({'stage': 'finished', 'blogger': '全部',
            'done': state['done'], 'total': total,
            'failures': failures,
            'stopped': state['stopped'] or cancelled(),
            'unknown_types': dict(sorted(unknown.items()))})

    return {'done': state['done'], 'total': total,
            'stopped': state['stopped'], 'failures': failures,
            'unknown_types': unknown}
