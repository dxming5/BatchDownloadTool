# -*- coding: utf-8 -*-
"""
单文件下载

只负责把一个地址拉成本地文件，不关心存到哪、叫什么、属于哪一条：
    1. 失败重试，失败时清掉半成品，避免留下损坏文件；
    2. 目标以 .part 结尾时表示扩展名待定，下载完成后按响应头与文件头定名。

进度、去重、命名、落盘后处理都不在这里。
"""

import os
import random
import time
import shutil
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
