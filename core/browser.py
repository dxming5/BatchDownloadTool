# -*- coding: utf-8 -*-
"""
浏览器通用能力

各平台共用：持久化上下文、滚动、登录等待、登录态判断。
平台差异通过参数传入（cookie 名、登录弹窗选择器、列表容器选择器）。
"""

import json

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0'
)

# 页面上的滚动容器层级随版本变化（window 本身可能不可滚），
# 因此对所有可滚动容器统一推进；已到底的先回弹一次，重新触发懒加载观察器
SCROLL_JS = """
(step) => {
  const targets = [];
  document.querySelectorAll('div, section, main').forEach((node) => {
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

DEFAULT_VIEWPORT = {'width': 1440, 'height': 900}


def launch_context(playwright, profile_dir, headless, channel=None, proxy=None,
                   viewport=None, user_agent=USER_AGENT):
    """启动持久化上下文；指定通道不可用时回退到内置 Chromium"""
    kwargs = {
        'headless': headless,
        'viewport': viewport or dict(DEFAULT_VIEWPORT),
        'locale': 'zh-CN',
        'user_agent': user_agent,
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


def scroll_step(page, selectors=(), step=2400):
    """
    向下滚动一步：先在列表区域发真实滚轮事件，再推进所有可滚动容器

    Returns:
        dict|None: {'targets': 可滚动容器数, 'moved': 发生位移的容器数}
    """
    box = None
    for selector in selectors or ():
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
        page.mouse.wheel(0, step)
    except Exception:
        pass

    try:
        return page.evaluate(SCROLL_JS, 2000)
    except Exception:
        return None


def cookie_names(page):
    """当前上下文的 Cookie 名集合（HttpOnly 也能取到）"""
    names = set()
    try:
        for cookie in page.context.cookies():
            if cookie.get('value'):
                names.add(cookie.get('name'))
    except Exception:
        return set()
    return names


def cookie_count(page):
    """当前窗口的 Cookie 数量，用于判断登录动作是否发生在这个窗口里"""
    try:
        return len(page.context.cookies())
    except Exception:
        return -1


def login_state(page, cookie_names_required, modal_selector=''):
    """
    判断当前页面的登录态

    Args:
        page: 页面对象
        cookie_names_required: 任一存在即视为已登录的 Cookie 名（可为字符串或序列）
        modal_selector: 登录弹窗/入口的 CSS 选择器，命中时提示未登录

    Returns:
        tuple: (是否已登录, 是否有登录弹窗/入口)
    """
    if isinstance(cookie_names_required, str):
        required = (cookie_names_required,)
    else:
        required = tuple(cookie_names_required or ())

    names = cookie_names(page)
    logged = bool(names & set(required))

    modal = False
    if modal_selector:
        try:
            modal = page.evaluate(
                '(selector) => !!document.querySelector(selector)', modal_selector)
        except Exception:
            modal = False
    return logged, modal


def wait_for_login(page, seconds, cookie_names_required, modal_selector='',
                   poll_ms=2000, should_stop=None, on_progress=None,
                   verbose=True, required_label='sessionid'):
    """
    未登录时阻塞等待人工登录

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
        logged, modal = login_state(page, cookie_names_required, modal_selector)
    except Exception:
        return False
    if logged:
        return True

    say('检测到未登录（{}），最多等待 {} 秒'.format(
        '登录弹窗已出现' if modal else 'Cookie 里没有 {}'.format(required_label), seconds))
    say('注意：必须在这个自动弹出的浏览器窗口里登录，你平时用的浏览器登录无效')

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
            logged, _ = login_state(page, cookie_names_required, modal_selector)
        except Exception:
            return False
        if logged:
            say('登录成功，继续抓取')
            return True
        if waited % 20000 == 0:
            say('已等待 {}/{} 秒（当前窗口 Cookie {} 个，{} 仍未出现）…'.format(
                waited // 1000, seconds, cookie_count(page), required_label))
    say('等待超时，未登录，已中止')
    return False


def ensure_logged_in(page, cookie_names, modal_selector, wait_seconds,
                     should_stop=None, on_progress=None, verbose=True,
                     required_label=''):
    """
    登录闸门：只判断登录态，已登录直接通过，未登录等待人工登录

    不取任何页面数据，也不刷新页面；刷新由调用方在返回 True 后执行。
    False 表示等待超时、被取消或页面不可用，调用方必须中止任务。

    Returns:
        bool: 是否已登录
    """
    try:
        logged, _modal = login_state(page, cookie_names, modal_selector)
    except Exception:
        logged = False
    if logged:
        return True
    return bool(wait_for_login(page, wait_seconds, cookie_names, modal_selector,
                               should_stop=should_stop, on_progress=on_progress,
                               verbose=verbose,
                               required_label=required_label or 'sessionid'))


def reload_after_login(page, url, timeout_ms, on_before_reload=None):
    """
    登录成功后重建目标页：先挂监听，再重新进入目标页

    顺序固定为「先挂后刷」的原因：刷新过程发出的接口响应同样是登录后数据，
    先挂监听才不会漏掉首屏；用 goto 而不是 reload，是因为等待登录期间页面
    可能已被导航到登录页，reload 只会重载登录页。

    Args:
        page:              页面对象
        url:               目标页地址（与登录前打开的一致）
        timeout_ms:        导航超时
        on_before_reload:  刷新前回调，用于挂响应监听、清空登录前的采集缓冲

    Raises:
        RuntimeError: 重新加载失败（调用方按抓取失败处理）
    """
    if on_before_reload:
        try:
            on_before_reload(page)
        except Exception:
            pass
    try:
        page.goto(url, wait_until='domcontentloaded',
                  timeout=max(5000, int(timeout_ms or 30000)))
    except Exception as error:
        raise RuntimeError('登录后重新加载目标页失败：{}'.format(error))
    return True


def response_json(response):
    """安全读取响应 JSON；body 已释放时返回 None"""
    try:
        return response.json()
    except Exception:
        pass
    try:
        return json.loads(response.text() or '{}')
    except Exception:
        return None
