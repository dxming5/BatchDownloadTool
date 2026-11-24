# -*- coding: utf-8 -*-
"""
流式逐篇会话

每个任务一个 DownloadSession：平台过完登录闸门、刷新页面后用 meta() 冻结
身份并定位目录；采集器逐条把统一 article 交给 article()，由这里统一下载、
计数与收口。身份数据只有这一个构造点，Task 只做投影，不另拼字段。
"""

from conf import settings
from core import downloader, store
from core.utils import safe_name

# 冻结身份的字段集，供平台与 Task 按名投影，不新增其它隐含字段
IDENTITY_FIELDS = ('platform', 'platform_label', 'nickname', 'id', 'user_url',
                   'profile')


def identity(platform, nickname, blogger_id, user_url):
    """构造身份数据：平台 key/label 取平台对象，昵称做目录名规范化"""
    return {
        'platform': platform.key,
        'platform_label': platform.label,
        'nickname': safe_name(nickname),
        'id': blogger_id or '',
        'user_url': user_url or '',
        'profile': {},
    }


class DownloadSession(object):
    """一次任务的流式编排器：冻结身份、逐篇下载、统计收口"""

    def __init__(self, platform, task, target='', media_label=''):
        self.platform = platform
        self.task = task
        self.target = target
        self.media_label = media_label
        self.identity = None
        self.stats = {
            'candidates': 0,       # 交到本会话的条目数
            'downloaded': 0,       # 本次全成功的篇数
            'skipped_known': 0,    # 已下载过的篇数
            'skipped_invalid': 0,  # 无时间 / 无媒体地址而跳过的篇数
            'files': 0,            # 成功落盘的文件数
            'failed_files': 0,     # 失败的文件（含无媒体地址的篇）数
            'processed': 0,        # 处理过的文件数
            'unknown_types': {},   # 平台归一化阶段报出的未知类型统计
        }
        self.failures = []
        # 收口用：meta 是否成功、失败原因、是否已 close
        self.meta_ok = False
        self.meta_error = ''
        self._known_ids = set()
        self._blogger = None
        self._existed = False
        self._closed = False

    def meta(self, info):
        """
        冻结身份并定位目录；只接受第一次调用，重复上报直接忽略

        Returns:
            dict: {'known_ids': set, 'ok': bool}；校验失败时不 activate，
                  已 mark('error') 并置 stop_flag，调用方不得派发任何 article
        """
        if self.identity is not None:
            return {'known_ids': set(self._known_ids), 'ok': True}

        info = info if isinstance(info, dict) else {}
        # 平台 key/label 一律以当前平台对象为准，避免各平台各拼一份
        frozen = identity(self.platform, info.get('nickname'),
                          info.get('id'), info.get('user_url'))
        if info.get('profile'):
            frozen['profile'] = dict(info['profile'])

        nickname = frozen['nickname']
        if not nickname or not frozen['id']:
            message = '身份信息不完整：取不到博主昵称或 ID，已中止，不下载任何内容'
            self.meta_error = message
            self.task.mark('error', message)
            self.task.stop_flag.set()
            return {'known_ids': set(), 'ok': False}

        existed = store.blogger(self.platform.key, nickname) is not None
        blogger = store.ensure(self.platform.key, nickname)
        if not blogger:
            message = '博主目录不可用：{}'.format(nickname)
            self.meta_error = message
            self.task.mark('error', message)
            self.task.stop_flag.set()
            return {'known_ids': set(), 'ok': False}

        # 登记主页链接：之后点「更新」不用再粘贴；blogger_id 不参与目录定位
        blogger.save_homepage(frozen['user_url'])
        store.activate(blogger)

        self._blogger = blogger
        self._existed = existed
        self.identity = frozen
        self.meta_ok = True
        self._known_ids = blogger.read_ids()
        self.task.set_identity(frozen)
        self.task.push({'stage': 'start', 'blogger': nickname})
        return {'known_ids': set(self._known_ids), 'ok': True}

    def article(self, article):
        """
        下载一条统一 article：{'id', 'create_time', 'urls', 'kind'}

        create_time 是原始时间戳，这里用 downloader.format_create_time 转成
        文件名的命名前缀；一篇的全部媒体都成功才由下载层记 ID。

        Returns:
            bool: True 继续取下一篇，False 应停止采集
        """
        if (self._closed or self.task.stop_flag.is_set()
                or self.identity is None or store.current() is None):
            return False

        self.stats['candidates'] += 1
        article = article or {}
        article_id = str(article.get('id') or '')
        create_time = downloader.format_create_time(article.get('create_time'))
        urls = [url for url in (article.get('urls') or []) if url]

        # 已下载的篇：不计入本次文件数，保留目录与记录
        if article_id and article_id in self._known_ids:
            self.stats['skipped_known'] += 1
            self.task.push({'stage': 'notice',
                            'message': '    跳过：已下载过（{}）'.format(article_id)})
            return True

        # 没有发布时间就无法命名，按现有语义跳过且不记 ID
        if not create_time:
            self.stats['skipped_invalid'] += 1
            self.task.push({'stage': 'item', 'aweme_id': article_id,
                            'reason': 'no_time', 'ok': False})
            return True

        # 没有可用媒体地址：记失败、不记 ID
        if not urls:
            failure = {'url': '', 'file': article_id,
                       'error': '没有可下载的媒体地址（{}）'.format(
                           self.media_label or '媒体')}
            self.failures.append(failure)
            self.stats['skipped_invalid'] += 1
            self.stats['failed_files'] += 1
            self.task.push({'stage': 'failure', 'failure': failure})
            self.task.push({'stage': 'item', 'aweme_id': article_id,
                            'reason': 'no_media', 'ok': False})
            return True

        headers = None
        try:
            headers = self.platform.headers()
        except Exception:
            pass
        finish = getattr(self.platform, 'post_process', None)
        if not callable(finish):
            finish = None

        job = {'id': article_id, 'create_time': create_time, 'urls': urls,
               'kind': article.get('kind') or ''}
        result = downloader.fetch_article(
            job, headers, settings.DOWNLOAD_WORKERS,
            should_stop=self.task.stop_flag.is_set,
            finish=finish,
            log=lambda message: self.task.push({'stage': 'notice',
                                                'message': message}),
            record_ids=store.id_recorder())

        files = result.get('files') or 0
        failures = result.get('failures') or []
        self.stats['files'] += files
        self.stats['failed_files'] += len(failures)
        self.stats['processed'] += files + len(failures)
        self.failures.extend(failures)
        for failure in failures:
            self.task.push({'stage': 'failure', 'failure': failure})
        if result.get('ok'):
            self.stats['downloaded'] += 1

        file_name = result.get('file') or ''
        if not result.get('ok') and failures:
            file_name = failures[0].get('file') or file_name
        self.task.push({'stage': 'item', 'aweme_id': article_id,
                        'ok': result.get('ok'), 'file': file_name})

        return not (result.get('cancelled') or self.task.stop_flag.is_set())

    def close(self):
        """
        幂等收尾：删除本次新建的空目录、输出跳过汇总、store.clear()

        Returns:
            dict: self.stats，供编排层一次性收口
        """
        if self._closed:
            return self.stats
        self._closed = True
        try:
            # 一条都没交出来：撤掉本次刚建的空目录，保留已有博主目录
            if (self.stats['candidates'] == 0 and self._blogger
                    and not self._existed):
                self._blogger.delete()
            if self.stats['skipped_known']:
                self.task.push({'stage': 'notice',
                                'message': '跳过 {} 篇已下载过的内容'.format(
                                    self.stats['skipped_known'])})
            if self.stats['skipped_invalid']:
                self.task.push({'stage': 'notice',
                                'message': '跳过 {} 篇无效条目（无时间或无媒体地址）'.format(
                                    self.stats['skipped_invalid'])})
            unknown = self.stats['unknown_types']
            if unknown:
                self.task.push({'stage': 'notice',
                                'message': '跳过未知类型共 {} 篇：{}'.format(
                                    sum(unknown.values()),
                                    ', '.join('{}={}'.format(key, value)
                                              for key, value
                                              in sorted(unknown.items())))})
        finally:
            store.clear()
        return self.stats
