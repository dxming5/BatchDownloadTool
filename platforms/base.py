# -*- coding: utf-8 -*-
"""
平台接口约定

公用层不认识任何平台字段，只通过这里的东西与平台交互：
    Platform     平台需要实现的成员
    流式契约      平台打开网页后先冻结身份，再逐篇交出条目交给会话下载
"""

from conf import settings


class Platform(object):
    """
    平台实现需要覆盖的成员

    必须：
        key                    平台标识，同时作为下载根下的目录名
        label                  界面显示名
        match(url)             链接是否属于本平台
        parse_target           从链接取出目标标识
        scrape_stream          打开网页、过登录闸门后流式发现条目并逐篇下载

    可选：
        headers()              下载用的请求头
        post_process           落盘后的处理（如 HEIC 转 JPG）
        options()              该平台的抓取参数
        supports_following / following()
                               抓当前登录账号的关注列表（只出链接，不下载）
        user_id_of()           从主页链接取账号标识，用于判断是否已有下载目录
    """

    key = ''
    label = ''
    media_label = '作品'
    supports_following = False      # 是否支持抓取当前登录账号的关注列表

    # ---------------- 必须实现 ----------------

    def match(self, url):
        return False

    def parse_target(self, url):
        raise NotImplementedError

    def scrape_stream(self, target, options, session,
                      on_progress=None, should_stop=None):
        """打开目标网页，过登录闸门后流式逐篇处理，不再返回全量结果。

        三平台统一顺序：
            1. page.goto 只等 DOM 就绪，不注册响应监听、不读页面状态、不发数据请求；
            2. 用 core.browser.ensure_logged_in 等待登录，失败即中止；
            3. 登录后强制刷新页面，丢弃登录前可能拿到的任何数据；
            4. 从页面级数据取昵称/ID，先调用一次 session.meta() 冻结身份；
            5. 逐页/逐详情发现条目，归一化成统一 article 后调用 session.article()，
               session.article() 返回 False 时立即停止翻页。

        article 只含 {'id', 'create_time'(raw 时间戳), 'urls', 'kind'}，不含 author；
        作者过滤由平台归一化函数用冻结后的博主名完成。on_meta 必须早于任何 on_article。

        Returns:
            dict: {'pages': 翻页数, 'count': 交出的条目数,
                   'cancelled': 是否被取消, 'stop_reason': 结束原因,
                   'unknown_types': 未知类型统计}
        """
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

    def user_id_of(self, url):
        """从主页链接里取账号标识；取不到返回空串（用于标注「已下载」）"""
        return ''

    def following(self, options, on_progress=None, should_stop=None):
        """抓取「当前登录账号」的关注列表（只出链接，不下载任何内容）

        目标固定为登录账号自己，不接受账号参数，调用方只负责挑平台。

        Returns:
            dict: {'owner': 昵称,
                   'users': [{'name': 昵称, 'url': 主页链接, 'uid': 平台内标识}],
                   'pages': 翻页数, 'total': 人数, 'cancelled': 是否被取消}
        """
        raise NotImplementedError('该平台暂未支持抓取关注列表')
