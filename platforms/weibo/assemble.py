# -*- coding: utf-8 -*-
"""
微博下载组装（流式归一化）

只负责把一条原始微博归一化成统一下载条目，作者过滤用打开网页时冻结的
博主名完成；取址、命名、去重、落盘与 ID 记录交给 core.session / downloader。

每篇（一条微博）按内容类型走两条链路，取址各不相同：
    图片  mblog.pics[].large.url（缺失时回退 bmiddle / thumbnail / url）
          微博图床的无水印版恒为最长边 690，这里固定取原图以保住最高画质
    视频  page_info 里的一个清晰度地址，落成 {发布时间}.mp4
          视频角标是转码时烧录的，所有直链都带，无法去水印
类型由 classify 判定：先看视频（视频微博的 pics 只是一张封面）。
两类都没有的篇（纯文字、头条文章、转发别人内容）返回 None 跳过。

create_time 保留秒级原始时间戳，由下载层转成文件名前缀；无时间、无地址、
作者不一致的条目也交给会话按现有语义处理，本模块只决定「这篇要不要下」。
"""

from conf import settings
from core.utils import safe_name
from platforms.weibo import scraper

KEY = 'weibo'


def headers():
    """下载请求头：公共头 + 平台段里的 referer / accept"""
    values = dict(settings.HEADER)
    options = settings.platform_settings(KEY)
    if options.get('referer'):
        values['Referer'] = options['referer']
    if options.get('accept'):
        values['Accept'] = options['accept']
    return values


# ---------------- 类型判定与取址 ----------------

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

    地址统一取接口给的原图（large，最长边可达 4000+）。
    微博图床的无水印版恒为最长边 690，与最高画质不可兼得，
    因此这里不换前缀，固定保留最高画质。

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


def _playback_urls(info):
    """
    新版接口的清晰度列表（playback_list），按 quality_level 从高到低取地址

    条目形如 {'play_info': {'label': 'mp4_720p', 'quality_level': 2, 'url': '...'}}，
    label 与 urls 的键名不同，因此单独收集后追加在 urls 之后。
    """
    items = []
    for holder in (info, info.get('media_info')):
        if not isinstance(holder, dict):
            continue
        for entry in holder.get('playback_list') or []:
            detail = entry.get('play_info') if isinstance(entry, dict) else None
            if not isinstance(detail, dict) or not detail.get('url'):
                continue
            try:
                level = int(detail.get('quality_level') or 0)
            except (TypeError, ValueError):
                level = 0
            items.append((level, detail.get('url')))
    return [url for _level, url in sorted(items, key=lambda item: item[0],
                                          reverse=True)]


def video_items(mblog):
    """
    取出视频微博的地址（一条视频取一个清晰度）

    地址可能出现在三处，按清晰度从高到低取第一个可用的：
        page_info.urls           多个清晰度，键名如 mp4_720p_mp4
        media_info.playback_list 新版清晰度列表，带 quality_level
        page_info.media_info     stream_url_hd / stream_url

    page_info.page_url 指向微博自己的播放页而不是媒体文件，不能用。

    注意：微博视频的角标（@昵称 + 微博视频）是转码时烧录进画面的，
    所有对外直链都是这份带水印文件的不同清晰度，改地址去不掉；
    这里只挑最高可用清晰度，不做去水印改写。

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
    # playback_list 的 label 不在这张表里，单独按 quality_level 追加
    ordered.extend(_playback_urls(info))
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


def normalize(mblog, nickname, stats=None):
    """
    把一条原始微博归一化成统一下载条目（流式逐条调用）

    Args:
        mblog:     scraper 派发的原始微博 dict
        nickname:  打开网页时冻结的博主名（core.session 已规范化）
        stats:     可选计数器 {原因: 条数}，只用于平台侧跳过汇总日志

    Returns:
        dict|None: {'id', 'create_time', 'urls', 'kind'}；作者不一致、纯文字、
                   无地址或无 ID 时返回 None（不下载、不记 ID）

    作者过滤只用冻结的博主名，不从条目里另取作者当身份；create_time 保留
    秒级原始时间戳，无时间仍交给会话按现有语义跳过并提示。
    """
    if not isinstance(mblog, dict):
        return None

    def skip(reason):
        if isinstance(stats, dict):
            stats[reason] = stats.get(reason, 0) + 1
        return None

    expected = safe_name(nickname or '')
    if expected and scraper.author_of(mblog) != expected:
        return skip('author_mismatch')

    kind = classify(mblog)
    # 纯文字、文章、转发等没有可下载的媒体，不参与下载
    if kind is None:
        return skip('no_media')

    media = video_items(mblog) if kind == 'video' else image_items(mblog)
    urls = [item['url'] for item in media if item.get('url')]
    if not urls:
        return skip('no_url')

    article_id = scraper.mblog_id_of(mblog)
    if not article_id:
        return skip('no_id')

    return {
        'id': article_id,
        'create_time': scraper.created_at(mblog),
        'urls': urls,
        'kind': kind,
    }
