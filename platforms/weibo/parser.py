# -*- coding: utf-8 -*-
"""
微博解析

只做纯解析，不碰网络与浏览器：

    链接   m.weibo.cn/u/<uid>、weibo.com/u/<uid>、卡片接口地址、裸 uid
    时间   'Fri Apr 21 00:00:00 +0800 2023' 这类带时区的英文格式
    图片   mblog.pics[].large.url（兼容 list 与 {"0": {...}} 两种形态）
    视频   mblog.page_info.urls / media_info 里的取一个清晰度地址
    类型   pics 与 page_info 组合判断（微博没有抖音那种单一 aweme_type）

微博的图片地址自带时效参数，但同一张图的 URL 会变，因此去重不依赖 URL，
由下载层用「微博 ID + 序号」记录。
"""

import datetime
import re

from core.utils import safe_name

# 微博卡片接口里的容器前缀：107603 是「用户微博列表」，其余为资料类容器
CONTAINER_PREFIXES = ('107603', '100505', '230413', '101511', '110003')

MONTHS = {'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
          'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12}

# 例：Fri Apr 21 00:00:00 +0800 2023
_CREATED_RE = re.compile(
    r'([A-Za-z]{3})\s+([A-Za-z]{3})\s+(\d{1,2})\s+'
    r'(\d{2}):(\d{2}):(\d{2})\s+([+-])(\d{2})(\d{2})\s+(\d{4})')

_EXT_RE = re.compile(r'\.([A-Za-z0-9]{2,5})$')

IMAGE_EXTS = ('jpg', 'png', 'gif', 'webp', 'bmp')
VIDEO_EXTS = ('mp4', 'mov', 'm4v')

# ext_of 的识别范围：视频地址一般以 .mp4 结尾，能直接定名，不必等下载完再探测
MEDIA_EXTS = IMAGE_EXTS + VIDEO_EXTS


# ---------------- 链接 ----------------

def parse_uid(raw):
    """
    从链接或裸 uid 中取博主 uid

    兼容：m.weibo.cn/u/<uid>、/profile/<uid>、weibo.com/u/<uid>、
         weibo.com/<uid>、卡片接口地址（containerid / value 参数）、裸数字 uid

    Raises:
        ValueError: 无法识别
    """
    value = (raw or '').replace('\ufeff', '').replace('\u200b', '').strip()
    value = value.strip('"').strip("'")
    if not value:
        raise ValueError('缺少微博博主链接或 uid')

    if value.isdigit() and len(value) >= 5:
        return value

    match = re.search(r'/(?:u|profile|n)/(\d{5,})', value)
    if match:
        return match.group(1)

    # weibo.com/<uid>
    match = re.search(r'weibo\.c(?:n|om)/(\d{5,})(?:[/?#]|$)', value)
    if match:
        return match.group(1)

    # 卡片接口地址：containerid=107603<uid>
    match = re.search(r'containerid=(\d{6,})', value)
    if match:
        digits = match.group(1)
        for prefix in CONTAINER_PREFIXES:
            if digits.startswith(prefix) and len(digits) > len(prefix):
                return digits[len(prefix):]
        return digits

    # 卡片接口地址：value=<uid>
    match = re.search(r'[?&]value=(\d{5,})', value)
    if match:
        return match.group(1)

    raise ValueError('无法从内容中识别微博 uid：{}'.format(value[:80]))


def home_url(uid):
    """规范化主页地址；写进 homepage.txt，「更新」时能被再次识别"""
    return 'https://m.weibo.cn/u/{}'.format(uid)


def posts_container(uid):
    """用户微博列表的容器 ID"""
    return '107603{}'.format(uid)


# ---------------- 微博字段 ----------------

def mblog_id_of(mblog):
    """微博 ID；取不到返回空串"""
    for key in ('id', 'mid', 'bid'):
        value = (mblog or {}).get(key)
        if value:
            return str(value)
    return ''


def author_of(mblog):
    """作者昵称；取不到返回空串"""
    user = (mblog or {}).get('user') or {}
    return safe_name(user.get('screen_name') or user.get('name') or '')


def created_at(mblog):
    """
    发布时间转成秒级时间戳

    微博给的是带时区的英文格式（Fri Apr 21 00:00:00 +0800 2023），
    手工解析的原因：struct/parsedate 对「日期在月份之前、年份在末尾」的写法不稳定。
    解析失败返回 0，由下载层按「缺少发布时间」跳过。
    """
    text = ((mblog or {}).get('created_at') or '').strip()
    if not text:
        return 0

    match = _CREATED_RE.search(text)
    if match:
        _week, month, day, hour, minute, second, sign, off_h, off_m, year = match.groups()
        offset = (int(off_h) * 60 + int(off_m)) * (1 if sign == '+' else -1)
        try:
            zone = datetime.timezone(datetime.timedelta(minutes=offset))
            moment = datetime.datetime(int(year), MONTHS.get(month, 1), int(day),
                                       int(hour), int(minute), int(second), tzinfo=zone)
            return int(moment.timestamp())
        except ValueError:
            return 0

    for pattern in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return int(datetime.datetime.strptime(text, pattern).timestamp())
        except ValueError:
            continue
    return 0


# ---------------- 图片地址 ----------------

def _pic_url(pic):
    """取一张图的最优地址：large > bmiddle > thumbnail > 顶层 url"""
    for key in ('large', 'bmiddle', 'thumbnail'):
        node = pic.get(key)
        if isinstance(node, dict) and node.get('url'):
            return str(node['url']).replace('\\/', '/')
    value = pic.get('url')
    return str(value).replace('\\/', '/') if isinstance(value, str) else ''


def image_items(mblog):
    """
    取出微博里的图片

    pics 有两种形态：列表，或以 "0"/"1" 为键的字典（旧接口）。
    序号用图片在原微博里的位置（从 1 开始），与落盘命名 `- {序号}` 对齐。

    Returns:
        list[dict]: [{'index': 1, 'url': 'https://...'}]
    """
    pics = (mblog or {}).get('pics')
    images = []

    if isinstance(pics, list):
        for position, pic in enumerate(pics, 1):
            if not isinstance(pic, dict):
                continue
            url = _pic_url(pic)
            if url:
                images.append({'index': position, 'url': url})
        return images

    if isinstance(pics, dict):
        def order(key):
            return int(key) if str(key).isdigit() else 0

        for position, key in enumerate(sorted(pics, key=order), 1):
            pic = pics[key]
            if not isinstance(pic, dict):
                continue
            url = _pic_url(pic)
            if not url:
                continue
            number = int(key) + 1 if str(key).isdigit() else position
            images.append({'index': number, 'url': url})

    return images


# ---------------- 视频与类型 ----------------

# page_info.type / object_type 等于这些值就是视频微博
VIDEO_PAGE_TYPES = ('video',)

# 同一条视频接口会给多个清晰度，只能挑一个下载。
# 前四个键名来自 page_info.urls，stream_url_hd / stream_url 来自 media_info；
# 高清排前面，标清退后面，取不到的键直接跳过
VIDEO_QUALITY_ORDER = ('mp4_1080p_mp4', 'mp4_720p_mp4', 'mp4_hd_mp4',
                       'mp4_hd_url', 'stream_url_hd',
                       'mp4_sd_url', 'mp4_ld_mp4', 'mp4_ld_url', 'stream_url')

# 播放列表不是媒体文件，落盘只会得到一份文本，直接排除
PLAYLIST_SUFFIXES = ('.m3u8', '.m3u')


def _page_info(mblog):
    """page_info 节点；缺失或类型不对时返回空 dict"""
    info = (mblog or {}).get('page_info')
    return info if isinstance(info, dict) else {}


def _clean_url(value):
    """接口里的地址可能被转义，且只接受 http(s)"""
    if not isinstance(value, str):
        return ''
    value = value.replace('\\/', '/').strip()
    return value if value.startswith('http') else ''


def video_items(mblog):
    """
    取出视频微博的地址（一条视频取一个清晰度）

    地址可能出现在两处，按清晰度从高到低取第一个可用的：
        page_info.urls         多个清晰度，键名如 mp4_720p_mp4
        page_info.media_info   stream_url_hd / stream_url

    page_info.page_url 指向微博自己的播放页而不是媒体文件，不能用。

    Returns:
        list[dict]: [{'index': 1, 'url': 'https://...'}]；取不到时为空列表
    """
    info = _page_info(mblog)
    urls = info.get('urls')
    media = info.get('media_info')

    # 两处的键名不同，先合并成一张表，再按统一优先级取
    candidates = {}
    if isinstance(urls, dict):
        candidates.update(urls)
    if isinstance(media, dict):
        candidates['stream_url_hd'] = media.get('stream_url_hd')
        candidates['stream_url'] = media.get('stream_url')

    ordered = [candidates.get(key) for key in VIDEO_QUALITY_ORDER]
    # 键名变了也别漏：其余值按接口给的顺序兜底
    ordered.extend(value for key, value in candidates.items()
                   if key not in VIDEO_QUALITY_ORDER)

    for value in ordered:
        url = _clean_url(value)
        if url and not url.split('?')[0].lower().endswith(PLAYLIST_SUFFIXES):
            return [{'index': 1, 'url': url}]
    return []


def is_video(mblog):
    """
    是否视频微博

    优先看 page_info 的 type / object_type；两者都缺失时退回看有没有
    media_info：接口改字段时不至于把视频当成「无内容」静默丢掉。
    """
    info = _page_info(mblog)
    for key in ('object_type', 'type'):
        if str(info.get(key) or '').strip().lower() in VIDEO_PAGE_TYPES:
            return True
    media = info.get('media_info')
    return isinstance(media, dict) and bool(
        media.get('stream_url') or media.get('stream_url_hd'))


def classify(mblog):
    """
    判定一条微博的内容类型

    微博没有抖音那种单一 aweme_type 枚举，只能按字段组合判断：
        视频  page_info 标记为 video，或带 media_info
        图片  pics 非空
    两者都没有的（纯文字、头条文章、转发别人内容、无图）不是可下载的「篇」。

    先判视频：视频微博的 pics 通常只有一张封面图，按图片处理会下成封面。

    Returns:
        str|None: 'video' / 'image' / None
    """
    if is_video(mblog):
        return 'video'
    if image_items(mblog):
        return 'image'
    return None


def ext_of(url):
    """从地址后缀取扩展名；识别不出返回空串（下载时按响应头/文件头探测）"""
    path = (url or '').split('?')[0].split('#')[0]
    match = _EXT_RE.search(path)
    if not match:
        return ''
    ext = match.group(1).lower()
    if ext == 'jpeg':
        return 'jpg'
    return ext if ext in MEDIA_EXTS else ''


# ---------------- 说明 ----------------
# 条目挑选、取址、命名与进度由 download.py 负责，这里只保留解析用到的工具函数。
