# -*- coding: utf-8 -*-
"""
小红书主页笔记抓取

与抖音最大的不同有三处，都靠真实浏览器规避，不手写签名：

1. 首屏约 60 条由页面状态下发（SSR），首页不发列表请求，
   只监听接口会整页丢失，因此先读页面状态再监听接口分页；
2. 列表只用来发现条目，数据不全（可能不带媒体地址），一律带 xsec_token
   打开详情取完整数据；详情页对 xsec_source 敏感，两个来源参数都试一次；
3. 登录态看 web_session（a1 只是设备标识），浏览器数据目录与抖音隔离。

笔记字段读取（ID / 作者 / 时间 / token）也在这里；图文视频判定、去水印、
选码率与下载组装在 assemble.py。
"""

import json
import re
import time

from conf import settings
from core import browser
from core.utils import safe_name

LIST_MARK = '/api/sns/web/v1/user_posted'   # 主页笔记列表
FEED_MARK = '/api/sns/web/v1/feed'          # 笔记详情

LOGIN_COOKIE_NAMES = ('web_session',)
LOGIN_MODAL_SELECTOR = ('.login-container, .login-modal, .login-btn,'
                        ' #login-btn, .sign-in')

# 笔记列表容器：滚轮必须发到容器内部，否则落在空白处空转，
# 连续几次无新增就提前结束翻页，表现为只抓到最近一部分、越往下越容易漏
LIST_SELECTORS = (
    '#userPostedFeeds',
    '.feeds-page .feeds-container',
    '.feeds-container',
    '.note-list',
)

# 抓取范围早停阈值：连续这么多条笔记都早于截止时间就停止翻页
DEFAULT_STALE_LIMIT = 12

# 等详情接口响应的上限；响应一到就返回，不靠固定间隔轮询
DETAIL_WAIT_MS = 3000

# 首屏由 SSR 下发；结构是 arr(5) → arr(60) → note，这里递归收集原始 note 对象，
# 不做字段裁剪——noteCard 里可能已经带了图片列表，能省掉补详情
SSR_NOTES_JS = """
() => {
  const state = window.__INITIAL_STATE__;
  const user = (state && state.user) || {};
  let notes = user.notes;
  if (notes && typeof notes === 'object' && notes.__v_isRef) notes = notes._value;

  const out = [];
  const seen = {};
  const walk = (node, depth) => {
    if (depth > 5 || !node || typeof node !== 'object') return;
    if (Array.isArray(node)) {
      node.forEach((item) => walk(item, depth + 1));
      return;
    }
    const id = node.id || node.noteId || node.note_id;
    const card = node.noteCard || node.note_card || null;
    if (id && (card || node.type)) {
      if (!seen[id]) {
        seen[id] = 1;
        out.push(node);
      }
      return;
    }
    Object.keys(node).forEach((key) => walk(node[key], depth + 1));
  };
  walk(notes, 0);
  return JSON.stringify(out);
}
"""

# 详情页可能把笔记数据直接写进页面状态，不走 XHR
NOTE_STATE_JS = """
(noteId) => {
  const state = window.__INITIAL_STATE__;
  if (!state || !state.note) return null;
  const map = state.note.noteDetailMap || {};
  let entry = map[noteId];
  if (!entry) {
    const values = Object.values(map);
    entry = values.length ? values[0] : null;
  }
  if (!entry) return null;
  const note = entry.note || entry;
  try { return JSON.parse(JSON.stringify(note)); } catch (error) { return null; }
}
"""


# ---------------- 字段读取 ----------------

def note_card_of(note):
    """列表项里的 note_card（不同版本字段名大小写不一）"""
    return note.get('note_card') or note.get('noteCard') or {}


def image_list_of(note):
    """图片列表（兼容 imageList / image_list）"""
    return note.get('imageList') or note.get('image_list') or []


def note_id_of(note):
    return note.get('note_id') or note.get('noteId') or note.get('id')


def author_of(note):
    """作者昵称：列表项可能在顶层，也可能在 note_card 里"""
    for source in (note, note_card_of(note)):
        user = source.get('user') or {}
        nickname = user.get('nickname') or user.get('nickName') or ''
        if nickname:
            return safe_name(nickname)
    return ''


def created_at(note):
    """发布时间统一为秒（小红书给的是毫秒）"""
    for source in (note, note_card_of(note)):
        value = source.get('time')
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        if value > 10 ** 11:      # 毫秒
            value //= 1000
        return value
    return 0


def has_token(note):
    """是否带 xsec_token（没有它打不开详情）"""
    for source in (note, note_card_of(note)):
        if source.get('xsec_token') or source.get('xsecToken'):
            return True
    return False


def parse_user_id(raw):
    """
    从主页链接取 user_id 与规范化后的主页地址

    Returns:
        tuple: (user_id, user_url)
            保留原始链接（含 xsec_token），部分内容不带 token 会看不到
    """
    raw = (raw or '').replace('\ufeff', '').replace('\u200b', '').strip()
    raw = raw.strip('"').strip("'")
    if not raw:
        raise ValueError('缺少小红书主页链接')

    match = re.search(r'/user/profile/([0-9a-zA-Z]+)', raw)
    if match and match.group(1) != 'profile':
        return match.group(1), raw

    if re.match(r'^[0-9a-fA-F]{24}$', raw):
        return raw, 'https://www.xiaohongshu.com/user/profile/' + raw

    raise ValueError('无法从内容中识别小红书 user_id：{}'.format(raw[:80]))


def read_ssr_notes(page):
    """读取首屏下发的笔记列表；取不到时返回空列表"""
    try:
        raw = page.evaluate(SSR_NOTES_JS)
    except Exception:
        return []
    try:
        data = json.loads(raw or '[]')
    except ValueError:
        return []
    return [item for item in data
            if isinstance(item, dict) and note_id_of(item)]


def read_note_state(page, note_id):
    """读取详情页状态里的笔记数据"""
    try:
        return page.evaluate(NOTE_STATE_JS, note_id)
    except Exception:
        return None


def _feed_note(captured):
    """从已捕获的详情接口响应里取出笔记数据"""
    while captured:
        payload = browser.response_json(captured.pop(0))
        if not isinstance(payload, dict):
            continue
        items = (payload.get('data') or {}).get('items') or []
        if not items:
            continue
        card = (items[0] or {}).get('note_card') or {}
        if card:
            return card
    return None


def open_detail(context, note, timeout_ms):
    """
    打开一条笔记的详情页并取回完整数据

    详情页对 xsec_source 敏感，两种来源都试；状态与接口两路取到即用。

    Returns:
        dict|None: 笔记数据（字段大小写与列表可能不同，由解析层兼容）
    """
    note_id = note_id_of(note)
    token = (note.get('xsec_token') or note.get('xsecToken')
             or note_card_of(note).get('xsec_token')
             or note_card_of(note).get('xsecToken') or '')
    if not note_id or not token:
        return None

    def is_feed_response(response):
        return FEED_MARK in response.url and response.status == 200

    for source in ('pc_user', 'pc_followed'):
        url = ('https://www.xiaohongshu.com/explore/{}'
               '?xsec_token={}&xsec_source={}').format(note_id, token, source)
        page = context.new_page()
        captured = []

        def on_response(response, bucket=captured):
            if is_feed_response(response):
                bucket.append(response)

        page.on('response', on_response)

        detail = None
        try:
            page.goto(url, wait_until='domcontentloaded', timeout=timeout_ms)

            # 部分详情直接写进页面状态，先读一次；没有再看已到达的接口响应
            detail = read_note_state(page, note_id) or _feed_note(captured)

            if not detail:
                # 等详情接口的响应：数据一到立刻返回，不再按固定间隔轮询
                try:
                    with page.expect_response(is_feed_response,
                                              timeout=min(timeout_ms, DETAIL_WAIT_MS)) as info:
                        pass
                    detail = _feed_note([info.value])
                except Exception:
                    # 等待期间响应也可能已被 on_response 收下
                    detail = _feed_note(captured)
        except Exception:
            detail = detail or _feed_note(captured)
        finally:
            try:
                page.close()
            except Exception:
                pass

        if detail:
            return detail
    return None


def fill_details(context, notes, options, on_progress=None, should_stop=None,
                 verbose=False, skip_ids=None):
    """
    逐条取详情：列表数据只用于发现笔记，下载一律用详情

    没有 xsec_token 或详情打不开的笔记直接丢弃，不拿列表数据凑数；
    已经下载过的（skip_ids 命中）连详情页都不打开。

    Returns:
        tuple: (details, stats)
            details: 详情数据列表
            stats:   {'found': 发现条数, 'filled': 取到条数,
                      'known': 已下载跳过条数, 'skipped': 丢弃条数}
    """
    skip_ids = set(skip_ids or ())
    timeout_ms = max(2000, int(options.get('timeout_ms') or 30000))

    targets = list(notes)

    def emit(message):
        if verbose:
            print('  ' + message)
        if on_progress:
            try:
                on_progress({'stage': 'scrape', 'message': message})
            except Exception:
                pass

    details = []
    known = 0
    for index, note in enumerate(targets, 1):
        if should_stop and should_stop():
            break

        note_id = note_id_of(note)
        if not note_id or not has_token(note):
            continue
        if str(note_id) in skip_ids:
            known += 1
            continue
        emit('取详情 {}/{}…'.format(index, len(targets)))
        detail = open_detail(context, note, timeout_ms)
        if not detail:
            continue

        detail = dict(detail)
        # 详情偶尔不带 ID：只补标识，不动内容字段
        if not note_id_of(detail):
            detail['note_id'] = note_id
        details.append(detail)

    return details, {'found': len(targets), 'filled': len(details), 'known': known,
                     'skipped': len(targets) - len(details) - known}


def fetch_user_notes(target, options=None, since_ts=None,
                     on_progress=None, should_stop=None, known_ids=None,
                     verbose=False):
    """
    抓取某博主的主页笔记

    Args:
        target:       主页链接（建议带 xsec_token）
        options:      抓取参数（profile_dir / headless / wait_login / max_scroll /
                      idle_rounds / timeout_ms / browser_channel / proxy）
        since_ts:     只保留发布时间 >= since_ts 的笔记
        on_progress:  进度回调
        should_stop:  取消检查函数
        verbose:      是否打印到控制台

    Returns:
        dict: user_id / user_url / nickname / pages / notes / has_more /
              restricted / cancelled / details / fetch_time
    """
    from playwright.sync_api import sync_playwright

    options = options or {}
    user_id, user_url = parse_user_id(target)
    profile_dir = options.get('profile_dir') or settings.platform_profile_dir('xiaohongshu')
    headless = bool(options.get('headless', True))
    max_scroll = max(1, int(options.get('max_scroll') or 200))
    idle_rounds = max(1, int(options.get('idle_rounds') or 4))
    timeout_ms = max(5000, int(options.get('timeout_ms') or 30000))
    channel = options.get('browser_channel')
    proxy = options.get('proxy')
    wait_login = max(0, int(options.get('wait_login') or 0))
    stale_limit = max(1, int(options.get('stale_limit') or DEFAULT_STALE_LIMIT))

    pages = []
    pending = []
    notes = []
    seen_ids = set()
    state = {'has_more': 1, 'cursor': ''}
    page_index = 0
    stale_run = 0
    early_stop = False

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

    def accept(note):
        """入库一条笔记；返回是否新增（按 since_ts 过滤并驱动早停）"""
        nonlocal stale_run, early_stop
        note_id = note_id_of(note)
        if not note_id or note_id in seen_ids:
            return False
        created = created_at(note)
        if since_ts and created and created < since_ts:
            stale_run += 1
            if stale_run >= stale_limit:
                early_stop = True
            return False
        stale_run = 0
        seen_ids.add(note_id)
        notes.append(note)
        return True

    def on_response(response):
        if LIST_MARK in response.url and response.status == 200:
            pending.append(response)

    def drain():
        """把已捕获的列表响应解析入队"""
        nonlocal page_index
        while pending:
            response = pending.pop(0)
            payload = browser.response_json(response)
            if not isinstance(payload, dict):
                continue

            data = payload.get('data') or {}
            raw_notes = data.get('notes') or []
            pages.append(payload)
            state['has_more'] = data.get('has_more', state['has_more'])
            state['cursor'] = data.get('cursor', state['cursor'])

            added = 0
            for note in raw_notes:
                if isinstance(note, dict) and accept(note):
                    added += 1

            page_index += 1
            message = '第 {} 页：本页 {} 条，累计 {} 条，has_more={}'.format(
                page_index, len(raw_notes), len(notes), data.get('has_more'))
            log(message)
            emit({'stage': 'scrape', 'pages': page_index,
                  'count': len(notes), 'message': message})

            if payload.get('success') is False:
                log('接口返回失败：code={} msg={}'.format(
                    payload.get('code'), payload.get('msg')))

    stop_reason = 'max_scroll'
    restricted = False
    nickname = ''
    stopped = False
    detail_notes = []
    detail_stats = {'found': 0, 'filled': 0, 'known': 0, 'skipped': 0}

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
                  'message': '已打开主页，等待笔记列表…'})

            # 首屏：页面状态下发，不发列表请求；状态是增量填充的，稍等再读一次取更全
            first_page = read_ssr_notes(page)
            if not first_page:
                waited = 0
                while not first_page and waited < 10000:
                    if cancelled():
                        break
                    page.wait_for_timeout(500)
                    waited += 500
                    first_page = read_ssr_notes(page)
            page.wait_for_timeout(800)
            later = read_ssr_notes(page)
            if len(later) > len(first_page):
                first_page = later

            for note in first_page:
                accept(note)
            if first_page:
                emit({'stage': 'scrape', 'pages': 0, 'count': len(notes),
                      'message': '首屏 {} 条，累计 {} 条'.format(len(first_page), len(notes))})

            # 等分页接口；期间可人工登录、过滑块
            waited = 0
            while not pages and waited < wait_login * 1000:
                if cancelled():
                    stop_reason = '用户取消'
                    stopped = True
                    break
                page.wait_for_timeout(1000)
                waited += 1000
                drain()
                if waited % 15000 == 0:
                    logged, modal = browser.login_state(
                        page, LOGIN_COOKIE_NAMES, LOGIN_MODAL_SELECTOR)
                    log('已等待 {} 秒：登录态={}{}'.format(
                        waited // 1000, '已登录' if logged else '未登录',
                        '，页面有登录入口' if modal else ''))

            logged, modal = browser.login_state(
                page, LOGIN_COOKIE_NAMES, LOGIN_MODAL_SELECTOR)
            if not logged or modal:
                logged_in = browser.wait_for_login(
                    page, wait_login, LOGIN_COOKIE_NAMES, LOGIN_MODAL_SELECTOR,
                    should_stop=should_stop, on_progress=on_progress,
                    verbose=verbose, required_label='web_session')
                restricted = not logged_in
                if logged_in:
                    # 登录后重新拉一次，避免匿名态只给首屏
                    page.reload(wait_until='domcontentloaded')
                    waited = 0
                    before = len(pages)
                    while len(pages) == before and waited < 15000:
                        if cancelled():
                            break
                        page.wait_for_timeout(1000)
                        waited += 1000
                        drain()
            if not notes and not pages and not stopped:
                raise RuntimeError(
                    '未抓取到任何笔记：可能未登录、登录已失效、xsec_token 已过期，'
                    '或该主页没有公开笔记（请在浏览器窗口确认后重试）')

            # 滚动翻页
            last_total = len(notes)
            idle = 0
            for _index in range(max_scroll):
                if cancelled():
                    stop_reason = '用户取消'
                    stopped = True
                    break
                if early_stop:
                    stop_reason = '已到时间范围边界'
                    break
                if state['has_more'] == 0:
                    stop_reason = 'has_more=0'
                    break
                info = browser.scroll_step(page, LIST_SELECTORS)
                page.wait_for_timeout(1200)
                drain()
                if len(notes) > last_total:
                    last_total = len(notes)
                    idle = 0
                else:
                    idle += 1
                    if idle % 2 == 0:
                        log('连续 {} 次无新增（滚动容器 {} 个，位移 {} 个）'.format(
                            idle, (info or {}).get('targets', 0), (info or {}).get('moved', 0)))
                    if idle >= idle_rounds:
                        stop_reason = '连续 {} 次无新增'.format(idle)
                        break
            else:
                stop_reason = '达到 max_scroll 上限'

            # 列表只用于发现条目，逐条取详情，下载一律用详情数据
            if notes and not stopped:
                detail_notes, detail_stats = fill_details(
                    context, notes, options,
                    on_progress=on_progress,
                    should_stop=should_stop,
                    skip_ids=known_ids,
                    verbose=verbose)

            for note in detail_notes:
                nickname = author_of(note)
                if nickname:
                    break
            if not nickname:
                try:
                    nickname = page.title().split(' - ')[0].strip()
                except Exception:
                    nickname = ''
        finally:
            try:
                context.close()
            except Exception:
                pass

    log('抓取结束：{}（{} 条，取到详情 {} 条）'.format(
        stop_reason, len(detail_notes), detail_stats['filled']))
    if detail_stats['known']:
        log('已下载跳过：{} 条（未打开详情页）'.format(detail_stats['known']))
    if detail_stats['skipped']:
        log('详情未取到：丢弃 {} 条（无 xsec_token 或详情打不开）'.format(
            detail_stats['skipped']))
    emit({'stage': 'scrape_done', 'pages': page_index, 'count': len(detail_notes),
          'message': '抓取结束：{}，共 {} 条'.format(stop_reason, len(detail_notes))})

    return {
        'user_id': user_id,
        'user_url': user_url,
        'nickname': nickname,
        'pages': pages,
        'notes': detail_notes,
        'has_more': state.get('has_more'),
        'restricted': restricted,
        'cancelled': stopped,
        'details': detail_stats,
        'since_ts': since_ts,
        'fetch_time': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
