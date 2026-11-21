# 抖音 / 小红书 内容批量下载工具

一款简洁高效的博主内容批量下载工具，支持抖音与小红书，下载视频和图片并自动整理归类。

## 功能特性

- **浏览器图形界面**：本地启动 Web 服务，粘贴链接即可抓取并下载，无需命令行操作
- **多平台**：抖音与小红书共用一套界面，博主列表按平台分组；新增平台只需补平台实现
- **一键抓取**：Web 页面粘贴博主主页链接，自动抓取作品列表并直接下载，无需 F12 复制 JSON
- **一键更新**：抓取过的博主会记住主页链接，之后点「更新」即可增量补全
- **自动分类**：视频和图片自动分类存储
- **断点续传**：按作品 ID 记录已下载内容（抖音 aweme_id / 小红书 note_id），避免重复下载
- **原始画质**：抖音图片保留原始 WebP，小红书图片还原无水印原图
- **时间命名**：使用发布时间作为文件名，方便管理
- **去重机制**：以作品 ID 为唯一依据，与媒体 URL 的时效签名无关
- **类型可配**：抖音按 aweme type 分类（配置可改），未知类型跳过并提示
- **UTF-8 编码**：源码、日志、列表与 ID 记录全部以 UTF-8 读写

## 目录结构

```
douyin/
├── web_app.py                 # 程序入口（启动 Web 服务并自动打开浏览器）
├── conf/
│   ├── config.ini             # 统一配置文件（带注释，所有可配置参数）
│   └── settings.py            # 配置读取（固定读 conf/config.ini）
├── core/                      # 公用层：与平台无关的机制
│   ├── task.py                # 任务状态、进度、日志、取消
│   ├── pipeline.py            # 编排：认平台 → 抓取 → 定位存储 → 交给平台下载
│   ├── registry.py            # 平台注册表
│   ├── store.py               # 博主目录、去重记录、主页链接
│   ├── naming.py              # 时间格式与落盘命名
│   ├── browser.py             # 通用浏览器能力（启动、滚动、登录等待）
│   ├── downloader.py          # 单个地址下载（重试、清理、.part 定名）+ 并发调度
│   └── utils.py               # 工具函数
├── platforms/                 # 平台实现层（每个平台一份抓取 / 工具 / 下载）
│   ├── base.py                # 平台接口约定
│   ├── douyin/
│   │   ├── scraper.py         # 链接解析、列表采集
│   │   ├── parser.py          # 作品类型判定
│   │   └── download.py        # 挑条目、取地址、命名、落盘、进度
│   └── xiaohongshu/
│       ├── scraper.py         # 首屏 SSR + 接口分页 + 逐条取详情
│       ├── parser.py          # 图文/视频判定、去水印、选码率
│       └── download.py        # 挑条目、取地址、命名、落盘、HEIC 转 JPG
├── media/                     # 缩略图与视频封面缓存
├── web/
│   ├── app.py                 # 路由
│   └── media_api.py           # 媒体响应（按区间读文件）
├── static/                    # 前端单页（HTML/CSS/JS，无需构建）
│   ├── index.html
│   ├── app.js
│   └── style.css
├── .pw_profile/               # 抖音抓取用的浏览器数据目录（保存登录态，自动生成）
├── .pw_profile_xhs/           # 小红书抓取用的浏览器数据目录（与抖音隔离）
├── requirements.txt           # 依赖清单
├── download/                  # 下载根目录示例（实际位置由 download_dir 决定，自动创建）
│   ├── douyin/                # 平台目录（平台标识）
│   │   └── {博主名称}/        # 目录名即博主名，目录本身就是博主列表
│   │       ├── homepage.txt   # 主页链接（抓取成功后写入，供一键更新；未抓取时不存在）
│   │       ├── aweme_id.txt   # 已下载作品的 ID（一篇一行，下载时创建）
│   │       ├── {发布时间}.mp4  # 视频文件，如 2026-01-01 10.00.00.mp4
│   │       ├── picture/       # 图片目录（有图片作品时才创建）
│   │       │   └── {发布时间} - {序号}.webp   # 抖音保留原始 WebP
│   │       └── .cache/        # 预览缓存（缩略图 thumbs、视频封面 covers，可随时删除）
│   └── xiaohongshu/           # 结构同上，图片可能是 jpg/webp/avif/gif
└── README.md                  # 说明文档
```

下载目录的实际位置由 `conf/config.ini` 的 `[download] download_dir` 决定，与项目代码目录无关：

- 目录不存在时自动创建；创建失败（盘符不存在、无权限等）时程序报错退出
- 相对路径按**启动程序时所在的工作目录**解析（如示例 `download/`），换目录启动会指向不同位置，建议直接写绝对路径
- 每个博主一个目录 `{下载根目录}/{平台目录}/{博主名}/`，不存在时自动创建；`aweme_id.txt` 在下载该博主时创建（可能为空文件）
- 平台目录下的一级子目录中，含 `homepage.txt`、`picture/` 或 `*.mp4` 的才算博主；零散文件和 `.cache/` 等隐藏目录会被忽略
- 把已有的博主目录整个拷进对应平台目录，重启后界面即可识别并预览（缺 `homepage.txt` 时「更新」按钮不可用）
- 从旧版本升级时，原有 `{下载根目录}/{博主名}/` 需要移入 `{下载根目录}/douyin/` 下（同盘移动是瞬时改名，不复制数据）

## 使用方法

```bash
pip install -r requirements.txt
playwright install chromium      # 一键抓取需要
python web_app.py
```

启动后会自动打开浏览器（默认 `http://127.0.0.1:5000`，端口被占用时自动顺延）。页面分两块：

1. **下载**：粘贴博主主页链接，程序按域名自动识别平台，选择「全部作品」或「最近一个月」，点「抓取并下载」。输入框支持一次粘贴多个链接（每行一个，也可用空格/逗号分隔，自动去重），多条时按顺序逐个抓取下载，单条失败不影响后续。支持两种链接：
   - 抖音：`https://www.douyin.com/user/MS4w...`（也接受裸 `sec_user_id`）
   - 小红书：`https://www.xiaohongshu.com/user/profile/<24位id>?xsec_token=...`（**建议带上 xsec_token**，否则可能看不到内容）

   抓取过程会用 Playwright 打开真实浏览器，抓到后立即下载；进度条与日志实时显示「抓取中 · 第 N 页 · 累计 M 条」以及下载进度，同时提供失败列表（可一键复制失败 URL），任务可随时取消
2. **博主管理**：按平台分组列出下载目录里的博主，点击某一行即可在右侧预览该博主的作品（默认「图片」，可在预览顶部切到「视频」，并支持手动刷新），行内提供一键更新与删除；右上角「更新」按钮按下拉框所选平台批量更新全部博主（选「全部平台」时逐个平台跑），串行执行、可随时取消，单个博主失败或没有新增内容都不影响后续

### 一键更新

抓取成功后，程序会把该博主的主页链接写入 `{下载根目录}/{平台目录}/{博主名}/homepage.txt`。之后在「博主管理」里点该博主的「更新」，就会直接读这个链接重新抓取，不用再粘贴：

- 更新按「全部作品」抓取，能补上历史漏项
- 已下载的作品由 `aweme_id.txt` 里的作品 ID（抖音 aweme_id / 小红书 note_id）跳过，只下载新增内容
- 小红书会在**抓取阶段**就排除已下载的笔记（连详情页都不打开）；抖音本来就不用取详情，在下载阶段跳过
- 进度条按作品条数统计，已完成的会快速跳过
- 目录里没有 `homepage.txt`（例如手动拷入的目录）时按钮为禁用状态，重新粘贴一次链接抓取即可生成

### 抓取说明

- 登录态按平台隔离：抖音在 `.pw_profile/`、小红在 `.pw_profile_xhs/`（可用 `conf/config.ini` 对应平台段的 `profile_dir` 改），**每个平台只需扫码登录一次**，之后无头运行即可
- 首次使用或登录失效时，程序会等待最多 `wait_login_seconds` 秒（默认 180），期间在弹出的浏览器窗口里扫码即可
- 「最近一个月」会在翻到时间边界时提前停止，不必抓完全部作品
- 同一时刻只允许一个任务；抓取与下载在同一个任务里先后完成（下载阶段内部多地址并发）
- 未知 `aweme_type` 的作品会被跳过并在日志中提示，需要下载时把它加进 `conf/config.ini` 的 `[platforms.douyin]` 下 `image_types` / `video_types` 后重跑

### 小红书说明

- 主页首屏约 60 条由页面下发（不走接口），程序会先读首屏再监听后续分页，两者合并去重
- 列表只用于发现笔记；每一条都打开详情取完整数据，**下载与命名一律用详情**，不与列表数据合并
- 取不到详情（没有 `xsec_token`、详情页打不开）的笔记直接丢弃，不拿列表数据凑数
- 已下载过的笔记在抓取阶段就跳过，不打开详情页（按主页链接里的 `user_id` 反查 `aweme_id.txt`）
- 详情接口的响应一到就立刻返回，不按固定间隔轮询；页面加载超时取 `timeout_ms`，等响应最多再多等 3 秒
- 判定规则：图片全部带实况标记 → 按图片；有视频流且只剩 1 张图 → 按视频；多图带视频流是平台合成的轮播视频，仍按图片只下原图
- 图片会还原成无水印原图；HEIC 图片落盘后转成 JPG（需要 `pillow-heif`，未安装时保留原文件）
- 链接里的 `xsec_token` 会过期，过期后详情打不开，重新复制一次主页链接即可

所有配置均通过编辑 `conf/config.ini` 修改，重启程序后生效；界面不支持修改配置。

> 服务只监听 `127.0.0.1`，不对外网开放。

## 配置说明

所有可配置参数集中在 `conf/config.ini`，以 `#` 或 `;` 开头的行是注释。`conf/config.ini` 必须存在，缺失时程序会给出明确提示。修改配置后需重启程序生效。

```ini
[download]
download_dir = download/            # 下载根目录，不存在会自动创建；相对路径按启动时的工作目录解析
timeout = 30                        # 下载超时（秒）
max_retry = 3                       # 失败重试次数
workers = 4                         # 同时下载的 URL 数；1 表示逐个下载，过大会被 CDN 限流

[network]
verify_ssl = false                  # false 表示忽略证书校验

[network.header]
User-Agent = Mozilla/5.0 ...        # 浏览器标识
Accept = application/json, text/plain, */*

[web]
host = 127.0.0.1                    # 只应绑定本机
port = 5000                         # Web 服务端口，被占用时自动顺延
port_range = 10                     # 顺延查找的端口数量
title = 博主作品归档                # 浏览器标签标题

[platforms.default]
# 所有平台共用的抓取参数；平台段没写的项都从这里取
headless = true                     # 抓取时是否无头；首次登录需 false
profile_dir =                       # 浏览器数据目录，留空 = 项目根/.pw_profile_*
wait_login_seconds = 180            # 未登录时等待扫码的时间，0 表示不等
max_scroll = 400                    # 最大滚动次数
idle_rounds = 8                     # 连续多少次滚动无新增就停止
month_days = 30                     # 「最近一个月」对应的天数
browser_channel =                   # 留空用内置 Chromium，可填 msedge / chrome
proxy =                             # 代理地址，留空不使用
timeout_ms = 30000                  # 页面加载超时（毫秒）

[platforms.douyin]
referer = https://www.douyin.com/   # 下载请求的 Referer
image_types = 68                    # 判定为图片的 aweme type
video_types = 0,51,55,61,66         # 判定为视频的 aweme type

[platforms.xiaohongshu]
referer = https://www.xiaohongshu.com/   # 下载请求的 Referer
accept = */*                        # 图片 CDN 要求的 Accept
```

### 修改配置

直接编辑 `conf/config.ini` 中对应的项（如 `download_dir`、`[network.header]` 下的 `User-Agent`），重启后生效。

## 环境依赖

- **Python 3.8+**
- **Pillow**（Web 端缩略图生成）
- **Flask**（Web 服务）
- **Playwright**（一键抓取必需，未安装时抓取入口不可用）
- **pillow-heif**（可选，小红书 HEIC 图片转 JPG；未安装时保留原文件）

安装依赖：

```bash
pip install -r requirements.txt
playwright install chromium      # 一键抓取需要，下载浏览器内核
```

**标准库依赖**：`urllib`, `ssl`, `json`, `os`, `time`, `socket`, `sys`, `re`, `shutil`, `datetime`, `threading`, `hashlib`, `configparser`

## 项目架构

项目按「公用层 + 平台实现层」组织：公用层只写机制（任务、编排、存储、命名、浏览器、单文件下载、媒体），平台层写规则与下载流程（链接识别、列表采集、类型判定、取地址、命名落盘、进度上报）。

关键约定：**抓取结果不做统一转换**。`platform.scrape()` 返回平台自己的原始结构，`platform.download(result, store, ...)` 原样接收，由平台按自己的字段挑条目、取地址、命名。公用层不引入跨平台的中间数据结构，也就不会因为某个平台的字段差异反过来改核心代码。

新增平台只需补 `platforms/<平台>/` 三个文件（scraper / parser / download）并在 `platforms/__init__.py` 注册一行。

| 模块 | 职责 |
|------|------|
| `web_app.py` | 程序入口，寻找空闲端口并打开浏览器 |
| `conf/settings.py` | 配置读取（固定读 `conf/config.ini`，按平台分段） |
| `core/task.py` | 任务状态、进度、日志、取消、快照；百分比按 done/total 换算 |
| `core/pipeline.py` | 编排：认平台 → 抓取 → 定位存储 → `platform.download()` |
| `core/registry.py` | 平台注册表（key、显示名、链接识别） |
| `core/store.py` | 博主目录、去重记录、主页链接（无全局路径态） |
| `core/naming.py` | 时间格式与落盘命名 |
| `core/browser.py` | 通用浏览器能力（上下文、滚动、登录等待、响应解析） |
| `core/downloader.py` | 单个地址下载（重试、清理半成品、`.part` 扩展名探测）与批量并发调度 `fetch_all` |
| `platforms/base.py` | 平台接口约定（match / parse_target / scrape / download / headers / options） |
| `platforms/douyin/` | 抖音：链接解析、列表采集、类型判定、挑条目、取地址、命名落盘 |
| `platforms/xiaohongshu/` | 小红书：首屏 SSR + 接口分页、逐条取详情、图文/视频判定、去水印、HEIC 转 JPG、下载流程 |
| `media/` | 缩略图与视频封面缓存 |
| `web/app.py` | Flask 路由：任务、博主、封面上传 |
| `web/media_api.py` | 媒体响应（按区间读文件、缩略图、封面） |
| `static/` | 前端单页（原生 HTML/CSS/JS，轮询进度，博主按平台分组） |

### 下载流程

一次任务严格分两阶段，**先抓完再下载**：

1. `platform.scrape(target, options)` —— 平台开浏览器翻页，结果全量累加后一次性返回（抖音是接口 JSON 列表，小红书是逐条补齐的详情列表）
2. `platform.download(result, store)` —— 平台从自己返回的结构里挑出可下载条目，按 `{发布时间}.mp4` / `picture/{发布时间} - {序号}.webp` 落盘；一条里的全部媒体都成功才把 ID 写入 `aweme_id.txt`

并发只发生在第 2 步内部：平台先算出全部落盘路径，再把这些地址交给线程池（`[download] workers`，默认 4，1 表示逐个下载），结果回到主线程聚合，一条作品的媒体全部回来才计进度、才记 ID。

分工边界：

| 层 | 负责 |
|---|---|
| 平台 `download()` | 挑哪些条目、怎么取地址、怎么命名、何时记 ID、进度怎么报 |
| `core/downloader.py` | 把「一个 URL + 一个目标路径」拉成文件（重试与扩展名定名），并提供批量并发调度 |
| `core/task.py` | 收 `done` / `total` 换算百分比，维护日志、失败清单、取消状态 |
| `core/pipeline.py` | 串联两阶段，处理取消与「没有可下载内容」的收尾 |

进度只有一种粒度：**已处理条数 / 总条数**。单个文件下载过程不上报进度，一条作品的媒体全部回来时计数一次（并发下按完成顺序计数）。

## 注意事项

1. **文件编码**：`homepage.txt`、`aweme_id.txt` 均按 UTF-8 读写
2. **数据时效性**：抖音 API 数据具有时效性，建议获取后尽快下载
3. **登录状态**：确保抖音账号已登录，否则可能无法获取完整数据
4. **下载频率**：默认同时下载 4 个地址；被 CDN 限流时把 `[download] workers` 调小（1 即逐个下载），避免触发风控
5. **存储空间**：视频文件较大，请确保目标磁盘空间充足

## 免责声明

本工具仅供学习交流使用，请勿用于商业传播或任何盈利性活动。下载内容仅供个人学习研究使用，遵守抖音平台服务协议及相关法律法规。
