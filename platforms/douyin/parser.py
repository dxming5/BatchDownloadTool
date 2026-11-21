# -*- coding: utf-8 -*-
"""
抖音作品类型判定

下载地址、作者、时间都由 download.py 直接从原始接口数据里取，
这里只保留两边都可能用到的类型判定规则。
"""


def classify(aweme, image_types, video_types):
    """按配置判定作品类型；不属于任何已配置类型时返回 None"""
    aweme_type = aweme.get('aweme_type')
    if aweme_type in image_types:
        return 'image'
    if aweme_type in video_types:
        return 'video'
    return None
