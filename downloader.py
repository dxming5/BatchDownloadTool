# -*- coding: utf-8 -*-
"""
下载模块
以「一篇作品」为最小单位下载，去重依据是 aweme_id

设计说明：
    1. 去重记录为「博主目录/aweme_id.txt」，一篇一行；文件是否存在不参与成功判定，
       因为文件名只精确到秒，同一秒发布的两篇作品会撞名；
    2. 一篇作品的全部媒体都下载成功才记录 aweme_id，否则整篇留待下次重试；
    3. 未知 aweme_type 一律跳过并告知，且不记录 aweme_id——以后把它补进 config.ini
       的 image_types / video_types 后重跑就能下到；
    4. 下载过程通过 progress 回调上报事件，由 Web 层渲染。
"""

import os
import random
import time
import shutil
from datetime import datetime
from urllib import request

import config
from utils import read_txt, make_dir, append_list_txt
import paths  # 导入模块而非具体变量，以便动态获取更新后的值


# 全局失败记录
_download_failures = []


def build_opener():
    """构造带请求头与忽略证书校验的下载器"""
    opener = request.build_opener(request.HTTPSHandler(context=config.SSL_CONTEXT))
    opener.addheaders = list(config.HEADER.items())
    return opener


def save_single_url(url, file_name):
    """下载单个 url 到 file_name，最多重试 MAX_RETRY 次"""
    global _download_failures
    for attempt in range(config.MAX_RETRY + 1):
        try:
            opener = build_opener()
            with opener.open(url, timeout=config.DOWNLOAD_TIMEOUT) as response, \
                    open(file_name, 'wb') as file_handle:
                shutil.copyfileobj(response, file_handle)
            return True
        except Exception as error:
            # 清理下载失败产生的半截文件
            if os.path.exists(file_name):
                try:
                    os.remove(file_name)
                except OSError:
                    pass
            if attempt >= config.MAX_RETRY:
                error_msg = '超过最大重试次数，最后异常：{}'.format(error)
                print("\n    -->当前url({})下载异常次数超过{}次，退出下载！".format(url, config.MAX_RETRY))
                # 记录失败信息
                _download_failures.append({
                    'url': url,
                    'file': file_name,
                    'error': error_msg
                })
                return False
            time.sleep(random.uniform(0.5, 1.5))
    return False


def _notify(progress, **event):
    """安全地上报进度事件"""
    if progress:
        try:
            progress(event)
        except Exception:
            # 界面层的异常不应中断下载
            pass


def _type_label(aweme_type):
    """未知类型在日志里的展示名"""
    return '缺失' if aweme_type is None else str(aweme_type)


def format_create_time(aweme):
    """
    把 create_time 转成文件名前缀

    Returns:
        str|None: 形如 2026-01-01 10.00.00；缺失或非法时返回 None
    """
    try:
        return datetime.fromtimestamp(int(aweme.get('create_time'))).strftime('%Y-%m-%d %H.%M.%S')
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def build_target(kind, create_time, index):
    """
    生成落盘路径，保持原有命名：
        视频 博主目录/{时间}.mp4
        图片 博主目录/picture/{时间} - {序号}.webp

    命名只精确到秒，同一秒的两篇作品会撞名，因此不能拿文件存在性当作已下载的依据。
    """
    if kind == 'image':
        return os.path.join(paths.blogger_dir, 'picture',
                            '{} - {}.webp'.format(create_time, index + 1))
    return os.path.join(paths.blogger_dir, '{}.mp4'.format(create_time))


def download(raw_lines, progress=None, should_stop=None):
    """
    下载协调函数：按「作品」逐个下载，篇级去重

    Args:
        raw_lines: 接口数据行（每行一条 Response），必填
        progress: 进度回调，接收 dict 事件
                  （notice / start / file_start / item / finished）
        should_stop: 取消检查函数，在作品边界与篇内文件边界都会检查
    """
    clear_failures()
    from url_parser import collect_awemes, aweme_media, classify_aweme  # 延迟导入，避免循环依赖

    def notice(message):
        """提示信息统一走进度回调"""
        _notify(progress, stage='notice', message=message)

    raw_lines = list(raw_lines or [])
    if not raw_lines:
        notice('没有可下载的接口数据')
        return

    plan, stats = collect_awemes(raw_lines)
    if stats['skipped_lines']:
        notice('跳过 {} 行无法解析或无 aweme_list 的数据'.format(stats['skipped_lines']))
    if stats['duplicated']:
        notice('忽略 {} 条重复 aweme_id'.format(stats['duplicated']))
    if stats['no_author']:
        notice('跳过 {} 条缺少作者昵称的作品'.format(stats['no_author']))
    if stats['no_id']:
        notice('跳过 {} 条缺少 aweme_id 的作品'.format(stats['no_id']))
    if not plan:
        notice('未能从数据中解析出任何作品，请检查数据内容')
        return

    total = len(plan)
    state = {'done': 0, 'stopped': False}

    def relay(event):
        """把篇级进度折算成总进度后上抛"""
        # 跳过的已下载作品也算「已处理」，否则进度永远到不了 100%
        if event.get('stage') == 'item':
            state['done'] = min(state['done'] + 1, total)
        event['done'] = state['done']
        event['total'] = total
        event['percent'] = int(state['done'] / total * 100) if total else 100
        _notify(progress, **event)

    # 按博主分组，保持作品出现顺序
    groups = {}
    for nickname, aweme_id, aweme in plan:
        groups.setdefault(nickname, []).append((aweme_id, aweme))

    unknown_summary = {}
    no_time_total = 0

    for blogger_name, items in groups.items():
        if should_stop and should_stop():
            state['stopped'] = True
            break

        paths.set_sub_path(blogger_name)
        done_ids = set(read_txt(paths.aweme_id_txt_dir))

        video_total = image_total = 0
        for _aweme_id, aweme in items:
            kind = classify_aweme(aweme)
            if kind == 'video':
                video_total += 1
            elif kind == 'image':
                image_total += 1

        _notify(progress, stage='start', blogger=blogger_name,
                video_total=video_total, picture_total=image_total,
                video_items=video_total, picture_items=image_total,
                done=state['done'], total=total,
                percent=int(state['done'] / total * 100) if total else 100)

        picture_dir = None
        written = {}        # 本次运行内 目标路径 -> aweme_id，仅用于撞名提示
        new_ids = []

        for aweme_id, aweme in items:
            if should_stop and should_stop():
                state['stopped'] = True
                break

            create_time = format_create_time(aweme)
            kind, urls, aweme_type = aweme_media(aweme)

            # 已下载过：跳过，但仍计入进度
            if aweme_id in done_ids:
                relay({'stage': 'item', 'file': aweme_id,
                       'url': '', 'index': 0, 'count': 0, 'aweme_id': aweme_id,
                       'create_time': create_time or '', 'unit': '篇',
                       'skipped': True, 'ok': True, 'reason': 'done'})
                continue

            # 未知类型：跳过并告知，不记录 aweme_id
            if kind == 'unknown':
                label = _type_label(aweme_type)
                unknown_summary[label] = unknown_summary.get(label, 0) + 1
                relay({'stage': 'item', 'file': aweme_id,
                       'url': '', 'index': 0, 'count': 0, 'aweme_id': aweme_id,
                       'create_time': create_time or '', 'unit': '篇',
                       'skipped': True, 'ok': False, 'reason': 'unknown_type',
                       'aweme_type': label})
                continue

            # 没有发布时间就无法命名，跳过且不记录
            if not create_time:
                no_time_total += 1
                relay({'stage': 'item', 'file': aweme_id,
                       'url': '', 'index': 0, 'count': 0, 'aweme_id': aweme_id,
                       'create_time': '', 'unit': '篇',
                       'skipped': True, 'ok': False, 'reason': 'no_time'})
                continue

            # 类型对但没有可用地址：记失败，不记录 aweme_id
            if not urls:
                _download_failures.append({
                    'url': '', 'file': aweme_id,
                    'error': '作品没有可下载的媒体地址（aweme_type={}）'.format(_type_label(aweme_type))})
                relay({'stage': 'item', 'file': aweme_id,
                       'url': '', 'index': 0, 'count': 0, 'aweme_id': aweme_id,
                       'create_time': create_time, 'unit': '篇',
                       'skipped': True, 'ok': False, 'reason': 'no_media'})
                continue

            media_type = '视频' if kind == 'video' else '图片'
            if kind == 'image' and picture_dir is None:
                picture_dir = os.path.join(paths.blogger_dir, 'picture')
                make_dir(picture_dir)

            all_ok = True
            target = ''
            for index, url in enumerate(urls):
                if should_stop and should_stop():
                    state['stopped'] = True
                    all_ok = False
                    break

                target = build_target(kind, create_time, index)
                owner = written.get(target)
                if owner and owner != aweme_id:
                    notice('文件名冲突：{} 已属于 aweme_id={}，本次将被 aweme_id={} 覆盖'.format(
                        os.path.basename(target), owner, aweme_id))
                written[target] = aweme_id

                relay({'stage': 'file_start', 'media_type': media_type,
                       'file': os.path.basename(target), 'url': url,
                       'index': index + 1, 'count': len(urls),
                       'create_time': create_time, 'aweme_id': aweme_id})

                if not save_single_url(url, target):
                    all_ok = False

            # 取消发生在篇内：这一篇不算完成，直接结束
            if state['stopped']:
                break

            if all_ok:
                new_ids.append(aweme_id)
                done_ids.add(aweme_id)

            relay({'stage': 'item', 'media_type': media_type,
                   'file': os.path.basename(target), 'url': urls[-1],
                   'index': len(urls), 'count': len(urls), 'aweme_id': aweme_id,
                   'create_time': create_time, 'unit': '篇',
                   'skipped': False, 'ok': all_ok})

        # 每个博主处理完立即落盘，中途崩溃也不会丢掉已完成的记录
        if new_ids:
            append_list_txt(paths.aweme_id_txt_dir, new_ids)

        if state['stopped']:
            break

    if unknown_summary:
        notice('跳过未知 aweme_type 共 {} 篇：{}'.format(
            sum(unknown_summary.values()),
            ', '.join('{}={}'.format(key, value)
                      for key, value in sorted(unknown_summary.items()))))
        notice('如需下载这些作品，请把对应类型加入 config.ini 的 '
               'image_types / video_types 后重跑（未记录 aweme_id，可重新抓取）')
    if no_time_total:
        notice('跳过 {} 篇缺少发布时间、无法命名的作品'.format(no_time_total))

    stopped = state['stopped'] or bool(should_stop and should_stop())
    percent = int(state['done'] / total * 100) if total else 100
    _notify(progress, stage='finished', blogger='全部',
            done=state['done'], total=total, percent=percent,
            failures=get_failure_list(), stopped=stopped,
            unknown_types=dict(sorted(unknown_summary.items())))


def get_failure_list():
    """获取下载失败列表"""
    return _download_failures.copy()


def clear_failures():
    """清空失败记录"""
    global _download_failures
    _download_failures = []
