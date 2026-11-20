# -*- coding: utf-8 -*-
"""
配置模块
从 config.ini 读取全部可配置参数；配置只读，修改请直接编辑 config.ini 后重启。
"""

import os
import ssl
import configparser


# ---------------- 固定路径（非可配置项） ----------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.ini')


def _parse_types(value):
    """把逗号分隔的整数串转成 tuple（标准库无此类型，需自行解析）"""
    return tuple(int(item.strip()) for item in str(value).split(',') if item.strip())


if not os.path.exists(CONFIG_PATH):
    raise SystemExit('缺少配置文件 {}，请先创建（参考 README 配置说明）'.format(CONFIG_PATH))

_parser = configparser.ConfigParser()
_parser.optionxform = str
_parser.read(CONFIG_PATH, encoding='utf-8')

CONFIGURED_DOWNLOAD_DIR = _parser.get('download', 'download_dir', fallback='F:/图片/douyin/')
IMAGE_TYPES = _parse_types(_parser.get('download', 'image_types', fallback='68'))
VIDEO_TYPES = _parse_types(_parser.get('download', 'video_types', fallback='0,51,55,61,66'))
DOWNLOAD_TIMEOUT = _parser.getint('download', 'timeout', fallback=30)
MAX_RETRY = _parser.getint('download', 'max_retry', fallback=3)

HEADER = {}
for _key, _value in _parser.items('network.header'):
    if _key.strip():
        HEADER[_key.strip()] = _value.strip()
if not HEADER:
    HEADER.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0',
        'Accept': 'application/json, text/plain, */*',
        'Referer': 'https://www.douyin.com/'
    })

VERIFY_SSL = _parser.getboolean('network', 'verify_ssl', fallback=False)
SSL_CONTEXT = ssl.create_default_context() if VERIFY_SSL else ssl._create_unverified_context()

HOST = _parser.get('web', 'host', fallback='127.0.0.1')
PORT = _parser.getint('web', 'port', fallback=5000)
PORT_RANGE = _parser.getint('web', 'port_range', fallback=10)
IMPORT_MEMORY_LIMIT = max(1, _parser.getint('web', 'import_memory_limit_mb', fallback=100)) * 1024 * 1024
