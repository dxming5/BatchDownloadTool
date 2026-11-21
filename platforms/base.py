# -*- coding: utf-8 -*-
"""
平台接口约定

公用层不认识任何平台字段，只通过这里的东西与平台交互：
    Platform     平台需要实现的成员
    抓取结果      平台自定结构，原样交回平台自己解析与下载
"""

from conf import settings


class Platform(object):
    """
    平台实现需要覆盖的成员

    必须：
        key            平台标识，同时作为下载根下的目录名
        label          界面显示名
        match(url)     链接是否属于本平台
        parse_target   从链接取出目标标识
        scrape         抓取并返回平台自定的结果结构
        download       这批内容怎么下载（挑条目、取地址、命名、上报都由平台决定）

    可选：
        headers()      下载用的请求头
        post_process   落盘后的处理（如 HEIC 转 JPG）
        options()      该平台的抓取参数
    """

    key = ''
    label = ''
    media_label = '作品'

    # ---------------- 必须实现 ----------------

    def match(self, url):
        return False

    def parse_target(self, url):
        raise NotImplementedError

    def scrape(self, target, options, on_progress=None, should_stop=None):
        raise NotImplementedError

    def download(self, result, store, on_progress=None, should_stop=None,
                 media_label=''):
        """这批内容怎么下载：挑哪些、地址怎么取、进度怎么报都由平台决定"""
        raise NotImplementedError

    # ---------------- 可选 ----------------

    def headers(self):
        """下载请求头；默认用配置里的公共头"""
        return dict(settings.HEADER)

    def post_process(self, path):
        """落盘后处理；默认原样返回"""
        return path

    def options(self):
        """该平台的抓取参数（由配置段合并默认值）"""
        return settings.platform_settings(self.key)
