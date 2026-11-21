# -*- coding: utf-8 -*-
"""
配置模块
从 conf/config.ini 读取全部可配置参数；配置只读，修改后重启生效。

位置基准是项目根（conf 的上一级），与启动时的工作目录无关。
平台参数按 platforms.<key> 段读取，取不到时回落到 [platforms.default]；
为兼容旧配置，也支持回退到 [scrape] 段。
"""

import os
import ssl
import configparser


# ---------------- 固定路径（非可配置项） ----------------

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONF_DIR = os.path.join(BASE_DIR, 'conf')
CONFIG_PATH = os.path.join(CONF_DIR, 'config.ini')
LEGACY_CONFIG_PATH = os.path.join(BASE_DIR, 'config.ini')

# 各平台共用的抓取参数段
DEFAULT_SECTION = 'platforms.default'

# 浏览器数据目录根：所有平台共用，各平台在下面按 key 建二级目录（保存登录态）
PROFILE_ROOT = os.path.join(BASE_DIR, '.pw_profile')


def _parse_types(value):
    """把逗号分隔的整数串转成 tuple（标准库无此类型，需自行解析）"""
    return tuple(int(item.strip()) for item in str(value).split(',') if item.strip())


def _locate_config():
    """配置文件只认 conf/config.ini；旧位置留给迁移提示"""
    if os.path.exists(CONFIG_PATH):
        return CONFIG_PATH
    if os.path.exists(LEGACY_CONFIG_PATH):
        raise SystemExit(
            '检测到配置文件仍在项目根：请把它移到 {}\n'
            '（新版本固定从 conf/config.ini 读取）'.format(CONFIG_PATH))
    raise SystemExit('缺少配置文件 {}，请先创建（参考 README 配置说明）'.format(CONFIG_PATH))


_parser = configparser.ConfigParser()
_parser.optionxform = str
_parser.read(_locate_config(), encoding='utf-8')


# ---------------- 下载 ----------------

CONFIGURED_DOWNLOAD_DIR = _parser.get('download', 'download_dir', fallback='download/')
DOWNLOAD_TIMEOUT = _parser.getint('download', 'timeout', fallback=30)
MAX_RETRY = _parser.getint('download', 'max_retry', fallback=3)
# 同时下载的 URL 数；配错或为 1 时按 1 处理（即逐个下载），上限 16
DOWNLOAD_WORKERS = min(max(1, _parser.getint('download', 'workers', fallback=4)), 16)


# ---------------- 网络 ----------------

HEADER = {}
for _key, _value in _parser.items('network.header'):
    if _key.strip():
        HEADER[_key.strip()] = _value.strip()
if not HEADER:
    HEADER.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0',
        'Accept': 'application/json, text/plain, */*',
    })

VERIFY_SSL = _parser.getboolean('network', 'verify_ssl', fallback=False)
SSL_CONTEXT = ssl.create_default_context() if VERIFY_SSL else ssl._create_unverified_context()


# ---------------- Web ----------------

HOST = _parser.get('web', 'host', fallback='127.0.0.1')
PORT = _parser.getint('web', 'port', fallback=5000)
PORT_RANGE = _parser.getint('web', 'port_range', fallback=10)

# 浏览器标签标题；配置留空时用这里的默认值
WEB_TITLE = _parser.get('web', 'title', fallback='').strip() or '抖音视频 / 图片 批量下载'


# ---------------- 平台（抓取 + 下载） ----------------

_PLATFORM_DEFAULTS = {
    'headless': 'true',
    'profile_dir': '',
    'wait_login_seconds': '180',
    'max_scroll': '400',
    'idle_rounds': '8',
    'month_days': '30',
    'browser_channel': '',
    'proxy': '',
    'timeout_ms': '30000',
    'image_types': '68',
    'video_types': '0,51,55,61,66',
    'referer': '',
    'accept': '',
}


def platform_profile_dir(key):
    """某个平台默认的浏览器数据目录：<项目根>/.pw_profile/<平台 key>"""
    return os.path.join(PROFILE_ROOT, key)


def _platform_section(key):
    """平台配置段名；不存在时回退到旧的 [scrape] 段"""
    section = 'platforms.' + key
    if _parser.has_section(section):
        return section
    if _parser.has_section('scrape'):
        return 'scrape'
    return section


def platform_settings(key):
    """
    读取某个平台的参数

    取值顺序：平台段 → [platforms.default] → 代码内默认值

    Returns:
        dict: profile_dir / headless / wait_login / max_scroll / idle_rounds /
              month_days / browser_channel / proxy / timeout_ms /
              image_types / video_types / referer / accept
    """
    section = _platform_section(key)
    fallback = _PLATFORM_DEFAULTS

    def raw(option):
        for name in (section, DEFAULT_SECTION):
            if _parser.has_option(name, option):
                return (_parser.get(name, option) or '').strip()
        return fallback[option]

    def number(option, minimum):
        try:
            value = int(raw(option) or fallback[option])
        except ValueError:
            value = int(fallback[option])
        return max(minimum, value)

    def types(option):
        try:
            return _parse_types(raw(option) or fallback[option])
        except ValueError:
            return _parse_types(fallback[option])

    def flag(option):
        value = raw(option) or fallback[option]
        return value.strip().lower() in ('1', 'true', 'yes', 'on')

    # 留空时统一落到 .pw_profile/<平台 key>，新增平台无需额外配置
    profile_dir = raw('profile_dir') or platform_profile_dir(key)

    return {
        'profile_dir': profile_dir,
        'headless': flag('headless'),
        'wait_login': number('wait_login_seconds', 0),
        'max_scroll': number('max_scroll', 1),
        'idle_rounds': number('idle_rounds', 1),
        'month_days': number('month_days', 1),
        'browser_channel': raw('browser_channel') or None,
        'proxy': raw('proxy') or None,
        'timeout_ms': number('timeout_ms', 5000),
        'image_types': types('image_types'),
        'video_types': types('video_types'),
        'referer': raw('referer'),
        'accept': raw('accept'),
    }
