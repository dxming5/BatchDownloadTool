# -*- coding: utf-8 -*-
"""微博平台实现：抓取 + 下载组装（流式）"""

from conf import settings
from core.session import identity
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
        """返回规范化的主页地址，写入 homepage.txt 供「更新」复用"""
        return scraper.home_url(scraper.parse_uid(url))

    # ---------------- 流式抓取与下载 ----------------

    def scrape_stream(self, target, options, session, on_progress=None,
                      should_stop=None):
        """打开主页 → 登录闸门 → 刷新后冻结身份 → 逐页逐条交出微博

        身份只在登录刷新后由 on_meta 上报一次；每条微博先归一化，再交给
        会话逐篇下载。跳过汇总仍由平台输出，可下载条目由会话负责。

        Returns:
            dict: pages / count / cancelled / stop_reason / unknown_types
        """
        skipped = {}

        def on_meta(nickname, blogger_id, user_url):
            # 平台只交原始三元组，身份结构由 core.session 统一构造并冻结
            return session.meta(identity(self, nickname, blogger_id, user_url))

        def on_article(mblog):
            frozen = getattr(session, 'identity', None) or {}
            article = assemble.normalize(mblog, frozen.get('nickname', ''), skipped)
            if article is None:
                return True     # 纯文字 / 作者不符 / 无地址：跳过，继续下一条
            return session.article(article)

        result = scraper.fetch_user_posts(
            target,
            options=options,
            since_ts=options.get('since_ts'),
            on_meta=on_meta,
            on_article=on_article,
            on_progress=on_progress,
            should_stop=should_stop,
            verbose=False,
        ) or {}

        # 跳过汇总：只记日志，不参与进度统计
        if skipped and on_progress:
            try:
                on_progress({'stage': 'notice', 'message': '微博跳过汇总：{}'.format(
                    ', '.join('{}={}'.format(name, count)
                              for name, count in sorted(skipped.items())))})
            except Exception:
                pass

        return {
            'pages': result.get('pages', 0),
            'count': result.get('count', 0),
            'cancelled': result.get('cancelled', False),
            'stop_reason': result.get('stop_reason', ''),
            'unknown_types': result.get('unknown_types', {}),
        }

    def headers(self):
        return assemble.headers()

    def options(self):
        if self._options is None:
            self._options = settings.platform_settings(self.key)
        return dict(self._options)
