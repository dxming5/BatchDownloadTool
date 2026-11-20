# -*- coding: utf-8 -*-
"""
Flask 应用模块
提供浏览器界面所需的后端接口：抓取与下载任务、博主管理、媒体回放

约束：
    paths 模块的博主路径为全局态，因此同一时刻只允许一个任务；
    服务只应绑定 127.0.0.1，避免无鉴权的删除/写文件接口暴露到局域网。
"""

import base64
import collections
import hashlib
import importlib.util
import os
import threading
import time
import uuid
from urllib.parse import quote

from flask import Flask, Response, abort, jsonify, request, send_file

import config
import paths
import downloader
import scraper
from utils import rm_dir, make_dir, safe_name


IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.webp')
VIDEO_EXTS = ('.mp4',)
MEDIA_EXTS = IMAGE_EXTS + VIDEO_EXTS
CACHE_DIR_NAME = '.cache'            # 每个博主目录下的预览缓存文件夹

_TASKS = collections.OrderedDict()   # task_id -> Task，保持插入顺序便于淘汰
_TASKS_LIMIT = 20
_ACTIVE = None                       # 当前运行中的任务
_LOCK = threading.Lock()


class Task(object):
    """单个任务的进度与日志容器（线程安全）"""

    def __init__(self, blogger):
        self.id = uuid.uuid4().hex
        self.blogger = blogger
        self.mode = 'scrape'
        self.phase = 'scrape'          # scrape / download，前端据此显示阶段
        self.status = 'running'        # running / done / cancelled / error
        self.percent = 0
        self.done = 0
        self.total = 0
        self.media_type = ''
        self.current = ''
        self.video_total = 0
        self.picture_total = 0
        self.video_items = 0
        self.picture_items = 0
        self.scrape_pages = 0
        self.scrape_count = 0
        self.unknown_types = {}
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

    def set_phase(self, phase):
        with self._lock:
            self.phase = phase

    def push(self, event):
        """接收 downloader 或 scraper 的进度回调"""
        stage = event.get('stage')
        with self._lock:
            self.percent = event.get('percent', self.percent)
            self.done = event.get('done', self.done)
            self.total = event.get('total', self.total)
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
                self.logs.append('抓取完成，共 {} 条作品，开始下载'.format(self.scrape_count))
            elif stage == 'start':
                self.video_total = event.get('video_total', 0)
                self.picture_total = event.get('picture_total', 0)
                self.video_items = event.get('video_items', 0)
                self.picture_items = event.get('picture_items', 0)
                self.logs.append('开始下载 <{}>：视频 {} 篇，图文 {} 篇'.format(
                    self.blogger, self.video_total, self.picture_total))
            elif stage == 'notice':
                message = (event.get('message') or '').strip()
                if message:
                    self.logs.append(message)
            elif stage == 'file_start':
                self.media_type = event.get('media_type', self.media_type)
                self.current = event.get('file', '')
                self.logs.append('[{}] 下载 {}'.format(self.media_type, self.current))
            elif stage == 'item':
                self.media_type = event.get('media_type', self.media_type)
                reason = event.get('reason')
                if reason == 'unknown_type':
                    self.logs.append('    跳过：未知 aweme_type={}（aweme_id={}）'.format(
                        event.get('aweme_type'), event.get('aweme_id')))
                elif reason == 'no_time':
                    self.logs.append('    跳过：缺少发布时间（aweme_id={}）'.format(
                        event.get('aweme_id')))
                elif reason == 'no_media':
                    self.logs.append('    跳过：没有可下载的媒体地址（aweme_id={}）'.format(
                        event.get('aweme_id')))
                elif reason == 'done':
                    # 已下载的作品数量可能很大，不逐条写日志
                    pass
                elif not event.get('ok'):
                    self.logs.append('    失败：{}'.format(event.get('file', '')))
            elif stage == 'finished':
                self.failures = event.get('failures') or []
                self.unknown_types = event.get('unknown_types') or {}
                self.status = 'cancelled' if event.get('stopped') else 'done'
                self.current = ''
                self.logs.append('任务结束，共处理 {}/{} 篇，失败 {} 个'.format(
                    self.done, self.total, len(self.failures)))
                if self.unknown_types:
                    self.logs.append('跳过未知 aweme_type：{}'.format(
                        ', '.join('{}={}'.format(key, value)
                                  for key, value in self.unknown_types.items())))

    def mark(self, status, message=''):
        with self._lock:
            self.status = status
            if message:
                self.logs.append(message)

    def add_log(self, message):
        with self._lock:
            if message:
                self.logs.append(message)

    def is_running(self):
        with self._lock:
            return self.status == 'running'

    def snapshot(self):
        with self._lock:
            return {
                'id': self.id,
                'blogger': self.blogger,
                'mode': self.mode,
                'phase': self.phase,
                'scrape_pages': self.scrape_pages,
                'scrape_count': self.scrape_count,
                'status': self.status,
                'percent': self.percent,
                'done': self.done,
                'total': self.total,
                'media_type': self.media_type,
                'current': self.current,
                'video_total': self.video_total,
                'picture_total': self.picture_total,
                'video_items': self.video_items,
                'picture_items': self.picture_items,
                'unknown_types': dict(self.unknown_types),
                'logs': list(self.logs),
                'failures': list(self.failures),
                'elapsed': int(time.time() - self.started),
            }


def _trim_tasks():
    """只保留最近的若干任务"""
    while len(_TASKS) > _TASKS_LIMIT:
        _TASKS.popitem(last=False)


def _scraper_ready():
    """Playwright 是否已安装；浏览器内核是否下载只能在运行时发现"""
    try:
        return importlib.util.find_spec('playwright') is not None
    except (ImportError, ValueError):
        return False


def _run_scrape_task(task, sec_user_id, month_days):
    """
    后台线程：抓取博主主页作品 → 转成接口数据行 → 复用下载流程

    抓取与下载串行在同一个任务里，避免占用第二个 _ACTIVE 槽位：
    paths 的博主路径是全局态，并发下载会互相覆盖目录。
    """
    global _ACTIVE
    try:
        paths.set_total_path()
        task.set_phase('scrape')
        since_ts = int(time.time()) - month_days * 86400 if month_days else None
        result = scraper.fetch_user_awemes(
            sec_user_id,
            profile_dir=config.SCRAPE_PROFILE_DIR,
            headless=config.SCRAPE_HEADLESS,
            max_scroll=config.SCRAPE_MAX_SCROLL,
            idle_rounds=config.SCRAPE_IDLE_ROUNDS,
            timeout_ms=config.SCRAPE_TIMEOUT_MS,
            channel=config.SCRAPE_BROWSER_CHANNEL,
            proxy=config.SCRAPE_PROXY,
            wait_login=config.SCRAPE_WAIT_LOGIN,
            since_ts=since_ts,
            on_progress=task.push,
            should_stop=task.stop_flag.is_set,
            verbose=False,
        )

        if task.stop_flag.is_set() or result.get('cancelled'):
            task.mark('cancelled', '抓取已取消')
            return

        # 目录名由 downloader 按 safe_name 生成，登记链接时必须用同一个名字
        folder_name = safe_name(result.get('nickname') or '') or sec_user_id
        task.set_name(folder_name)
        raw_lines = scraper.result_to_lines(result)
        if not raw_lines:
            task.mark('error', '未抓到作品：可能未登录、该主页没有公开作品，或作品超出所选时间范围')
            return

        # 登记主页链接，之后点「更新」就不用再粘贴
        paths.save_homepage(folder_name, result.get('user_url') or '')

        task.set_phase('download')
        task.add_log('开始下载：本次抓到 {} 条作品'.format(len(result.get('aweme_list') or [])))
        downloader.download(progress=task.push,
                            should_stop=task.stop_flag.is_set,
                            raw_lines=raw_lines)
        if task.stop_flag.is_set():
            task.mark('cancelled', '任务已取消')
        elif task.is_running():
            task.mark('done')
    except ImportError as error:
        task.mark('error', '未安装 playwright 组件（{}）：请执行 '
                           'pip install playwright 与 playwright install chromium'.format(error))
    except Exception as error:  # 线程内异常必须兜住，否则前端一直轮询
        task.mark('error', '抓取失败：{}'.format(error))
    finally:
        with _LOCK:
            if _ACTIVE is task:
                _ACTIVE = None


def _blogger_dir(name):
    """把博主名解析为下载目录内的绝对路径，越界或不存在时返回 None"""
    # 拒绝 .cache 等隐藏路径：缓存目录不是博主，不该被当成可浏览目录
    parts = (name or '').replace('\\', '/').split('/')
    if any(part.startswith('.') for part in parts):
        return None
    root = os.path.realpath(paths.get_download_dir())
    target = os.path.realpath(os.path.join(root, name or ''))
    if target == root or not target.startswith(root + os.sep):
        return None
    if not os.path.isdir(target):
        return None
    return target


def _list_blogger_dirs():
    """下载目录下的博主目录；有主页链接或媒体文件的才算"""
    root = paths.get_download_dir()
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return []

    names = []
    for entry in entries:
        if entry.startswith('.'):
            continue
        full = os.path.join(root, entry)
        if not os.path.isdir(full):
            continue
        if os.path.isfile(os.path.join(full, paths.HOMEPAGE_NAME)):
            names.append(entry)
            continue
        if os.path.isdir(os.path.join(full, 'picture')):
            names.append(entry)
            continue
        try:
            has_video = any(item.lower().endswith(VIDEO_EXTS)
                            for item in os.listdir(full))
        except OSError:
            has_video = False
        if has_video:
            names.append(entry)
    return names


def _start_task(sec_user_id, month_days):
    """创建并启动抓取任务；已有任务在执行时返回 (None, 原因)"""
    global _ACTIVE
    with _LOCK:
        if _ACTIVE is not None and _ACTIVE.is_running():
            return None, '已有任务在执行，请等待或先取消'
        task = Task(sec_user_id)
        _TASKS[task.id] = task
        _trim_tasks()
        _ACTIVE = task

    threading.Thread(target=_run_scrape_task,
                     args=(task, sec_user_id, month_days), daemon=True).start()
    return task, ''


def _blogger_item(name):
    """博主列表项：目录名即博主名，附带登记过的主页链接"""
    return {'name': name, 'homepage': paths.read_homepage(name)}


def _resolve_media(rel_path):
    """把相对路径解析为下载目录内的绝对路径，越界返回 None"""
    root = os.path.realpath(paths.get_download_dir())
    target = os.path.realpath(os.path.join(root, (rel_path or '').strip()))
    if target != root and not target.startswith(root + os.sep):
        return None
    if not os.path.isfile(target):
        return None
    return target


def _cache_root(target):
    """
    媒体文件对应的预览缓存根目录：<博主目录>/.cache

    博主目录取下载根目录下的一级子目录；直接放在下载根目录的媒体文件
    退回 <下载根目录>/.cache。缓存与媒体同级，删除博主目录时一并清掉，
    也不会混进博主的图片列表。
    返回 (缓存根目录, 媒体文件相对博主目录的路径)。
    """
    root = os.path.realpath(paths.get_download_dir())
    rel = os.path.relpath(os.path.realpath(target), root).replace('\\', '/')
    parts = rel.split('/')
    if len(parts) > 1 and not parts[0].startswith('.'):
        return os.path.join(root, parts[0], CACHE_DIR_NAME), '/'.join(parts[1:])
    return os.path.join(root, CACHE_DIR_NAME), rel


def _thumb_path(target, size):
    cache_root, inner = _cache_root(target)
    stamp = '{}|{}|{}'.format(inner, os.path.getmtime(target), size)
    key = hashlib.md5(stamp.encode('utf-8')).hexdigest()
    return os.path.join(cache_root, 'thumbs', key + '.jpg')


def _make_thumb(target, size):
    """用 Pillow 生成缩略图并缓存"""
    from PIL import Image
    output = _thumb_path(target, size)
    if os.path.exists(output):
        return output
    make_dir(os.path.dirname(output))
    with Image.open(target) as image:
        image = image.convert('RGB')
        image.thumbnail((size, size))
        image.save(output, 'JPEG', quality=85)
    return output


def _cover_path(target, mtime=None):
    """
    视频封面的缓存路径

    key 由「相对博主目录的路径 + mtime」决定，与缩略图同构：
    视频被重新下载或覆盖后 key 变化，旧封面自然失效。
    封面放在 博主目录/.cache/covers 下，不会混进博主的图片列表。

    mtime 由调用方传入可省掉一次 stat（目录遍历时本就已拿到）。
    """
    cache_root, inner = _cache_root(target)
    if mtime is None:
        try:
            mtime = int(os.path.getmtime(target))
        except OSError:
            mtime = 0
    key = hashlib.md5('{}|{}'.format(inner, mtime).encode('utf-8')).hexdigest()
    return os.path.join(cache_root, 'covers', key + '.jpg')


def _static_stamp(static_folder):
    """静态资源的内容指纹，作为前端资源版本号（内容一变版本号必变）"""
    digest = hashlib.md5()
    for name in ('app.js', 'style.css'):
        try:
            with open(os.path.join(static_folder, name), 'rb') as handle:
                digest.update(handle.read())
        except OSError:
            continue
    return digest.hexdigest()[:12]


def create_app():
    """构建 Flask 应用"""
    app = Flask(__name__,
                static_folder=os.path.join(config.BASE_DIR, 'static'),
                static_url_path='/static')
    try:
        app.json.ensure_ascii = False
    except AttributeError:
        pass

    # ---------------- 页面 ----------------

    @app.get('/')
    def index():
        """返回首页，并按静态资源 mtime 注入版本号，避免浏览器沿用旧 JS/CSS"""
        with open(os.path.join(app.static_folder, 'index.html'), encoding='utf-8') as handle:
            html = handle.read()
        stamp = _static_stamp(app.static_folder)
        html = html.replace('/static/app.js', '/static/app.js?v=' + stamp)
        html = html.replace('/static/style.css', '/static/style.css?v=' + stamp)
        response = Response(html, mimetype='text/html')
        response.headers['Cache-Control'] = 'no-cache, must-revalidate'
        return response

    @app.after_request
    def disable_static_cache(response):
        """
        静态资源与媒体默认每次回源校验

        缩略图与视频封面例外：前端请求 URL 都带 mtime 版本号（&v=xxx），
        内容一变 URL 必变，因此可以按内容长期缓存，避免每次打开预览都重新校验。
        """
        if not request.path.startswith(('/static/', '/api/media')):
            return response

        rel = (request.args.get('path') or '').replace('\\', '/')
        versioned = (
            request.path == '/api/media'
            and response.status_code == 200
            and request.args.get('v')                      # 必须带版本号才敢长缓存
            and (request.args.get('thumb')                  # 缩略图
                 or '/.cache/covers/' in rel)               # 视频封面
        )
        if versioned:
            response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        else:
            response.headers['Cache-Control'] = 'no-cache, must-revalidate'
        return response

    @app.get('/favicon.ico')
    def favicon():
        return '', 204

    # ---------------- 状态 ----------------

    @app.get('/api/status')
    def api_status():
        with _LOCK:
            active = _ACTIVE.snapshot() if _ACTIVE and _ACTIVE.is_running() else None
        return jsonify({
            'blogger_count': len(_list_blogger_dirs()),
            'active_task': active,
            'scrape_ready': _scraper_ready(),
            'scrape_month_days': config.SCRAPE_MONTH_DAYS,
        })

    # ---------------- 抓取并下载 ----------------

    @app.post('/api/scrape')
    def api_scrape():
        """抓取博主主页作品并直接下载"""
        payload = request.get_json(silent=True) or {}
        raw = (payload.get('url') or '').strip()
        if not raw:
            return jsonify({'error': '请先填写博主主页链接'}), 400
        if not _scraper_ready():
            return jsonify({
                'error': '未安装 playwright：请执行 pip install playwright '
                         '与 playwright install chromium 后重启服务'
            }), 503
        try:
            sec_user_id = scraper.parse_sec_user_id(raw)
        except ValueError:
            return jsonify({
                'error': '无法识别该链接，请粘贴博主主页地址（https://www.douyin.com/user/...）'
            }), 400

        want_month = (payload.get('range') or 'all').strip().lower() == 'month'
        month_days = config.SCRAPE_MONTH_DAYS if want_month else 0

        task, error = _start_task(sec_user_id, month_days)
        if not task:
            return jsonify({'error': error}), 409
        return jsonify({'task_id': task.id, 'task': task.snapshot()}), 202

    @app.get('/api/tasks/<task_id>')
    def api_task(task_id):
        task = _TASKS.get(task_id)
        if not task:
            return jsonify({'error': '任务不存在或已过期'}), 404
        return jsonify(task.snapshot())

    @app.post('/api/tasks/<task_id>/cancel')
    def api_task_cancel(task_id):
        task = _TASKS.get(task_id)
        if not task:
            return jsonify({'error': '任务不存在或已过期'}), 404
        task.stop_flag.set()
        return jsonify(task.snapshot())

    # ---------------- 博主管理 ----------------

    @app.get('/api/bloggers')
    def api_bloggers():
        items = [_blogger_item(name) for name in _list_blogger_dirs()]
        return jsonify({'items': items})

    @app.post('/api/bloggers/<path:name>/update')
    def api_blogger_update(name):
        """用目录里登记的主页链接重新抓取；靠 aweme_id 去重，只补新增"""
        if not _blogger_dir(name):
            return jsonify({'error': '博主不存在'}), 404
        if not _scraper_ready():
            return jsonify({
                'error': '未安装 playwright：请执行 pip install playwright '
                         '与 playwright install chromium 后重启服务'
            }), 503

        home = paths.read_homepage(name)
        if not home:
            return jsonify({
                'error': '该博主目录缺少 homepage.txt，请重新粘贴主页链接抓取一次以登记'
            }), 400
        try:
            sec_user_id = scraper.parse_sec_user_id(home)
        except ValueError:
            return jsonify({'error': 'homepage.txt 里的链接无法识别，请检查文件内容'}), 400

        # 更新走全量抓取，已下载的作品由 aweme_id.txt 跳过
        task, error = _start_task(sec_user_id, 0)
        if not task:
            return jsonify({'error': error}), 409
        return jsonify({'task_id': task.id, 'task': task.snapshot()}), 202

    @app.delete('/api/bloggers/<path:name>')
    def api_blogger_delete(name):
        target = _blogger_dir(name)
        if not target:
            return jsonify({'error': '博主不存在'}), 404
        with _LOCK:
            if _ACTIVE is not None and _ACTIVE.is_running():
                return jsonify({'error': '下载任务进行中，暂不能删除'}), 409
        rm_dir(target)
        return jsonify({'deleted': name})

    @app.get('/api/bloggers/<path:name>/files')
    def api_blogger_files(name):
        base = os.path.join(paths.get_download_dir(), name)
        items = []
        if os.path.isdir(base):
            root_dir = paths.get_download_dir()
            for current, dirs, files in os.walk(base):
                # 跳过 .cache 等隐藏目录：缓存缩略图不能出现在博主的图片列表里
                dirs[:] = [entry for entry in dirs if not entry.startswith('.')]
                for file_name in files:
                    lower = file_name.lower()
                    if not lower.endswith(MEDIA_EXTS):
                        continue
                    full = os.path.join(current, file_name)
                    try:
                        info = os.stat(full)
                    except OSError:
                        continue
                    size = info.st_size
                    mtime = int(info.st_mtime)
                    rel = os.path.relpath(full, root_dir).replace('\\', '/')
                    kind = 'video' if lower.endswith(VIDEO_EXTS) else 'image'
                    item = {
                        'name': file_name,
                        'kind': kind,
                        'size': size,
                        'mtime': mtime,
                        'rel': rel,
                        'url': '/api/media?path=' + quote(rel),
                        'thumb': '/api/media?thumb=320&path=' + quote(rel),
                    }
                    # 视频封面由前端取帧后回存，存在就直接返回，前端不必再取帧
                    if kind == 'video':
                        cover = _cover_path(full, mtime)
                        if os.path.isfile(cover):
                            cover_rel = os.path.relpath(cover, root_dir).replace('\\', '/')
                            item['cover'] = '/api/media?path=' + quote(cover_rel)
                    items.append(item)
        # 仅影响预览展示：按文件名降序（保存与命名逻辑不变）
        items.sort(key=lambda item: item['name'], reverse=True)
        return jsonify({'name': name, 'count': len(items), 'items': items})

    @app.post('/api/covers')
    def api_cover_save():
        """前端把取到的视频首帧回存到 博主目录/.cache/covers，下次直接复用"""
        payload = request.get_json(silent=True) or {}
        target = _resolve_media(payload.get('path', ''))
        if not target or not target.lower().endswith(VIDEO_EXTS):
            return jsonify({'error': '无效的视频路径'}), 400

        data = (payload.get('data') or '').strip()
        if not data.startswith('data:image/'):
            return jsonify({'error': '封面数据无效'}), 400
        try:
            raw = base64.b64decode(data.split(',', 1)[1], validate=True)
        except Exception:
            return jsonify({'error': '封面数据无法解码'}), 400
        if not raw or len(raw) > 512 * 1024:
            return jsonify({'error': '封面数据大小异常'}), 413

        output = _cover_path(target)
        make_dir(os.path.dirname(output))
        try:
            with open(output, 'wb') as handle:
                handle.write(raw)
        except OSError as error:
            return jsonify({'error': '封面写入失败：{}'.format(error)}), 500

        root_dir = paths.get_download_dir()
        cover_rel = os.path.relpath(output, root_dir).replace('\\', '/')
        return jsonify({'cover': '/api/media?path=' + quote(cover_rel)})

    # ---------------- 媒体回放 ----------------

    @app.get('/api/media')
    def api_media():
        target = _resolve_media(request.args.get('path', ''))
        if not target:
            abort(404)
        thumb = request.args.get('thumb')
        if thumb and target.lower().endswith(IMAGE_EXTS):
            try:
                size = max(64, min(1024, int(thumb)))
                return send_file(_make_thumb(target, size), mimetype='image/jpeg')
            except Exception:
                pass
        return send_file(target, conditional=True)

    return app
