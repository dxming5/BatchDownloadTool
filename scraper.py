# -*- coding: utf-8 -*-
"""
抖音博主主页作品抓取模块（Playwright）

设计说明：
    1. 既能当独立脚本跑（python scraper.py <链接>），也能被 web 层当库调用；
    2. 不手工构造 a_bogus / msToken / uifid / timestamp——全部交给真实浏览器生成；
    3. 通过监听 /aweme/v1/web/aweme/post/ 接口的响应体拿到原始 JSON，
       滚动到底部触发翻页，直到接口返回 has_more = 0 为止；
    4. 产物（CLI 模式下写入 ./scrape_output/<博主昵称>/）：
        - aweme_pages.jsonl  每行一页原始接口响应（可直接喂给 url_parser.parse_aweme）
        - aweme_all.json     按 aweme_id 去重后的作品汇总

用法：
    python scraper.py MS4wLjABAAAAtSF9kjnZ-wkMSYE7L9mllkDC4P3JPGeHIkz0BPejGO0
    python scraper.py https://www.douyin.com/user/MS4wLjABAAAA...
    python scraper.py <target> --headless --max-scroll 300

    作为库调用：
        result = fetch_user_awemes(sec_user_id, since_ts=截止时间戳,
                                   on_progress=回调, should_stop=取消检查)
        lines = result_to_lines(result)   # 交给 downloader.download(raw_lines=lines)

首次运行会弹出浏览器，请在其中完成登录/滑块验证，脚本会自动等待首屏数据。
"""

import argparse
import json
import os
import re
import sys
import time

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0'
)
API_MARK = '/aweme/v1/web/aweme/post/'
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PROFILE = os.path.join(BASE_DIR, '.pw_profile')
DEFAULT_OUTPUT = os.path.join(BASE_DIR, 'scrape_output')

# 抖音用户页的作品列表节点随版本变化，按优先级探测
LIST_SELECTORS = (
    '[data-e2e="user-post-item-list"]',
    '[data-e2e="user-post-list"]',
    '[data-e2e="scroll-list"]',
)

# 抖音用户页的滚动容器层级随版本变化（window 本身不可滚），
# 因此对所有可滚动容器统一推进；已到底的先回弹一次，重新触发懒加载观察器
SCROLL_JS = """
(step) => {
  const targets = [];
  document.querySelectorAll('div').forEach((node) => {
    const style = getComputedStyle(node);
    if (style.overflowY !== 'auto' && style.overflowY !== 'scroll') return;
    if (node.scrollHeight - node.clientHeight > 80) targets.push(node);
  });
  let moved = 0;
  for (const node of targets) {
    const before = node.scrollTop;
    if (before + node.clientHeight >= node.scrollHeight - 5) {
      node.scrollTop = Math.max(0, before - 320);
    }
    node.scrollTop = Math.min(node.scrollHeight, node.scrollTop + step);
    if (node.scrollTop !== before) moved += 1;
  }
  window.scrollBy(0, step);
  return {targets: targets.length, moved: moved};
}
"""

# 只要能读到这些 Cookie 之一，就说明浏览器里已有登录态
# （sessionid 是 HttpOnly，document.cookie 读不到，必须走 context.cookies()）
LOGIN_COOKIE_NAMES = ('sessionid', 'sessionid_ss', 'sid_tt')

# 抓取范围早停阈值：连续这么多条作品都早于截止时间，就不再往下翻
DEFAULT_STALE_LIMIT = 12


def configure_encoding():
    """修正 Windows 控制台中文输出"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8')
        except (AttributeError, ValueError, OSError):
            pass


def parse_sec_user_id(raw):
    """
    从用户主页 URL 或 sec_user_id 中提取 sec_user_id

    Args:
        raw: 主页 URL / 带 sec_user_id 参数的接口 URL / sec_user_id 本体

    Returns:
        str: sec_user_id
    """
    # 兼容 PowerShell 管道/复制粘贴带来的 BOM、零宽空格与首尾引号
    raw = (raw or '').replace('\ufeff', '').replace('\u200b', '')
    raw = raw.strip().strip('"').strip("'")
    if not raw:
        raise ValueError('缺少 sec_user_id 或 用户主页 URL')

    match = re.search(r'/user/([^/?#]+)', raw)
    if match:
        return match.group(1)

    match = re.search(r'sec_user_id=([^&#]+)', raw)
    if match:
        return match.group(1)

    if re.match(r'^MS4wLjAB[\w\-]+$', raw):
        return raw

    raise ValueError('无法从内容中识别 sec_user_id：{}'.format(raw[:80]))


def _scroll_step(page):
    """
    向下滚动一步：先在作品列表上发真实滚轮事件，再推进所有可滚动容器

    Returns:
        dict: {'targets': 可滚动容器数, 'moved': 实际发生位移的容器数}; 失败返回 None
    """
    box = None
    for selector in LIST_SELECTORS:
        try:
            handle = page.query_selector(selector)
            if handle:
                box = handle.bounding_box()
                break
        except Exception:
            box = None

    try:
        if box:
            page.mouse.move(box['x'] + box['width'] / 2,
                            box['y'] + min(box['height'] / 2, 300))
        else:
            page.mouse.move(720, 500)
        page.mouse.wheel(0, 2400)
    except Exception:
        pass

    try:
        return page.evaluate(SCROLL_JS, 2000)
    except Exception:
        return None


def login_state(page):
    """
    判断当前页面的登录态

    Returns:
        tuple: (是否已登录, 是否有登录弹窗)
    """
    # sessionid 是 HttpOnly，document.cookie 读不到，必须用 Playwright 的 Cookie API
    names = set()
    try:
        for cookie in page.context.cookies():
            if cookie.get('value'):
                names.add(cookie.get('name'))
    except Exception:
        names = set()
    logged = bool(names & set(LOGIN_COOKIE_NAMES))
    try:
        modal = page.evaluate(
            '() => !!document.querySelector(\'[data-e2e="login-modal"],'
            ' [data-e2e="login-panel"], .login-mask, #login-panel-new\')')
    except Exception:
        modal = False
    return logged, modal


def _launch_context(playwright, profile_dir, headless, channel, proxy, viewport):
    """启动持久化上下文；指定通道不可用时回退到内置 Chromium"""
    kwargs = {
        'headless': headless,
        'viewport': viewport,
        'locale': 'zh-CN',
        'user_agent': USER_AGENT,
        'ignore_default_args': ['--enable-automation'],
        'args': ['--disable-blink-features=AutomationControlled'],
    }
    if proxy:
        kwargs['proxy'] = {'server': proxy}

    def _create(use_channel):
        options = dict(kwargs)
        if use_channel:
            options['channel'] = use_channel
        return playwright.chromium.launch_persistent_context(profile_dir, **options)

    try:
        return _create(channel)
    except Exception as error:
        if not channel:
            raise
        print('  浏览器通道 {} 不可用（{}），回退内置 Chromium'.format(channel, error))
        return _create(None)


def cookie_count(page):
    """当前窗口的 Cookie 数量，用于判断登录动作是否发生在这个窗口里"""
    try:
        return len(page.context.cookies())
    except Exception:
        return -1


def wait_for_login(page, seconds, poll_ms=2000, should_stop=None,
                   on_progress=None, verbose=True):
    """
    未登录时阻塞等待人工登录（抖音匿名态只放首屏一页，必须登录才能翻页）

    Args:
        page:        页面对象
        seconds:     最长等待秒数
        poll_ms:     轮询间隔（毫秒）
        should_stop: 取消检查函数，返回 True 时立刻结束等待
        on_progress: 进度回调，接收 {'stage': 'scrape', 'message': ...}
        verbose:     是否打印到控制台

    Returns:
        bool: 是否已登录
    """
    def say(text):
        if verbose:
            print('  ' + text)
        if on_progress:
            try:
                on_progress({'stage': 'scrape', 'message': text})
            except Exception:
                pass

    try:
        logged, modal = login_state(page)
    except Exception:
        return False
    if logged:
        return True

    say('检测到未登录（{}），最多等待 {} 秒'.format(
        '登录弹窗已出现' if modal else 'Cookie 里没有 sessionid', seconds))
    say('注意：必须在这个自动弹出的浏览器窗口里登录，你平时用的 Edge/Chrome 登录无效')
    waited = 0
    while waited < seconds * 1000:
        if should_stop and should_stop():
            say('等待登录已取消')
            return False
        try:
            page.wait_for_timeout(poll_ms)
        except Exception:
            return False
        waited += poll_ms
        try:
            logged, _ = login_state(page)
        except Exception:
            return False
        if logged:
            say('登录成功，继续抓取')
            return True
        if waited % 20000 == 0:
            say('已等待 {}/{} 秒（当前窗口 Cookie {} 个，sessionid 仍未出现）…'.format(
                waited // 1000, seconds, cookie_count(page)))
    say('等待超时，按未登录状态继续（最多只能拿到首屏约一页作品）')
    return False


def fetch_user_awemes(sec_user_id, profile_dir=DEFAULT_PROFILE, headless=False,
                      max_scroll=400, idle_rounds=8, timeout_ms=30000,
                      channel=None, proxy=None, wait_login=180,
                      viewport=None, since_ts=None,
                      stale_limit=DEFAULT_STALE_LIMIT, on_progress=None,
                      should_stop=None, verbose=True):
    """
    抓取某博主的主页作品

    Args:
        sec_user_id: 博主 sec_user_id（必填）
        profile_dir: 浏览器用户数据目录，保留登录态（复用后无需重复登录）
        headless:    是否无头运行；首次登录/过验证请用 False
        max_scroll:  最大滚动次数，防止死循环
        idle_rounds: 连续多少次滚动没有新作品就停止
        timeout_ms:  页面加载/接口等待超时
        channel:     浏览器通道，如 'msedge' / 'chrome'；None 用内置 Chromium
        proxy:       代理地址，如 'http://127.0.0.1:7890'
        wait_login:  未登录时等待人工登录的秒数
        viewport:    视口尺寸字典
        since_ts:    只保留 create_time >= since_ts 的作品；连续 stale_limit 条
                     都早于它时提前停止翻页（列表近似按时间倒序）
        stale_limit: 早停阈值，避免为增量抓取翻几百页
        on_progress: 进度回调，接收 {'stage': 'scrape', 'pages': n,
                     'count': m, 'message': '...'}
        should_stop: 取消检查函数，返回 True 时尽快结束（保留已抓部分）
        verbose:     是否打印到控制台；作为库调用时应传 False

    Returns:
        dict: {
            'sec_user_id': str, 'nickname': str, 'user_url': str,
            'pages': [接口响应 dict, ...],
            'aweme_list': [去重后的作品 dict, ...],
            'has_more': int, 'restricted': bool, 'cancelled': bool,
            'since_ts': int|None, 'fetch_time': str
        }
    """
    from playwright.sync_api import sync_playwright

    user_url = 'https://www.douyin.com/user/{}'.format(sec_user_id)
    pages = []
    pending = []
    awemes = []
    seen_ids = set()
    last_page = {'has_more': 1}
    page_index = 0
    stale_run = 0
    early_stop = False

    def log(text):
        if verbose:
            print('  ' + text)

    def emit(event):
        """安全地上报抓取进度，回调异常不影响抓取"""
        if on_progress:
            try:
                on_progress(event)
            except Exception:
                pass

    def cancelled():
        return bool(should_stop and should_stop())

    def on_response(response):
        """只记录目标接口的响应对象，解析留给主循环，避免在回调里做阻塞调用"""
        if API_MARK in response.url and response.status == 200:
            pending.append(response)

    def drain():
        """把已抓到的响应解析入队，并按 since_ts 过滤与判断早停"""
        nonlocal last_page, page_index, stale_run, early_stop
        while pending:
            response = pending.pop(0)
            payload = None
            try:
                payload = response.json()
            except Exception:
                try:
                    payload = json.loads(response.text() or '{}')
                except Exception:
                    payload = None
            if not isinstance(payload, dict) or 'aweme_list' not in payload:
                continue

            raw_list = payload.get('aweme_list') or []
            kept = []
            for aweme in raw_list:
                create_time = aweme.get('create_time') or 0
                if since_ts and create_time and create_time < since_ts:
                    stale_run += 1
                    if stale_run >= stale_limit:
                        early_stop = True
                    continue
                stale_run = 0
                aweme_id = aweme.get('aweme_id')
                if aweme_id and aweme_id in seen_ids:
                    continue
                if aweme_id:
                    seen_ids.add(aweme_id)
                kept.append(aweme)

            awemes.extend(kept)
            if kept or not since_ts:
                if since_ts:
                    # 只把范围内的作品留在分页数据里，避免下游解析到过期作品
                    page_payload = dict(payload)
                    page_payload['aweme_list'] = kept
                    pages.append(page_payload)
                else:
                    pages.append(payload)
            page_index += 1
            last_page = payload

            message = '第 {} 页：本页 {} 条，累计 {} 条，has_more={}'.format(
                page_index, len(raw_list), len(awemes), payload.get('has_more'))
            log(message)
            emit({'stage': 'scrape', 'pages': page_index,
                  'count': len(awemes), 'message': message})

    stop_reason = 'max_scroll'
    restricted = False
    nickname = ''
    stopped = False
    emit({'stage': 'scrape', 'pages': 0, 'count': 0, 'message': '正在启动浏览器…'})
    with sync_playwright() as playwright:
        context = _launch_context(playwright, profile_dir, headless, channel, proxy,
                                 viewport or {'width': 1440, 'height': 900})
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.on('response', on_response)
            page.goto(user_url, wait_until='domcontentloaded', timeout=timeout_ms)
            emit({'stage': 'scrape', 'pages': 0, 'count': 0, 'message': '已打开主页，等待作品列表…'})

            # 等待首屏接口数据；期间可人工登录、拖滑块
            waited = 0
            while not pages and waited < wait_login * 1000:
                if cancelled():
                    stop_reason = '用户取消'
                    stopped = True
                    break
                page.wait_for_timeout(1000)
                waited += 1000
                drain()
                if waited % 10000 == 0:
                    logged, modal = login_state(page)
                    if modal or not logged:
                        log('已等待 {} 秒：检测到未登录（{}），请在浏览器窗口完成登录'.format(
                            waited // 1000, '登录弹窗已出现' if modal else '无 sessionid'))
                    else:
                        log('已等待 {} 秒：已登录，继续等待接口返回'.format(waited // 1000))
            if not pages and not stopped:
                raise RuntimeError(
                    '未捕获到 {} 的响应：请确认已登录、已通过验证，或该主页确实无作品'.format(API_MARK))

            # 匿名态抖音只给首屏一页：先等人工登录，再开始翻页
            logged_in = True
            if not stopped and last_page.get('has_more') != 0 and not early_stop:
                logged_in = wait_for_login(page, wait_login, should_stop=should_stop,
                                          on_progress=on_progress, verbose=verbose)
                if logged_in and not cancelled():
                    # 登录后重新加载主页，让列表按已登录身份重新拉取
                    before = len(pages)
                    page.reload(wait_until='domcontentloaded')
                    waited = 0
                    while len(pages) == before and waited < 20000:
                        if cancelled():
                            break
                        page.wait_for_timeout(1000)
                        waited += 1000
                        drain()

            # 滚动翻页，直到 has_more=0、到达时间边界、连续无新增或被取消
            last_total = len(awemes)
            idle = 0
            for index in range(max_scroll):
                if cancelled():
                    stop_reason = '用户取消'
                    stopped = True
                    break
                if early_stop:
                    stop_reason = '已到时间范围边界'
                    break
                if last_page.get('has_more') == 0:
                    stop_reason = 'has_more=0'
                    break
                info = _scroll_step(page)
                page.wait_for_timeout(1000)
                drain()
                if len(awemes) > last_total:
                    last_total = len(awemes)
                    idle = 0
                else:
                    idle += 1
                    if idle % 3 == 0:
                        log('连续 {} 次无新增（滚动容器 {} 个，位移 {} 个）'.format(
                            idle, (info or {}).get('targets', 0), (info or {}).get('moved', 0)))
                    if idle >= idle_rounds:
                        stop_reason = '连续 {} 次无新增'.format(idle)
                        break
            else:
                stop_reason = '达到 max_scroll 上限'
            if not stopped and not early_stop and last_page.get('has_more') == 0:
                stop_reason = 'has_more=0（已到最后一页）'

            nickname = ''
            for aweme in awemes:
                nickname = (aweme.get('author') or {}).get('nickname') or ''
                if nickname:
                    break
            if not nickname:
                try:
                    nickname = page.title().replace(' - 抖音', '').strip()
                except Exception:
                    nickname = ''

            logged, modal = login_state(page)
            restricted = (not logged_in) or (not logged) or modal
        finally:
            try:
                context.close()
            except Exception:
                pass

    log('抓取结束：{}（{} 条）'.format(stop_reason, len(awemes)))
    if restricted and last_page.get('has_more') != 0:
        log('提示：当前未登录，抖音只放出首屏约一页作品；'
            '请在浏览器窗口扫码登录后重试（登录态保存在 {}）'.format(profile_dir))
    emit({'stage': 'scrape_done', 'pages': page_index, 'count': len(awemes),
          'message': '抓取结束：{}，共 {} 条'.format(stop_reason, len(awemes))})
    return {
        'sec_user_id': sec_user_id,
        'user_url': user_url,
        'nickname': nickname,
        'pages': pages,
        'aweme_list': awemes,
        'has_more': last_page.get('has_more'),
        'restricted': restricted,
        'cancelled': stopped,
        'since_ts': since_ts,
        'fetch_time': time.strftime('%Y-%m-%d %H:%M:%S'),
    }


def result_to_lines(result):
    """
    把抓取结果转成「每行一条接口 Response」的字符串列表

    该格式正是 url_parser.parse_aweme / downloader.download(raw_lines=...) 的输入，
    因此抓取与既有的下载链路可以零改动对接。
    """
    return [json.dumps(payload, ensure_ascii=False)
            for payload in result.get('pages') or []]


def safe_name(name):
    """剔除 Windows 非法文件夹字符"""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', '', name or '')
    return name.strip().rstrip('.').strip()


def save_result(result, out_root=DEFAULT_OUTPUT):
    """
    落盘：aweme_pages.jsonl（每行一页原始响应）+ aweme_all.json（去重汇总）

    Returns:
        tuple: (输出目录, 两个文件的路径)
    """
    folder_name = safe_name(result.get('nickname')) or result['sec_user_id']
    folder = os.path.join(out_root, folder_name)
    if not os.path.isdir(folder):
        os.makedirs(folder)

    pages_path = os.path.join(folder, 'aweme_pages.jsonl')
    with open(pages_path, mode='w', encoding='utf-8') as file_handle:
        for payload in result['pages']:
            file_handle.write(json.dumps(payload, ensure_ascii=False) + '\n')

    all_path = os.path.join(folder, 'aweme_all.json')
    with open(all_path, mode='w', encoding='utf-8') as file_handle:
        json.dump({
            'sec_user_id': result['sec_user_id'],
            'user_url': result['user_url'],
            'nickname': result['nickname'],
            'fetch_time': result['fetch_time'],
            'has_more': result['has_more'],
            'total': len(result['aweme_list']),
            'aweme_list': result['aweme_list'],
        }, file_handle, ensure_ascii=False, indent=2)
    return folder, pages_path, all_path


def print_summary(result):
    """打印作品类型分布与时间范围，便于判断是否抓全"""
    aweme_list = result['aweme_list']
    type_counter = {}
    for aweme in aweme_list:
        key = aweme.get('aweme_type')
        type_counter[key] = type_counter.get(key, 0) + 1
    times = [aweme['create_time'] for aweme in aweme_list if aweme.get('create_time')]

    print('  博主：{}'.format(result['nickname'] or '(未知)'))
    print('  主页：{}'.format(result['user_url']))
    print('  页数：{}，作品总数：{}，aweme_type 分布：{}'.format(
        len(result['pages']), len(aweme_list), type_counter))
    if times:
        print('  时间范围：{} ~ {}'.format(
            time.strftime('%Y-%m-%d', time.localtime(min(times))),
            time.strftime('%Y-%m-%d', time.localtime(max(times)))))


def build_parser():
    """命令行参数"""
    parser = argparse.ArgumentParser(
        description='抓取抖音博主主页全部作品（Playwright，独立脚本）')
    parser.add_argument('target', nargs='?', default=None,
                        help='用户主页 URL 或 sec_user_id（省略则交互式输入）')
    parser.add_argument('--headless', action='store_true', help='无头运行（登录态已缓存后可用）')
    parser.add_argument('--profile', default=DEFAULT_PROFILE, help='浏览器用户数据目录')
    parser.add_argument('--out', default=DEFAULT_OUTPUT, help='产物输出目录')
    parser.add_argument('--browser', default=None,
                        help='浏览器通道：msedge / chrome；留空用内置 Chromium')
    parser.add_argument('--proxy', default=None, help='代理，如 http://127.0.0.1:7890')
    parser.add_argument('--max-scroll', type=int, default=400, help='最大滚动次数')
    parser.add_argument('--idle-rounds', type=int, default=8,
                        help='连续多少次滚动无新增即停止')
    parser.add_argument('--timeout', type=int, default=30000, help='页面加载超时（毫秒）')
    parser.add_argument('--wait-login', type=int, default=180,
                        help='未登录时等待人工扫码登录的秒数；设 0 表示不等（默认 180）')
    parser.add_argument('--no-save', action='store_true', help='只抓取不落盘')
    return parser


def ask_target():
    """未提供命令行参数时，交互式读取目标"""
    print('  请输入博主主页链接或 sec_user_id（直接回车退出）：')
    while True:
        try:
            raw = input('  > ').replace('\ufeff', '').replace('\u200b', '')
            raw = raw.strip().strip('"').strip("'")
        except (EOFError, KeyboardInterrupt):
            return ''
        # 纯不可见字符（BOM/管道乱码/空白）一律视为退出
        if not re.search(r'[0-9A-Za-z]', raw):
            return ''
        try:
            return parse_sec_user_id(raw)
        except ValueError:
            print('  识别不出 sec_user_id，请粘贴完整主页链接，例如 '
                  'https://www.douyin.com/user/MS4wLjABAAAA...')


def main():
    """脚本入口"""
    configure_encoding()
    args = build_parser().parse_args()
    if args.target:
        try:
            sec_user_id = parse_sec_user_id(args.target)
        except ValueError as error:
            print('  参数错误：{}'.format(error))
            return 2
    else:
        sec_user_id = ask_target()
        if not sec_user_id:
            print('  未提供目标，已退出')
            return 2

    print('  目标 sec_user_id：{}'.format(sec_user_id))
    print('  浏览器数据目录：{}'.format(args.profile))
    try:
        result = fetch_user_awemes(
            sec_user_id,
            profile_dir=args.profile,
            headless=args.headless,
            max_scroll=args.max_scroll,
            idle_rounds=args.idle_rounds,
            timeout_ms=args.timeout,
            channel=args.browser,
            proxy=args.proxy,
            wait_login=args.wait_login,
        )
    except Exception as error:
        print('  抓取失败：{}'.format(error))
        return 1

    print_summary(result)
    if not args.no_save:
        folder, pages_path, all_path = save_result(result, args.out)
        print('  输出目录：{}'.format(folder))
        print('  原始分页：{}'.format(pages_path))
        print('  去重汇总：{}'.format(all_path))
    return 0


if __name__ == '__main__':
    sys.exit(main())
