# -*- coding: utf-8 -*-
"""
平台接口约定

公用层不认识任何平台字段，只通过这里的东西与平台交互：
    Platform     平台需要实现的成员
    流式契约      平台打开网页后先冻结身份，再逐篇交出条目交给编排层下载
"""

from core import config


class Platform(object):
    """
    平台实现需要覆盖的成员

    必须：
        key                    平台标识，同时作为下载根下的目录名
        label                  界面显示名
        match(url)             链接是否属于本平台
        parse_target(url)      从链接取出 (账号标识, 规范化主页链接)
        scrape_stream          打开网页、过登录闸门后流式发现条目并逐篇交出

    可选：
        headers()              下载用的请求头
        post_process           落盘后的处理（如 HEIC 转 JPG）
        options()              该平台的抓取参数
        supports_following / following()
                               抓当前登录账号的关注列表（只出链接，不下载）
        user_id_of()           从主页链接取账号标识（定位已有目录、标注已下载）
    """

    key = ''
    label = ''
    media_label = '作品'
    supports_following = False      # 是否支持抓取当前登录账号的关注列表

    # ---------------- 必须实现 ----------------

    def match(self, url):
        return False

    def parse_target(self, url):
        """返回 (账号标识, 规范化主页链接)；两者都由链接解析得到"""
        raise NotImplementedError

    def scrape_stream(self, target, options, on_meta, on_article,
                      on_progress=None, should_stop=None):
        """打开目标网页，过登录闸门后流式逐篇处理，不再返回全量结果。

        Args:
            target: 规范化主页链接（平台 parse_target 的第二项）

        流程与平台无关，平台只实现「方言」那几格：

            1. S1 打开：page.goto 只等 DOM 就绪，不注册响应监听、不读页面
               状态、不发数据请求；
            2. S2 登录闸门：core.browser.ensure_logged_in，失败即抛未登录；
            3. S3 重建页面数据（平台只做 3.2）：
               3.1 公共：core.browser.reload_after_login 先挂监听再重新进入目标页，
                   丢弃登录前可能拿到的任何数据；
               3.2 平台：从页面级数据取昵称（read_nickname），只交昵称字符串；
               3.3 公共：on_meta(nickname) 冻结身份——建目录、写 homepage.txt、
                   读入已知 ID；ID 与主页链接在编排层已从链接解析得到；
            4. S4 逐页/逐详情发现条目，归一化成统一 article 后调用
               on_article()，返回 False 时立即停止。

        on_meta(nickname) 只认第一次调用，返回 {'known_ids': set, 'ok': bool}；
        ok 为 False 时不得再派发任何 article。on_article(article) 返回
        True 继续、False 停止。article 只含 {'id', 'create_time'(raw 时间戳),
        'urls', 'kind'}，不含 author；作者过滤由平台归一化函数用昵称完成。

        Returns:
            dict: {'pages': 翻页数, 'count': 交出的条目数,
                   'cancelled': 是否被取消, 'stop_reason': 结束原因,
                   'unknown_types': 未知类型统计, 'error': 抓取错误}
        """
        raise NotImplementedError

    # ---------------- 可选 ----------------

    def headers(self):
        """下载请求头；默认用配置里的公共头"""
        return dict(config.HEADER)

    def post_process(self, path):
        """落盘后处理；默认原样返回"""
        return path

    def options(self):
        """该平台的抓取参数（由配置段合并默认值）"""
        return config.platform_settings(self.key)

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
