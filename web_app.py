# -*- coding: utf-8 -*-
"""
Web 入口模块
启动本地 Flask 服务并自动打开浏览器

用法：
    python web_app.py
"""

import socket
import threading
import webbrowser

import config
from utils import configure_encoding
import paths


def find_free_port(host, start, span=None):
    """在 [start, start+span) 区间内寻找可用端口"""
    span = span or config.PORT_RANGE
    for offset in range(span):
        port = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    raise OSError('端口 {}~{} 均被占用，请修改 config.ini 中的 port'.format(start, start + span - 1))


def main():
    """初始化路径、启动服务、打开浏览器"""
    configure_encoding()
    paths.set_total_path()

    try:
        from web.app import create_app
    except ImportError:
        raise SystemExit('缺少 Flask 依赖，请先执行：pip install -r requirements.txt')

    app = create_app()
    port = find_free_port(config.HOST, config.PORT)
    url = 'http://{}:{}'.format(config.HOST, port)

    print('  下载目录：{}'.format(paths.download_dir))
    print('  Web 地址：{}  （Ctrl+C 退出）'.format(url))

    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=config.HOST, port=port, threaded=True, debug=False, use_reloader=False)


if __name__ == '__main__':
    main()
