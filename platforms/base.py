# -*- coding: utf-8 -*-
"""
平台接口约定

公用层不认识任何平台字段，只通过这里的东西与平台交互：
    Platform     平台需要实现的成员
    抓取结果      平台自定结构，原样交回平台自己解析与下载
"""

from conf import settings


class Platform(object):
    """
    平台实现需要覆盖的成员

    必须：
        key            平台标识，同时作为下载根下的目录名
        label          界面显示名
        match(url)     链接是否属于本平台
        parse_target   从链接取出目标标识
        scrape         抓取并返回平台自定的结果结构
        assemble       这批内容怎么下（挑条目、取地址、命名材料都由平台决定）

    可选：
        headers()      下载用的请求头
        post_process   落盘后的处理（如 HEIC 转 JPG）
        options()      该平台的抓取参数
        supports_following / following()
                       抓当前登录账号的关注列表（只出链接，不下载）
        user_id_of()   从主页链接取账号标识，用于判断是否已有下载目录
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

    def scrape(self, target, options, on_progress=None, should_stop=None):
        raise NotImplementedError

    def assemble(self, result, on_progress=None, should_stop=None,
                 media_label=''):
        """这批内容怎么下：挑哪些、地址怎么取由平台决定

        博主名与已记录的 ID 由 store 提供（编排层已 activate）；把条目整理成
        (篇 ID, 发布时间, [地址]) 交给 downloader.fetch_all，由下载层统一校验
        去重、拼文件名落盘、并发下载与按地址计进度。平台既不接收博主对象，
        也不接收目录或路径参数，落盘路径由下载层向 store 要。
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
