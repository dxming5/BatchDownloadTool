# -*- coding: utf-8 -*-
"""
响应解析与媒体 URL 提取模块

设计说明：
    collect_awemes 把「每行一条接口 Response」的原始数据摊平成「一篇作品一条」的计划列表，
    不再按 create_time 聚合（旧版 {昵称: (图片dict, 视频dict)} 会让同一秒发布的作品互相覆盖）。

    aweme_media 按 config 中的 image_types / video_types 判定类型并取出待下载 URL；
    类型未知时由调用方跳过并告知，且不记录 aweme_id，便于以后补充配置后重下。

    文件命名、去重、下载全部交给 downloader。
"""

import json

import config
from utils import safe_name


def classify_aweme(aweme):
    """
    按配置判定作品类型

    Returns:
        str|None: 'image' / 'video'；不属于任何已配置类型时返回 None
    """
    aweme_type = aweme.get('aweme_type')
    if aweme_type in config.IMAGE_TYPES:
        return 'image'
    if aweme_type in config.VIDEO_TYPES:
        return 'video'
    return None


def aweme_media(aweme):
    """
    提取一篇作品待下载的 URL

    Args:
        aweme: 接口响应中的单个作品 dict

    Returns:
        tuple: (kind, urls, aweme_type)
            - kind: 'image' / 'video' / 'unknown'
            - urls: 按顺序排列的 URL 列表；unknown 时为空列表
            - aweme_type: 原始 aweme_type，供日志说明未知类型
    """
    aweme_type = aweme.get('aweme_type')
    kind = classify_aweme(aweme)
    if kind is None:
        return 'unknown', [], aweme_type

    if kind == 'image':
        urls = []
        for image_list in aweme.get('images') or []:
            image_urls = (image_list or {}).get('url_list') or []
            if image_urls:
                urls.append(image_urls[0])
        return 'image', urls, aweme_type

    play_addr = (aweme.get('video') or {}).get('play_addr') or {}
    video_urls = play_addr.get('url_list') or []
    return 'video', video_urls[:1], aweme_type


def collect_awemes(json_data_list):
    """
    把接口原始响应摊平成「一篇作品一条」的计划列表

    Args:
        json_data_list: 每行一条接口 Response 的字符串列表

    Returns:
        tuple: (plan, stats)
            - plan: [(nickname, aweme_id, aweme), ...]，按 aweme_id 全局去重并保持出现顺序
            - stats: {'skipped_lines', 'duplicated', 'no_author', 'no_id'}
    """
    plan = []
    seen_ids = set()
    stats = {'skipped_lines': 0, 'duplicated': 0, 'no_author': 0, 'no_id': 0}

    for json_data in json_data_list:
        try:
            resp_json = json.loads(json_data)
        except ValueError:
            stats['skipped_lines'] += 1
            continue

        if not isinstance(resp_json, dict) or 'aweme_list' not in resp_json:
            stats['skipped_lines'] += 1
            continue

        for aweme in resp_json.get('aweme_list') or []:
            if not isinstance(aweme, dict):
                stats['skipped_lines'] += 1
                continue

            aweme_id = aweme.get('aweme_id')
            if not aweme_id:
                stats['no_id'] += 1
                continue
            aweme_id = str(aweme_id)
            if aweme_id in seen_ids:
                stats['duplicated'] += 1
                continue

            author = aweme.get('author') or {}
            nickname = safe_name(author.get('nickname') or '')
            if not nickname:
                stats['no_author'] += 1
                continue

            seen_ids.add(aweme_id)
            plan.append((nickname, aweme_id, aweme))

    return plan, stats
