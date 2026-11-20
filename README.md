# 抖音视频/图片批量下载工具

一款简洁高效的抖音内容批量下载工具，支持下载指定博主的视频和图片，并自动整理归类。

## 功能特性

- **浏览器图形界面**：本地启动 Web 服务，粘贴链接即可抓取并下载，无需命令行操作
- **一键抓取**：Web 页面粘贴博主主页链接，自动抓取作品列表并直接下载，无需 F12 复制 JSON
- **一键更新**：抓取过的博主会记住主页链接，之后点「更新」即可增量补全
- **批量下载**：支持同时下载多位抖音博主的内容
- **自动分类**：视频和图片自动分类存储
- **断点续传**：按作品 aweme_id 记录已下载内容，避免重复下载
- **原始画质**：图片保留抖音下发的原始 WebP 格式，不做有损二次编码
- **时间命名**：使用发布时间作为文件名，方便管理
- **去重机制**：以 aweme_id 为唯一依据，与媒体 URL 的时效签名无关
- **类型可配**：按 config.ini 的 aweme type 分类，未知类型跳过并提示
- **UTF-8 编码**：源码、日志、列表与 aweme_id 记录全部以 UTF-8 读写

## 目录结构

```
douyin/
├── web_app.py             # 程序入口（启动 Web 服务并自动打开浏览器）
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
├── downloader.py          # 下载与去重
├── scraper.py             # Playwright 抓取（主页作品列表，可单独运行）
├── utils.py               # 工具函数
├── .pw_profile/           # 抓取用的浏览器数据目录（保存登录态，自动生成）
├── requirements.txt       # 依赖清单
├── download/              # 下载目录（自动生成）
│   └── {博主名称}/        # 目录名即博主名，目录本身就是博主列表
│       ├── homepage.txt   # 主页链接（抓取成功后写入，供一键更新）
│       ├── aweme_id.txt   # 已下载作品的 aweme_id（一篇一行）
│       ├── *.mp4          # 视频文件
│       └── picture/       # 图片目录
│           └── *.webp     # 图片文件（原始 WebP）
└── README.md              # 说明文档
```

## 使用方法

```bash
pip install -r requirements.txt
playwright install chromium      # 一键抓取需要
python web_app.py
```

启动后会自动打开浏览器（默认 `http://127.0.0.1:5000`，端口被占用时自动顺延）。页面分两块：

1. **下载**：粘贴博主主页链接（`https://www.douyin.com/user/MS4w...`，也接受裸 `sec_user_id`），选择「全部作品」或「最近一个月」，点「抓取并下载」，程序会用 Playwright 打开真实浏览器抓取接口数据，抓到后立即下载；进度条与日志实时显示「抓取中 · 第 N 页 · 累计 M 条」以及下载进度，同时提供失败列表（可一键复制失败 URL），任务可随时取消
2. **博主管理**：列出下载目录里的博主，点击某一行即可在右侧预览该博主的作品（默认「图片」，可在预览顶部切到「视频」，并支持手动刷新），行内提供一键更新与删除

### 一键更新

抓取成功后，程序会把该博主的主页链接写入 `download/{博主名}/homepage.txt`。之后在「博主管理」里点该博主的「更新」，就会直接读这个链接重新抓取，不用再粘贴：

- 更新按「全部作品」抓取，能补上历史漏项
- 已下载的作品由 `aweme_id.txt` 跳过，只下载新增内容
- 进度条按作品篇数统计，已完成的会快速跳过
- 目录里没有 `homepage.txt`（例如手动拷入的目录）时按钮为禁用状态，重新粘贴一次链接抓取即可生成

### 抓取说明

- 登录态保存在 `.pw_profile/`（可用 `[scrape] profile_dir` 改），**只需扫码登录一次**，之后无头运行即可
- 首次使用或登录失效时，程序会等待最多 `wait_login_seconds` 秒（默认 180），期间在弹出的浏览器窗口里扫码即可
- 「最近一个月」会在翻到时间边界时提前停止，不必抓完全部作品
- 同一时刻只允许一个任务；抓取与下载在同一个任务里串行完成
- 未知 `aweme_type` 的作品会被跳过并在日志中提示，需要下载时把它加进 `config.ini` 的 `image_types` / `video_types` 后重跑

所有配置均通过编辑 `config.ini` 修改，重启程序后生效；界面不支持修改配置。

> 服务只监听 `127.0.0.1`，不对外网开放。

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

[scrape]
headless = true                     # 抓取时是否无头；首次登录需 false
profile_dir =                       # 浏览器数据目录，留空 = 项目根/.pw_profile
wait_login_seconds = 180            # 未登录时等待扫码的时间，0 表示不等
max_scroll = 400                    # 最大滚动次数
idle_rounds = 8                     # 连续多少次滚动无新增就停止
month_days = 30                     # 「最近一个月」对应的天数
browser_channel =                   # 留空用内置 Chromium，可填 msedge / chrome
proxy =                             # 代理地址，留空不使用
timeout_ms = 30000                  # 页面加载超时（毫秒）
```

### 修改配置

直接编辑 `config.ini` 中对应的项（如 `download_dir`、`[network.header]` 下的 `User-Agent`），重启后生效。

## 环境依赖

- **Python 3.8+**
- **Pillow**（Web 端缩略图生成）
- **Flask**（Web 服务）
- **Playwright**（Web 模式的一键抓取必需，未安装时抓取入口不可用）

安装依赖：

```bash
pip install -r requirements.txt
playwright install chromium      # 一键抓取需要，下载浏览器内核
```

**标准库依赖**：`urllib`, `ssl`, `json`, `os`, `time`, `socket`, `sys`, `re`, `shutil`, `datetime`, `threading`, `hashlib`, `configparser`

## 项目架构

项目采用模块化设计，下载核心与界面解耦：`downloader` 通过进度回调上报事件，由 Web 层渲染。

| 模块 | 职责 |
|------|------|
| [web_app.py](file:///d:/Git%E4%BB%93%E5%BA%93/douyin/web_app.py) | 程序入口，寻找空闲端口并打开浏览器 |
| [web/app.py](file:///d:/Git%E4%BB%93%E5%BA%93/douyin/web/app.py) | Flask 接口层：任务管理、博主管理、媒体回放 |
| [static/](file:///d:/Git%E4%BB%93%E5%BA%93/douyin/static) | 前端单页（原生 HTML/CSS/JS，轮询进度） |
| [config.py](file:///d:/Git%E4%BB%93%E5%BA%93/douyin/config.py) | 配置读取模块（只读 config.ini） |
| config.ini | 统一配置文件（所有可配置参数，带注释） |
| [paths.py](file:///d:/Git%E4%BB%93%E5%BA%93/douyin/paths.py) | 路径管理（下载目录、文件路径、运行时切换目录） |
| [url_parser.py](file:///d:/Git%E4%BB%93%E5%BA%93/douyin/url_parser.py) | 响应摊平（`collect_awemes`）、类型判定与媒体 URL 提取 |
| [downloader.py](file:///d:/Git%E4%BB%93%E5%BA%93/douyin/downloader.py) | 按作品下载、aweme_id 去重、进度回调 |
| [scraper.py](file:///d:/Git%E4%BB%93%E5%BA%93/douyin/scraper.py) | Playwright 抓取博主主页作品（可库调用，也可单独运行） |
| [utils.py](file:///d:/Git%E4%BB%93%E5%BA%93/douyin/utils.py) | 通用工具函数（文件读写、编码处理） |

## 注意事项

1. **文件编码**：`homepage.txt`、`aweme_id.txt` 均按 UTF-8 读写
2. **数据时效性**：抖音 API 数据具有时效性，建议获取后尽快下载
3. **登录状态**：确保抖音账号已登录，否则可能无法获取完整数据
4. **下载频率**：大量下载时建议适当增加延时，避免触发风控
5. **存储空间**：视频文件较大，请确保目标磁盘空间充足

## 免责声明

本工具仅供学习交流使用，请勿用于商业传播或任何盈利性活动。下载内容仅供个人学习研究使用，遵守抖音平台服务协议及相关法律法规。
