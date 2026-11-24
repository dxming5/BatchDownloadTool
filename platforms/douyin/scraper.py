# -*- coding: utf-8 -*-
"""
抖音主页作品抓取

不手工构造 a_bogus / msToken 等签名——全部交给真实浏览器生成；
监听 /aweme/v1/web/aweme/post/ 的响应体拿到原始 JSON，
滚动到底部触发翻页，直到接口返回 has_more = 0 为止。

通用浏览器能力（启动上下文、滚动、登录闸门）由 core.browser 提供。
作品归一化在 assemble.py，逐篇下载由 core.session 负责。
登录之前不挂响应监听、不读页面状态、不发数据请求；登录后强制刷新页面，
再取身份并逐页交出作品（不保存全量列表）。
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

# 主页昵称来源一：页面状态里的用户对象（不遍历作品列表里的 author）
PAGE_USER_JS = """
() => {
  const roots = [window._ROUTER_DATA, window.__INITIAL_STATE__, window.RENDER_DATA];
  const pick = (obj, depth) => {
    if (!obj || typeof obj !== 'object' || depth > 4) return '';
    for (const key of ['user', 'userInfo', 'owner', 'blogger']) {
      const node = obj[key];
      if (!node || typeof node !== 'object') continue;
      const name = node.nickname || node.nick_name || node.name;
      if (typeof name === 'string' && name.trim()) return name.trim();
      const nested = pick(node, depth + 1);
      if (nested) return nested;
    }
    return '';
  };
  for (const root of roots) {
    if (!root || typeof root !== 'object') continue;
    const name = pick(root.loaderData || root, 0) || pick(root, 0);
    if (name) return name;
  }
  return '';
}
"""

# 主页昵称来源二：昵称 DOM 节点
PAGE_NICKNAME_JS = """
() => {
  const sels = [
    '[data-e2e="user-info"] h1',
    '[data-e2e="user-nickname"]',
    '[data-e2e="user-name"]',
    '.user-info h1',
    'h1[data-e2e="user-info"]',
  ];
  for (const sel of sels) {
    const node = document.querySelector(sel);
    const text = node && (node.innerText || node.textContent || '').trim();
    if (text) return text;
  }
  return '';
}
"""

# 抓取范围早停阈值：连续这么多条作品都早于截止时间，就不再往下翻
DEFAULT_STALE_LIMIT = 12

# ---------------- 关注列表 ----------------

# 只匹配路径里的关键片段：抖音换前缀（user / im / relation）或去掉 /list/ 时仍能命中
FOLLOWING_HINT = 'following'
USER_INFO_MARK = '/aweme/v1/web/im/user/info/'

# 「我的关注」：user/self 会被抖音重定向到当前登录账号的个人页
SELF_URL = 'https://www.douyin.com/'
SELF_FOLLOWING_URL = 'https://www.douyin.com/user/self?showTab=following'

# 关注页的列表容器随版本变化，探测不到时退化为全页扫描
FOLLOWING_SELECTORS = (
    '[data-e2e="user-following-list"]',
    '[data-e2e="following-list"]',
    '[data-e2e="scroll-list"]',
)

# 页面里现有的用户链接快照：点击「关注」前后的差集才是真的关注对象
SNAPSHOT_JS = """
() => {
  const out = [];
  document.querySelectorAll('a[href*="/user/"]').forEach((node) => {
    const match = (node.getAttribute('href') || '').match(/\\/user\\/([A-Za-z0-9_\\-]+)/);
    if (match && match[1] !== 'self') out.push(match[1]);
  });
  return out;
}
"""

# DOM 兜底：接口一页都没拿到时，从页面链接里收 sec_user_id
# 返回 {scope, items}：scope 表示采集范围，调用方据此决定是否可信
DOM_FOLLOWING_JS = """
() => {
  const out = [];
  const seen = {};
  const NOISE = /^(已关注|互相关注|关注|粉丝|私信|回关|取消关注)$/;
  const collect = (root) => {
    root.querySelectorAll('a[href*="/user/"]').forEach((node) => {
      const match = (node.getAttribute('href') || '').match(/\\/user\\/([A-Za-z0-9_\\-]+)/);
      if (!match || match[1] === 'self' || seen[match[1]]) return;
      seen[match[1]] = 1;
      const card = node.closest('li, [data-e2e], [class*="item"], [class*="user"], [class*="card"]');
      const cardText = card ? (card.innerText || '') : '';
      // 卡片上只有「关注」按钮、没有「已关注/互相关注」的，属于推荐位，不是已关注对象
      if (cardText && !/已关注|互相关注/.test(cardText)
          && /(^|\\n)\\s*关注\\s*(\\n|$)/.test(cardText)) {
        return;
      }
      // 头像链接本身通常没有文字，昵称要从最近的卡片里找
      let name = (node.innerText || node.textContent || '').trim().split('\\n')[0];
      if (!name || NOISE.test(name)) {
        const lines = (cardText || '').split('\\n')
          .map((line) => line.trim()).filter(Boolean);
        name = lines.find((line) => !NOISE.test(line)) || '';
      }
      out.push({sec_user_id: match[1], nickname: name});
    });
  };
  // 范围优先级：关注列表容器 → 含「关注」的弹窗 → 全页（全页不可信，交给差集过滤）
  const box = document.querySelector('[data-e2e="following-list"], [data-e2e="user-following-list"]');
  if (box) {
    collect(box);
    return {scope: 'container', items: out};
  }
  const dialogs = Array.from(document.querySelectorAll(
    '[role="dialog"], [class*="modal"], [class*="drawer"]'));
  const hit = dialogs.find((node) => (node.innerText || '').indexOf('关注') >= 0);
  if (hit) {
    collect(hit);
    return {scope: 'dialog', items: out};
  }
  collect(document);
  return {scope: 'page', items: out};
}
"""


# 直接带 ?showTab=following 时抖音不一定真的切到关注标签，
# 页面没数据时补一次点击：找文字形如「关注 123」的可点元素
ACTIVATE_FOLLOWING_JS = """
() => {
  const nodes = Array.from(document.querySelectorAll('div, span, a, p, button'));
  const depth = (node) => {
    let level = 0;
    let current = node;
    while (current && current.parentElement) {
      level += 1;
      current = current.parentElement;
    }
    return level;
  };
  let target = null;
  let targetText = '';
  for (const node of nodes) {
    const text = (node.innerText || '').trim();
    if (!text || text.length > 16) continue;
    if (!/^关注\\s*[\\d.万亿wW+]*$/.test(text)) continue;
    const rect = node.getBoundingClientRect();
    if (!rect.width || !rect.height) continue;
    // 取最贴近文字的那一层：点击它会冒泡到真正绑了事件的祖先
    if (!target || depth(node) > depth(target)) {
      target = node;
      targetText = text;
    }
  }
  if (!target) return '';
  target.click();
  return targetText;
}
"""

# 页面自检：用于在没有数据时判断「是不是根本没进关注列表页」
PAGE_STATS_JS = """
() => ({
  url: location.href,
  title: document.title,
  anchors: document.querySelectorAll('a[href*="/user/"]').length,
  box: !!document.querySelector('[data-e2e="following-list"], [data-e2e="user-following-list"]'),
})
"""


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


def page_nickname(page):
    """从页面级数据取主页昵称：状态用户对象 → 昵称 DOM → 页面标题"""
    for script in (PAGE_USER_JS, PAGE_NICKNAME_JS):
        try:
            value = (page.evaluate(script) or '').strip()
        except Exception:
            value = ''
        if value:
            return value
    try:
        return (page.title() or '').replace(' - 抖音', '').strip()
    except Exception:
        return ''


def fetch_user_awemes(sec_user_id, options=None, since_ts=None,
                      on_meta=None, on_article=None,
                      on_progress=None, should_stop=None, verbose=False):
    """
    流式抓取某博主的主页作品

    登录闸门通过后才挂响应监听、取身份并翻页；每页逐条通过 on_article
    交出，不保存全量列表。on_meta 必须在任何 on_article 之前调用，
    on_article 返回 False（下载端要求停止）时立即停止翻页。

    Args:
        sec_user_id:  博主 sec_user_id
        options:      平台抓取参数（profile_dir / headless / wait_login /
                      max_scroll / idle_rounds / timeout_ms / browser_channel / proxy）
        since_ts:     只保留 create_time >= since_ts 的作品；连续若干条早于它时提前停止
        on_meta:      (nickname, blogger_id, user_url) -> {'ok': bool, ...}，先于条目调用
        on_article:   单条原始作品回调，返回 False 表示停止翻页
        on_progress:  进度回调
        should_stop:  取消检查函数，返回 True 时尽快结束（保留已抓部分）
        verbose:      是否打印到控制台

    Returns:
        dict: 含 nickname / blogger_id / user_url / meta_ok / pages / count /
              stop_reason / cancelled 等，不再返回全量 aweme_list
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
    first_page_wait = 20000

    user_url = 'https://www.douyin.com/user/{}'.format(sec_user_id)
    blogger_id = parse_sec_user_id(sec_user_id)
    pending = []
    seen_ids = set()
    last_page = {'has_more': 1}
    nickname = ''
    page_index = 0
    count = 0
    stale_run = 0
    early_stop = False
    stopped = False
    meta_ok = True
    stop_reason = 'max_scroll'

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
        """逐页解析响应并逐条交出作品，不保存全量列表"""
        nonlocal last_page, page_index, count, stale_run, early_stop, stopped, stop_reason
        while pending:
            response = pending.pop(0)
            payload = browser.response_json(response)
            if not isinstance(payload, dict) or 'aweme_list' not in payload:
                continue

            raw_list = payload.get('aweme_list') or []
            keep_going = True
            for aweme in raw_list:
                if not isinstance(aweme, dict):
                    continue
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
                if on_article:
                    keep_going = on_article(aweme)
                    if keep_going is False:
                        stopped = True
                        stop_reason = '用户取消' if cancelled() else '下载端要求停止'
                        break
                count += 1

            last_page = payload
            page_index += 1
            message = '第 {} 页：本页 {} 条，累计 {} 条，has_more={}'.format(
                page_index, len(raw_list), count, payload.get('has_more'))
            log(message)
            emit({'stage': 'scrape', 'pages': page_index,
                  'count': count, 'message': message})
            if not keep_going:
                pending.clear()
                break

    emit({'stage': 'scrape', 'pages': 0, 'count': 0, 'message': '正在启动浏览器…'})

    with sync_playwright() as playwright:
        context = browser.launch_context(
            playwright, profile_dir, headless,
            channel=channel, proxy=proxy)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            # 只等 DOM 就绪：登录前不挂响应监听、不读页面状态、不发数据请求
            page.goto(user_url, wait_until='domcontentloaded', timeout=timeout_ms)

            logged_in = browser.ensure_logged_in(
                page, LOGIN_COOKIE_NAMES, LOGIN_MODAL_SELECTOR,
                wait_login, should_stop, on_progress)
            if not logged_in:
                raise RuntimeError(
                    '未登录 抖音：请在浏览器窗口完成登录后重试（登录态保存在 {}）'.format(profile_dir))

            # 登录成功：强制刷新，丢弃登录前可能拿到的任何数据
            page.reload(wait_until='domcontentloaded')
            emit({'stage': 'scrape', 'pages': 0, 'count': 0,
                  'message': '已登录，刷新页面后重新获取…'})

            # 刷新之后才挂响应监听、读页面状态、等首屏
            page.on('response', on_response)
            nickname = page_nickname(page)
            if on_meta:
                meta = on_meta(nickname, blogger_id, user_url) or {}
                meta_ok = bool(meta.get('ok'))
            else:
                meta_ok = False

            if not meta_ok:
                stop_reason = '身份信息不可用，未下载任何作品'
                log(stop_reason)
            else:
                log('身份已冻结：{}（{}）'.format(nickname, blogger_id))

                # 等首屏接口数据（登录刷新之后）
                waited = 0
                while page_index == 0 and waited < first_page_wait:
                    if cancelled():
                        stop_reason = '用户取消'
                        stopped = True
                        break
                    page.wait_for_timeout(1000)
                    waited += 1000
                    drain()

                if not stopped:
                    # 滚动翻页，直到 has_more=0、时间边界、连续无新增或被取消
                    last_total = count
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
                        if stopped:
                            break
                        if count > last_total:
                            last_total = count
                            idle = 0
                        else:
                            idle += 1
                            if idle % 3 == 0:
                                log('连续 {} 次无新增（滚动容器 {} 个，位移 {} 个）'.format(
                                    idle, (info or {}).get('targets', 0),
                                    (info or {}).get('moved', 0)))
                            if idle >= idle_rounds:
                                stop_reason = '连续 {} 次无新增'.format(idle)
                                break
                    else:
                        stop_reason = '达到 max_scroll 上限'
                    if not stopped and not early_stop and last_page.get('has_more') == 0:
                        stop_reason = 'has_more=0（已到最后一页）'
        finally:
            try:
                context.close()
            except Exception:
                pass

    log('抓取结束：{}（{} 条）'.format(stop_reason, count))
    emit({'stage': 'scrape_done', 'pages': page_index, 'count': count,
          'message': '抓取结束：{}，共 {} 条'.format(stop_reason, count)})

    return {
        'sec_user_id': sec_user_id,
        'user_url': user_url,
        'nickname': nickname,
        'blogger_id': blogger_id,
        'meta_ok': meta_ok,
        'pages': page_index,
        'count': count,
        'has_more': last_page.get('has_more'),
        'stop_reason': stop_reason,
        'cancelled': cancelled(),
        'since_ts': since_ts,
        'fetch_time': time.strftime('%Y-%m-%d %H:%M:%S'),
    }


def _pick(payload, key, default=None):
    """在响应顶层或 data 里取字段：抖音不同版本的包装层级不一致"""
    if isinstance(payload, dict):
        if key in payload:
            return payload[key]
        data = payload.get('data')
        if isinstance(data, dict) and key in data:
            return data[key]
    return default


def _following_items(payload):
    """在关注列表响应里找到「人」的数组，兼容几种包装"""
    for key in ('followings', 'follow_list', 'user_list', 'users'):
        value = _pick(payload, key)
        if isinstance(value, list):
            return value
    return []


def _following_users(payload):
    """从关注列表接口响应里取出 [{name, uid}]；字段随版本略有差异"""
    users = []
    for item in _following_items(payload):
        if not isinstance(item, dict):
            continue
        user = item.get('user') if isinstance(item.get('user'), dict) else item
        uid = (user.get('sec_user_id') or item.get('sec_uid') or '').strip()
        if not uid or uid == 'self':
            continue
        name = (user.get('nickname') or user.get('unique_id') or '').strip()
        users.append({'name': name, 'uid': uid})
    return users


def fetch_following(options=None, on_progress=None, should_stop=None, verbose=False):
    """
    抓取「当前登录账号」的关注列表

    与抓作品一样不手工构造签名：打开关注页，监听关注列表接口
    （路径含 following/list）的响应体，滚动触发翻页；
    页面没显示关注列表时补一次「关注」入口点击，接口始终没响应时
    退化为扫描页面上的用户链接。

    Returns:
        dict: owner / users / pages / total / cancelled
              users 每项为 {'name': 昵称, 'url': 主页链接, 'uid': sec_user_id}
    """
    from playwright.sync_api import sync_playwright

    options = options or {}
    profile_dir = options.get('profile_dir') or settings.platform_profile_dir('douyin')
    headless = bool(options.get('headless', True))
    max_scroll = max(1, int(options.get('following_max_scroll') or 200))
    limit = max(0, int(options.get('following_limit') or 0))
    interval = max(0.0, float(options.get('following_interval') or 1.0))
    idle_rounds = max(1, int(options.get('idle_rounds') or 8))
    timeout_ms = max(5000, int(options.get('timeout_ms') or 30000))
    channel = options.get('browser_channel')
    proxy = options.get('proxy')
    wait_login = max(0, int(options.get('wait_login') or 0))

    users = []
    seen = set()
    pending = []
    info_pending = []
    owner = ''
    page_index = 0
    has_more = 1
    stopped = False
    api_seen = []          # 诊断用：本次任务见过的接口路径（去重、限量）
    following_hits = 0     # 诊断用：命中「关注列表」接口的次数
    expected = 0           # 页面上显示的关注数，用于核对抓取结果

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

    def on_response(response):
        """只记录响应对象，解析留给主循环，避免在回调里做阻塞调用"""
        nonlocal following_hits
        url = response.url or ''
        # 诊断用：记录所有业务接口路径（去掉域名与查询串），便于发现真正的关注列表接口
        if '/aweme/' in url or '/api/' in url:
            bare = url.split('?', 1)[0].split('://', 1)[-1]
            path = bare[bare.find('/'):] if '/' in bare else bare
            if len(api_seen) < 80 and path not in api_seen:
                api_seen.append(path)
        if FOLLOWING_HINT in url:
            following_hits += 1
            if response.status == 200:
                pending.append(response)
            return
        if USER_INFO_MARK in url and response.status == 200:
            info_pending.append(response)

    def drain_info():
        """从用户信息接口取当前登录账号昵称"""
        nonlocal owner
        while info_pending:
            payload = browser.response_json(info_pending.pop(0))
            user = (payload or {}).get('user') or {}
            name = (user.get('nickname') or '').strip()
            if name:
                owner = name

    def drain():
        """把已抓到的关注列表响应解析入队"""
        nonlocal page_index, has_more
        while pending:
            payload = browser.response_json(pending.pop(0))
            if not isinstance(payload, dict):
                continue
            code = _pick(payload, 'status_code')
            if code not in (None, 0):
                raise RuntimeError(
                    '关注列表接口返回失败（status_code={}）：{}；'
                    '可能是登录态失效或被风控'.format(
                        code, _pick(payload, 'status_msg') or '未知原因'))

            raw_items = _following_items(payload)
            # 只认带分页字段的响应：URL 里含 following 的其他接口（如关注 feed）不算
            if not raw_items and _pick(payload, 'has_more') is None:
                continue
            for user in _following_users(payload):
                if user['uid'] in seen:
                    continue
                seen.add(user['uid'])
                users.append(user)

            page_index += 1
            has_more = _pick(payload, 'has_more', 1)
            message = '第 {} 页：本页 {} 人，累计 {} 人'.format(
                page_index, len(raw_items), len(users))
            log(message)
            emit({'stage': 'following', 'pages': page_index,
                  'count': len(users), 'message': message})

    def page_snapshot():
        """点击前页面里已有的用户链接，用于过滤全页扫描带进来的旧链接"""
        try:
            values = page.evaluate(SNAPSHOT_JS) or []
        except Exception:
            return set()
        return set(value for value in values if value)

    def merge_dom(baseline=None, accept_page=False):
        """从页面链接兜底收人；返回新增数量

        全页扫描不可信（推荐位、侧边栏也会带 /user/ 链接），
        因此只在「列表容器 / 关注弹窗」里直接采信；全页结果必须
        相对点击前的快照是新增的才收，或这个页面本身就是点开的新页面。
        """
        try:
            result = page.evaluate(DOM_FOLLOWING_JS) or {}
        except Exception:
            return 0
        scope = result.get('scope') or 'page'
        items = result.get('items') or []
        if scope == 'page' and not accept_page:
            # 没有基线说明页面还没稳定，全页结果一律不采信
            if not baseline:
                return 0
            items = [item for item in items
                     if ((item or {}).get('sec_user_id') or '') not in baseline]

        added = 0
        for item in items:
            uid = ((item or {}).get('sec_user_id') or '').strip()
            if not uid or uid in seen:
                continue
            seen.add(uid)
            name = ((item or {}).get('nickname') or '').strip()
            users.append({'name': name, 'uid': uid})
            added += 1

        if added:
            label = {'container': '关注列表容器', 'dialog': '关注弹窗',
                     'page': '关注页新增链接'}.get(scope, scope)
            first = users[-added]
            message = '从{}补充 {} 人，累计 {} 人（首个：{}）'.format(
                label, added, len(users), first['name'] or first['uid'])
            log(message)
            emit({'stage': 'following', 'pages': page_index,
                  'count': len(users), 'message': message})
        return added

    def page_stats():
        """页面现场信息：用于判断「没进列表页」还是「接口换了」"""
        try:
            return page.evaluate(PAGE_STATS_JS) or {}
        except Exception:
            return {}

    stop_reason = 'max_scroll'
    emit({'stage': 'following', 'pages': 0, 'count': 0, 'message': '正在启动浏览器…'})

    with sync_playwright() as playwright:
        context = browser.launch_context(
            playwright, profile_dir, headless,
            channel=channel, proxy=proxy)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.on('response', on_response)
            page.goto(SELF_URL, wait_until='domcontentloaded', timeout=timeout_ms)

            # 关注列表只有登录后才可见：先确保登录，再进关注页
            logged, _modal = browser.login_state(
                page, LOGIN_COOKIE_NAMES, LOGIN_MODAL_SELECTOR)
            if not logged:
                emit({'stage': 'following', 'pages': 0, 'count': 0,
                      'message': '未登录，等待扫码…'})
                logged = browser.wait_for_login(
                    page, wait_login, LOGIN_COOKIE_NAMES, LOGIN_MODAL_SELECTOR,
                    should_stop=should_stop, on_progress=on_progress,
                    verbose=verbose, required_label='sessionid')
            if not logged:
                raise RuntimeError(
                    '未登录抖音：请把 [platforms.douyin] 的 headless 设为 false 后重启，'
                    '在弹出的窗口里扫码登录（登录态保存在 {}）'.format(profile_dir))

            page.goto(SELF_FOLLOWING_URL, wait_until='domcontentloaded',
                      timeout=timeout_ms)
            emit({'stage': 'following', 'pages': 0, 'count': 0,
                  'message': '已打开关注列表，等待接口返回…'})

            # 先等首屏：直接带 showTab 参数时抖音不一定真的切到关注标签
            waited = 0
            while not users and waited < 6000:
                if cancelled():
                    stop_reason = '用户取消'
                    stopped = True
                    break
                page.wait_for_timeout(1000)
                waited += 1000
                drain()
                drain_info()

            # 点击前的用户链接快照：之后全页扫描只收新增的
            baseline = page_snapshot()
            page_trusted = False

            # 还没有数据：点一次「关注 N」入口，再等接口与页面
            if not users and not stopped:
                clicked = ''
                try:
                    clicked = page.evaluate(ACTIVATE_FOLLOWING_JS) or ''
                except Exception:
                    clicked = ''
                if clicked:
                    digits = re.sub(r'\D', '', clicked)
                    if digits:
                        expected = int(digits)
                    emit({'stage': 'following', 'pages': 0, 'count': 0,
                          'message': '页面未显示关注列表，已点击「{}」，等待数据…'.format(
                              clicked.replace('\n', ' '))})
                page.wait_for_timeout(2000)
                drain()
                drain_info()

                # 点击可能新开一个页面（tab/弹窗），跟到最新的那个
                if len(context.pages) > 1:
                    page = context.pages[-1]
                    try:
                        page.on('response', on_response)
                    except Exception:
                        pass
                    # 这个页面是点击直接打开的，整页内容就是列表，可全页采信
                    page_trusted = True
                    emit({'stage': 'following', 'pages': 0, 'count': 0,
                          'message': '打开了新页面：{}'.format(page.url)})

                # 关注列表很可能是页面直接渲染的（没有 XHR），先扫一次 DOM
                merge_dom(baseline, page_trusted)
                if not users:
                    stats = page_stats()
                    emit({'stage': 'following', 'pages': 0, 'count': 0,
                          'message': '点击后页面：用户链接 {} 个，列表容器 {}'.format(
                              stats.get('anchors', 0),
                              '有' if stats.get('box') else '无')})

            # 继续等：每 3 秒扫一次 DOM，每 5 秒汇报一次（上限固定 20 秒）
            waited = 0
            while not users and waited < 20000:
                if cancelled():
                    stop_reason = '用户取消'
                    stopped = True
                    break
                page.wait_for_timeout(1000)
                waited += 1000
                drain()
                drain_info()
                if waited % 3000 == 0:
                    merge_dom(baseline, page_trusted)
                if waited % 5000 == 0:
                    stats = page_stats()
                    emit({'stage': 'following', 'pages': page_index,
                          'count': len(users),
                          'message': '等待关注列表数据…（{} 秒，命中接口 {} 次，'
                                     '页面用户链接 {} 个，列表容器 {}）'.format(
                                         waited // 1000, following_hits,
                                         stats.get('anchors', 0),
                                         '有' if stats.get('box') else '无')})

            # 仍然没人：把现场信息打进日志，区分「没进列表页」和「接口换了」
            if not users and not stopped:
                stats = page_stats()
                emit({'stage': 'following', 'pages': 0, 'count': 0,
                      'message': '页面自检：url={} | 标题={} | 用户链接 {} 个 | 列表容器={}'.format(
                          stats.get('url', ''), stats.get('title', ''),
                          stats.get('anchors', 0), '有' if stats.get('box') else '无')})
                if api_seen:
                    emit({'stage': 'following', 'pages': 0, 'count': 0,
                          'message': '已捕获接口：{}'.format('、'.join(api_seen[:20]))})
                else:
                    emit({'stage': 'following', 'pages': 0, 'count': 0,
                          'message': '未捕获到任何 /aweme/v1/web/ 接口：页面可能没加载完，'
                                     '或被登录/验证拦住了'})

            if not users:
                # 页面没进关注列表（或接口没命中）：不再空滚几百次
                stop_reason = '页面未进入关注列表'
            else:
                last_total = len(users)
                idle = 0
                for _index in range(max_scroll):
                    if cancelled():
                        stop_reason = '用户取消'
                        stopped = True
                        break
                    if limit and len(users) >= limit:
                        stop_reason = '达到人数上限'
                        break
                    if has_more in (0, False):
                        stop_reason = 'has_more=0'
                        break
                    browser.scroll_step(page, FOLLOWING_SELECTORS)
                    page.wait_for_timeout(int(max(0.2, interval) * 1000))
                    drain()
                    drain_info()
                    if len(users) > last_total:
                        last_total = len(users)
                        idle = 0
                        continue
                    idle += 1
                    # 接口连续几次没有增量时，试一次页面兜底
                    if idle >= 2:
                        merge_dom(baseline, page_trusted)
                        if len(users) > last_total:
                            last_total = len(users)
                            idle = 0
                            continue
                    if idle >= idle_rounds:
                        stop_reason = '连续 {} 次无新增'.format(idle)
                        break
                else:
                    stop_reason = '达到 following_max_scroll 上限'

            # 接口一次都没响应、页面也没兜到人：多半是页面没进列表或接口变了
            if page_index == 0 and not users and not stopped:
                raise RuntimeError(
                    '未拿到关注列表：页面未进入关注列表，或接口「{}」没有触发。'
                    '请确认账号已登录且关注列表可见；若抖音改版，'
                    '需要按日志里的「已捕获接口」更新 platforms/douyin/scraper.py'.format(
                        FOLLOWING_HINT))

            # 从页面拿到数据（没有命中接口）：顺手把接口清单打进日志，便于后续改成接口采集
            if users and page_index == 0 and api_seen:
                emit({'stage': 'following', 'pages': 0, 'count': len(users),
                      'message': '本次数据来自页面渲染；已捕获接口：{}'.format(
                          '、'.join(api_seen[:10]))})

            if not owner:
                try:
                    owner = page.title().replace(' - 抖音', '').strip()
                except Exception:
                    owner = ''
        finally:
            try:
                context.close()
            except Exception:
                pass

    if limit and len(users) > limit:
        users = users[:limit]

    names = [user['name'] or user['uid'] for user in users[:5]]
    detail = '（前几位：{}）'.format('、'.join(names)) if names else ''
    log('关注列表抓取结束：{}（{} 人）'.format(stop_reason, len(users)))
    emit({'stage': 'following', 'pages': page_index, 'count': len(users),
          'message': '抓取结束：{}，共 {} 人{}'.format(stop_reason, len(users), detail)})
    # 页面上的关注数可作粗校验：不等就提示，避免把推荐位/其他列表当成关注列表
    if expected and users and expected != len(users):
        emit({'stage': 'following', 'pages': page_index, 'count': len(users),
              'message': '注意：页面显示关注 {} 人，实际收集 {} 人，'
                         '可能有遗漏或混入其他账号'.format(expected, len(users))})

    return {
        'owner': owner,
        'users': [{'name': user['name'] or user['uid'],
                   'url': 'https://www.douyin.com/user/{}'.format(user['uid']),
                   'uid': user['uid']}
                  for user in users],
        'pages': page_index,
        'total': len(users),
        'cancelled': stopped,
    }


def pages_to_lines(result):
    """把分页数据转成「每行一条接口响应」的字符串列表，便于排查问题"""
    return [json.dumps(payload, ensure_ascii=False)
            for payload in result.get('pages') or []]
