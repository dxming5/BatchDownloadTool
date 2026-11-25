# -*- coding: utf-8 -*-
"""
任务模块

任务状态、进度、日志、取消都在这里收口，前端只轮询快照。
所有阶段的事件结构保持一致，前端不需要区分来自抓取还是下载。
"""

import collections
import threading
import time
import uuid


class Task(object):
    """单个任务的进度与日志容器（线程安全）"""

    def __init__(self, target='', platform_key='', platform_label='', mode='scrape'):
        self.id = uuid.uuid4().hex
        self.platform_key = platform_key
        self.platform_label = platform_label
        self.blogger = target
        self.mode = mode
        # scrape / download / following，前端据此显示阶段与文案
        self.phase = 'scrape'
        self.status = 'running'        # running / done / cancelled / error
        self.percent = 0
        self.done = 0
        self.total = 0
        self.current = ''
        self.video_total = 0
        self.picture_total = 0
        self.scrape_pages = 0
        self.scrape_count = 0
        self.unknown_types = {}
        self.following = []            # 关注列表结果（结束时一次性写入）
        self.following_count = 0
        self.logs = collections.deque(maxlen=300)
        self.failures = []
        self.started = time.time()
        self.stop_flag = threading.Event()
        self._lock = threading.Lock()

    def set_name(self, blogger):
        """抓到博主昵称后更新显示名"""
        if not blogger:
            return
        with self._lock:
            self.blogger = blogger

    def set_platform(self, key, label):
        with self._lock:
            self.platform_key = key or self.platform_key
            self.platform_label = label or self.platform_label

    def set_phase(self, phase):
        with self._lock:
            self.phase = phase

    def set_following(self, users):
        """关注列表结果；只在抓取结束时写一次，避免轮询反复传大 JSON"""
        with self._lock:
            self.following = list(users or [])
            self.following_count = len(self.following)

    def push(self, event):
        """接收抓取或下载的进度回调"""
        stage = event.get('stage')
        with self._lock:
            self.done = event.get('done', self.done)
            self.total = event.get('total', self.total)
            # 百分比在界面层换算：下载侧只提供已处理条数与总条数
            if self.total:
                self.percent = int(self.done / self.total * 100)
            if stage == 'scrape':
                self.phase = 'scrape'
                self.scrape_pages = event.get('pages', self.scrape_pages)
                self.scrape_count = event.get('count', self.scrape_count)
                message = (event.get('message') or '').strip()
                if message and (not self.logs or self.logs[-1] != message):
                    self.logs.append('抓取：{}'.format(message))
            elif stage == 'scrape_done':
                self.phase = 'scrape'
                self.scrape_pages = event.get('pages', self.scrape_pages)
                self.scrape_count = event.get('count', self.scrape_count)
                self.logs.append('抓取完成，共 {} 条作品'.format(self.scrape_count))
            elif stage == 'following':
                self.phase = 'following'
                self.following_count = event.get('count', self.following_count)
                message = (event.get('message') or '').strip()
                if message and (not self.logs or self.logs[-1] != message):
                    self.logs.append('关注列表：{}'.format(message))
            elif stage == 'start':
                self.video_total = event.get('video_total', 0)
                self.picture_total = event.get('picture_total', 0)
                self.logs.append('开始下载 {} <{}>'.format(
                    self.platform_label, self.blogger))
            elif stage == 'notice':
                message = (event.get('message') or '').strip()
                if message:
                    self.logs.append(message)
            elif stage == 'item':
                reason = event.get('reason')
                if reason == 'no_time':
                    self.logs.append('    跳过：缺少发布时间（aweme_id={}）'.format(
                        event.get('aweme_id')))
                elif reason == 'no_media':
                    self.logs.append('    跳过：没有可下载的媒体地址（aweme_id={}）'.format(
                        event.get('aweme_id')))
                elif not event.get('ok'):
                    self.logs.append('    失败：{}'.format(event.get('file', '')))
            elif stage == 'finished':
                self.failures = event.get('failures') or []
                self.unknown_types = event.get('unknown_types') or {}
                self.status = 'cancelled' if event.get('stopped') else 'done'
                self.current = ''
                self.logs.append('任务结束，共处理 {}/{} 个文件，失败 {} 个'.format(
                    self.done, self.total, len(self.failures)))
                if self.unknown_types:
                    self.logs.append('跳过未知 aweme_type：{}'.format(
                        ', '.join('{}={}'.format(key, value)
                                  for key, value in self.unknown_types.items())))
                # 下载结束的收尾提示：取消时不打印，避免与「已取消」混淆
                if not event.get('stopped'):
                    self.logs.append('下载 {} {} 完成'.format(
                        self.platform_label or self.platform_key, self.blogger))

    def mark(self, status, message=''):
        with self._lock:
            self.status = status
            if message:
                self.logs.append(message)

    def is_running(self):
        with self._lock:
            return self.status == 'running'

    def snapshot(self):
        with self._lock:
            return {
                'id': self.id,
                'platform': self.platform_key,
                'platform_label': self.platform_label,
                'blogger': self.blogger,
                'mode': self.mode,
                'phase': self.phase,
                'scrape_pages': self.scrape_pages,
                'scrape_count': self.scrape_count,
                'status': self.status,
                'percent': self.percent,
                'done': self.done,
                'total': self.total,
                'current': self.current,
                'video_total': self.video_total,
                'picture_total': self.picture_total,
                'unknown_types': dict(self.unknown_types),
                'following': list(self.following),
                'following_count': self.following_count,
                'logs': list(self.logs),
                'failures': list(self.failures),
                'elapsed': int(time.time() - self.started),
            }
