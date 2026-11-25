# -*- coding: utf-8 -*-
"""抖音平台实现：流式抓取 + 逐篇下载"""

from core import config
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
        """返回 (sec_user_id, 规范化主页链接)"""
        sec_user_id = scraper.parse_sec_user_id(url)
        return sec_user_id, scraper.home_url(sec_user_id)

    # ---------------- 流式抓取 + 下载 ----------------

    def scrape_stream(self, target, options, on_meta, on_article,
                      on_progress=None, should_stop=None):
        """过登录闸门后逐页发现作品，逐篇交给编排层下载。

        身份由 handle_meta 一次性冻结（早于任何条目派发）；平台只交页面
        昵称，ID 与主页链接在编排层已从链接解析得到。归一化用该昵称做
        作者过滤，article 里不带 author。
        """
        stats = {'skipped': {}, 'unknown_types': {}}
        frozen = {'nickname': ''}

        def notice(message):
            """汇总提示走事件输出，界面异常不影响抓取"""
            if not on_progress:
                return
            try:
                on_progress({'stage': 'notice', 'message': message})
            except Exception:
                pass

        def handle_meta(nickname):
            frozen['nickname'] = nickname or ''
            return on_meta(nickname)

        def handle_article(item):
            article = assemble.normalize(item, frozen['nickname'], stats)
            if article is None:
                return True     # 跳过条目不打断翻页
            return on_article(article)

        result = scraper.fetch_user_awemes(
            target,
            options=options,
            since_ts=(options or {}).get('since_ts'),
            on_meta=handle_meta,
            on_article=handle_article,
            on_progress=on_progress,
            should_stop=should_stop,
            verbose=False,
        ) or {}
        # 无时间 / 无媒体的语义由编排层处理，这里只提示作者不一致的跳过
        author_skipped = stats['skipped'].get('author')
        if author_skipped:
            notice('跳过 {} 篇作者与目标博主不一致的作品'.format(author_skipped))
        # 统一返回：只留编排层认识的字段，平台私货不进契约
        return {
            'pages': result.get('pages', 0),
            'count': result.get('count', 0),
            'cancelled': result.get('cancelled', False),
            'stop_reason': result.get('stop_reason', ''),
            'unknown_types': dict(sorted(stats['unknown_types'].items())),
            'error': '',
        }

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
            self._options = config.platform_settings(self.key)
        return dict(self._options)
