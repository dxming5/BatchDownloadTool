# -*- coding: utf-8 -*-
"""
路径管理模块
包含目录/文件路径管理、创建等功能
"""

import os

import config  # 导入模块而非具体变量，便于运行时切换下载目录
from utils import make_dir, make_txt


# 全局路径变量
download_dir = ''
aweme_list_dir = ''
blogger_dir = ''
blogger_url_txt_dir = ''


def normalize_dir(dir_path):
    """目录路径统一使用 / 结尾，便于拼接"""
    return dir_path.rstrip('/\\') + os.sep


def set_total_path():
    """设置总下载目录路径"""
    global download_dir, aweme_list_dir
    download_dir = ''
    for candidate in (config.CONFIGURED_DOWNLOAD_DIR, config.DEFAULT_DOWNLOAD_DIR):
        if not candidate:
            continue
        try:
            make_dir(candidate)
            download_dir = normalize_dir(candidate)
            break
        except OSError as error:
            print('  下载目录不可用（{}）：{}'.format(candidate, error))
    if not download_dir:
        download_dir = normalize_dir(config.DEFAULT_DOWNLOAD_DIR)
        make_dir(download_dir)

    aweme_list_dir = os.path.join(download_dir, 'aweme_list.json')
    make_txt(aweme_list_dir)


def set_sub_path(blogger_name):
    """设置博主子目录路径"""
    global blogger_dir, blogger_url_txt_dir
    blogger_dir = normalize_dir(os.path.join(download_dir, blogger_name))
    make_dir(blogger_dir)
    blogger_url_txt_dir = os.path.join(blogger_dir, 'url.txt')
    make_txt(blogger_url_txt_dir)


def set_download_dir(dir_path):
    """运行时切换下载目录，返回实际生效的目录"""
    global download_dir
    config.CONFIGURED_DOWNLOAD_DIR = dir_path
    set_total_path()
    return download_dir


def get_download_dir():
    """获取当前生效的下载目录（不存在时先初始化）"""
    if not download_dir:
        set_total_path()
    return download_dir


def get_json_log_path():
    """
    json.log 优先取脚本目录下的，其次取下载目录下的（兼容旧用法）
    """
    script_log = os.path.join(config.BASE_DIR, 'json.log')
    if os.path.exists(script_log):
        return script_log
    return os.path.join(get_download_dir(), 'json.log')
