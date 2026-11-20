# -*- coding: utf-8 -*-
"""
JSON 解析与 URL 提取模块
负责解析 json.log 文件，提取博主信息、图片URL和视频URL

设计说明：
    parse_aweme 是纯函数（不交互、不写文件），按博主分组返回数据，CLI 与 Web 共用；
    load_all_bloggers 从 json.log 读取并解析，供 CLI 使用。
"""

import json
from datetime import datetime

import config
from utils import read_txt, write_txt, safe_name
import paths  # 导入模块而非具体变量，以便动态获取更新后的值


def parse_aweme(json_data_list):
    """
    解析 JSON 行列表，按博主分组提取 URL

    Args:
        json_data_list: 每行一条接口 Response 的字符串列表

    Returns:
        tuple: (bloggers, stats)
            - bloggers: {博主名: (picture_dict, video_dict)}，按出现顺序
                - picture_dict / video_dict: key=发布时间, value=URL列表
            - stats: {'skipped_lines': 无法解析的行数, 'unknown_types': 未知类型条数}
    """
    bloggers = {}
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

        for aweme in resp_json['aweme_list']:
            if 'create_time' not in aweme:
                continue

            author = aweme.get('author') or {}
            nickname = safe_name(author.get('nickname') or '')
            if not nickname:
                continue

            picture_dict, video_dict = bloggers.setdefault(nickname, ({}, {}))

            time_tick = aweme['create_time']
            dt = datetime.fromtimestamp(time_tick)
            create_time = dt.strftime("%Y-%m-%d %H.%M.%S")

            aweme_type = aweme.get('aweme_type')
            if aweme_type in config.IMAGE_TYPES:
                url_list = []
                for image_list in aweme.get('images') or []:
                    image_urls = image_list.get('url_list') or []
                    if image_urls:
                        url_list.append(image_urls[0])
                if url_list:
                    picture_dict[create_time] = url_list
            elif aweme_type in config.VIDEO_TYPES:
                play_addr = (aweme.get('video') or {}).get('play_addr') or {}
                video_urls = play_addr.get('url_list') or []
                if video_urls:
                    video_dict[create_time] = [video_urls[0]]
            else:
                stats['unknown_types'] += 1

    return bloggers, stats


def register_blogger(blogger_name):
    """把博主写入 aweme_list.json（幂等），返回是否新增"""
    if not blogger_name:
        return False
    blogger_list = read_txt(paths.aweme_list_dir)
    if blogger_name in blogger_list:
        return False
    write_txt(paths.aweme_list_dir, blogger_name)
    return True


def load_all_bloggers():
    """
    CLI 入口：解析 json.log，返回按博主分组的全部数据，并登记所有博主

    Returns:
        dict: {博主名: (picture_dict, video_dict)}
    """
    json_log_path = paths.get_json_log_path()
    json_data_list = read_txt(json_log_path)
    if not json_data_list:
        print('  未在 {} 中读取到有效数据，请先把接口返回的 JSON 按行追加进去！'.format(json_log_path))
        return {}

    bloggers, stats = parse_aweme(json_data_list)
    if stats['skipped_lines']:
        print('  跳过 {} 行无法解析或无 aweme_list 的数据'.format(stats['skipped_lines']))
    if stats['unknown_types']:
        print('  跳过 {} 条未知类型的内容'.format(stats['unknown_types']))
    if not bloggers:
        print("  未能从数据中解析出博主昵称，请检查 json.log 内容！")
        return {}

    for name in bloggers:
        register_blogger(name)
    return bloggers
