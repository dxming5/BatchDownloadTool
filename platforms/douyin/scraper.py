# -*- coding: utf-8 -*-
"""
抖音主页作品抓取

不手工构造 a_bogus / msToken 等签名——全部交给真实浏览器生成；
监听 /aweme/v1/web/aweme/post/ 的响应体拿到原始 JSON，
滚动到底部触发翻页，直到接口返回 has_more = 0 为止。

通用浏览器能力（启动上下文、滚动、登录等待）由 core.browser 提供。
作品类型判定、取址与下载组装在 assemble.py。
"""

import json
import re
import time

from conf import settings
from core import browser

API_MARK = '/aweme/v1/web/aweme/post/'

# 用户页的作品列表节点随版本变化，按优先级探测
LIST_SELECTORS = (
    '[data-e2e="user-post-item-list"]',
    '[data-e2e="user-post-list"]',
    '[data-e2e="scroll-list"]',
)

# 只要能读到这些 Cookie 之一，就说明浏览器里已有登录态
# （sessionid 是 HttpOnly，document.cookie 读不到，必须走 context.cookies()）
LOGIN_COOKIE_NAMES = ('sessionid', 'sessionid_ss', 'sid_tt')

LOGIN_MODAL_SELECTOR = ('[data-e2e="login-modal"], [data-e2e="login-panel"],'
                        ' .login-mask, #login-panel-new')

# 抓取范围早停阈值：连续这么多条作品都早于截止时间，就不再往下翻
DEFAULT_STALE_LIMIT = 12


def parse_sec_user_id(raw):
    """
    从用户主页 URL / 带 sec_user_id 的接口 URL / sec_user_id 本体中提取

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


def fetch_user_awemes(sec_user_id, options=None, since_ts=None,
                      on_progress=None, should_stop=None, verbose=False):
    """
    抓取某博主的主页作品

    Args:
        sec_user_id:  博主 sec_user_id
        options:      平台抓取参数（profile_dir / headless / wait_login /
                      max_scroll / idle_rounds / timeout_ms / browser_channel / proxy）
        since_ts:     只保留 create_time >= since_ts 的作品；连续若干条早于它时提前停止
        on_progress:  进度回调
        should_stop:  取消检查函数，返回 True 时尽快结束（保留已抓部分）
        verbose:      是否打印到控制台

    Returns:
        dict: 见返回值里的键（nickname / user_url / pages / aweme_list ...）
    """
    from playwright.sync_api import sync_playwright

    options = options or {}
    profile_dir = options.get('profile_dir') or settings.platform_profile_dir('douyin')
    headless = bool(options.get('headless', True))
    max_scroll = max(1, int(options.get('max_scroll') or 400))
    idle_rounds = max(1, int(options.get('idle_rounds') or 8))
    timeout_ms = max(5000, int(options.get('timeout_ms') or 30000))
    channel = options.get('browser_channel')
    proxy = options.get('proxy')
    wait_login = max(0, int(options.get('wait_login') or 0))
    stale_limit = max(1, int(options.get('stale_limit') or DEFAULT_STALE_LIMIT))

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
            payload = browser.response_json(response)
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
        context = browser.launch_context(
            playwright, profile_dir, headless,
            channel=channel, proxy=proxy)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.on('response', on_response)
            page.goto(user_url, wait_until='domcontentloaded', timeout=timeout_ms)
            emit({'stage': 'scrape', 'pages': 0, 'count': 0,
                  'message': '已打开主页，等待作品列表…'})

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
                    logged, modal = browser.login_state(
                        page, LOGIN_COOKIE_NAMES, LOGIN_MODAL_SELECTOR)
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
                # 已登录就不重载：reload 会让首屏被重复请求一次
                already_logged, _ = browser.login_state(
                    page, LOGIN_COOKIE_NAMES, LOGIN_MODAL_SELECTOR)
                if already_logged:
                    log('已登录，跳过首屏重载')
                else:
                    logged_in = browser.wait_for_login(
                        page, wait_login, LOGIN_COOKIE_NAMES, LOGIN_MODAL_SELECTOR,
                        should_stop=should_stop, on_progress=on_progress,
                        verbose=verbose, required_label='sessionid')
                    if logged_in and not cancelled():
                        # 刚登录：重新加载主页，让列表按已登录身份重新拉取
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
            for _index in range(max_scroll):
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
                info = browser.scroll_step(page, LIST_SELECTORS)
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

            logged, modal = browser.login_state(
                page, LOGIN_COOKIE_NAMES, LOGIN_MODAL_SELECTOR)
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


def pages_to_lines(result):
    """把分页数据转成「每行一条接口响应」的字符串列表，便于排查问题"""
    return [json.dumps(payload, ensure_ascii=False)
            for payload in result.get('pages') or []]
