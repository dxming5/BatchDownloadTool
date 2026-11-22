# -*- coding: utf-8 -*-
"""
下载执行

统一下载层：按篇接收地址、校验去重、命名落盘、并发调度、进度统计都在这里，
平台只负责挑条目、算发布时间、把 (篇 ID, 时间, [地址]) 交给 fetch_all。

    fetch_media：把一个地址拉成本地文件——失败重试，失败时清掉半成品，
       先落 .part，下载完成后按响应头/文件头/URL 后缀定扩展名与落盘目录；
    fetch_all：按篇校验 done_ids（整篇跳过并计入进度），展开地址并发拉取，
       结果按完成顺序回传，落盘后处理（如 HEIC 转 JPG）在下载线程里执行，
       并按篇聚合记 ID（一篇全成功才记，满一批落盘一次）。

ID 记录由这里按篇聚合后统一写入；每个地址开始下载时的日志也由这里统一输出。
"""

import os
import random
import time
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib import request
from urllib.parse import urlparse

from conf import settings
from core import naming
from core.store import PICTURE_DIR_NAME

# ---------------- 扩展名识别 ----------------

EXT_BY_TYPE = {
    'image/webp': 'webp',
    'image/jpeg': 'jpg',
    'image/jpg': 'jpg',
    'image/png': 'png',
    'image/avif': 'avif',
    'image/gif': 'gif',
    'image/heic': 'heic',
    'image/heif': 'heif',
    'video/mp4': 'mp4',
    'video/quicktime': 'mov',
    'video/x-m4v': 'm4v',
}

VIDEO_EXTS = ('mp4', 'mov', 'm4v')

PART_EXT = 'part'


def detect_media_ext(path):
    """按文件头识别真实格式；识别不出返回空串"""
    try:
        with open(path, 'rb') as handle:
            head = handle.read(12)
    except OSError:
        return ''
    if head[:3] == b'\xff\xd8\xff':
        return 'jpg'
    if head[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'webp'
    if head[:6] in (b'GIF87a', b'GIF89a'):
        return 'gif'
    if head[4:8] == b'ftyp':
        brand = head[8:12]
        if brand in (b'heic', b'heix', b'hevc', b'hevx', b'mif1', b'msf1'):
            return 'heic'
        if brand == b'avif':
            return 'avif'
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
    for ext in VIDEO_EXTS + ('jpg', 'jpeg', 'png', 'webp', 'gif', 'heic', 'heif', 'avif'):
        if path.endswith('.' + ext):
            return 'jpg' if ext == 'jpeg' else ext
    return ''


# ---------------- 单文件下载 ----------------

def _build_opener(headers):
    opener = request.build_opener(request.HTTPSHandler(context=settings.SSL_CONTEXT))
    if headers:
        opener.addheaders = list(headers.items())
    return opener


def _cleanup(path):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def final_target(blogger_dir, create_time, index, ext):
    """按扩展名决定最终落盘路径：视频在根目录，图片在 picture/"""
    if ext in VIDEO_EXTS:
        return os.path.join(blogger_dir, '{}.{}'.format(create_time, ext))
    suffix = '{} - {}.{}'.format(create_time, index + 1, ext)
    return os.path.join(blogger_dir, PICTURE_DIR_NAME, suffix)


def fetch_media(url, target, headers=None, blogger_dir=None, create_time=None,
                index=0, log=None):
    """
    下载单个地址：先落 .part，完成后按响应头/文件头/URL 后缀定扩展名，
    再按扩展名决定落盘目录（视频在根目录、图片在 picture/）。

    target 是 .part 临时路径（放根目录）；blogger_dir/create_time/index 用于算最终路径。
    log 可选，拿到响应头后打印「正在下载」日志，尽量显示最终文件名。

    Returns:
        tuple: (是否成功, 最终路径, 错误信息)
    """
    content_type = ''
    logged = False
    for attempt in range(settings.MAX_RETRY + 1):
        try:
            opener = _build_opener(headers)
            with opener.open(url, timeout=settings.DOWNLOAD_TIMEOUT) as response, \
                    open(target, 'wb') as handle:
                content_type = response.headers.get_content_type() or ''
                if log and not logged:
                    logged = True
                    ext = ext_from_content_type(content_type) or ext_from_url(url)
                    if ext and blogger_dir is not None and create_time is not None:
                        display = final_target(blogger_dir, create_time, index, ext)
                    else:
                        display = target
                    try:
                        log('正在下载 {}'.format(os.path.basename(display)))
                    except Exception:
                        pass
                shutil.copyfileobj(response, handle)
            ext = (ext_from_content_type(content_type)
                   or detect_media_ext(target)
                   or ext_from_url(url)
                   or naming.DEFAULT_IMAGE_EXT)
            if blogger_dir is not None and create_time is not None:
                final = final_target(blogger_dir, create_time, index, ext)
            else:
                final = target[:-len(PART_EXT) - 1] + '.' + ext
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
            if attempt >= settings.MAX_RETRY:
                return False, target, '超过最大重试次数，最后异常：{}'.format(error)
            time.sleep(random.uniform(0.5, 1.5))
    return False, target, '下载失败'


# ---------------- 批量并发下载 ----------------

CANCELLED_ERROR = '已取消'


def _fetch_one(key, url, target, headers, should_stop, finish, log=None,
               blogger_dir=None, create_time=None, index=0):
    """线程池里的单个任务：已取消就不再发起请求，成功后执行落盘后处理"""
    if should_stop and should_stop():
        return key, url, target, False, target, CANCELLED_ERROR

    ok, final, error = fetch_media(url, target, headers, blogger_dir,
                                   create_time, index, log)
    if not ok:
        return key, url, target, False, final, error

    if finish:
        try:
            final = finish(final) or final
        except Exception as exc:
            # 后处理失败不算下载失败：文件已落盘，只把原因带回去提示
            return key, url, target, True, final, '落盘后处理失败：{}'.format(exc)
    return key, url, target, True, final, ''


def _temp_target(blogger_dir, create_time, index):
    """下载中的临时路径：统一放博主根目录，扩展名待定"""
    return os.path.join(blogger_dir,
                        '{} - {}.{}'.format(create_time, index + 1, PART_EXT))


def fetch_all(jobs, blogger_dir, headers=None, workers=1, should_stop=None,
              finish=None, log=None, done_ids=None, on_done=None,
              record_ids=None, on_article_done=None, flush_batch=20):
    """
    按篇接收地址，统一校验去重、命名落盘、并发下载，边完成边回传

    Args:
        jobs:        [(id, time, [url, ...])]，一篇一项；id 为篇 ID，time 为
                     已格式化的发布时间（命名前缀），[url] 为该篇全部媒体地址
        blogger_dir: 博主目录（最终落盘的根）
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
    tasks = []          # (id, url, temp_target, index, create_time)
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
            tasks.append((item_id, url, _temp_target(blogger_dir, create_time, index),
                          index, create_time))

    total = len(tasks)
    done = 0

    # 按篇聚合：一篇的全部地址都成功才记 ID，满 flush_batch 条落盘一次
    remaining = {}       # 篇 ID -> 剩余地址数
    article_ok = {}      # 篇 ID -> 是否仍全部成功
    last_file = {}       # 篇 ID -> 最后一个成功落盘的文件名
    create_time_of = {}  # 篇 ID -> 发布时间（命名前缀）
    new_ids = []         # 待落盘的 ID 缓冲

    for item_id, _url, _target, _index, create_time in tasks:
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
        item_id, url, target, index, create_time = task
        return _fetch_one(item_id, url, target, headers, should_stop, finish,
                          log, blogger_dir, create_time, index)

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
                    item_id, url, target, index, create_time = task
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = item_id, url, target, False, target, '下载线程异常：{}'.format(exc)
                    settle()
                    aggregate(result)
                    yield result
            finally:
                for future in futures:
                    future.cancel()
        finally:
            flush_ids()
