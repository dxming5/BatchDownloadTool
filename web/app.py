# -*- coding: utf-8 -*-
"""
Flask 应用模块
提供浏览器界面所需的后端接口：日志导入、解析、下载任务、博主管理、媒体回放

约束：
    paths 模块的博主路径为全局态，因此同一时刻只允许一个下载任务；
    服务只应绑定 127.0.0.1，避免无鉴权的删除/写文件接口暴露到局域网。
"""

import collections
import hashlib
import importlib.util
import json
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
from utils import (read_txt, write_list_txt, append_list_txt, rm_dir,
                   make_dir, safe_name)
from url_parser import parse_aweme, register_blogger


IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.webp')
VIDEO_EXTS = ('.mp4',)
MEDIA_EXTS = IMAGE_EXTS + VIDEO_EXTS

_TASKS = collections.OrderedDict()   # task_id -> Task，保持插入顺序便于淘汰
_TASKS_LIMIT = 20
_ACTIVE = None                       # 当前运行中的任务
_LOCK = threading.Lock()

# 导入的接口数据只驻留内存（不写 json.log），重启即丢失
_JSON_LOCK = threading.Lock()
_JSON_LINES = []                     # 每行一条接口 Response
_JSON_BYTES = 0                      # 已占用字节数（utf-8 估算，含换行）


class Task(object):
    """单个任务的进度与日志容器（线程安全）；抓取与下载共用"""

    def __init__(self, blogger, mode='download'):
        self.id = uuid.uuid4().hex
        self.blogger = blogger
        self.mode = mode               # download（粘贴 JSON）/ scrape（抓取并下载）
        self.phase = 'download'        # scrape / download，前端据此显示阶段
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
                self.logs.append('开始下载 <{}>：视频 {} 条（{} 个文件），图片 {} 条（{} 张）'.format(
                    self.blogger, self.video_total, self.video_items,
                    self.picture_total, self.picture_items))
            elif stage == 'file_start':
                self.media_type = event.get('media_type', self.media_type)
                self.current = event.get('file', '')
                self.logs.append('[{}] 下载 {}'.format(self.media_type, self.current))
            elif stage == 'item':
                self.media_type = event.get('media_type', self.media_type)
                if event.get('skipped'):
                    self.logs.append('    跳过（已下载）')
                elif not event.get('ok'):
                    self.logs.append('    失败')
            elif stage == 'finished':
                self.failures = event.get('failures') or []
                self.status = 'cancelled' if event.get('stopped') else 'done'
                self.current = ''
                self.logs.append('任务结束，共处理 {}/{} 项，失败 {} 个'.format(
                    self.done, self.total, len(self.failures)))

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
                'logs': list(self.logs),
                'failures': list(self.failures),
                'elapsed': int(time.time() - self.started),
            }


def _trim_tasks():
    """只保留最近的若干任务"""
    while len(_TASKS) > _TASKS_LIMIT:
        _TASKS.popitem(last=False)


def _clear_imported():
    """清空内存中的导入数据（Web 导入不落盘）"""
    global _JSON_BYTES
    with _JSON_LOCK:
        _JSON_LINES.clear()
        _JSON_BYTES = 0


def _run_task(task, raw_lines):
    """后台线程：执行一次下载（raw_lines 为导入数据快照）"""
    global _ACTIVE
    try:
        paths.set_total_path()
        downloader.download(progress=task.push,
                            should_stop=task.stop_flag.is_set,
                            raw_lines=raw_lines)
        if task.stop_flag.is_set():
            task.mark('cancelled', '任务已取消')
        elif task.is_running():
            task.mark('done')
            # 这份数据已经处理完，清空以免下次粘贴时和旧数据一起被下载
            _clear_imported()
    except Exception as error:  # 线程内异常必须兜住，否则前端一直轮询
        task.mark('error', '任务异常：{}'.format(error))
    finally:
        with _LOCK:
            if _ACTIVE is task:
                _ACTIVE = None


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

        task.set_name(result.get('nickname') or sec_user_id)
        raw_lines = scraper.result_to_lines(result)
        if not raw_lines:
            task.mark('error', '未抓到作品：可能未登录、该主页没有公开作品，或作品超出所选时间范围')
            return

        register_blogger(task.blogger)
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


def _json_log_info():
    """导入数据的统计信息（数据只驻留内存，不读写 json.log）"""
    with _JSON_LOCK:
        lines = len(_JSON_LINES)
        size = _JSON_BYTES
        tail = _JSON_LINES[-1][-200:] if lines else ''
    return {'path': 'memory', 'exists': lines > 0, 'lines': lines,
            'size': size, 'limit': config.IMPORT_MEMORY_LIMIT, 'tail': tail}


def _blogger_stats(name):
    """统计某个博主目录下的视频数、图片数与占用空间"""
    base = os.path.join(paths.get_download_dir(), name)
    videos = pictures = 0
    size = 0
    if os.path.isdir(base):
        for current, _dirs, files in os.walk(base):
            for file_name in files:
                lower = file_name.lower()
                if lower.endswith(VIDEO_EXTS):
                    videos += 1
                elif lower.endswith(IMAGE_EXTS):
                    pictures += 1
                else:
                    continue
                try:
                    size += os.path.getsize(os.path.join(current, file_name))
                except OSError:
                    continue
    return {'name': name, 'video': videos, 'picture': pictures,
            'size': size, 'size_mb': round(size / 1024.0 / 1024.0, 2),
            'exists': os.path.isdir(base)}


def _resolve_media(rel_path):
    """把相对路径解析为下载目录内的绝对路径，越界返回 None"""
    root = os.path.realpath(paths.get_download_dir())
    target = os.path.realpath(os.path.join(root, (rel_path or '').strip()))
    if target != root and not target.startswith(root + os.sep):
        return None
    if not os.path.isfile(target):
        return None
    return target


def _thumb_path(target, size):
    stamp = '{}|{}|{}'.format(target, os.path.getmtime(target), size)
    key = hashlib.md5(stamp.encode('utf-8')).hexdigest()
    return os.path.join(paths.get_download_dir(), '.thumbs', key + '.jpg')


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


def _static_stamp(static_folder):
    """静态资源的最新修改时间，作为前端资源版本号"""
    latest = 0
    for name in ('app.js', 'style.css'):
        try:
            latest = max(latest, os.path.getmtime(os.path.join(static_folder, name)))
        except OSError:
            continue
    return latest or time.time()


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
        stamp = str(int(_static_stamp(app.static_folder)))
        html = html.replace('/static/app.js', '/static/app.js?v=' + stamp)
        html = html.replace('/static/style.css', '/static/style.css?v=' + stamp)
        response = Response(html, mimetype='text/html')
        response.headers['Cache-Control'] = 'no-cache, must-revalidate'
        return response

    @app.after_request
    def disable_static_cache(response):
        """静态资源每次回源校验，避免前端代码改了浏览器还在用旧缓存"""
        if request.path.startswith('/static/'):
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
            'json_log': _json_log_info(),
            'blogger_count': len(read_txt(paths.aweme_list_dir)),
            'active_task': active,
            'scrape_ready': _scraper_ready(),
            'scrape_month_days': config.SCRAPE_MONTH_DAYS,
        })

    # ---------------- 导入数据（内存） ----------------

    @app.get('/api/json-log')
    def api_json_log():
        return jsonify(_json_log_info())

    @app.post('/api/json-log')
    def api_json_log_import():
        """导入接口数据：默认覆盖上一次，append=True 时才追加"""
        global _JSON_BYTES
        payload = request.get_json(silent=True) or {}
        text = payload.get('text') or ''
        append = bool(payload.get('append'))

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return jsonify({'error': '没有可导入的内容'}), 400

        valid = []
        invalid = 0
        for line in lines:
            try:
                json.loads(line)
            except ValueError:
                invalid += 1
                continue
            valid.append(line)

        # 按 utf-8 编码长度估算占用，末尾换行计入
        incoming = sum(len(line.encode('utf-8')) + 1 for line in valid)
        limit = config.IMPORT_MEMORY_LIMIT
        with _JSON_LOCK:
            base = _JSON_BYTES if append else 0
            if base + incoming > limit:
                return jsonify({
                    'error': '本次导入超过 {} MB 内存上限，请减少粘贴内容'.format(
                        limit // 1024 // 1024)
                }), 413
            if append:
                _JSON_LINES.extend(valid)
                _JSON_BYTES += incoming
            else:
                _JSON_LINES[:] = valid
                _JSON_BYTES = incoming
            total = len(_JSON_LINES)
            size = _JSON_BYTES

        return jsonify({'added': len(valid), 'invalid': invalid,
                        'lines': total, 'size': size, 'limit': limit})

    @app.post('/api/json-log/clear')
    def api_json_log_clear():
        _clear_imported()
        return jsonify({'lines': 0, 'size': 0, 'limit': config.IMPORT_MEMORY_LIMIT})

    # ---------------- 解析 ----------------

    @app.post('/api/parse')
    def api_parse():
        with _JSON_LOCK:
            data = list(_JSON_LINES)
        if not data:
            return jsonify({'error': '还没有导入数据，请先粘贴接口 JSON'}), 400
        bloggers, stats = parse_aweme(data)
        if not bloggers:
            return jsonify({'error': '未能解析出博主昵称，请检查数据内容'}), 400
        video_items = sum(sum(len(v) for v in video.values())
                          for _picture, video in bloggers.values())
        picture_items = sum(sum(len(v) for v in picture.values())
                            for picture, _video in bloggers.values())
        return jsonify({
            'blogger_count': len(bloggers),
            'video_items': video_items,
            'picture_items': picture_items,
            'skipped_lines': stats['skipped_lines'],
            'unknown_types': stats['unknown_types'],
        })

    @app.post('/api/bloggers')
    def api_blogger_register():
        payload = request.get_json(silent=True) or {}
        name = safe_name(payload.get('name') or '')
        if not name:
            return jsonify({'error': '博主名称不能为空'}), 400
        created = register_blogger(name)
        return jsonify({'name': name, 'created': created})

    # ---------------- 下载任务 ----------------

    @app.post('/api/download')
    def api_download():
        global _ACTIVE
        with _JSON_LOCK:
            raw_lines = list(_JSON_LINES)
        if not raw_lines:
            return jsonify({'error': '还没有导入数据，请先粘贴接口 JSON'}), 400

        # 本次任务会下载全部博主，先把它们登记进 aweme_list，博主管理才会显示
        bloggers, _stats = parse_aweme(raw_lines)
        if not bloggers:
            return jsonify({'error': '未能解析出博主昵称，请检查数据内容'}), 400
        for blogger_name in bloggers:
            register_blogger(blogger_name)

        with _LOCK:
            if _ACTIVE is not None and _ACTIVE.is_running():
                return jsonify({'error': '已有下载任务在执行，请等待或先取消'}), 409
            task = Task('全部博主')
            _TASKS[task.id] = task
            _trim_tasks()
            _ACTIVE = task

        threading.Thread(target=_run_task, args=(task, raw_lines), daemon=True).start()
        return jsonify({'task_id': task.id, 'task': task.snapshot()})

    @app.post('/api/scrape')
    def api_scrape():
        """抓取博主主页作品并直接下载（一步式，无需粘贴 JSON）"""
        global _ACTIVE
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

        with _LOCK:
            if _ACTIVE is not None and _ACTIVE.is_running():
                return jsonify({'error': '已有任务在执行，请等待或先取消'}), 409
            task = Task(sec_user_id, mode='scrape')
            task.set_phase('scrape')
            _TASKS[task.id] = task
            _trim_tasks()
            _ACTIVE = task

        threading.Thread(target=_run_scrape_task,
                         args=(task, sec_user_id, month_days), daemon=True).start()
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
        names = read_txt(paths.aweme_list_dir)
        items = [_blogger_stats(name) for name in names]
        known = set(names)

        # 目录里已下载但未登记的博主（旧版本下载、手动拷入）一并列出，并补登记
        download_dir = paths.get_download_dir()
        try:
            entries = sorted(os.listdir(download_dir))
        except OSError:
            entries = []
        missing = []
        for entry in entries:
            if entry.startswith('.') or entry in known:
                continue
            if not os.path.isdir(os.path.join(download_dir, entry)):
                continue
            stats = _blogger_stats(entry)
            if not stats['video'] and not stats['picture']:
                continue
            known.add(entry)
            missing.append(entry)
            items.append(stats)
        if missing:
            # 追加而非整体重写：保持“按出现先后”的顺序，也不覆盖并发新增的行
            append_list_txt(paths.aweme_list_dir, missing)

        return jsonify({'items': items})

    @app.delete('/api/bloggers/<path:name>')
    def api_blogger_delete(name):
        names = read_txt(paths.aweme_list_dir)
        if name not in names:
            return jsonify({'error': '博主不在列表中'}), 404
        with _LOCK:
            if _ACTIVE is not None and _ACTIVE.is_running():
                return jsonify({'error': '下载任务进行中，暂不能删除'}), 409
        rm_dir(os.path.join(paths.get_download_dir(), name))
        names.remove(name)
        write_list_txt(paths.aweme_list_dir, names)
        return jsonify({'deleted': name})

    @app.get('/api/bloggers/<path:name>/files')
    def api_blogger_files(name):
        base = os.path.join(paths.get_download_dir(), name)
        items = []
        if os.path.isdir(base):
            root_dir = paths.get_download_dir()
            for current, _dirs, files in os.walk(base):
                for file_name in files:
                    lower = file_name.lower()
                    if not lower.endswith(MEDIA_EXTS):
                        continue
                    full = os.path.join(current, file_name)
                    try:
                        size = os.path.getsize(full)
                        mtime = int(os.path.getmtime(full))
                    except OSError:
                        continue
                    rel = os.path.relpath(full, root_dir).replace('\\', '/')
                    items.append({
                        'name': file_name,
                        'kind': 'video' if lower.endswith(VIDEO_EXTS) else 'image',
                        'size': size,
                        'mtime': mtime,
                        'rel': rel,
                        'url': '/api/media?path=' + quote(rel),
                        'thumb': '/api/media?thumb=320&path=' + quote(rel),
                    })
        # 仅影响预览展示：按文件名降序（保存与命名逻辑不变）
        items.sort(key=lambda item: item['name'], reverse=True)
        return jsonify({'name': name, 'count': len(items), 'items': items})

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
