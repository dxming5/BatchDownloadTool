# 抖音 / 小红书 / 微博 内容批量下载工具

一款简洁高效的博主内容批量下载工具，支持抖音、小红书与微博，下载视频和图片并自动整理归类。

## 功能特性

- **浏览器图形界面**：本地启动 Web 服务，粘贴链接即可抓取并下载，无需命令行操作
- **多平台**：抖音、小红书与微博共用一套界面，博主列表按平台分组；新增平台只需补平台实现
- **一键抓取**：Web 页面粘贴博主主页链接，自动抓取作品列表并直接下载，无需 F12 复制 JSON
- **一键更新**：抓取过的博主会记住主页链接，之后点「更新」即可增量补全
- **自动分类**：视频和图片自动分类存储
- **断点续传**：按作品 ID 记录已下载内容（抖音 aweme_id / 小红书 note_id / 微博 mblog_id），一条的全部媒体成功才记录，避免重复下载
- **原始画质**：抖音图片保留原始 WebP，小红书图片还原无水印原图，微博图片取 `large` 原图、视频取最高可用清晰度
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
│   ├── xiaohongshu/
│   │   ├── scraper.py         # 首屏 SSR + 接口分页 + 逐条取详情
│   │   ├── parser.py          # 图文/视频判定、去水印、选码率
│   │   └── download.py        # 挑条目、取地址、命名、落盘、HEIC 转 JPG
│   └── weibo/
│       ├── scraper.py         # Playwright 扫码登录 + 卡片接口 since_id 翻页
│       ├── parser.py          # 链接取 uid、时间解析、类型判定、pics → large.url / page_info → 视频地址
│       └── download.py        # 挑条目（图片/视频）、并发下载、按篇报进度、按微博 ID 去重
├── media/                     # 缩略图与视频封面缓存
├── web/
│   ├── app.py                 # 路由
│   └── media_api.py           # 媒体响应（按区间读文件）
├── static/                    # 前端单页（HTML/CSS/JS，无需构建）
│   ├── index.html
│   ├── app.js
│   └── style.css
├── .pw_profile/               # 浏览器数据目录根（保存登录态，自动生成）
│   ├── douyin/                # 抖音的浏览器数据目录
│   ├── xiaohongshu/           # 小红书的浏览器数据目录
│   └── weibo/                 # 微博的浏览器数据目录（新增平台同样在下面建目录）
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
│   ├── xiaohongshu/           # 结构同上，图片可能是 jpg/webp/avif/gif
│   └── weibo/                 # 结构同上，图片为 large 原图，命名 {时间} - {序号}.jpg
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

1. **下载**：粘贴博主主页链接，程序按域名自动识别平台，选择「全部作品」或「最近一个月」，点「抓取并下载」。输入框支持一次粘贴多个链接（每行一个，也可用空格/逗号分隔，自动去重），多条时按顺序逐个抓取下载，单条失败不影响后续。支持三种链接：
   - 抖音：`https://www.douyin.com/user/MS4w...`（也接受裸 `sec_user_id`）
   - 小红书：`https://www.xiaohongshu.com/user/profile/<24位id>?xsec_token=...`（**建议带上 xsec_token**，否则可能看不到内容）
   - 微博：`https://m.weibo.cn/u/<uid>`（也接受 `https://weibo.com/u/<uid>`、卡片接口地址与裸 uid）

   抓取过程会用 Playwright 打开真实浏览器，抓到后立即下载；进度条与日志实时显示「抓取中 · 第 N 页 · 累计 M 条」以及下载进度，同时提供失败列表（可一键复制失败 URL），任务可随时取消
2. **博主管理**：按平台分组列出下载目录里的博主，点击某一行即可在右侧预览该博主的作品（默认「图片」，可在预览顶部切到「视频」，并支持手动刷新），行内提供一键更新与删除；右上角「更新」按钮按下拉框所选平台批量更新全部博主（选「全部平台」时逐个平台跑），串行执行、可随时取消，单个博主失败或没有新增内容都不影响后续

### 一键更新

抓取成功后，程序会把该博主的主页链接写入 `{下载根目录}/{平台目录}/{博主名}/homepage.txt`。之后在「博主管理」里点该博主的「更新」，就会直接读这个链接重新抓取，不用再粘贴：

- 更新按「全部作品」抓取，能补上历史漏项
- 已下载的作品由 `aweme_id.txt` 里的作品 ID（抖音 aweme_id / 小红书 note_id / 微博 mblog_id）跳过，只下载新增内容
- 小红书会在**抓取阶段**就排除已下载的笔记（连详情页都不打开）；抖音与微博由下载层在下载阶段按篇 ID 统一跳过
- 进度条按**实际媒体文件数**统计，每个图片 / 视频下载完成即前进；已下载的篇由下载层排除、不计入总数，进度从 0 开始
- 目录里没有 `homepage.txt`（例如手动拷入的目录）时按钮为禁用状态，重新粘贴一次链接抓取即可生成

### 抓取说明

- 登录态按平台隔离：统一放在 `.pw_profile/` 下的平台子目录——抖音在 `.pw_profile/douyin/`、小红书在 `.pw_profile/xiaohongshu/`、微博在 `.pw_profile/weibo/`（可用 `conf/config.ini` 对应平台段的 `profile_dir` 改），**每个平台只需扫码登录一次**，之后无头运行即可
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

### 微博说明

- **图片**：取 `mblog.pics[].large.url`（缺失时回退 `bmiddle` / `thumbnail` / `url`），落成 `picture/{发布时间} - {序号}.{扩展名}`。微博的图片水印与分辨率实测不可兼得：`large` / `original` 是同一份 4032x2688 原图但带博主角标，`oslarge` / `osj1080` 无水印但最长边固定 690（图床忽略自定义尺寸参数，`mw2000`、`orj1080` 也都带角标），因此固定取 `large`，保住最高画质
- **视频**：按 `page_info` 取一个清晰度落成 `{发布时间}.mp4`。视频角标（@昵称 + 微博视频）是微博转码时烧录进画面的，所有直链都带，无法通过改地址去水印；要干净原片只能走作者开启下载权限时的 App「保存到相册」，或视频本身来自外链时去原平台取
- **视频取址**：先看 `page_info.urls`（`mp4_1080p_mp4` → `mp4_720p_mp4` → `mp4_hd_mp4` → `mp4_hd_url`），再按 `quality_level` 取 `media_info.playback_list` 里的地址，最后退到 `page_info.media_info.stream_url_hd` → `stream_url` → 标清；一条视频只下一个文件，`m3u8` 播放列表直接忽略（落盘只会得到文本）
- **类型判定**：微博没有抖音那种单一 `aweme_type`，按字段组合判断——`page_info.type` / `object_type` 为 `video`（或带 `media_info`）算视频，且先于图片判断（视频微博的 `pics` 只是封面图）；`pics` 非空算图片；两者都没有的（纯文字、头条文章、转发他人内容）跳过，并在日志里汇总提示跳过几篇
- 不下载 Live 图与转发微博里的图片 / 视频
- 抓取不靠滚动页面：登录后直接请求 `m.weibo.cn/api/container/getIndex` 卡片接口，按 `since_id` 翻页，每页间隔 1~2 秒；`card_type != 9`（分组、广告、推荐）自动跳过
- 首次使用需在弹出的浏览器窗口扫码登录（`headless` 必须为 `false`）；登录态存进 `.pw_profile/weibo/`，之后可无头运行
- 登录态以 `m.weibo.cn/api/config` 的 `data.login` 为准：匿名访客的 Cookie 里同样有 `SUB`/`SUBP`，只看 Cookie 名会把游客当已登录，匿名态只能抓到首页的一小部分微博（表现为「下载不全」），因此未登录会直接中止并提示扫码
- Cookie 失效或触发风控时接口返回 `ok=0`，程序会直接报错提示重新登录，不会静默返回「没有内容」；但翻到最后一页时接口同样返回 `ok=0`（`msg` 为「这里还没有内容」），这属于正常的结束信号，按抓取完成处理、不报错；抓完第一页还会复核一次登录态，中途掉登录同样中止，不下载残缺数据
- 进度按微博条数统计：一条微博的图片 / 视频全部处理完才走 1 篇，与抖音、小红书一致
- 去重按微博 ID（`mblog_id`）：一条微博的全部媒体成功才记录，任一个失败则整条不记录，下次整条重下，与抖音、小红书一致
- 翻页上限复用 `[platforms.default]` 的 `max_scroll`（微博按「翻页次数」使用，每页约 10 条），防止接口异常时死循环；选「最近一个月」时翻到时间边界会提前停止

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
profile_dir =                       # 浏览器数据目录，留空 = 项目根/.pw_profile/<平台 key>
wait_login_seconds = 180            # 未登录时等待扫码的时间，0 表示不等
max_scroll = 400                    # 最大滚动次数（微博按翻页次数使用）
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

[platforms.weibo]
referer = https://m.weibo.cn/       # 图片与视频下载的 Referer（403 时可改 https://weibo.com/）
# 翻页上限复用 [platforms.default] 的 max_scroll（微博按页数计，每页约 10 条）
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

**标准库依赖**：`urllib`, `ssl`, `json`, `os`, `time`, `random`, `socket`, `sys`, `re`, `shutil`, `datetime`, `threading`, `hashlib`, `configparser`

## 项目架构

项目按「公用层 + 平台实现层」组织：公用层只写机制（任务、编排、存储、命名、浏览器、单文件下载、媒体），平台层写规则与下载流程（链接识别、列表采集、类型判定、取地址、命名落盘、进度上报）。

关键约定：**抓取结果不做统一转换**。`platform.scrape()` 返回平台自己的原始结构，`platform.download(result, store, ...)` 原样接收，由平台按自己的字段挑条目、取地址、命名。公用层不引入跨平台的中间数据结构，也就不会因为某个平台的字段差异反过来改核心代码。

新增平台只需补 `platforms/<平台>/` 三个文件（scraper / parser / download）并在 `platforms/__init__.py` 注册一行；下载进度必须按「篇」上报、ID 按篇记录、日志固定为 `下载 {发布时间}`，见下节「进度约定」「ID 记录约定」与「下载日志约定」。

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
| `platforms/weibo/` | 微博：链接取 uid、扫码登录、卡片接口 `since_id` 翻页、类型判定（图片/视频）、`pics → large.url` 与 `page_info → 视频地址` 取址、按篇报进度、按微博 ID 去重与下载流程 |
| `media/` | 缩略图与视频封面缓存 |
| `web/app.py` | Flask 路由：任务、博主、封面上传 |
| `web/media_api.py` | 媒体响应（按区间读文件、缩略图、封面） |
| `static/` | 前端单页（原生 HTML/CSS/JS，轮询进度，博主按平台分组） |

### 下载流程

一次任务严格分两阶段，**先抓完再下载**：

1. `platform.scrape(target, options)` —— 平台开浏览器翻页，结果全量累加后一次性返回（抖音是接口 JSON 列表，小红书是逐条补齐的详情列表，微博是卡片接口按页累积的 mblog 列表）
2. `platform.download(result, store)` —— 平台从自己返回的结构里挑出可下载条目，按 `{发布时间}.mp4` / `picture/{发布时间} - {序号}.webp`（微博为 `.jpg`）落盘；每个文件下载完成即计 1 个进度，全部成功才把篇 ID 写入 `aweme_id.txt`（抖音 aweme_id / 小红书 note_id / 微博 mblog_id）

并发只发生在第 2 步内部：平台先算出全部落盘路径，再把这些地址交给线程池（`[download] workers`，默认 4，1 表示逐个下载），每个文件完成即计 1 个进度，结果回到主线程按篇聚合，全成功才记 ID。

分工边界：

| 层 | 负责 |
|---|---|
| 平台 `download()` | 挑哪些条目、取地址、算发布时间、何时记 ID；把 `(篇 ID, 发布时间, [地址])` 交给下载层 |
| `core/downloader.py` | 校验已下载、命名落盘（按扩展名分目录）、重试与扩展名探测、批量并发调度、按实际地址数统一计算进度 |
| `core/task.py` | 收 `done` / `total` 换算百分比，维护日志、失败清单、取消状态 |
| `core/pipeline.py` | 串联两阶段，处理取消与「没有可下载内容」的收尾 |

#### 进度约定：按实际媒体地址数计

下载进度按**实际媒体地址**（每个图片 / 视频文件）计，由 `core/downloader.py` 的 `fetch_all` 统一计算并上报：

- `fetch_all` 以本批**实际要下载**的地址数为分母，每个地址下载完成即 +1，通过不带 message 的 `notice` 事件上报 `done` / `total`，界面显示如 `60%（6/10）`；日志只有每地址一条「正在下载 xxx」
- 已下载的篇由 `fetch_all` 按 `done_ids` 统一校验、直接排除，不计入总数；进度从 0 开始，不需要平台自己判断
- 一篇内的多个媒体各自计进度；去重与断点续传仍按篇粒度：一个下载单元一个 ID（抖音 aweme_id / 小红书 note_id / 微博 mblog_id），ID 由平台按篇聚合后写入
- **新增平台必须遵守**：把条目整理成 `(篇 ID, 发布时间, [地址])` 交给 `downloader.fetch_all`，传 `blogger_dir`、`done_ids` 与 `on_done` 回调；命名、扩展名探测、去重校验都由下载层负责，平台只管按篇聚合结果记 ID，不要自己维护 `done` / `total`

#### ID 记录约定：一篇一个 ID，全部成功才记录

断点续传与去重共用一个粒度：`{博主目录}/aweme_id.txt` 里一行一个篇 ID（抖音 aweme_id / 小红书 note_id / 微博 mblog_id），与进度完全对应。

- 一篇内的全部媒体都下载成功，才在篇完成时写入该篇 ID；任一张失败或没有可用地址，整篇都不记录
- 下载前先把记录读成内存 `set`，交给 `fetch_all` 统一校验；篇 ID 已存在就整篇排除（不计入进度总数）
- 上次部分失败的篇不会留下半篇记录，下次整篇重下（同名文件覆盖），保证「有记录 = 这一篇完整」
- ID 攒够 20 个批量追加一次，任务结束再补写一次；读取时忽略空行，不依赖文件顺序
- **新增平台必须遵守**：一篇一个 ID、全部成功才记录，不要按单张图片或单个 URL 记录

#### 下载日志约定：每地址一条「正在下载 xxx」

下载阶段的日志由 `core/downloader.py` 统一输出，每个地址开始下载时打一条 `正在下载 {临时文件名}`：

- 任务进入下载阶段时，由公用层统一打印 `开始下载 {平台显示名} <{博主名}>`（`core/task.py` 收到 `stage=start` 后输出），平台实现不用自己打
- 临时文件名形如 `2026-01-01 10.00.00 - 1.part`——下载时扩展名未定，先落 `.part`，完成后再按扩展名决定落盘目录与最终文件名；所以日志里的名字带 `.part`，与最终文件名不同
- 一篇里有几个媒体就打几条，按地址粒度；跳过或取消的地址不打这条
- 新增平台不用自己打下载日志，交给 `fetch_all` 的 `log` 回调即可

## 注意事项

1. **文件编码**：`homepage.txt`、`aweme_id.txt` 均按 UTF-8 读写
2. **数据时效性**：抖音 API 数据具有时效性，建议获取后尽快下载
3. **登录状态**：确保对应平台账号已登录（微博首次需在自动弹出的窗口里扫码），否则可能无法获取完整数据
4. **下载频率**：默认同时下载 4 个地址；被 CDN 限流时把 `[download] workers` 调小（1 即逐个下载），避免触发风控
5. **存储空间**：视频文件较大，请确保目标磁盘空间充足

## 免责声明

本工具仅供学习交流使用，请勿用于商业传播或任何盈利性活动。下载内容仅供个人学习研究使用，遵守抖音平台服务协议及相关法律法规。
