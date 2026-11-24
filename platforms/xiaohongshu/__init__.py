# -*- coding: utf-8 -*-
"""小红书平台实现：抓取 + 下载组装"""

from conf import settings
from core import store
from core.session import identity
from core.utils import safe_name
from platforms.base import Platform
from platforms.xiaohongshu import assemble, scraper


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

    def scrape_stream(self, target, options, session, on_progress=None,
                      should_stop=None):
        """
        流式抓取 + 下载：登录闸门后冻结身份，详情到手逐篇交给会话

        on_meta 只上报一次，返回的 known_ids 由采集器与平台反查的
        known_ids 取并集；on_article 负责归一化后立即调 session.article。
        """
        frozen = {'nickname': ''}

        def on_meta(nickname, blogger_id, user_url):
            # 身份数据只在 core.session 里构造一次，平台只交原始三元组
            result = session.meta(identity(self, nickname, blogger_id, user_url))
            try:
                frozen['nickname'] = (session.identity or {}).get('nickname', '') or ''
            except Exception:
                frozen['nickname'] = safe_name(nickname or '')
            return result

        def on_article(note):
            if note.get('_known'):
                # 已下载笔记不打开详情，只把 ID 交回会话计入 skipped_known
                return session.article({'id': str(note.get('note_id') or '')})
            article = assemble.normalize(note, frozen['nickname'])
            if article is None:
                return True     # 不属于目标博主 / 无法归一化：丢弃，继续
            return session.article(article)

        result = scraper.fetch_user_notes(
            target,
            options=options,
            since_ts=options.get('since_ts'),
            on_progress=on_progress,
            should_stop=should_stop,
            known_ids=self.known_ids(target),
            on_meta=on_meta,
            on_article=on_article,
            verbose=False,
        )
        return {
            'pages': result.get('pages', 0),
            'count': result.get('count', 0),
            'cancelled': result.get('cancelled', False),
            'stop_reason': result.get('stop_reason', ''),
            'unknown_types': result.get('unknown_types', {}),
        }

    def known_ids(self, target):
        """按主页链接反查已有目录，取出已下载的笔记 ID；找不到返回空集"""
        wanted = _user_id_of(target)
        if not wanted:
            return set()
        for blogger in store.platform_bloggers(self.key):
            homepage = blogger.read_homepage()
            if homepage and _user_id_of(homepage) == wanted:
                return blogger.read_ids()
        return set()

    # ---------------- 下载组装 ----------------

    def headers(self):
        return assemble.headers()

    def post_process(self, path):
        return assemble.post_process(path)

    def options(self):
        if self._options is None:
            self._options = settings.platform_settings(self.key)
        return dict(self._options)
