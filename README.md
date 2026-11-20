# 抖音视频/图片批量下载工具

一款简洁高效的抖音内容批量下载工具，支持下载指定博主的视频和图片，并自动整理归类。

## 功能特性

- **两种用法**：浏览器图形界面（Web）与命令行界面（CLI）并存，共用同一套下载核心
- **批量下载**：支持同时下载多位抖音博主的内容
- **自动分类**：视频和图片自动分类存储
- **断点续传**：智能识别已下载内容，避免重复下载
- **格式转换**：图片自动从 WebP 格式转换为 JPG
- **时间命名**：使用发布时间作为文件名，方便管理
- **去重机制**：通过 URL 特征码识别重复内容
- **UTF-8 编码**：源码、日志、列表与 URL 记录全部以 UTF-8 读写

## 目录结构

```
douyin/
├── main.py                # CLI 入口
├── cli.py                 # 命令行交互界面
├── web_app.py             # Web 入口（自动打开浏览器）
├── web/
│   └── app.py             # Flask 接口层（任务、博主、媒体回放）
├── static/                # 前端单页（HTML/CSS/JS，无需构建）
│   ├── index.html
│   ├── app.js
│   └── style.css
├── config.py              # 配置读取（只读 config.ini）
├── config.ini             # 统一配置文件（带注释，所有可配置参数）
├── paths.py               # 路径管理
├── url_parser.py          # JSON 解析与 URL 提取
├── downloader.py          # 下载与格式转换
├── utils.py               # 工具函数
├── requirements.txt       # 依赖清单
├── download/              # 下载目录（自动生成）
│   ├── json.log           # API 响应日志（仅 CLI 使用，Web 导入不写入）
│   ├── aweme_list.json    # 博主列表（自动生成）
│   ├── {博主名称}/
│   │   ├── url.txt        # 已下载 URL 记录
│   │   ├── *.mp4          # 视频文件
│   │   └── picture/       # 图片目录
│   │       └── *.jpg      # 图片文件
│   └── ...
└── README.md              # 说明文档
```

## 使用方法

### 第一步：获取抖音数据

1. 打开抖音网页版 (https://www.douyin.com)
2. 登录后进入目标博主的主页
3. 按 F12 打开开发者工具，切换到 **Network** 标签
4. 向下滚动加载博主所有视频内容
5. 找到类型为 `fetch/xhr` 且包含 `aweme_list` 的请求
6. 复制 Response 内容，追加到下载目录下的 `json.log` 文件中（每行一条 JSON）

> 使用 Web 模式时无需手动建文件：直接在页面上粘贴即可，数据只驻留内存（上限 100 MB），不写入 `json.log`，重启服务后需要重新导入。

### 方式一：Web 界面（推荐）

```bash
pip install -r requirements.txt
python web_app.py
```

启动后会自动打开浏览器（默认 `http://127.0.0.1:5000`，端口被占用时自动顺延）。页面分两步：

1. **下载**：粘贴接口 Response 后点「开始下载」，数据存入内存并直接解析（自动跳过非法行）；确认视频/图片数量与博主名后再次点击「开始下载」，下方显示实时进度条、文件日志与失败列表（可一键复制失败 URL）
2. **博主管理**：查看每位博主的视频/图片数量与占用空间，支持图片预览、视频预览与删除

所有配置均通过编辑 `config.ini` 修改，重启程序后生效；界面不支持修改配置。

> 服务只监听 `127.0.0.1`，不对外网开放；同一时刻只允许一个下载任务。

### 方式二：命令行界面

```bash
python main.py
```

程序启动后显示主菜单：

| 指令 | 功能 |
|------|------|
| `a` | 开始下载所有博主的内容 |
| `b` | 删除指定博主（清除已下载内容） |
| `c` | 查看博主列表 |
| `x` | 退出程序 |

## 配置说明

所有可配置参数集中在项目根目录的 `config.ini`，以 `#` 或 `;` 开头的行是注释。`config.ini` 必须存在，缺失时程序会报错退出。修改配置后需重启程序生效。

```ini
[download]
download_dir = F:/图片/douyin/      # 下载根目录，必须可创建
image_types = 68                    # 判定为图片的 aweme type
video_types = 0,51,55,61,66         # 判定为视频的 aweme type
timeout = 30                        # 下载超时（秒）
max_retry = 3                       # 失败重试次数

[network]
verify_ssl = false                  # false 表示忽略证书校验

[network.header]
User-Agent = Mozilla/5.0 ...        # 浏览器标识
Accept = application/json, text/plain, */*
Referer = https://www.douyin.com/

[web]
host = 127.0.0.1                    # 只应绑定本机
port = 5000                         # Web 服务端口，被占用时自动顺延
port_range = 10                     # 顺延查找的端口数量
import_memory_limit_mb = 100        # 导入数据的内存上限（MB）
```

### 修改配置

直接编辑 `config.ini` 中对应的项（如 `download_dir`、`[network.header]` 下的 `User-Agent`），重启后生效。

## 环境依赖

- **Python 3.8+**
- **Pillow**（图片格式转换、缩略图）
- **Flask**（仅 Web 模式需要；CLI 模式不依赖）

安装依赖：

```bash
pip install -r requirements.txt
```

**标准库依赖**：`urllib`, `ssl`, `json`, `os`, `time`, `socket`, `sys`, `re`, `shutil`, `datetime`, `threading`, `hashlib`, `configparser`

## 项目架构

项目采用模块化设计，下载核心与界面解耦：`downloader` 通过进度回调上报事件，CLI 与 Web 各自实现渲染。

| 模块 | 职责 |
|------|------|
| [main.py](file:///d:/Git%E4%BB%93%E5%BA%93/douyin/main.py) | CLI 入口，初始化并启动命令行界面 |
| [cli.py](file:///d:/Git%E4%BB%93%E5%BA%93/douyin/cli.py) | 命令行交互界面，把进度事件渲染为终端输出 |
| [web_app.py](file:///d:/Git%E4%BB%93%E5%BA%93/douyin/web_app.py) | Web 入口，寻找空闲端口并打开浏览器 |
| [web/app.py](file:///d:/Git%E4%BB%93%E5%BA%93/douyin/web/app.py) | Flask 接口层：任务管理、博主管理、媒体回放 |
| [static/](file:///d:/Git%E4%BB%93%E5%BA%93/douyin/static) | 前端单页（原生 HTML/CSS/JS，轮询进度） |
| [config.py](file:///d:/Git%E4%BB%93%E5%BA%93/douyin/config.py) | 配置读取模块（只读 config.ini） |
| config.ini | 统一配置文件（所有可配置参数，带注释） |
| [paths.py](file:///d:/Git%E4%BB%93%E5%BA%93/douyin/paths.py) | 路径管理（下载目录、文件路径、运行时切换目录） |
| [url_parser.py](file:///d:/Git%E4%BB%93%E5%BA%93/douyin/url_parser.py) | JSON 纯解析（`parse_aweme`）与 CLI 交互包装 |
| [downloader.py](file:///d:/Git%E4%BB%93%E5%BA%93/douyin/downloader.py) | 网络下载、去重、WebP 转 JPG、进度回调 |
| [utils.py](file:///d:/Git%E4%BB%93%E5%BA%93/douyin/utils.py) | 通用工具函数（文件读写、编码处理） |

## 注意事项

1. **文件编码**：`json.log`、`aweme_list.json`、`url.txt` 均按 UTF-8 读取
2. **数据时效性**：抖音 API 数据具有时效性，建议获取后尽快下载
3. **登录状态**：确保抖音账号已登录，否则可能无法获取完整数据
4. **下载频率**：大量下载时建议适当增加延时，避免触发风控
5. **存储空间**：视频文件较大，请确保目标磁盘空间充足

## 免责声明

本工具仅供学习交流使用，请勿用于商业传播或任何盈利性活动。下载内容仅供个人学习研究使用，遵守抖音平台服务协议及相关法律法规。
