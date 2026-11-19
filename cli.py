# -*- coding: utf-8 -*-
"""
命令行交互界面模块
包含菜单控制、博主列表显示、删除等功能
"""

import sys
import os

from utils import read_txt, read_int, rm_dir, write_list_txt
import paths  # 导入模块而非具体变量，以便动态获取更新后的值
from downloader import download, print_failure_summary


def delete_blogger(blogger_list):
    """删除指定博主及其下载内容"""
    for i in range(0, len(blogger_list)):
        print(str(int(i + 1)) + '：' + blogger_list[i])
    blog_id = read_int('输入对应的 blog id：')
    while blog_id <= 0 or blog_id > len(blogger_list):
        blog_id = read_int('blog id 输入错误，重新输入：')
    blogger_name = blogger_list[blog_id - 1]
    rm_dir(os.path.join(paths.download_dir, blogger_name))
    blogger_list.remove(blogger_name)
    write_list_txt(paths.aweme_list_dir, blogger_list)
    print('  已删除 <{}> 及其下载内容'.format(blogger_name))


def show_blogger_list(blogger_list):
    """显示博主列表"""
    if not blogger_list:
        print('  博主列表为空')
        return
    for i in range(0, len(blogger_list)):
        print(str(int(i + 1)) + '：' + blogger_list[i])


def _cli_progress(event):
    """把下载回调渲染成终端输出，保持原有观感"""
    stage = event.get('stage')
    if stage == 'start':
        print('*' * 20 + ' <blogger：' + event['blogger'] + '> ' + '*' * 20)
        print(' <{}>视频抖音{}条，图片抖音{}条'.format(
            event['blogger'], event['video_total'], event['picture_total']))
    elif stage == 'file_start':
        print('\r [{}已下载进度：{}%({}/{})] 开始下载<{}>时刻第{}{}{}'.format(
            event['media_type'], event['percent'], event['done'], event['total'],
            event['create_time'], event['index'], event['unit'], event['media_type']), end='')
    elif stage == 'item':
        print('\r [{}已下载进度：{}%({}/{})]'.format(
            event['media_type'], event['percent'], event['done'], event['total']), end='')
    elif stage == 'finished':
        print('\r 下载任务结束，共处理 {}/{} 项{}'.format(
            event['done'], event['total'], '（已取消）' if event.get('stopped') else ' ' * 30))
        print_failure_summary()
        print('*' * 45 + '\n')


def run_cli():
    """运行交互式命令行循环"""
    while True:
        function_selection = input('\n  a:下载 b:删除blog c:获取blogger列表 x:退出程序 \n请输入对应的指令：').strip().lower()
        blogger_list = read_txt(paths.aweme_list_dir)
        if function_selection == 'a':
            download(progress=_cli_progress)
        elif function_selection == 'b':
            delete_blogger(blogger_list)
        elif function_selection == 'c':
            show_blogger_list(blogger_list)
        elif function_selection == 'x':
            sys.exit(0)
        else:
            print('指令输入错误，请重新输入！')
