# -*- coding: utf-8 -*-
"""抖音平台实现：流式抓取 + 逐篇下载"""

from conf import settings
from core.session import identity
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

    # ---------------- 流式抓取 + 下载 ----------------

    def scrape_stream(self, target, options, session,
                      on_progress=None, should_stop=None):
        """过登录闸门后逐页发现作品，逐篇交给会话下载。

        身份由 on_meta 一次性冻结（早于任何条目派发）；归一化用冻结昵称
        做作者过滤，article 里不带 author。
        """
        stats = {'skipped': {}, 'unknown_types': {}}

        def notice(message):
            """汇总提示走事件输出，界面异常不影响抓取"""
            if not on_progress:
                return
            try:
                on_progress({'stage': 'notice', 'message': message})
            except Exception:
                pass

        def on_meta(nickname, blogger_id, user_url):
            return session.meta(identity(self, nickname, blogger_id, user_url))

        def on_article(item):
            article = assemble.normalize(
                item, session.identity['nickname'], stats)
            if article is None:
                return True     # 跳过条目不打断翻页
            return session.article(article)

        result = scraper.fetch_user_awemes(
            target,
            options=options,
            since_ts=(options or {}).get('since_ts'),
            on_meta=on_meta,
            on_article=on_article,
            on_progress=on_progress,
            should_stop=should_stop,
            verbose=False,
        )
        # unknown_types 交给 pipeline 合并进会话，由 close() 统一汇总输出；
        # 无时间 / 无媒体的语义也由会话处理，这里只提示作者不一致的跳过。
        result['unknown_types'] = dict(sorted(stats['unknown_types'].items()))
        author_skipped = stats['skipped'].get('author')
        if author_skipped:
            notice('跳过 {} 篇作者与目标博主不一致的作品'.format(author_skipped))
        return result

    # ---------------- 关注列表 ----------------

    def following(self, options, on_progress=None, should_stop=None):
        """抓当前登录账号的关注列表（只出链接，不下载）"""
        return scraper.fetch_following(
            options=options,
            on_progress=on_progress,
            should_stop=should_stop,
            verbose=False,
        )

    def user_id_of(self, url):
        try:
            return scraper.parse_sec_user_id(url)
        except ValueError:
            return ''

    def headers(self):
        return assemble.headers()

    def options(self):
        if self._options is None:
            self._options = settings.platform_settings(self.key)
        return dict(self._options)
