# -*- coding: utf-8 -*-
"""
路径管理模块
包含目录/文件路径管理、创建等功能

博主信息由下载目录本身承载，不再维护单独的列表文件：
    下载目录/{博主名}/            目录名即博主名
        homepage.txt              主页链接，供一键更新复用
        aweme_id.txt              已下载作品的 aweme_id（一篇一行）
        *.mp4 / picture/*.webp    媒体文件
"""

import os

import config  # 导入模块而非具体变量，便于运行时切换下载目录
from utils import make_dir, make_txt


HOMEPAGE_NAME = 'homepage.txt'
AWEME_ID_NAME = 'aweme_id.txt'


# 全局路径变量
download_dir = ''
blogger_dir = ''
aweme_id_txt_dir = ''
homepage_txt_dir = ''


def normalize_dir(dir_path):
    """目录路径统一使用 / 结尾，便于拼接"""
    return dir_path.rstrip('/\\') + os.sep


def set_total_path():
    """设置总下载目录路径"""
    global download_dir
    try:
        make_dir(config.CONFIGURED_DOWNLOAD_DIR)
    except OSError as error:
        raise SystemExit('  下载目录不可用（{}）：{}'.format(
            config.CONFIGURED_DOWNLOAD_DIR, error))
    download_dir = normalize_dir(config.CONFIGURED_DOWNLOAD_DIR)


def set_sub_path(blogger_name):
    """设置博主子目录路径"""
    global blogger_dir, aweme_id_txt_dir, homepage_txt_dir
    blogger_dir = normalize_dir(os.path.join(download_dir, blogger_name))
    make_dir(blogger_dir)
    # 已下载作品的 aweme_id（一篇一行），是下载去重的唯一依据
    aweme_id_txt_dir = os.path.join(blogger_dir, AWEME_ID_NAME)
    make_txt(aweme_id_txt_dir)
    # 主页链接：只在抓取成功后写入，未登记时文件不存在
    homepage_txt_dir = os.path.join(blogger_dir, HOMEPAGE_NAME)


def get_download_dir():
    """获取当前生效的下载目录（不存在时先初始化）"""
    if not download_dir:
        set_total_path()
    return download_dir


def get_blogger_dir(blogger_name):
    """某个博主目录的绝对路径（只计算，不创建也不改变全局路径）"""
    return normalize_dir(os.path.join(get_download_dir(), blogger_name))


def save_homepage(blogger_name, url):
    """把主页链接写入 博主目录/homepage.txt，供后续一键更新复用"""
    if not blogger_name or not (url or '').strip():
        return ''
    folder = get_blogger_dir(blogger_name)
    make_dir(folder)
    path = os.path.join(folder, HOMEPAGE_NAME)
    with open(path, mode='w', encoding='utf-8') as handle:
        handle.write(url.strip() + '\n')
    return path


def read_homepage(blogger_name):
    """读取 博主目录/homepage.txt；未登记或读取失败时返回空串"""
    if not blogger_name:
        return ''
    path = os.path.join(get_blogger_dir(blogger_name), HOMEPAGE_NAME)
    if not os.path.isfile(path):
        return ''
    try:
        with open(path, mode='r', encoding='utf-8') as handle:
            return handle.read().strip()
    except OSError:
        return ''
