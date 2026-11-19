# -*- coding: utf-8 -*-
"""
JSON 解析与 URL 提取模块
负责解析 json.log 文件，提取博主信息、图片URL和视频URL

设计说明：
    parse_aweme 是纯函数（不交互、不写文件），CLI 与 Web 共用；
    get_all_url 保留原有的终端交互语义，仅供 CLI 使用。
"""

import json
from datetime import datetime

from config import IMAGE_TYPES, VIDEO_TYPES
from utils import read_txt, write_txt, safe_name
import paths  # 导入模块而非具体变量，以便动态获取更新后的值


def parse_aweme(json_data_list):
    """
    解析 JSON 行列表，提取博主名、图片URL、视频URL

    Args:
        json_data_list: 每行一条接口 Response 的字符串列表

    Returns:
        tuple: (blogger_names, all_picture_url_dict, all_video_url_dict, stats)
            - blogger_names: 按出现顺序去重的博主昵称列表
            - all_picture_url_dict: key=发布时间, value=图片URL列表
            - all_video_url_dict: key=发布时间, value=视频URL列表
            - stats: {'skipped_lines': 无法解析的行数, 'unknown_types': 未知类型条数}
    """
    blogger_names = []
    # 记录所有图片的url：字典结构，key=发布时间，value=每条抖音中图片的url list
    all_picture_url_dict = dict()
    # 记录所有视频的url：字典结构，key=发布时间，value=每条抖音中视频的url list
    all_video_url_dict = dict()
    stats = {'skipped_lines': 0, 'unknown_types': 0}

    for json_data in json_data_list:
        try:
            resp_json = json.loads(json_data)
        except ValueError:
            stats['skipped_lines'] += 1
            continue

        if 'aweme_list' not in resp_json:
            stats['skipped_lines'] += 1
            continue

        # 获取当页所有视频URL
        for aweme in resp_json['aweme_list']:
            if 'create_time' not in aweme:
                continue

            # 获取博主名字
            author = aweme.get('author') or {}
            nickname = safe_name(author.get('nickname') or '')
            if nickname and nickname not in blogger_names:
                blogger_names.append(nickname)

            # 获取抖音发布时间
            time_tick = aweme['create_time']
            dt = datetime.fromtimestamp(time_tick)
            create_time = dt.strftime("%Y-%m-%d %H.%M.%S")

            picture_url_mblog = []  # 记录每条抖音图片url
            video_url_mblog = []  # 记录每条抖音视频 url
            aweme_type = aweme.get('aweme_type')
            # 图片抖音
            if aweme_type in IMAGE_TYPES:
                for image_list in aweme.get('images') or []:
                    url_list = image_list.get('url_list') or []
                    if url_list:
                        picture_url_mblog.append(url_list[0])
            # 视频抖音
            elif aweme_type in VIDEO_TYPES:
                play_addr = (aweme.get('video') or {}).get('play_addr') or {}
                url_list = play_addr.get('url_list') or []
                if url_list:
                    video_url_mblog.append(url_list[0])
            else:
                stats['unknown_types'] += 1

            if picture_url_mblog:
                all_picture_url_dict[create_time] = picture_url_mblog
            if video_url_mblog:
                all_video_url_dict[create_time] = video_url_mblog

    return blogger_names, all_picture_url_dict, all_video_url_dict, stats


def register_blogger(blogger_name):
    """把博主写入 aweme_list.json（幂等），返回是否新增"""
    if not blogger_name:
        return False
    blogger_list = read_txt(paths.aweme_list_dir)
    if blogger_name in blogger_list:
        return False
    write_txt(paths.aweme_list_dir, blogger_name)
    return True


def rename_blogger(old_name, new_name):
    """把博主列表中已登记的名字替换为新名字"""
    blogger_list = read_txt(paths.aweme_list_dir)
    if old_name in blogger_list and new_name and new_name != old_name:
        blogger_list = [new_name if item == old_name else item for item in blogger_list]
        write_list(paths.aweme_list_dir, blogger_list)


def write_list(txt_dir, content_list):
    """兼容旧调用：整体覆盖写列表"""
    from utils import write_list_txt
    write_list_txt(txt_dir, content_list)


def get_all_url():
    """
    CLI 入口：解析 json.log，并在需要时交互确认博主名

    Returns:
        tuple: (blogger_name, all_picture_url_dict, all_video_url_dict)
    """
    json_log_path = paths.get_json_log_path()
    json_data_list = read_txt(json_log_path)
    if not json_data_list:
        print('  未在 {} 中读取到有效数据，请先把接口返回的 JSON 按行追加进去！'.format(json_log_path))
        return '', {}, {}

    blogger_names, all_picture_url_dict, all_video_url_dict, stats = parse_aweme(json_data_list)
    if stats['skipped_lines']:
        print('  跳过 {} 行无法解析或无 aweme_list 的数据'.format(stats['skipped_lines']))
    if stats['unknown_types']:
        print('  跳过 {} 条 aweme_type 非 0、51、55、61、68 的内容'.format(stats['unknown_types']))
    if not blogger_names:
        print("  未能从数据中解析出博主昵称，请检查 json.log 内容！")
        return '', all_picture_url_dict, all_video_url_dict

    blogger_name = blogger_names[0]
    blogger_list = read_txt(paths.aweme_list_dir)
    if blogger_name in blogger_list:
        print(' 当前blogger <' + blogger_name + '> 已在列表中，名字无需修正！')
    else:
        change_name = input(
            " 当前blogger<{}>不在列表中，名字是否修正（需要修正：输入修正名字；不需要修正：按回车键）：".format(blogger_name))
        if change_name:
            blogger_name = safe_name(change_name)
        # 新下载的blogger写入到文本文档bloglist.txt
        write_txt(paths.aweme_list_dir, blogger_name)

    return blogger_name, all_picture_url_dict, all_video_url_dict
