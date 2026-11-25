# -*- coding: utf-8 -*-
"""
小红书笔记归一化

把抓取到的原始详情转成统一下载条目，交给 core.pipeline 逐篇下载。

与另外两个平台不同的地方有三处：
1. 请求头要带小红书自己的 Referer，否则 CDN 可能拒
2. 媒体地址不带后缀，扩展名由下载机制按响应头 + 文件头探测
3. HEIC 图片浏览器与 Pillow 都读不了，落盘后转成 JPG 并删原件

平台侧只做归一化与取址：作者过滤用会话冻结的昵称完成，发布时间保留原始
时间戳（秒）交给会话层格式化，下载与记 ID 全由 core 承担。
"""

import os
import re

from core import config
from core.utils import safe_name
from platforms.xiaohongshu import scraper

KEY = 'xiaohongshu'

ID_RECORD_LABEL = '小红书笔记'

# 小红书原图的 CDN 主机；展示链路下发的是带平台水印的压缩档
ORIGIN_HOST = 'https://sns-img-qc.xhscdn.com/'

# HEIC 解码依赖：未安装 pillow-heif 时跳过转码，保留原文件
try:
    from PIL import Image
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIC_SUPPORT = True
except Exception:      # 依赖缺失或平台不支持
    HEIC_SUPPORT = False


def headers():
    """下载请求头：公共头 + 平台段里的 referer / accept"""
    values = dict(config.HEADER)
    options = config.platform_settings(KEY)
    if options.get('referer'):
        values['Referer'] = options['referer']
    if options.get('accept'):
        values['Accept'] = options['accept']
    return values


def heic_to_jpeg(path, quality=98):
    """
    把 HEIC 转成 JPG，返回 JPG 路径（失败时返回原路径）

    HEVC → JPEG 属于换编码，用 quality=98 + 无色度抽样做到视觉无损；原 HEIC 不保留。
    """
    if not HEIC_SUPPORT:
        print('    （未安装 pillow-heif，跳过 HEIC 转码）')
        return path

    target = os.path.splitext(path)[0] + '.jpg'
    if os.path.exists(target):
        # 已有转码结果，清理残留的 HEIC
        try:
            os.remove(path)
        except OSError:
            pass
        return target

    try:
        with Image.open(path) as image:
            image.convert('RGB').save(target, 'JPEG', quality=quality,
                                      subsampling=0, optimize=True)
    except Exception as error:
        # 转码失败时保留原 HEIC，绝不删
        print('    HEIC 转码失败：{}'.format(error))
        return path

    try:
        os.remove(path)
    except OSError:
        pass
    return target


def post_process(path):
    """落盘后处理：HEIC/HEIF 转 JPG"""
    value = (path or '').lower()
    if not value.endswith(('.heic', '.heif')):
        return path
    return heic_to_jpeg(path)


# ---------------- 类型判定与取址 ----------------

def is_live_album(note):
    """实况照片合集：type 是 video，但每张图都带 livePhoto 标记"""
    images = scraper.image_list_of(note)
    return bool(images) and all(image.get('livePhoto') for image in images)


def extract_video(note):
    """
    取视频播放地址

    不同版本的流字段名不一致（h264/h265 或 EF4~EF7 等编码档位），
    因此不认具体键名，遍历所有档位按码率（取不到按高度）挑最优一路。
    """
    video = note.get('video') or {}
    streams = (video.get('media') or {}).get('stream') or {}

    best_url = ''
    best_score = -1
    for items in streams.values():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            url = item.get('masterUrl') or item.get('master_url') or ''
            if not url:
                continue
            score = item.get('videoBitrate') or item.get('avgBitrate') or 0
            if not score:
                score = (item.get('height') or 0) * 1000
            if score > best_score:
                best_url, best_score = url, score
    return best_url


def classify(note):
    """
    判定按图片还是视频处理

    图片全部带实况标记         → 图片（实况合集）
    有视频流且图片至多 1 张     → 视频（那 1 张只是封面）
    其余（多图 + 有视频流）     → 图片（平台把多图合成了轮播视频，不能当视频下）
    """
    images = scraper.image_list_of(note)
    if is_live_album(note):
        return 'image'
    if extract_video(note) and len(images) <= 1:
        return 'video'
    return 'image'


def no_watermark_url(url):
    """
    把带水印的展示图地址还原为无水印原图

    展示链路下发的是压缩档（如 !nd_dft），平台水印合成在这份上；
    原图在 sns-img-qc 上直接用「目录/图片ID」寻址，分辨率与体积都最大且无水印。
    转换规则不成立时原样返回，避免把可用地址改坏。
    """
    if not url:
        return url
    clean = url.split('!')[0].rstrip('/')
    parts = clean.rsplit('/', 2)
    if len(parts) < 2:
        return url

    last = parts[-1]
    prev = parts[-2]
    # 上一级可能是哈希目录（32 位 hex）或时间戳目录（纯数字），
    # 这两类只是 CDN 分片前缀，不属于图片路径
    if re.match(r'^([0-9a-f]{32}|\d{8,14})$', prev):
        path = last
    else:
        path = prev + '/' + last

    if not re.match(r'^([0-9a-zA-Z_]+/)?[0-9a-zA-Z]+$', path):
        return url
    return ORIGIN_HOST + path


def extract_images(note, no_watermark=True):
    """
    取出全部图片地址（默认转成无水印原图）

    SSR 的 imageList 每项可能有 urlDefault，也可能只给 infoList：
    WB_DFT 是原图，WB_PRV 是预览图。
    """
    urls = []
    for image in scraper.image_list_of(note):
        url = image.get('urlDefault') or image.get('url_default') or ''
        if not url:
            infos = image.get('infoList') or image.get('info_list') or []
            by_scene = {info.get('imageScene'): info.get('url')
                        for info in infos if info.get('url')}
            url = by_scene.get('WB_DFT') or by_scene.get('WB_PRV') or ''
        if url:
            urls.append(no_watermark_url(url) if no_watermark else url)
    return urls


def normalize(note, nickname):
    """
    把一条笔记详情归一化成统一下载条目

    作者与目标博主不一致的笔记（合作、改名、接口异常）直接丢弃：一次任务只
    对应一个博主目录。作者过滤用会话冻结的昵称完成，不从笔记里取身份。
    发布时间保留原始秒级时间戳，由会话层格式化；没有地址也照常返回，由
    会话层按「无媒体失败」处理。

    Args:
        note:      一条笔记详情（字段大小写由 scraper 兼容）
        nickname:  会话冻结的博主昵称（可能已被 safe_name 规范化）

    Returns:
        dict|None: {'id','create_time','urls','kind'}；不属于目标博主或
                   无法识别的笔记返回 None（丢弃，不计数）
    """
    if not isinstance(note, dict):
        return None

    note_id = scraper.note_id_of(note)
    if not note_id:
        return None
    note_id = str(note_id)

    # 作者过滤：昵称取不到时放弃过滤，避免误杀；作者取不到则丢弃
    expected = safe_name(nickname or '')
    author = scraper.author_of(note)
    if not author:
        return None
    if expected and author != expected:
        return None

    kind = classify(note)
    if kind == 'video':
        urls = [extract_video(note)]
    else:
        urls = extract_images(note)
    urls = [url for url in urls if url]

    return {'id': note_id, 'create_time': scraper.created_at(note),
            'urls': urls, 'kind': kind}
