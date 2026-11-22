# -*- coding: utf-8 -*-
"""
平台注册表

公用层不认识任何具体平台，只维护「平台 key → 平台实例」的映射。
具体平台在 platforms/__init__.py 里调用 register() 注册。

平台实例需提供（见 platforms/base.py）：
    key        平台标识，同时作为下载根下的目录名（douyin / xiaohongshu）
    label      界面显示名（抖音 / 小红书）
    match(url) 判断链接属于该平台
"""

_PLATFORMS = []
_BY_KEY = {}


def register(platform):
    """注册平台；重复注册同一个 key 时忽略"""
    key = getattr(platform, 'key', '')
    if not key or key in _BY_KEY:
        return platform
    _BY_KEY[key] = platform
    _PLATFORMS.append(platform)
    return platform


def resolve(url):
    """按链接识别平台；无法识别时返回 None"""
    for platform in _PLATFORMS:
        try:
            if platform.match(url):
                return platform
        except Exception:
            continue
    return None


def get(key):
    """按平台 key 取平台实例；未注册返回 None"""
    return _BY_KEY.get(key or '')


def all_platforms():
    """全部已注册平台（保持注册顺序）"""
    return list(_PLATFORMS)


def is_platform_dir(name):
    """目录名是否是平台目录（下载根下的一级目录用）"""
    return (name or '') in _BY_KEY
