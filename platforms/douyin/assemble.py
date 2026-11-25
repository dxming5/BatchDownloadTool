# -*- coding: utf-8 -*-
"""
抖音作品归一化

把抓取到的单条原始作品转成统一 article，交给 core.pipeline 逐篇下载。
作者过滤在此用冻结后的博主名完成：一次任务只对应一个博主目录。
抖音的 Referer 请求头也由这里提供。
"""

from core import config
from core.utils import safe_name

KEY = 'douyin'

ID_RECORD_LABEL = '抖音作品'

# 跳过原因（归一化时累加到 stats['skipped']）
SKIP_EMPTY = 'empty'          # 空条目或没有 ID
SKIP_AUTHOR = 'author'        # 作者与目标博主不一致
SKIP_TYPE = 'type'            # 未知作品类型
# 无时间 / 无媒体的条目也照常产出 article（create_time=0 / urls=[]），
# 由会话先判已下载 ID、再按现有语义跳过或记失败，避免已知 ID 被漏计。


def headers():
    """下载请求头：公共头 + 平台段里的 referer / accept"""
    values = dict(config.HEADER)
    options = config.platform_settings(KEY)
    if options.get('referer'):
        values['Referer'] = options['referer']
    if options.get('accept'):
        values['Accept'] = options['accept']
    return values


def classify(aweme, image_types, video_types):
    """按配置判定作品类型；不属于任何已配置类型时返回 None"""
    aweme_type = aweme.get('aweme_type')
    if aweme_type in image_types:
        return 'image'
    if aweme_type in video_types:
        return 'video'
    return None


def media_urls(aweme, kind):
    """
    抖音取址规则

    图片：每张图取 url_list 里的第一个；
    视频：取 play_addr.url_list 的第一个（同一文件的多条地址是备用 CDN，不能都下）。
    """
    if kind == 'image':
        urls = []
        for image in aweme.get('images') or []:
            candidates = (image or {}).get('url_list') or []
            if candidates:
                urls.append(candidates[0])
        return urls
    play_addr = (aweme.get('video') or {}).get('play_addr') or {}
    return (play_addr.get('url_list') or [])[:1]


def normalize(aweme, nickname, stats=None):
    """把一条原始作品归一化成统一 article。

    Args:
        aweme: 接口返回的单条作品原始 dict
        nickname: 冻结后的博主名（来自 core.location.identity()）
        stats: 可选统计容器，就地累加 stats['skipped'][原因] 与
               stats['unknown_types'][类型标签]，供调用方输出跳过汇总

    Returns:
        dict or None: {'id', 'create_time', 'urls', 'kind'}；跳过时返回 None。
    """
    stats = stats if stats is not None else {}
    skipped = stats.setdefault('skipped', {})
    unknown = stats.setdefault('unknown_types', {})

    def skip(reason):
        skipped[reason] = skipped.get(reason, 0) + 1
        return None

    if not isinstance(aweme, dict):
        return skip(SKIP_EMPTY)

    aweme_id = aweme.get('aweme_id')
    if not aweme_id:
        return skip(SKIP_EMPTY)
    aweme_id = str(aweme_id)

    # 作者过滤：昵称取不到时放弃过滤（沿用旧规则，避免误杀）
    expected = safe_name(nickname or '')
    author = safe_name((aweme.get('author') or {}).get('nickname') or '')
    if expected and author and author != expected:
        return skip(SKIP_AUTHOR)

    values = config.platform_settings(KEY)
    kind = classify(aweme, values.get('image_types') or (),
                    values.get('video_types') or ())
    if kind is None:
        aweme_type = aweme.get('aweme_type')
        label = '缺失' if aweme_type is None else str(aweme_type)
        unknown[label] = unknown.get(label, 0) + 1
        return skip(SKIP_TYPE)

    # 无时间 / 无媒体不在平台层丢弃：会话先判已知 ID（计入 skipped_known），
    # 再按现有语义跳过无时间、把无媒体记为失败。这里只带原始值交出去。
    create_time = aweme.get('create_time') or 0
    urls = [url for url in media_urls(aweme, kind) if url]
    return {'id': aweme_id, 'create_time': create_time,
            'urls': urls, 'kind': kind}
