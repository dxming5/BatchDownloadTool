# -*- coding: utf-8 -*-
"""
平台注册

新增平台只需：
    1. 在 platforms/<key>/ 下实现 scraper / parser / download 与平台类
    2. 在这里 import 并 register

公用层通过 core.registry 使用平台，不直接 import 本包。
"""

from core import registry
from platforms.douyin import DouyinPlatform
from platforms.xiaohongshu import XiaohongshuPlatform
from platforms.weibo import WeiboPlatform

registry.register(DouyinPlatform())
registry.register(XiaohongshuPlatform())
registry.register(WeiboPlatform())

__all__ = ['DouyinPlatform', 'XiaohongshuPlatform', 'WeiboPlatform']
