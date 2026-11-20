# -*- coding: utf-8 -*-
"""
下载与格式转换模块
包含网络下载、去重比对、webp转jpg等功能

设计说明：
    下载过程通过 progress 回调上报事件，CLI 与 Web 各自实现回调；
    不传 progress 时退化为原来的终端打印行为。
"""

import os
import random
import time
import shutil
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


def get_url_signature(url, start_tag, end_tag):
    """提取 url 中不变的片段用于去重比对（url 本身带有时效参数，会变动）"""
    start = url.find(start_tag)
    if start < 0:
        return url
    start += len(start_tag)
    end = url.find(end_tag, start)
    if end < 0:
        return url[start:]
    return url[start:end]


def convert_webp_to_jpg(file_name):
    """webp 转 jpg，转换成功后删除源 webp 文件，返回是否成功"""
    from PIL import Image  # 延迟导入，避免启动时依赖 PIL
    try:
        im = Image.open(file_name)
        if im.mode == "RGBA":
            im.load()  # required for png.split()
            background = Image.new("RGB", im.size, (255, 255, 255))
            background.paste(im, mask=im.split()[3])
        save_name = os.path.splitext(file_name)[0] + '.jpg'
        im.save('{}'.format(save_name), 'JPEG')
        im.close()
        if os.path.exists(file_name):
            os.remove(file_name)
        return True
    except Exception as error:
        print('\n    -->图片转换失败（{}）：{}'.format(file_name, error))
        return False


def _notify(progress, **event):
    """安全地上报进度事件"""
    if progress:
        try:
            progress(event)
        except Exception:
            # 界面层的异常不应中断下载
            pass


def download_by_type(all_url_dict, media_type, progress=None, should_stop=None):
    """
    根据类型下载dict中所有的url

    Args:
        all_url_dict: 时间 -> url 列表
        media_type: '视频' 或 '图片'
        progress: 进度回调，接收 dict 事件
        should_stop: 返回 True 时在当前 URL 边界停止
    """
    global _download_failures
    # 从 paths 模块动态获取最新值
    blogger_url_txt_dir = paths.blogger_url_txt_dir
    blogger_dir = paths.blogger_dir

    saved_url_list = read_txt(blogger_url_txt_dir)
    all_mblog_num = len(all_url_dict)
    if not all_mblog_num:
        return False

    if media_type == '图片':
        picture_dir = os.path.join(blogger_dir, 'picture')
        make_dir(picture_dir)
        # 图片url是动态的，所以使用"/tos-cn-i-......~tplv-dy-aweme-images"字符串来存储比较
        start_tag, end_tag = '/tos-cn-i-', '~tplv-dy-aweme-images'
        ext = '.webp'
    else:
        # 视频url是动态的，所以使用"&rc=......&l="字符串来存储比较
        start_tag, end_tag = '&rc=', '&l='
        ext = '.mp4'

    # 直接存储特征码集合（无需再从URL提取）
    saved_signature_set = set(saved_url_list)

    # 记录本次新下载成功的URL（延迟写入）
    new_downloaded_urls = []

    download_mblog_num = 0
    stopped = False
    for creat_time, url_mblog in all_url_dict.items():
        if should_stop and should_stop():
            stopped = True
            break
        for index in range(0, len(url_mblog)):
            if should_stop and should_stop():
                stopped = True
                break
            url = url_mblog[index]
            # 视频用"条"，图片用"张"
            unit = '条' if media_type == '视频' else '张'

            # 提取当前URL的特征码
            url_signature = get_url_signature(url, start_tag, end_tag)

            if media_type == '图片':
                file_name = os.path.join(picture_dir, '{} - {}{}'.format(creat_time, index + 1, ext))
            else:
                file_name = os.path.join(blogger_dir, '{}{}'.format(creat_time, ext))

            if url_signature in saved_signature_set:
                _notify(progress, stage='item', media_type=media_type,
                        file=os.path.basename(file_name), url=url,
                        index=index + 1, count=len(url_mblog),
                        group_done=download_mblog_num, group_total=all_mblog_num,
                        create_time=creat_time, unit=unit, skipped=True, ok=True)
                continue

            _notify(progress, stage='file_start', media_type=media_type,
                    file=os.path.basename(file_name), url=url,
                    index=index + 1, count=len(url_mblog),
                    group_done=download_mblog_num, group_total=all_mblog_num,
                    create_time=creat_time, unit=unit, skipped=False)

            ok = save_single_url(url, file_name)
            if ok:
                # 添加到特征码集合（用于本次运行时的去重）
                saved_signature_set.add(url_signature)
                # 延迟写入：只保存特征码
                new_downloaded_urls.append(url_signature)
                # webp转为jpg
                if media_type == '图片' and os.path.exists(file_name):
                    if not convert_webp_to_jpg(file_name):
                        _download_failures.append({
                            'url': url,
                            'file': file_name,
                            'error': 'webp 转 jpg 失败'
                        })

            _notify(progress, stage='item', media_type=media_type,
                    file=os.path.basename(file_name), url=url,
                    index=index + 1, count=len(url_mblog),
                    group_done=download_mblog_num, group_total=all_mblog_num,
                    create_time=creat_time, unit=unit, skipped=False, ok=ok)

        download_mblog_num += 1

    # 所有URL下载完成后，统一批量追加写入文件
    if new_downloaded_urls:
        append_list_txt(blogger_url_txt_dir, new_downloaded_urls)
    return stopped


def get_failure_list():
    """获取下载失败列表"""
    return _download_failures.copy()


def clear_failures():
    """清空失败记录"""
    global _download_failures
    _download_failures = []


def print_failure_summary():
    """打印下载失败汇总"""
    if not _download_failures:
        return

    print('\n' + '=' * 60)
    print('下载失败列表（共 {} 个）：'.format(len(_download_failures)))
    print('=' * 60)
    for idx, failure in enumerate(_download_failures, 1):
        print('\n[{}]'.format(idx))
        print('  文件: {}'.format(failure['file']))
        print('  URL: {}'.format(failure['url']))
        print('  原因: {}'.format(failure['error']))
    print('\n' + '=' * 60)


def download(progress=None, should_stop=None, raw_lines=None):
    """
    下载协调函数：按博主分组下载全部内容

    Args:
        progress: 进度回调，接收 dict 事件
        should_stop: 取消检查函数
        raw_lines: 已导入的接口数据行；为 None 时读 json.log（CLI 行为）
    """
    clear_failures()
    from url_parser import load_all_bloggers, parse_aweme  # 延迟导入，避免循环依赖

    if raw_lines is None:
        bloggers = load_all_bloggers()
    else:
        bloggers, _ = parse_aweme(raw_lines)
    if not bloggers:
        return

    # 统计全部博主的总条数与总文件数
    totals = []
    for _name, (picture_dict, video_dict) in bloggers.items():
        video_items = sum(len(urls) for urls in video_dict.values())
        picture_items = sum(len(urls) for urls in picture_dict.values())
        totals.append((len(video_dict), len(picture_dict), video_items, picture_items))
    video_items_total = sum(item[2] for item in totals)
    picture_items_total = sum(item[3] for item in totals)
    total = video_items_total + picture_items_total
    state = {'done': 0}

    def relay(event):
        """把分组进度折算成总进度后上抛"""
        # 跳过的已下载文件也算“已处理”，否则进度条永远到不了 100%
        if event.get('stage') == 'item':
            state['done'] = min(state['done'] + 1, total)
        event['done'] = state['done']
        event['total'] = total
        event['percent'] = int(state['done'] / total * 100) if total else 100
        _notify(progress, **event)

    from paths import set_sub_path
    for blogger_name, (all_picture_dict, all_video_dict) in bloggers.items():
        set_sub_path(blogger_name)
        video_total = len(all_video_dict)
        picture_total = len(all_picture_dict)
        video_items = sum(len(urls) for urls in all_video_dict.values())
        picture_items = sum(len(urls) for urls in all_picture_dict.values())

        _notify(progress, stage='start', blogger=blogger_name,
                video_total=video_total, picture_total=picture_total,
                video_items=video_items, picture_items=picture_items,
                done=state['done'], total=total,
                percent=int(state['done'] / total * 100) if total else 100)

        download_by_type(all_video_dict, '视频', relay, should_stop)
        download_by_type(all_picture_dict, '图片', relay, should_stop)

    stopped = bool(should_stop and should_stop())
    percent = int(state['done'] / total * 100) if total else 100
    _notify(progress, stage='finished', blogger='全部',
            done=state['done'], total=total, percent=percent,
            failures=get_failure_list(), stopped=stopped)

    if not progress:
        print('\n 全部博主下载结束，已处理 {}/{} 项{}'.format(
            state['done'], total, '（已取消）' if stopped else ''))
        print_failure_summary()
        print('*' * 45 + '\n')
