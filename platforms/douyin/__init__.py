# -*- coding: utf-8 -*-
"""抖音平台实现：抓取 + 下载组装"""

from conf import settings
from platforms.base import Platform
from platforms.douyin import assemble, scraper


class DouyinPlatform(Platform):
    key = 'douyin'
    label = '抖音'
    media_label = '抖音作品'
    supports_following = True

    def __init__(self):
        self._options = None

    # ---------------- 识别 ----------------

    def match(self, url):
        value = (url or '').replace('\ufeff', '').replace('\u200b', '').lower()
        return 'douyin.com' in value or 'iesdouyin.com' in value

    def parse_target(self, url):
        return scraper.parse_sec_user_id(url)

    # ---------------- 抓取 ----------------

    def scrape(self, target, options, on_progress=None, should_stop=None,
               on_profile=None):
        return scraper.fetch_user_awemes(
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

    # ---------------- 关注列表 ----------------

    def following(self, options, on_progress=None, should_stop=None):
        """抓当前登录账号的关注列表（只出链接，不下载）"""
        return scraper.fetch_following(
            options=options,
            on_progress=on_progress,
            should_stop=should_stop,
            verbose=False,
        )

    def headers(self):
        return assemble.headers()

    def options(self):
        if self._options is None:
            self._options = settings.platform_settings(self.key)
        return dict(self._options)
