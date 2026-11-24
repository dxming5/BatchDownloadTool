# -*- coding: utf-8 -*-
"""
微博主页图片抓取

与抖音/小红书不同，微博不靠滚动页面收集数据，而是直接调卡片接口：

    1. Playwright 打开 m.weibo.cn 用户页，未登录时引导到 passport 扫码；
    2. 用同一 context 的 context.request 请求 getIndex 接口（自动带 Cookie）；
    3. 按 data.cardlistInfo.since_id 翻页，直到没有下一页、到达时间边界、
       或达到翻页上限（复用配置里的 max_scroll，按页数计，防止死循环）。

为什么不用页面 DOM：网页本身也是同一接口分页取数，滚动只是触发请求；
直接请求拿到的是结构化 JSON，字段稳定，且能控制节奏、支持取消与早停。

登录校验以 m.weibo.cn/api/config 的 data.login 为准：匿名访客的 Cookie 里
同样会出现 SUB/SUBP，单看 Cookie 名会把访客判成已登录，于是拿匿名态去取数，
只能看到首页的一小部分内容（表现为「下载不全」）。Cookie 只在接口不可用时兜底。

登录闸门之后强制刷新用户页，再从页面级数据取昵称（不遍历条目作者），先上报
on_meta，然后逐页逐条通过 on_article 交出微博，不再累积全量 mblogs。
链接取 uid、昵称与时间解析也在这里；图片/视频取址与归一化在 assemble.py。
"""

import datetime
import random
import re
import time

from conf import settings
from core import browser
from core.utils import safe_name

API = 'https://m.weibo.cn/api/container/getIndex'

# 登录态接口：data.login 是微博自己给出的登录标志，比 Cookie 名可靠
CONFIG_API = 'https://m.weibo.cn/api/config'

# Cookie 兜底：SUB/SUBP 是登录票据名；_T_WM、XSRF-TOKEN 匿名访客也有，不能用来判断
LOGIN_COOKIE_NAMES = ('SUB', 'SUBP')

# 等待人工扫码时的轮询间隔（毫秒）：平时只读本地 Cookie，票据变化后才调接口复核
LOGIN_POLL_MS = 2000

# 票据已变但接口还没确认时的复核间隔（毫秒）
LOGIN_VERIFY_INTERVAL_MS = 4000

LOGIN_MODAL_SELECTOR = '.m-login, .login-btn, [class*="login"]'

# 直接给扫码登录页，比在页面里点登录入口稳定
LOGIN_URL = 'https://passport.weibo.cn/signin/login?entry=mweico'

# 翻页上限的兜底值：正常情况下由 [platforms.default] 的 max_scroll 决定
# （微博按「翻页次数」使用该值，接口每页约 10 条），只在参数缺失时启用
DEFAULT_PAGE_LIMIT = 400

# 抓取范围早停阈值：连续这么多条微博都早于截止时间就停止翻页
DEFAULT_STALE_LIMIT = 12

# 页间等待区间（秒）：微博接口对高频请求敏感，沿用旧工具节奏
PAGE_INTERVAL = (1.0, 2.0)

# 翻到底时接口不返回空列表，而是 ok=0 + 这类提示：属于正常结束，不是错误
# （「这里还没有内容」包含「没有内容」，文案小改也能命中）
NO_MORE_MESSAGES = ('没有内容', '没有更多', '暂无内容', '内容为空')

REQUEST_HEADERS = {
    'Accept': 'application/json, text/plain, */*',
    'Referer': 'https://m.weibo.cn/',
    'X-Requested-With': 'XMLHttpRequest',
}


# ---------------- 链接与字段解析 ----------------

# 微博卡片接口里的容器前缀：107603 是「用户微博列表」，其余为资料类容器
CONTAINER_PREFIXES = ('107603', '100505', '230413', '101511', '110003')

MONTHS = {'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
          'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12}

# 例：Fri Apr 21 00:00:00 +0800 2023
_CREATED_RE = re.compile(
    r'([A-Za-z]{3})\s+([A-Za-z]{3})\s+(\d{1,2})\s+'
    r'(\d{2}):(\d{2}):(\d{2})\s+([+-])(\d{2})(\d{2})\s+(\d{4})')


def parse_uid(raw):
    """
    从链接或裸 uid 中取博主 uid

    兼容：m.weibo.cn/u/<uid>、/profile/<uid>、weibo.com/u/<uid>、
         weibo.com/<uid>、卡片接口地址（containerid / value 参数）、裸数字 uid

    Raises:
        ValueError: 无法识别
    """
    value = (raw or '').replace('\ufeff', '').replace('\u200b', '').strip()
    value = value.strip('"').strip("'")
    if not value:
        raise ValueError('缺少微博博主链接或 uid')

    if value.isdigit() and len(value) >= 5:
        return value

    match = re.search(r'/(?:u|profile|n)/(\d{5,})', value)
    if match:
        return match.group(1)

    # weibo.com/<uid>
    match = re.search(r'weibo\.c(?:n|om)/(\d{5,})(?:[/?#]|$)', value)
    if match:
        return match.group(1)

    # 卡片接口地址：containerid=107603<uid>
    match = re.search(r'containerid=(\d{6,})', value)
    if match:
        digits = match.group(1)
        for prefix in CONTAINER_PREFIXES:
            if digits.startswith(prefix) and len(digits) > len(prefix):
                return digits[len(prefix):]
        return digits

    # 卡片接口地址：value=<uid>
    match = re.search(r'[?&]value=(\d{5,})', value)
    if match:
        return match.group(1)

    raise ValueError('无法从内容中识别微博 uid：{}'.format(value[:80]))


def home_url(uid):
    """规范化主页地址；写进 homepage.txt，「更新」时能被再次识别"""
    return 'https://m.weibo.cn/u/{}'.format(uid)


# 页面级昵称：先读全局配置，再读昵称节点；绝不从条目作者里取身份
PAGE_NICKNAME_JS = """
() => {
    const clean = (v) => String(v == null ? '' : v).trim();
    const configs = [window.$CONFIG, window.__INITIAL_STATE__, window.pageConfig];
    for (const cfg of configs) {
        if (!cfg || typeof cfg !== 'object') continue;
        const user = cfg.user || cfg.userInfo || {};
        const values = [cfg.nickname, cfg.nick, cfg.screen_name,
                        user.screen_name, user.name, user.nickname];
        for (const value of values) {
            const text = clean(value);
            if (text) return text;
        }
    }
    const selectors = ['.m-login .name', '.user-info .name', '.card-name',
                       '.profile .name', '[class*="nickname"]'];
    for (const selector of selectors) {
        const node = document.querySelector(selector);
        if (!node) continue;
        const text = clean(node.textContent || node.innerText);
        if (text) return text;
    }
    return '';
}
"""


def page_nickname(page):
    """
    从页面自身取昵称：全局配置 / 昵称 DOM → 标题按「的微博」截断 → 标题原文

    只用于身份冻结，不读任何条目数据；取不到时返回空串，由会话判为身份缺失。
    """
    value = ''
    try:
        value = page.evaluate(PAGE_NICKNAME_JS)
    except Exception:
        value = ''
    value = str(value).strip() if value else ''
    if not value:
        try:
            value = (page.title() or '').strip()
        except Exception:
            value = ''
        if '的微博' in value:
            value = value.split('的微博')[0].strip()
    return safe_name(value)


def posts_container(uid):
    """用户微博列表的容器 ID"""
    return '107603{}'.format(uid)


def mblog_id_of(mblog):
    """微博 ID；取不到返回空串"""
    for key in ('id', 'mid', 'bid'):
        value = (mblog or {}).get(key)
        if value:
            return str(value)
    return ''


def author_of(mblog):
    """作者昵称；取不到返回空串"""
    user = (mblog or {}).get('user') or {}
    return safe_name(user.get('screen_name') or user.get('name') or '')


def created_at(mblog):
    """
    发布时间转成秒级时间戳

    微博给的是带时区的英文格式（Fri Apr 21 00:00:00 +0800 2023），
    手工解析的原因：struct/parsedate 对「日期在月份之前、年份在末尾」的写法不稳定。
    解析失败返回 0，由下载层按「缺少发布时间」跳过。
    """
    text = ((mblog or {}).get('created_at') or '').strip()
    if not text:
        return 0

    match = _CREATED_RE.search(text)
    if match:
        _week, month, day, hour, minute, second, sign, off_h, off_m, year = match.groups()
        offset = (int(off_h) * 60 + int(off_m)) * (1 if sign == '+' else -1)
        try:
            zone = datetime.timezone(datetime.timedelta(minutes=offset))
            moment = datetime.datetime(int(year), MONTHS.get(month, 1), int(day),
                                       int(hour), int(minute), int(second), tzinfo=zone)
            return int(moment.timestamp())
        except ValueError:
            return 0

    for pattern in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return int(datetime.datetime.strptime(text, pattern).timestamp())
        except ValueError:
            continue
    return 0


def _sleep(seconds, should_stop=None):
    """可取消的等待：取消时最多多等一个轮询周期"""
    remained = float(seconds)
    while remained > 0:
        if should_stop and should_stop():
            return False
        step = min(0.2, remained)
        time.sleep(step)
        remained -= step
    return True


def _api_url(uid, since_id):
    return '{}?type=uid&value={}&containerid={}&since_id={}'.format(
        API, uid, posts_container(uid), since_id)


def _no_more_content(message):
    """接口提示是否表示「已没有更多内容」（翻到底，不是错误）"""
    text = str(message or '')
    return any(keyword in text for keyword in NO_MORE_MESSAGES)


def request_json(context, url, timeout_ms, retries=None):
    """
    请求接口并返回 JSON

    context.request 与页面共享 Cookie，登录后无需手工拼 Cookie 头。
    ok != 1 通常是未登录 / Cookie 失效 / 风控，直接抛出可读错误，
    不能静默返回空列表让用户误以为「没有内容」。
    例外：翻到最后一页时接口返回 ok=0 +「这里还没有内容」，
    这是「没有更多内容」的正常结束信号，返回 None 交给调用方收尾。

    Returns:
        dict|None: 接口 JSON；已经没有更多内容时返回 None
    """
    attempts = max(1, int(retries if retries is not None else settings.MAX_RETRY) + 1)
    last_error = ''

    for attempt in range(attempts):
        try:
            response = context.request.get(url, headers=REQUEST_HEADERS,
                                           timeout=max(5000, int(timeout_ms)))
        except Exception as error:
            last_error = str(error)
        else:
            payload = None
            try:
                payload = response.json()
            except Exception:
                payload = None

            if isinstance(payload, dict):
                if payload.get('ok') == 1:
                    return payload
                message = payload.get('msg') or payload.get('errmsg') or '未知错误'
                if _no_more_content(message):
                    return None
                raise RuntimeError(
                    '微博接口返回失败（ok={}，retcode={}）：{}；'
                    '通常是未登录、Cookie 失效或被风控，请重新登录后重试'.format(
                        payload.get('ok'), payload.get('retcode'), message))
            last_error = 'HTTP {} 响应不是 JSON'.format(response.status)

        if attempt < attempts - 1:
            time.sleep(random.uniform(1.0, 2.0))

    raise RuntimeError('微博接口请求失败：{}'.format(last_error or '未知异常'))


def api_login_state(context, timeout_ms):
    """
    用 m.weibo.cn 的 config 接口判定登录态

    匿名访客也带 SUB/SUBP Cookie，只查 Cookie 名会把「游客」当「已登录」，
    于是拿匿名态去取数，只能看到首页的一小部分微博。

    Returns:
        bool|None: True 已登录 / False 未登录 / None 接口不可用无法判定
    """
    try:
        response = context.request.get(
            CONFIG_API, headers=REQUEST_HEADERS,
            timeout=max(5000, int(timeout_ms)))
        payload = response.json()
    except Exception:
        return None

    data = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(data, dict) or 'login' not in data:
        return None
    return bool(data.get('login'))


def current_login_state(page, timeout_ms):
    """
    当前登录态：接口优先，接口不可用时退回 Cookie 判断

    Returns:
        bool: 是否已登录
    """
    state = api_login_state(page.context, timeout_ms)
    if state is not None:
        return state
    logged, _modal = browser.login_state(
        page, LOGIN_COOKIE_NAMES, LOGIN_MODAL_SELECTOR)
    return logged


def _session_token(page):
    """
    当前 SUB/SUBP 票据值

    访客票据和登录票据同名，靠「值是否变化」判断扫码是否真的写入了新票据，
    每次只读本地 Cookie，不发请求。限定 m.weibo.cn，避免拿到别的域上的旧票据。
    """
    try:
        for cookie in page.context.cookies('https://m.weibo.cn/'):
            if cookie.get('name') in LOGIN_COOKIE_NAMES and cookie.get('value'):
                return cookie.get('value')
    except Exception:
        pass
    return ''


def ensure_login(page, profile_dir, options, on_progress=None,
                 should_stop=None, verbose=False):
    """
    确保当前浏览器上下文已登录微博；未登录就绝不继续抓取

    Returns:
        bool: 是否已登录；未登录且等待超时返回 False
    """
    def emit(message, stage='scrape'):
        if verbose:
            print('  ' + message)
        if on_progress:
            try:
                on_progress({'stage': stage, 'message': message})
            except Exception:
                pass

    timeout_ms = max(5000, int(options.get('timeout_ms') or 30000))
    wait_login = max(0, int(options.get('wait_login') or 0))

    if current_login_state(page, timeout_ms):
        return True

    # 访客票据作基线：票据值变了才可能是真的扫码登录，再用接口复核
    baseline = _session_token(page)

    emit('检测到未登录微博（Cookie 里的票据不能作为登录依据），正在打开登录页…')
    try:
        page.goto(LOGIN_URL, wait_until='domcontentloaded', timeout=timeout_ms)
    except Exception:
        pass

    if wait_login <= 0:
        emit('未登录且未配置等待时间（wait_login_seconds=0），无法继续')
        return False

    emit('请在这个自动弹出的浏览器窗口扫码登录（最多等待 {} 秒）；'
         '你平时用的浏览器登录无效'.format(wait_login))

    waited = 0
    poll_ms = LOGIN_POLL_MS
    last_token = baseline
    retry_at = -1      # >=0 表示票据已变、正在等接口确认，之后按间隔重试
    while waited < wait_login * 1000:
        if should_stop and should_stop():
            emit('等待登录已取消')
            return False

        token = _session_token(page)
        if token and token != last_token:
            # 票据变了才可能真的登录了；其余时间只读本地 Cookie，不惊动接口
            last_token = token
            retry_at = waited
        if token and retry_at >= 0 and waited >= retry_at:
            if current_login_state(page, timeout_ms):
                emit('登录成功，继续抓取（登录态保存在 {}）'.format(profile_dir))
                return True
            # 票据刚写入时接口可能还没生效，隔一会儿再确认
            retry_at = waited + LOGIN_VERIFY_INTERVAL_MS

        time.sleep(poll_ms / 1000.0)
        waited += poll_ms
        if waited % 20000 == 0:
            emit('已等待 {}/{} 秒（接口仍显示未登录，请扫码）…'.format(
                waited // 1000, wait_login))

    emit('仍未登录（接口 data.login=false）：请在自动打开的浏览器窗口里扫码登录，'
         '或把 headless 设为 false 后重试；未登录只能看到少量微博，已中止以免下载不全')
    return False


def fetch_user_posts(target, options=None, since_ts=None,
                     on_meta=None, on_article=None,
                     on_progress=None, should_stop=None, verbose=False):
    """
    抓取某博主的微博列表（流式逐条交出，不累积全量）

    登录前只打开页面做登录检测，不发任何内容接口请求、不注册响应捕获；
    登录成功后强制刷新用户页，再从页面取身份并先上报 on_meta，随后逐页
    逐条通过 on_article 交出微博。

    Args:
        target:       主页链接 / 卡片接口地址 / uid
        options:      平台参数（profile_dir / headless / wait_login / timeout_ms /
                      browser_channel / proxy / max_scroll）
        since_ts:     只保留发布时间 >= since_ts 的微博
        on_meta:      身份回调 (nickname, blogger_id, user_url) -> {'ok': bool, ...}
        on_article:   条目回调 (mblog) -> bool；False 表示停止交出
        on_progress:  状态/日志回调
        should_stop:  取消检查函数
        verbose:      是否打印到控制台

    Returns:
        dict: uid / user_url / nickname / pages(页数) / count / cancelled /
              stop_reason / has_more / restricted / since_ts / fetch_time
    """
    from playwright.sync_api import sync_playwright

    options = options or {}
    uid = parse_uid(target)
    user_url = home_url(uid)

    profile_dir = options.get('profile_dir') or settings.platform_profile_dir('weibo')
    headless = bool(options.get('headless', True))
    timeout_ms = max(5000, int(options.get('timeout_ms') or 30000))
    channel = options.get('browser_channel')
    proxy = options.get('proxy')
    # 翻页上限复用公共参数 max_scroll，不为微博单独增加配置项
    page_limit = max(1, int(options.get('max_scroll') or DEFAULT_PAGE_LIMIT))
    stale_limit = max(1, int(options.get('stale_limit') or DEFAULT_STALE_LIMIT))

    seen_ids = set()
    stale_run = 0
    stopped = False
    restricted = False
    nickname = ''
    since_id = '0'
    page_index = 0
    count = 0
    has_more = False
    meta_ok = True
    stop_reason = ''

    def log(text):
        if verbose:
            print('  ' + text)

    def emit(event):
        if on_progress:
            try:
                on_progress(event)
            except Exception:
                pass

    def cancelled():
        return bool(should_stop and should_stop())

    emit({'stage': 'scrape', 'pages': 0, 'count': 0, 'message': '正在启动浏览器…'})

    with sync_playwright() as playwright:
        context = browser.launch_context(
            playwright, profile_dir, headless,
            channel=channel, proxy=proxy)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            # 登录前只打开页面：不注册监听、不读页面状态、不发内容接口请求
            page.goto(user_url, wait_until='domcontentloaded', timeout=timeout_ms)
            emit({'stage': 'scrape', 'pages': 0, 'count': 0,
                  'message': '已打开微博主页，检查登录状态…'})

            if not ensure_login(page, profile_dir, options,
                                on_progress=on_progress,
                                should_stop=should_stop, verbose=verbose):
                restricted = True
                if cancelled():
                    stopped = True
                    stop_reason = '用户取消'
                else:
                    raise RuntimeError(
                        '未登录 微博：请在浏览器窗口完成登录后重试（登录态保存在 {}）'.format(
                            profile_dir))
            else:
                # 登录成功后强制刷新用户页，丢弃登录前可能拿到的任何数据
                log('已登录，刷新页面后重新获取微博')
                try:
                    page.goto(user_url, wait_until='domcontentloaded',
                              timeout=timeout_ms)
                except Exception as error:
                    raise RuntimeError('登录后刷新微博主页失败：{}'.format(error))

                # 刷新完成后才取身份并上报，必须早于任何条目派发
                nickname = page_nickname(page)
                if on_meta:
                    try:
                        meta = on_meta(nickname, uid, user_url) or {}
                    except Exception:
                        meta = {}
                    if meta.get('ok') is False:
                        meta_ok = False
                        stop_reason = '身份信息不可用'
                if not meta_ok:
                    log('身份信息不可用，停止抓取，不下载任何内容')
                else:
                    emit({'stage': 'scrape', 'pages': 0, 'count': 0,
                          'message': '身份已冻结（{}），开始获取微博列表…'.format(
                              nickname or uid)})

            while meta_ok and not stopped and page_index < page_limit:
                if cancelled():
                    stopped = True
                    stop_reason = '用户取消'
                    break
                if stale_run >= stale_limit:
                    stop_reason = '已到时间范围边界'
                    break

                page_index += 1
                try:
                    payload = request_json(context, _api_url(uid, since_id),
                                           timeout_ms, settings.MAX_RETRY)
                except RuntimeError as error:
                    # 已经交出的内容不因后面某页失败而丢弃；一页都没拿到才算失败
                    if not count:
                        raise
                    page_index -= 1
                    stop_reason = '第 {} 页请求失败'.format(page_index + 1)
                    emit({'stage': 'notice',
                          'message': '第 {} 页请求失败，保留已抓取的 {} 条：{}'.format(
                              page_index + 1, count, error)})
                    break
                if payload is None:
                    # 翻到底：接口说「还没有内容」，属于正常结束，不是请求失败。
                    # 但一条都没拿到时要先排除掉登录失效，否则会把未登录当成空主页。
                    if not count and not cancelled() \
                            and not current_login_state(page, timeout_ms):
                        raise RuntimeError(
                            '微博接口返回「没有内容」且登录态已失效，'
                            '请重新登录后重试（登录态保存在 {}）'.format(profile_dir))
                    stop_reason = '没有更多内容'
                    message = '第 {} 页已无更多内容，抓取结束（累计 {} 条）'.format(
                        page_index, count)
                    log(message)
                    page_index -= 1
                    has_more = False
                    break

                data = payload.get('data') or {}
                cards = data.get('cards') or []

                # 首页后复核登录态：中途掉登录时接口只给残缺结果，必须早于
                # 该页的条目派发，否则残缺数据会被当成正常内容下载。
                if page_index == 1 and not cancelled() \
                        and not current_login_state(page, timeout_ms):
                    raise RuntimeError(
                        '微博登录态在抓取过程中失效（接口 data.login=false），'
                        '匿名访问只能拿到部分微博，已中止以免下载残缺数据；'
                        '请重新登录后重试（登录态保存在 {}）'.format(profile_dir))

                # 仅处理卡片类型 9（微博），其余是分组、广告、推荐等
                found = 0
                early_stop = False
                for card in cards:
                    if not isinstance(card, dict):
                        continue
                    if card.get('card_type') != 9 or 'mblog' not in card:
                        continue
                    mblog = card.get('mblog') or {}
                    if not isinstance(mblog, dict):
                        continue
                    found += 1

                    mblog_id = mblog_id_of(mblog)
                    if not mblog_id or mblog_id in seen_ids:
                        continue

                    created = created_at(mblog)
                    if since_ts and created and created < since_ts:
                        stale_run += 1
                        if stale_run >= stale_limit:
                            early_stop = True
                            break
                        continue
                    stale_run = 0

                    seen_ids.add(mblog_id)
                    count += 1
                    # 逐条交出：返回 False 立即停止（取消 / 会话不可用）
                    if on_article and on_article(mblog) is False:
                        stopped = True
                        stop_reason = '用户取消' if cancelled() else '会话已停止'
                        break

                message = '第 {} 页：本页 {} 条，累计 {} 条'.format(
                    page_index, found, count)
                log(message)
                emit({'stage': 'scrape', 'pages': page_index,
                      'count': count, 'message': message})

                if stopped:
                    break
                if early_stop:
                    stop_reason = '已到时间范围边界'
                    break

                next_id = str(((data.get('cardlistInfo') or {}).get('since_id')) or '')
                if not next_id or next_id == '0':
                    has_more = False
                    stop_reason = '没有更多内容'
                    break
                if next_id == since_id:
                    log('since_id 未变化（{}），提前结束翻页'.format(next_id))
                    has_more = False
                    stop_reason = 'since_id 未变化'
                    break
                has_more = True
                since_id = next_id

                if not _sleep(random.uniform(*PAGE_INTERVAL), should_stop):
                    stopped = True
                    stop_reason = '用户取消'
                    break
            else:
                if not stop_reason:
                    stop_reason = '达到 max_scroll 上限'
        finally:
            try:
                context.close()
            except Exception:
                pass

    if has_more and page_index >= page_limit:
        limit_message = ('已达翻页上限 {} 页，可能仍有更早的微博未抓到；'
                         '可调大 [platforms.default] 的 max_scroll 后重试').format(
                             page_limit)
        log(limit_message)
        emit({'stage': 'notice', 'message': limit_message})

    log('抓取结束：{}（共 {} 条，{} 页）'.format(stop_reason, count, page_index))
    emit({'stage': 'scrape_done', 'pages': page_index, 'count': count,
          'message': '抓取结束：{}，共 {} 条'.format(stop_reason, count)})

    return {
        'uid': uid,
        'user_url': user_url,
        'nickname': nickname,
        'pages': page_index,
        'count': count,
        'cancelled': bool(cancelled()),
        'stop_reason': stop_reason,
        'has_more': has_more,
        'restricted': restricted,
        'since_ts': since_ts,
        'fetch_time': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
