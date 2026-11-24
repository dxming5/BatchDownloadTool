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
        self.blogger_id = ''       # 冻结的博主 ID（不参与目录定位）
        self.profile = {}          # 身份扩展位，预留给后续平台字段
        self.mode = mode
        # scrape / stream / download / following，前端据此显示阶段与文案
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
        self.processed = 0             # 本次处理的文件数（结束时一次性写入）
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

    def set_identity(self, info):
        """冻结身份：只投影昵称、ID 与扩展资料，目录名仍用 safe_name 后的昵称"""
        if not info:
            return
        with self._lock:
            self.blogger = info.get('nickname') or self.blogger
            self.blogger_id = info.get('id') or ''
            self.profile = dict(info.get('profile') or {})

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
            if stage == 'scrape':
                # stream 阶段只记日志，不让阶段在 scrape / stream 间抖动
                if self.phase != 'stream':
                    self.phase = 'scrape'
                self.scrape_pages = event.get('pages', self.scrape_pages)
                self.scrape_count = event.get('count', self.scrape_count)
                message = (event.get('message') or '').strip()
                if message and (not self.logs or self.logs[-1] != message):
                    self.logs.append('抓取：{}'.format(message))
            elif stage == 'stream':
                # 流式抓取与下载交错：只记日志，不改 phase
                message = (event.get('message') or '').strip()
                if message and (not self.logs or self.logs[-1] != message):
                    self.logs.append(message)
            elif stage == 'scrape_done':
                if self.phase != 'stream':
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
            elif stage == 'failure':
                failure = event.get('failure')
                if failure:
                    self.failures.append(failure)
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
                # 进度只在收口写一次，不再逐文件刷新
                self.done = event.get('done', self.done)
                self.total = event.get('total', self.total)
                self.percent = int(self.done / self.total * 100) if self.total else 0
                self.failures = event.get('failures') or []
                self.unknown_types = event.get('unknown_types') or {}
                self.processed = event.get('processed', self.processed)
                # 取消 / 错误已由收口段 mark，这里不覆盖已有终态
                if self.status == 'running':
                    self.status = 'cancelled' if event.get('stopped') else 'done'
                self.current = ''
                self.logs.append('任务结束，共处理 {}/{} 个文件，失败 {} 个'.format(
                    self.done, self.total, len(self.failures)))
                if self.unknown_types:
                    self.logs.append('跳过未知类型：{}'.format(
                        ', '.join('{}={}'.format(key, value)
                                  for key, value in self.unknown_types.items())))
                # 收尾提示：只在正常完成时打印
                if self.status == 'done':
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
                'blogger_id': self.blogger_id,
                'profile': dict(self.profile),
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
