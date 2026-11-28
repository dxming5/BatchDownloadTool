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

from core import settings
from core.utils import configure_encoding


def find_free_port(host, start, span=None):
    """在 [start, start+span) 区间内寻找可用端口"""
    span = span or settings.PORT_RANGE
    for offset in range(span):
        port = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    raise OSError('端口 {}~{} 均被占用，请修改 conf/config.ini 中的 port'.format(
        start, start + span - 1))


def main():
    """启动服务并打开浏览器"""
    configure_encoding()

    try:
        from web.app import create_app
    except ImportError:
        raise SystemExit('缺少 Flask 依赖，请先执行：pip install -r requirements.txt')

    app = create_app()
    port = find_free_port(settings.HOST, settings.PORT)
    url = 'http://{}:{}'.format(settings.HOST, port)

    print('  下载目录：{}'.format(settings.CONFIGURED_DOWNLOAD_DIR))
    print('  Web 地址：{}  （Ctrl+C 退出）'.format(url))

    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=settings.HOST, port=port, threaded=True, debug=False, use_reloader=False)


if __name__ == '__main__':
    main()
