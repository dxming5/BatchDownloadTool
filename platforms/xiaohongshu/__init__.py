# -*- coding: utf-8 -*-
"""小红书平台实现：抓取 + 解析 + 下载参数"""

from conf import settings
from core.store import BloggerStore, download_root
from platforms.base import Platform
from platforms.xiaohongshu import download as download_impl, scraper


def _user_id_of(url):
    """从主页链接里取 user_id；取不到返回空串"""
    try:
        user_id, _url = scraper.parse_user_id(url)
        return user_id
    except ValueError:
        return ''


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
        """返回规范化后的主页链接（保留 xsec_token，部分内容不带 token 看不到）"""
        _user_id, user_url = scraper.parse_user_id(url)
        return user_url

    # ---------------- 抓取 ----------------

    def scrape(self, target, options, on_progress=None, should_stop=None):
        # 已经下载过的笔记不再打开详情页：这里只有小红书自己关心这件事
        store = BloggerStore(download_root(), self.key)
        return scraper.fetch_user_notes(
            target,
            options=options,
            since_ts=options.get('since_ts'),
            on_progress=on_progress,
            should_stop=should_stop,
            known_ids=self.known_ids(target, store),
            verbose=False,
        )

    def known_ids(self, target, store):
        """按主页链接反查已有目录，取出已下载的笔记 ID；找不到返回空集"""
        wanted = _user_id_of(target)
        if not wanted:
            return set()
        for name in store.list_bloggers():
            homepage = store.read_homepage(name)
            if homepage and _user_id_of(homepage) == wanted:
                return store.read_ids(name)
        return set()

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

    def post_process(self, path):
        return download_impl.post_process(path)

    def options(self):
        if self._options is None:
            self._options = settings.platform_settings(self.key)
        return dict(self._options)
