# -*- coding: utf-8 -*-
"""
配置常量模块
包含全局配置、数据类型定义、网络请求参数等
"""

import os
import json
import ssl


# 编码配置
ENCODING = 'utf-8'
LEGACY_ENCODINGS = ('gbk', 'mbcs')

# 抖音内容类型
IMAGE_TYPES = (68,)
VIDEO_TYPES = (0, 51, 55, 61, 66)

# HTTP 请求头
HEADER = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0',
    'Accept': 'application/json, text/plain, */*',
    'Referer': 'https://www.douyin.com/'
}

# SSL 上下文（忽略证书验证）
SSL_CONTEXT = ssl._create_unverified_context()

# 下载配置
DOWNLOAD_TIMEOUT = 30
MAX_RETRY = 3

# 导入数据的内存上限（字节）：Web 导入的接口数据只驻留内存，不写入 json.log
IMPORT_MEMORY_LIMIT = 100 * 1024 * 1024

# 路径配置
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIGURED_DOWNLOAD_DIR = 'F:/图片/douyin/'
DEFAULT_DOWNLOAD_DIR = os.path.join(BASE_DIR, 'download')

# Web 配置
SETTINGS_PATH = os.path.join(BASE_DIR, 'settings.json')
HOST = '127.0.0.1'
PORT = 5000
PORT_RANGE = 10

# 设置文件中的默认值快照（恢复默认时使用）
_DEFAULTS = {
    'download_dir': CONFIGURED_DOWNLOAD_DIR,
    'header': dict(HEADER),
    'port': PORT,
}


def load_settings():
    """读取 settings.json，文件不存在或损坏时返回空字典"""
    if not os.path.exists(SETTINGS_PATH):
        return {}
    try:
        with open(SETTINGS_PATH, mode='r', encoding=ENCODING) as file_handle:
            data = json.load(file_handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(patch):
    """合并并写入 settings.json"""
    data = load_settings()
    data.update(patch or {})
    with open(SETTINGS_PATH, mode='w', encoding=ENCODING) as file_handle:
        json.dump(data, file_handle, ensure_ascii=False, indent=2)
    return data


def apply_settings():
    """
    用 settings.json 覆盖默认配置。
    必须在调用 paths.set_total_path() 之前执行；运行时修改目录请用 paths.set_download_dir()
    """
    global CONFIGURED_DOWNLOAD_DIR, PORT
    data = load_settings()
    if not data:
        return data
    dir_path = data.get('download_dir')
    if dir_path:
        CONFIGURED_DOWNLOAD_DIR = dir_path
    header = data.get('header')
    if isinstance(header, dict):
        HEADER.update(header)
    try:
        PORT = int(data.get('port') or PORT)
    except (TypeError, ValueError):
        pass
    return data


def default_settings():
    """返回默认设置副本"""
    return json.loads(json.dumps(_DEFAULTS))
