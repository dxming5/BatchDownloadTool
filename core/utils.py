# -*- coding: utf-8 -*-
"""
工具函数模块
包含文件读写、编码处理、通用工具函数
"""

import os
import re
import shutil
import sys

ENCODING = 'utf-8'


def configure_encoding():
    """配置终端/标准输入输出使用 UTF-8 编码"""
    for _stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            _stream.reconfigure(encoding=ENCODING)
        except (AttributeError, ValueError, OSError):
            pass


def read_txt(txt_dir):
    """以 UTF-8 读取文本文档，返回非空行组成的列表"""
    if not os.path.exists(txt_dir):
        return []
    with open(txt_dir, mode='r', encoding=ENCODING) as file_handle:
        return [line.strip() for line in file_handle if line.strip()]


def append_list_txt(txt_dir, content_list):
    """批量追加写入列表到txt文档（UTF-8）"""
    with open(txt_dir, mode='a', encoding=ENCODING) as file_handle:
        for item in content_list:
            file_handle.write(item + '\n')


def make_dir(dir_path):
    """创建目录（如果不存在）"""
    if not os.path.exists(dir_path):
        os.makedirs(dir_path)


def make_txt(txt_str):
    """创建空文本文件（如果不存在）"""
    with open(txt_str, mode='a', encoding=ENCODING):
        pass


def rm_dir(dir_path):
    """删除目录（如果存在）"""
    if os.path.exists(dir_path):
        shutil.rmtree(dir_path)


def safe_name(name):
    """剔除不符合 Windows 文件夹名的字符：\\/:*?"<>| 与首尾空白、结尾点"""
    return re.sub(r'[\\/:*?"<>|\r\n\t]', '', name or '').strip().rstrip('.').strip()
