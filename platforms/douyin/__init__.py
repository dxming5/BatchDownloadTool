# -*- coding: utf-8 -*-
"""抖音平台实现：抓取 + 解析 + 下载参数"""

from conf import settings
from platforms.base import Platform
from platforms.douyin import download as download_impl, scraper


class DouyinPlatform(Platform):
    key = 'douyin'
    label = '抖音'
    media_label = '抖音作品'

    def __init__(self):
        self._options = None

    # ---------------- 识别 ----------------

    def match(self, url):
        value = (url or '').replace('\ufeff', '').replace('\u200b', '').lower()
        return 'douyin.com' in value or 'iesdouyin.com' in value

    def parse_target(self, url):
        return scraper.parse_sec_user_id(url)

    # ---------------- 抓取 ----------------

    def scrape(self, target, options, on_progress=None, should_stop=None):
        return scraper.fetch_user_awemes(
            target,
            options=options,
            since_ts=options.get('since_ts'),
            on_progress=on_progress,
            should_stop=should_stop,
            verbose=False,
        )

    # ---------------- 下载 ----------------

    def download(self, result, store, on_progress=None, should_stop=None,
                 media_label=''):
        return download_impl.download(
            result, store,
            on_progress=on_progress,
            should_stop=should_stop,
            media_label=media_label or self.media_label,
        )

    def headers(self):
        return download_impl.headers()

    def options(self):
        if self._options is None:
            self._options = settings.platform_settings(self.key)
        return dict(self._options)
