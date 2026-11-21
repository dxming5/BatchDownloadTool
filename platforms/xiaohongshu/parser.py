# -*- coding: utf-8 -*-
"""
小红书笔记解析

判定规则（实测结论）：
    图片全部带实况标记         → 图片（实况合集）
    有视频流且图片至多 1 张     → 视频（那 1 张只是封面）
    其余（多图 + 有视频流）     → 图片（平台把多图合成了轮播视频，不能当视频下）

图片地址还原成无水印原图；视频有多路码率档位，按码率（取不到按高度）挑最优。
城市字段在列表与详情里大小写不一致，统一两种写法都兼容。
"""

import re

from core.utils import safe_name

# 小红书原图的 CDN 主机；展示链路下发的是带平台水印的压缩档
ORIGIN_HOST = 'https://sns-img-qc.xhscdn.com/'


# ---------------- 字段兼容 ----------------

def note_card_of(note):
    """列表项里的 note_card（不同版本字段名大小写不一）"""
    return note.get('note_card') or note.get('noteCard') or {}


def image_list_of(note):
    """图片列表（兼容 imageList / image_list）"""
    return note.get('imageList') or note.get('image_list') or []


def note_id_of(note):
    return note.get('note_id') or note.get('noteId') or note.get('id')


def author_of(note):
    """作者昵称：列表项可能在顶层，也可能在 note_card 里"""
    for source in (note, note_card_of(note)):
        user = source.get('user') or {}
        nickname = user.get('nickname') or user.get('nickName') or ''
        if nickname:
            return safe_name(nickname)
    return ''


def created_at(note):
    """发布时间统一为秒（小红书给的是毫秒）"""
    for source in (note, note_card_of(note)):
        value = source.get('time')
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        if value > 10 ** 11:      # 毫秒
            value //= 1000
        return value
    return 0


# ---------------- 判定 ----------------

def is_live_album(note):
    """实况照片合集：type 是 video，但每张图都带 livePhoto 标记"""
    images = image_list_of(note)
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
    """判定按图片还是视频处理"""
    images = image_list_of(note)
    if is_live_album(note):
        return 'image'
    if extract_video(note) and len(images) <= 1:
        return 'video'
    return 'image'


# ---------------- 地址 ----------------

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
    for image in image_list_of(note):
        url = image.get('urlDefault') or image.get('url_default') or ''
        if not url:
            infos = image.get('infoList') or image.get('info_list') or []
            by_scene = {info.get('imageScene'): info.get('url')
                        for info in infos if info.get('url')}
            url = by_scene.get('WB_DFT') or by_scene.get('WB_PRV') or ''
        if url:
            urls.append(no_watermark_url(url) if no_watermark else url)
    return urls


def has_token(note):
    """是否带 xsec_token（没有它打不开详情）"""
    for source in (note, note_card_of(note)):
        if source.get('xsec_token') or source.get('xsecToken'):
            return True
    return False


# ---------------- 说明 ----------------
# 条目挑选（ID、作者、时间、类型）由 download.py 直接从原始详情里取，
# 这里只保留取址与判定用到的工具函数。
