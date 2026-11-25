# -*- coding: utf-8 -*-
"""小红书平台实现：抓取 + 下载组装"""

from core import config
from platforms.base import Platform
from platforms.xiaohongshu import assemble, scraper


class XiaohongshuPlatform(Platform):
    key = 'xiaohongshu'
    label = '小红书'
    media_label = '小红书笔记'

    def __init__(self):
        self._options = None

    # ---------------- 识别 ----------------

    def match(self, url):
        value = (url or '').replace('\ufeff', '').replace('\u200b', '').lower()
        return 'xiaohongshu.com' in value

    def parse_target(self, url):
        """返回 (user_id, 规范化主页链接)；链接保留 xsec_token"""
        return scraper.parse_user_id(url)

    def user_id_of(self, url):
        try:
            return scraper.parse_user_id(url)[0]
        except ValueError:
            return ''

    # ---------------- 抓取 ----------------

    def scrape_stream(self, target, options, on_meta, on_article,
                      on_progress=None, should_stop=None):
        """
        流式抓取 + 下载：登录闸门后冻结身份，详情到手逐篇交给编排层

        handle_meta 只上报一次，且只交页面昵称；已下载笔记的 ID 由编排层
        在 meta 时读入并回传，采集器据此连详情页都不打开。
        """
        frozen = {'nickname': ''}

        def handle_meta(nickname):
            frozen['nickname'] = nickname or ''
            return on_meta(nickname)

        def handle_article(note):
            if note.get('_known'):
                # 已下载笔记不打开详情，只把 ID 交回编排层计入 skipped_known
                return on_article({'id': str(note.get('note_id') or '')})
            article = assemble.normalize(note, frozen['nickname'])
            if article is None:
                return True     # 不属于目标博主 / 无法归一化：丢弃，继续
            return on_article(article)

        result = scraper.fetch_user_notes(
            target,
            options=options,
            since_ts=options.get('since_ts'),
            on_progress=on_progress,
            should_stop=should_stop,
            on_meta=handle_meta,
            on_article=handle_article,
            verbose=False,
        ) or {}
        return {
            'pages': result.get('pages', 0),
            'count': result.get('count', 0),
            'cancelled': result.get('cancelled', False),
            'stop_reason': result.get('stop_reason', ''),
            'unknown_types': result.get('unknown_types', {}),
            'error': '',
        }

    # ---------------- 下载组装 ----------------

    def headers(self):
        return assemble.headers()

    def post_process(self, path):
        return assemble.post_process(path)

    def options(self):
        if self._options is None:
            self._options = config.platform_settings(self.key)
        return dict(self._options)
