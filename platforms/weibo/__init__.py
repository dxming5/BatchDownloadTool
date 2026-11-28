# -*- coding: utf-8 -*-
"""微博平台实现：抓取 + 下载组装"""

from conf import settings
from platforms.base import Platform
from platforms.weibo import assemble, scraper


class WeiboPlatform(Platform):
    key = 'weibo'
    label = '微博'
    media_label = '微博作品'

    def __init__(self):
        self._options = None

    # ---------------- 识别 ----------------

    def match(self, url):
        value = (url or '').replace('\ufeff', '').replace('\u200b', '').strip()
        lowered = value.lower()
        if 'weibo.cn' in lowered or 'weibo.com' in lowered:
            return True
        # 裸 uid（纯数字）也认作微博
        return bool(value) and value.isdigit() and len(value) >= 5

    def parse_target(self, url):
        """返回规范化的主页地址，写入 author.json 供「更新」复用"""
        return scraper.home_url(scraper.parse_uid(url))

    # ---------------- 抓取 ----------------

    def scrape(self, target, options, on_progress=None, should_stop=None,
               on_profile=None):
        return scraper.fetch_user_posts(
            target,
            options=options,
            since_ts=options.get('since_ts'),
            on_progress=on_progress,
            should_stop=should_stop,
            on_profile=on_profile,
            verbose=False,
        )

    # ---------------- 下载组装 ----------------

    def assemble(self, result, on_progress=None, should_stop=None,
                 media_label=''):
        return assemble.run(
            result,
            on_progress=on_progress,
            should_stop=should_stop,
            media_label=media_label or self.media_label,
        )

    def headers(self):
        return assemble.headers()

    def options(self):
        if self._options is None:
            self._options = settings.platform_settings(self.key)
        return dict(self._options)
