# -*- coding: utf-8 -*-
"""
下载执行

分两层，都不关心存到哪、叫什么、属于哪一条：

    1. fetch_media：把一个地址拉成本地文件——失败重试，失败时清掉半成品，
       目标以 .part 结尾时表示扩展名待定，下载完成后按响应头与文件头定名；
    2. fetch_all：把这批地址并发拉下来——并发数由调用方给，可取消，
       结果按完成顺序回传，落盘后处理（如 HEIC 转 JPG）在下载线程里执行。

进度、去重、命名、ID 记录都不在这里。
"""

import os
import random
import time
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib import request

from conf import settings
from core import naming

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
}

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


def fetch_media(url, target, headers=None):
    """
    下载单个地址到 target

    target 以 .part 结尾时表示扩展名待定，下载完成后按响应头与文件头改名。

    Returns:
        tuple: (是否成功, 最终路径, 错误信息)
    """
    content_type = ''
    for attempt in range(settings.MAX_RETRY + 1):
        try:
            opener = _build_opener(headers)
            with opener.open(url, timeout=settings.DOWNLOAD_TIMEOUT) as response, \
                    open(target, 'wb') as handle:
                content_type = response.headers.get_content_type() or ''
                shutil.copyfileobj(response, handle)
            final = target
            if target.endswith('.' + PART_EXT):
                ext = (ext_from_content_type(content_type)
                       or detect_media_ext(target)
                       or naming.DEFAULT_IMAGE_EXT)
                final = target[:-len(PART_EXT) - 1] + '.' + ext
                try:
                    os.replace(target, final)
                except OSError:
                    _cleanup(target)
                    return False, target, '扩展名未知，下载后无法确定文件名'
            return True, final, ''
        except Exception as error:
            _cleanup(target)
            if attempt >= settings.MAX_RETRY:
                return False, target, '超过最大重试次数，最后异常：{}'.format(error)
            time.sleep(random.uniform(0.5, 1.5))
    return False, target, '下载失败'


# ---------------- 批量并发下载 ----------------

CANCELLED_ERROR = '已取消'


def _fetch_one(key, url, target, headers, should_stop, finish):
    """线程池里的单个任务：已取消就不再发起请求，成功后执行落盘后处理"""
    if should_stop and should_stop():
        return key, url, target, False, target, CANCELLED_ERROR

    ok, final, error = fetch_media(url, target, headers)
    if not ok:
        return key, url, target, False, final, error

    if finish:
        try:
            final = finish(final) or final
        except Exception as exc:
            # 后处理失败不算下载失败：文件已落盘，只把原因带回去提示
            return key, url, target, True, final, '落盘后处理失败：{}'.format(exc)
    return key, url, target, True, final, ''


def fetch_all(jobs, headers=None, workers=1, should_stop=None, finish=None):
    """
    并发下载一批地址，边完成边回传

    Args:
        jobs:        [(key, url, target)]，key 由调用方定义、原样回传
        headers:     请求头
        workers:     并发数；<=1 或只有一个地址时逐个下载（与旧行为一致）
        should_stop: 取消回调；命中时排队中的任务直接失败返回，不再发请求
        finish:      可选，落盘后的处理函数 finish(path) -> path，在下载线程内执行

    Yields:
        tuple: (key, url, target, 是否成功, 最终路径, 错误信息)

    调用方提前 break 时排队中的任务会被取消，正在下载的任务等其自然结束
    （网络读无法中断），因此取消最多会多等「一个文件」的时间。
    """
    jobs = list(jobs)
    if not jobs:
        return
    try:
        workers = max(1, int(workers or 1))
    except (TypeError, ValueError):
        workers = 1

    if workers == 1 or len(jobs) == 1:
        for key, url, target in jobs:
            yield _fetch_one(key, url, target, headers, should_stop, finish)
        return

    with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
        futures = {pool.submit(_fetch_one, key, url, target, headers,
                               should_stop, finish): (key, url, target)
                   for key, url, target in jobs}
        try:
            for future in as_completed(futures):
                key, url, target = futures[future]
                try:
                    yield future.result()
                except Exception as exc:
                    yield key, url, target, False, target, '下载线程异常：{}'.format(exc)
        finally:
            for future in futures:
                future.cancel()
