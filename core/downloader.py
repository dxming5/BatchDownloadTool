# -*- coding: utf-8 -*-
"""
下载执行

统一下载层：按篇接收地址、校验去重、拼文件名落盘、并发调度、进度统计都在这里，
平台只负责挑条目、算发布时间、把 (篇 ID, 时间, [地址]) 交给 fetch_all。

    fetch_media：把一个地址拉成本地文件——失败重试，失败时清掉半成品，
       先落临时文件，完成后按响应头/文件头/URL 后缀定扩展名，落盘路径向
       调用方给的路径来源要（本层不拼任何目录）；
    fetch_all：按篇校验 done_ids（整篇跳过并计入进度），展开地址并发拉取，
       结果按完成顺序回传，落盘后处理（如 HEIC 转 JPG）在下载线程里执行，
       并按篇聚合记 ID（一篇全成功才记，满一批落盘一次）；
    fetch_article：单篇入口，把统一 article 映射成一个 job 复用 fetch_all，
       一次回传该篇的成败、文件数、失败清单与提示，供流式会话逐篇调用。

ID 记录由这里按篇聚合后统一写入；每个地址开始下载时的日志也由这里统一输出。
"""

import os
import random
import time
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib import request
from urllib.parse import urlparse

from core import config
from core import store

# ---------------- 扩展名 ----------------

# 图片后缀：落盘进 store 的图片目录，同时作为媒体列表的白名单来源
IMAGE_EXTS = ('jpg', 'png', 'webp', 'gif', 'avif', 'heic', 'heif',
              'bmp', 'tif', 'ico')
# 视频后缀：落盘在路径来源给出的根目录
VIDEO_EXTS = ('mp4', 'mov', 'm4v', 'webm', 'mkv', 'avi', 'flv', 'wmv',
              'ts', '3gp', 'mpg')

# 同一格式的别名：URL 后缀识别时统一成标准后缀
IMAGE_ALIASES = {'jpeg': 'jpg', 'tiff': 'tif'}
VIDEO_ALIASES = {'mpeg': 'mpg', 'mpe': 'mpg'}

# 响应头、文件头、URL 后缀都识别不出时的兜底
FALLBACK_EXT = 'webp'

# 响应头 Content-Type → 扩展名
EXT_BY_TYPE = {
    'image/jpeg': 'jpg',
    'image/jpg': 'jpg',
    'image/png': 'png',
    'image/webp': 'webp',
    'image/gif': 'gif',
    'image/avif': 'avif',
    'image/heic': 'heic',
    'image/heif': 'heif',
    'image/bmp': 'bmp',
    'image/x-ms-bmp': 'bmp',
    'image/tiff': 'tif',
    'image/x-icon': 'ico',
    'image/vnd.microsoft.icon': 'ico',
    'video/mp4': 'mp4',
    'video/quicktime': 'mov',
    'video/x-m4v': 'm4v',
    'video/webm': 'webm',
    'video/x-matroska': 'mkv',
    'video/x-msvideo': 'avi',
    'video/x-flv': 'flv',
    'video/x-ms-wmv': 'wmv',
    'video/mp2t': 'ts',
    'video/3gpp': '3gp',
    'video/mpeg': 'mpg',
}

def format_create_time(timestamp):
    """
    把秒级时间戳转成文件名前缀（图片 / 视频文件名的一部分）

    Returns:
        str|None: 形如 2026-01-01 10.00.00；缺失或非法时返回 None
    """
    try:
        return datetime.fromtimestamp(int(timestamp)).strftime('%Y-%m-%d %H.%M.%S')
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def detect_media_ext(path):
    """按文件头识别真实格式；识别不出返回空串"""
    try:
        with open(path, 'rb') as handle:
            head = handle.read(256)
    except OSError:
        return ''
    if head[:3] == b'\xff\xd8\xff':
        return 'jpg'
    if head[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'webp'
    if head[:4] == b'RIFF' and head[8:12] == b'AVI ':
        return 'avi'
    if head[:6] in (b'GIF87a', b'GIF89a'):
        return 'gif'
    if head[:2] == b'BM':
        return 'bmp'
    if head[:4] in (b'II*\x00', b'MM\x00*'):
        return 'tif'
    if head[:4] == b'\x00\x00\x01\x00':
        return 'ico'
    if head[:3] == b'FLV':
        return 'flv'
    if head[:4] == b'\x1a\x45\xdf\xa3':
        return 'mkv'        # EBML 容器，webm 与 mkv 同族
    if head[:1] == b'\x47' and len(head) > 188 and head[188:189] == b'\x47':
        return 'ts'         # MPEG-TS：同步字节 0x47，每 188 字节一个
    if head[4:8] == b'ftyp':
        brand = head[8:12]
        if brand in (b'heic', b'heix', b'hevc', b'hevx', b'mif1', b'msf1'):
            return 'heic'
        if brand == b'avif':
            return 'avif'
        if brand == b'qt  ':
            return 'mov'
        if brand in (b'M4V ', b'M4VH', b'M4VP'):
            return 'm4v'
        if brand[:3] == b'3gp':
            return '3gp'
        return 'mp4'
    return ''


def ext_from_content_type(content_type):
    """按响应头推断扩展名"""
    value = (content_type or '').split(';')[0].strip().lower()
    return EXT_BY_TYPE.get(value, '')


def ext_from_url(url):
    """按 URL 路径后缀兜底推断扩展名（响应头/文件头都没识别出来时用）"""
    try:
        path = urlparse(url).path.lower()
    except Exception:
        return ''
    for ext in (VIDEO_EXTS + tuple(VIDEO_ALIASES)
                + IMAGE_EXTS + tuple(IMAGE_ALIASES)):
        if path.endswith('.' + ext):
            return VIDEO_ALIASES.get(ext) or IMAGE_ALIASES.get(ext) or ext
    return ''


# ---------------- 单文件下载 ----------------

def _build_opener(headers):
    opener = request.build_opener(request.HTTPSHandler(context=config.SSL_CONTEXT))
    if headers:
        opener.addheaders = list(headers.items())
    return opener


def _cleanup(path):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def fetch_media(url, create_time, index, headers, log=None):
    """
    下载单个地址：先落 .part，完成后按响应头/文件头/URL 后缀定扩展名。

    放哪由 store 决定（当前任务目标现算路径，含视频/图片分目录）；本层只判断
    「这是视频还是图片」，不拼任何目录，也不接收任何路径参数。
    log 可选，拿到响应头后打印「正在下载」日志，尽量显示最终文件名。

    Returns:
        tuple: (是否成功, 最终路径, 错误信息)
    """
    target = store.part_path(create_time, index)
    content_type = ''
    logged = False
    for attempt in range(config.MAX_RETRY + 1):
        try:
            opener = _build_opener(headers)
            with opener.open(url, timeout=config.DOWNLOAD_TIMEOUT) as response, \
                    open(target, 'wb') as handle:
                content_type = response.headers.get_content_type() or ''
                if log and not logged:
                    logged = True
                    ext = ext_from_content_type(content_type) or ext_from_url(url)
                    display = (store.media_path(create_time, index, ext,
                                                  ext in VIDEO_EXTS)
                               if ext else target)
                    try:
                        log('正在下载 {}'.format(os.path.basename(display)))
                    except Exception:
                        pass
                shutil.copyfileobj(response, handle)
            ext = (ext_from_content_type(content_type)
                   or detect_media_ext(target)
                   or ext_from_url(url)
                   or FALLBACK_EXT)
            final = store.media_path(create_time, index, ext, ext in VIDEO_EXTS)
            try:
                parent = os.path.dirname(final)
                if parent and not os.path.isdir(parent):
                    os.makedirs(parent)
                os.replace(target, final)
            except OSError:
                _cleanup(target)
                return False, target, '落盘失败：{}'.format(final)
            return True, final, ''
        except Exception as error:
            _cleanup(target)
            if attempt >= config.MAX_RETRY:
                return False, target, '超过最大重试次数，最后异常：{}'.format(error)
            time.sleep(random.uniform(0.5, 1.5))
    return False, target, '下载失败'


# ---------------- 批量并发下载 ----------------

CANCELLED_ERROR = '已取消'


def _fetch_one(item_id, url, create_time, index, headers,
               should_stop, finish, log=None):
    """线程池里的单个任务：已取消就不再发起请求，成功后执行落盘后处理"""
    target = store.part_path(create_time, index)
    if should_stop and should_stop():
        return item_id, url, target, False, target, CANCELLED_ERROR

    ok, final, error = fetch_media(url, create_time, index, headers, log)
    if not ok:
        return item_id, url, target, False, final, error

    if finish:
        try:
            final = finish(final) or final
        except Exception as exc:
            # 后处理失败不算下载失败：文件已落盘，只把原因带回去提示
            return item_id, url, target, True, final, '落盘后处理失败：{}'.format(exc)
    return item_id, url, target, True, final, ''


def fetch_all(jobs, headers=None, workers=1, should_stop=None,
              finish=None, log=None, done_ids=None, on_done=None,
              record_ids=None, on_article_done=None, flush_batch=20):
    """
    按篇接收地址，统一校验去重、命名落盘、并发下载，边完成边回传

    落盘路径由 store 的当前任务目标现算（编排层已 activate），本函数不接收
    博主对象，也不接收任何目录或路径参数。

    Args:
        jobs:        [(id, time, [url, ...])]，一篇一项；id 为篇 ID，time 为
                     已格式化的发布时间（命名前缀），[url] 为该篇全部媒体地址
        headers:     请求头
        workers:     并发数；<=1 或只有一个地址时逐个下载
        should_stop: 取消回调；命中时排队中的任务直接失败返回，不再发请求
        finish:      可选，落盘后的处理函数 finish(path) -> path，在下载线程内执行
        log:         可选，每个地址开始下载前的日志回调 log(message)
        done_ids:    可选，已下载的篇 ID 集合；命中的篇直接排除，不计入进度
        on_done:     可选，每个地址完成后的进度回调 on_done(done, total)
        record_ids:  可选，记 ID 回调 record_ids(ids)；一篇全部地址都成功后把
                     该篇 ID 攒进缓冲，满 flush_batch 条或结束时批量调用一次
        on_article_done: 可选，一篇聚合完成后的回调
                     on_article_done(item_id, ok, create_time, file_name)
        flush_batch: 记 ID 缓冲每满多少条落盘一次，默认 20

    Yields:
        tuple: (id, url, target, 是否成功, 最终路径, 错误信息)，按地址回传

    进度按本批实际要下载的地址数统计：已下载的篇由 done_ids 排除、不计入
    total，进度从 0 开始；每个地址完成即回调一次 on_done，无日志内容，只
    驱动界面进度。ID 记录由本层按篇聚合：一篇的全部地址都成功才记 ID，由
    record_ids 批量落盘；调用方不再自己维护聚合与落盘。调用方提前 break 时
    排队中的任务会被取消，正在下载的任务等其自然结束，已成功篇的 ID 由
    finally 兜底写出。
    """
    jobs = list(jobs)
    done_ids = done_ids or set()

    # 展开：(id, time, [url]) → 每个地址一个任务；已下载的篇直接排除，不计入进度
    # 同时按发布时间做撞名提示：同一时间前缀被不同篇占用时会被覆盖
    tasks = []          # (id, url, create_time, index)
    written = {}        # 时间前缀 -> 篇 ID
    for item_id, create_time, urls in jobs:
        if item_id in done_ids:
            continue
        owner = written.get(create_time)
        if owner and owner != item_id and log:
            try:
                log('文件名冲突：{} 已属于 {}，本次将被 {} 覆盖'.format(
                    create_time, owner, item_id))
            except Exception:
                pass
        written[create_time] = item_id
        for index, url in enumerate(urls):
            tasks.append((item_id, url, create_time, index))

    total = len(tasks)
    done = 0

    # 按篇聚合：一篇的全部地址都成功才记 ID，满 flush_batch 条落盘一次
    remaining = {}       # 篇 ID -> 剩余地址数
    article_ok = {}      # 篇 ID -> 是否仍全部成功
    last_file = {}       # 篇 ID -> 最后一个成功落盘的文件名
    create_time_of = {}  # 篇 ID -> 发布时间（命名前缀）
    new_ids = []         # 待落盘的 ID 缓冲

    for item_id, _url, create_time, _index in tasks:
        remaining[item_id] = remaining.get(item_id, 0) + 1
        article_ok.setdefault(item_id, True)
        create_time_of.setdefault(item_id, create_time)

    def flush_ids():
        if not new_ids:
            return
        if record_ids:
            record_ids(list(new_ids))
        del new_ids[:]

    def settle():
        nonlocal done
        done += 1
        if on_done:
            try:
                on_done(done, total)
            except Exception:
                pass

    def aggregate(result):
        """单个地址完成后更新聚合；该篇全部完成时记 ID 并回调"""
        item_id, _url, _target, ok, final_path, _error = result
        remaining[item_id] -= 1
        if not ok:
            article_ok[item_id] = False
        elif final_path:
            last_file[item_id] = os.path.basename(final_path)
        if remaining[item_id] <= 0:
            success = article_ok[item_id]
            if success:
                new_ids.append(item_id)
                done_ids.add(item_id)
                if len(new_ids) >= flush_batch:
                    flush_ids()
            if on_article_done:
                try:
                    on_article_done(item_id, success, create_time_of[item_id],
                                    last_file.get(item_id, ''))
                except Exception:
                    pass
        return result

    if on_done:
        try:
            on_done(done, total)
        except Exception:
            pass

    if not tasks:
        return
    try:
        workers = max(1, int(workers or 1))
    except (TypeError, ValueError):
        workers = 1

    def run(task):
        item_id, url, create_time, index = task
        return _fetch_one(item_id, url, create_time, index, headers,
                          should_stop, finish, log)

    if workers == 1 or len(tasks) == 1:
        try:
            for task in tasks:
                result = run(task)
                settle()
                aggregate(result)
                yield result
        finally:
            flush_ids()
        return

    with ThreadPoolExecutor(max_workers=min(workers, len(tasks))) as pool:
        futures = {pool.submit(run, task): task for task in tasks}
        try:
            try:
                for future in as_completed(futures):
                    task = futures[future]
                    item_id, url, create_time, index = task
                    try:
                        result = future.result()
                    except Exception as exc:
                        try:
                            target = store.part_path(create_time, index)
                        except Exception:
                            target = ''
                        result = (item_id, url, target, False, target,
                                  '下载线程异常：{}'.format(exc))
                    settle()
                    aggregate(result)
                    yield result
            finally:
                for future in futures:
                    future.cancel()
        finally:
            flush_ids()


def fetch_article(article, headers=None, workers=1, should_stop=None,
                  finish=None, log=None, record_ids=None):
    """
    下载单篇：把统一 article 映射成一个 job 复用 fetch_all

    article 约定为 {'id', 'create_time', 'urls', 'kind'}，create_time 是已
    格式化的命名前缀（由会话用 format_create_time 生成）。不接收进度回调，
    结果一次性回传，供流式会话逐篇记账。

    Returns:
        dict: {'ok', 'cancelled', 'files', 'failures', 'notices', 'file'}
            ok        该篇全部媒体成功
            cancelled 排队任务是否命中取消
            files     成功落盘的文件数
            failures  [{'url', 'file', 'error'}]，失败的地址
            notices   下载与后处理提示，调用方可原样上报
            file      最后成功落盘的文件名
    """
    article = article or {}
    article_id = article.get('id') or ''
    create_time = article.get('create_time') or ''
    urls = [url for url in (article.get('urls') or []) if url]

    files = 0
    cancelled = False
    last_file = ''
    notices = []
    failures = []

    def say(message):
        if not message:
            return
        notices.append(message)
        if log:
            try:
                log(message)
            except Exception:
                pass

    for _item_id, url, target, ok, final_path, error in fetch_all(
            [(article_id, create_time, urls)],
            headers, workers, should_stop,
            finish=finish, log=say, record_ids=record_ids):
        if error == CANCELLED_ERROR:
            cancelled = True
            break
        if ok:
            files += 1
            if final_path:
                last_file = os.path.basename(final_path)
            if error:
                # 落盘后处理失败（如 HEIC 转 JPG）只提示，不算下载失败
                say(error)
        else:
            failures.append({'url': url, 'file': os.path.basename(target or ''),
                             'error': error})

    return {
        'ok': bool(urls) and not failures and not cancelled,
        'cancelled': cancelled,
        'files': files,
        'failures': failures,
        'notices': notices,
        'file': last_file,
    }
