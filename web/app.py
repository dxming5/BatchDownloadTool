# -*- coding: utf-8 -*-
"""
Flask 应用

路由层只做「校验参数 → 交给公用层 → 返回结果」，不包含平台字段与业务规则。

约束：
    浏览器数据目录与下载目录按平台隔离，但同一平台内仍只允许一个任务
    （抓取要用同一个浏览器 profile，两个任务会互相抢占）。
"""

import base64
import collections
import hashlib
import importlib.util
import os
import threading
from urllib.parse import quote

from flask import Flask, Response, jsonify, request
from markupsafe import escape

import platforms  # noqa: F401  导入即注册平台
from conf import settings
from core import paths, registry, store
from core.pipeline import run as run_pipeline, run_following
from core.task import Task
from core.utils import make_dir
from web.media_api import MEDIA_EXTS, VIDEO_EXTS, cover_path, media_response

_TASKS = collections.OrderedDict()   # task_id -> Task，保持插入顺序便于淘汰
_TASKS_LIMIT = 20
_ACTIVE = None                       # 当前运行中的任务
_LOCK = threading.Lock()


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


def _run_task(task, url, month_days):
    """后台线程：按任务模式执行，异常必须兜住，否则前端会一直轮询"""
    global _ACTIVE
    try:
        if task.mode == 'following':
            run_following(task.platform_key, task)
        else:
            run_pipeline(url, month_days, task)
    except ImportError as error:
        task.mark('error', '未安装 playwright 组件（{}）：请执行 '
                           'pip install playwright 与 playwright install chromium'.format(error))
    except Exception as error:
        task.mark('error', '抓取失败：{}'.format(error))
    finally:
        with _LOCK:
            if _ACTIVE is task:
                _ACTIVE = None


def _start_task(url, month_days):
    """创建并启动任务；已有任务在执行时返回 (None, 原因)"""
    global _ACTIVE
    with _LOCK:
        if _ACTIVE is not None and _ACTIVE.is_running():
            return None, '已有任务在执行，请等待或先取消'
        platform = registry.resolve(url)
        if platform is None:
            return None, '无法识别该链接所属平台，请粘贴博主主页地址'
        task = Task(target=url,
                    platform_key=platform.key,
                    platform_label=platform.label)
        _TASKS[task.id] = task
        _trim_tasks()
        _ACTIVE = task

    threading.Thread(target=_run_task, args=(task, url, month_days), daemon=True).start()
    return task, ''


def _start_platform_task(platform_key, mode='following'):
    """按平台直接起任务（没有目标链接，如「我的关注」）；已有任务时返回 (None, 原因)"""
    global _ACTIVE
    platform = registry.get(platform_key)
    if platform is None:
        return None, '无法识别的平台'
    if not getattr(platform, 'supports_following', False):
        return None, '「{}」暂未支持抓取关注列表'.format(platform.label)
    with _LOCK:
        if _ACTIVE is not None and _ACTIVE.is_running():
            return None, '已有任务在执行，请等待或先取消'
        task = Task(target='我的关注',
                    platform_key=platform.key,
                    platform_label=platform.label,
                    mode=mode)
        _TASKS[task.id] = task
        _trim_tasks()
        _ACTIVE = task

    threading.Thread(target=_run_task, args=(task, '', 0), daemon=True).start()
    return task, ''


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
                static_folder=os.path.join(settings.BASE_DIR, 'static'),
                static_url_path='/static')
    try:
        app.json.ensure_ascii = False
    except AttributeError:
        pass

    # ---------------- 页面 ----------------

    @app.get('/')
    def index():
        """返回首页，并按静态资源内容注入版本号，避免浏览器沿用旧 JS/CSS"""
        with open(os.path.join(app.static_folder, 'index.html'), encoding='utf-8') as handle:
            html = handle.read()
        stamp = _static_stamp(app.static_folder)
        html = html.replace('/static/app.js', '/static/app.js?v=' + stamp)
        html = html.replace('/static/style.css', '/static/style.css?v=' + stamp)
        # 浏览器标签标题由配置注入（escape 防止配置里的标签被当 HTML 解析）
        html = html.replace('{{SITE_TITLE}}', str(escape(settings.WEB_TITLE)))
        response = Response(html, mimetype='text/html')
        response.headers['Cache-Control'] = 'no-cache, must-revalidate'
        return response

    @app.after_request
    def disable_static_cache(response):
        """
        静态资源与媒体默认每次回源校验

        缩略图与视频封面例外：前端请求 URL 都带 mtime 版本号（&v=xxx），
        内容一变 URL 必变，因此可以按内容长期缓存。
        """
        if not request.path.startswith(('/static/', '/api/media')):
            return response

        rel = (request.args.get('path') or '').replace('\\', '/')
        versioned = (
            request.path == '/api/media'
            and response.status_code == 200
            and request.args.get('v')
            and (request.args.get('thumb') or paths.is_cover(rel))
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
        count = 0
        for platform in registry.all_platforms():
            count += len(store.platform_bloggers(platform.key, VIDEO_EXTS))
        with _LOCK:
            active = _ACTIVE.snapshot() if _ACTIVE and _ACTIVE.is_running() else None
        return jsonify({
            'blogger_count': count,
            'active_task': active,
            'scrape_ready': _scraper_ready(),
            'platforms': [{'key': p.key, 'label': p.label,
                           'following': bool(getattr(p, 'supports_following', False))}
                          for p in registry.all_platforms()],
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

        platform = registry.resolve(raw)
        if platform is None:
            return jsonify({
                'error': '无法识别该链接，请粘贴博主主页地址'
                         '（如 https://www.douyin.com/user/...）'
            }), 400
        try:
            platform.parse_target(raw)
        except ValueError as error:
            return jsonify({'error': str(error)}), 400

        want_month = (payload.get('range') or 'all').strip().lower() == 'month'
        month_days = (platform.options().get('month_days') or 30) if want_month else 0

        task, error = _start_task(raw, month_days)
        if not task:
            return jsonify({'error': error}), 409
        return jsonify({'task_id': task.id, 'task': task.snapshot()}), 202

    # ---------------- 关注列表 ----------------

    @app.post('/api/following')
    def api_following():
        """读取本机已登录账号的关注列表；只返回清单，不触发下载"""
        payload = request.get_json(silent=True) or {}
        platform_key = (payload.get('platform') or 'douyin').strip()
        platform = registry.get(platform_key)
        if platform is None:
            return jsonify({'error': '无法识别的平台'}), 400
        if not getattr(platform, 'supports_following', False):
            return jsonify({
                'error': '「{}」暂未支持抓取关注列表'.format(platform.label)
            }), 400
        if not _scraper_ready():
            return jsonify({
                'error': '未安装 playwright：请执行 pip install playwright '
                         '与 playwright install chromium 后重启服务'
            }), 503

        task, error = _start_platform_task(platform_key, mode='following')
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
        """按平台分组返回博主列表"""
        groups = []
        total = 0
        for platform in registry.all_platforms():
            bloggers = store.platform_bloggers(platform.key, VIDEO_EXTS)
            if not bloggers:
                continue
            items = [dict(blogger.item(), platform=platform.key)
                     for blogger in bloggers]
            total += len(items)
            groups.append({'platform': platform.key,
                           'label': platform.label,
                           'items': items})
        return jsonify({'groups': groups, 'total': total})

    @app.post('/api/bloggers/<platform_key>/<name>/update')
    def api_blogger_update(platform_key, name):
        """用目录里登记的主页链接重新抓取；靠作品 ID 去重，只补新增"""
        blogger = store.blogger(platform_key, name)
        if not blogger:
            return jsonify({'error': '博主不存在'}), 404
        if not _scraper_ready():
            return jsonify({
                'error': '未安装 playwright：请执行 pip install playwright '
                         '与 playwright install chromium 后重启服务'
            }), 503

        home = blogger.read_homepage()
        if not home:
            return jsonify({
                'error': '该博主目录缺少 homepage.txt，请重新粘贴主页链接抓取一次以登记'
            }), 400

        # 更新走全量抓取，已下载的作品由记录文件跳过
        task, error = _start_task(home, 0)
        if not task:
            return jsonify({'error': error}), 409
        return jsonify({'task_id': task.id, 'task': task.snapshot()}), 202

    @app.delete('/api/bloggers/<platform_key>/<name>')
    def api_blogger_delete(platform_key, name):
        blogger = store.blogger(platform_key, name)
        if not blogger:
            return jsonify({'error': '博主不存在'}), 404
        with _LOCK:
            if _ACTIVE is not None and _ACTIVE.is_running():
                return jsonify({'error': '下载任务进行中，暂不能删除'}), 409
        blogger.delete()
        return jsonify({'deleted': name})

    @app.get('/api/bloggers/<platform_key>/<name>/files')
    def api_blogger_files(platform_key, name):
        blogger = store.blogger(platform_key, name)
        items = []
        if blogger:
            ident = '&platform={}&name={}'.format(quote(platform_key), quote(name))
            # 遍历规则（跳过缓存等隐藏目录）由 store 给出，路由层不复制布局
            for full in paths.media_files(blogger.key, blogger.name, MEDIA_EXTS):
                try:
                    info = os.stat(full)
                except OSError:
                    continue
                file_name = os.path.basename(full)
                lower = file_name.lower()
                mtime = int(info.st_mtime)
                rel = paths.relative(full)
                kind = 'video' if lower.endswith(VIDEO_EXTS) else 'image'
                item = {
                    'name': file_name,
                    'kind': kind,
                    'size': info.st_size,
                    'mtime': mtime,
                    'rel': rel,
                    'url': '/api/media?path=' + quote(rel) + ident,
                    'thumb': '/api/media?thumb=320&path=' + quote(rel) + ident,
                }
                # 视频封面由前端取帧后回存，存在就直接返回
                if kind == 'video':
                    cover = cover_path(blogger, full, mtime)
                    if os.path.isfile(cover):
                        item['cover'] = '/api/media?path=' + quote(
                            paths.relative(cover))
                items.append(item)
        # 仅影响预览展示：按文件名降序
        items.sort(key=lambda item: item['name'], reverse=True)
        return jsonify({'platform': platform_key, 'name': name,
                        'count': len(items), 'items': items})

    @app.post('/api/covers')
    def api_cover_save():
        """前端把取到的视频首帧回存到封面缓存目录，下次直接复用"""
        payload = request.get_json(silent=True) or {}
        blogger = store.blogger((payload.get('platform') or '').strip(),
                                (payload.get('name') or '').strip())
        target = paths.resolve(payload.get('path', ''))
        if not blogger or not target or not target.lower().endswith(VIDEO_EXTS):
            return jsonify({'error': '无效的视频路径'}), 400
        if not paths.is_inside(blogger.key, blogger.name, target):
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

        output = cover_path(blogger, target)
        make_dir(os.path.dirname(output))
        try:
            with open(output, 'wb') as handle:
                handle.write(raw)
        except OSError as error:
            return jsonify({'error': '封面写入失败：{}'.format(error)}), 500

        return jsonify({'cover': '/api/media?path=' + quote(paths.relative(output))})

    # ---------------- 媒体回放 ----------------

    @app.get('/api/media')
    def api_media():
        return media_response(request.args.get('platform', ''),
                              request.args.get('name', ''),
                              request.args.get('path', ''),
                              request.args.get('thumb'))

    return app
