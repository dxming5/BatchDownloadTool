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

from core import config
from core import browser
from core.utils import safe_name

LIST_MARK = '/api/sns/web/v1/user_posted'   # 主页笔记列表
FEED_MARK = '/api/sns/web/v1/feed'          # 笔记详情
# 用户资料类响应：只从这里取身份，绝不从笔记/详情的作者字段取
PROFILE_MARKS = ('/api/sns/web/v1/user/', '/api/sns/web/v2/user/')

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


# 身份只从页面级数据取，不遍历笔记作者：页面状态 → 昵称 DOM → 页面标题
PROFILE_NICKNAME_JS = """
() => {
  const state = window.__INITIAL_STATE__;
  const user = (state && state.user) || {};
  const unwrap = (value) => {
    if (value && typeof value === 'object' && value.__v_isRef) return value._value;
    return value;
  };
  const bags = [unwrap(user.userPageData), unwrap(user.userPage),
                unwrap(user.userInfo), unwrap(user.user), unwrap(user)];
  for (const bag of bags) {
    if (!bag || typeof bag !== 'object') continue;
    const basic = unwrap(bag.basicInfo) || unwrap(bag.basic_info) || {};
    const name = basic.nickname || basic.nickName ||
                 bag.nickname || bag.nickName || bag.name;
    if (name) return String(name).trim();
  }
  return '';
}
"""

DOM_NICKNAME_JS = """
() => {
  const selectors = ['.user-name', '.nickname', '.username',
                     '.user-info .name', '.user-detail .name',
                     '[class*="nickname"]', '[class*="userName"]'];
  for (const selector of selectors) {
    const node = document.querySelector(selector);
    if (!node) continue;
    const text = (node.innerText || node.textContent || '').trim();
    if (text) return text;
  }
  return '';
}
"""


# 标题兜底常见的是站点名而不是博主名，拿它当身份会建错目录
GENERIC_NICKNAMES = ('小红书', '小红书 - 小红书')


def _nickname_from_page(page):
    """从页面自身取博主昵称：页面状态 → 昵称 DOM → 标题按 ' - ' 截断"""
    for script in (PROFILE_NICKNAME_JS, DOM_NICKNAME_JS):
        try:
            value = (page.evaluate(script) or '').strip()
        except Exception:
            value = ''
        value = safe_name(value)
        if value and value not in GENERIC_NICKNAMES:
            return value
    try:
        value = safe_name(page.title().split(' - ')[0].strip())
    except Exception:
        value = ''
    return value if value and value not in GENERIC_NICKNAMES else ''


def _nickname_from_responses(responses, blogger_id='', on_message=None):
    """从登录后的用户资料响应取昵称；只认用户资料响应，不碰笔记/详情作者"""
    def emit(message):
        if on_message:
            try:
                on_message(message)
            except Exception:
                pass

    def walk(node, depth=0):
        if depth > 6 or not isinstance(node, dict):
            return ''
        name = node.get('nickname') or node.get('nickName') or node.get('name')
        if name and isinstance(name, str):
            ids = [str(node.get(key) or '') for key in
                   ('userId', 'user_id', 'id', 'redId', 'red_id')]
            # 没有目标 ID 时只认明确带 user 字段的对象，避免误取其它实体
            if blogger_id:
                matched = any(blogger_id == value for value in ids)
            else:
                matched = any(node.get(key) for key in ('userId', 'user_id'))
            if matched:
                return safe_name(name)
        for key in ('user', 'userInfo', 'basicInfo', 'basic_info',
                    'userPageData', 'userPage', 'data'):
            found = walk(node.get(key), depth + 1)
            if found:
                return found
        return ''

    for response in responses or []:
        url = getattr(response, 'url', '')
        # /user/me 是当前登录账号，不是目标博主，直接忽略
        if '/user/me' in url:
            continue
        try:
            payload = browser.response_json(response)
        except Exception:
            continue
        value = walk(payload)
        if value and value not in GENERIC_NICKNAMES:
            emit('身份资料响应：{} → {}'.format(
                getattr(response, 'url', ''), value))
            return value
        emit('身份资料响应未取到昵称：{}'.format(getattr(response, 'url', '')))
    return ''


def read_nickname(page, responses=(), blogger_id='', on_message=None):
    """登录后取博主昵称：用户资料响应优先，其次页面状态 / DOM / 标题

    只负责「昵称从哪读」，不构造身份；取不到返回空串，由编排层判定身份
    不完整并中止。来源写进日志，便于排查取错身份的现场。
    """
    def emit(message):
        if on_message:
            try:
                on_message(message)
            except Exception:
                pass

    value = _nickname_from_responses(responses, blogger_id, on_message)
    if value:
        emit('博主昵称来源：用户资料响应')
        return value
    value = _nickname_from_page(page)
    emit('博主昵称来源：页面状态/DOM/标题')
    return value


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


def fill_details(context, notes, options, on_article=None, should_stop=None,
                 verbose=False, skip_ids=None, on_message=None):
    """
    流式取详情：列表数据只用于发现笔记，详情到手立即交出，不保存全量列表

    没有 xsec_token 或详情打不开的笔记直接丢弃，不拿列表数据凑数；
    已经下载过的（skip_ids 命中）连详情页都不打开。

    Args:
        on_article: 每取到一条详情立即回调 on_article(detail)；返回 False 叫停

    Returns:
        dict: {'found': 本批条数, 'filled': 取到条数, 'known': 已下载跳过条数,
               'skipped': 丢弃条数, 'stopped': 是否被回调叫停/取消}
    """
    skip_ids = set(skip_ids or ())
    timeout_ms = max(2000, int(options.get('timeout_ms') or 30000))

    targets = list(notes)
    stats = {'found': len(targets), 'filled': 0, 'known': 0,
             'skipped': 0, 'stopped': False}

    def emit(message):
        if verbose:
            print('  ' + message)
        if on_message:
            try:
                on_message(message)
            except Exception:
                pass

    for index, note in enumerate(targets, 1):
        if should_stop and should_stop():
            stats['stopped'] = True
            break

        note_id = note_id_of(note)
        if not note_id or not has_token(note):
            stats['skipped'] += 1
            continue
        if str(note_id) in skip_ids:
            stats['known'] += 1
            # 不打开详情页，但仍回调一次让会话计入「已下载跳过」，
            # 否则「全部已下载」会因为没有候选条目被误判成无内容
            if on_article and on_article({'note_id': note_id, '_known': True}) is False:
                stats['stopped'] = True
                break
            continue
        emit('取详情 {}/{}…'.format(index, len(targets)))
        detail = open_detail(context, note, timeout_ms)
        if not detail:
            stats['skipped'] += 1
            continue

        detail = dict(detail)
        # 详情偶尔不带 ID：只补标识，不动内容字段
        if not note_id_of(detail):
            detail['note_id'] = note_id
        stats['filled'] += 1
        if on_article and on_article(detail) is False:
            stats['stopped'] = True
            break

    return stats


def fetch_user_notes(target, options=None, since_ts=None,
                     on_progress=None, should_stop=None,
                     on_meta=None, on_article=None, verbose=False):
    """
    流式抓取某博主的主页笔记

    顺序固定：打开主页只等 DOM 就绪 → 登录闸门 → 先挂监听再重建目标页
    （清空登录前数据）→ 读 SSR、取页面昵称并上报 meta → 首屏与滚动发现
    笔记，每发现一批就逐条取详情立即交给 on_article，不保存全量详情列表。

    Args:
        target:       主页链接（建议带 xsec_token）
        options:      抓取参数（profile_dir / headless / wait_login / max_scroll /
                      idle_rounds / timeout_ms / browser_channel / proxy）
        since_ts:     只保留发布时间 >= since_ts 的笔记
        on_progress:  进度/日志回调
        should_stop:  取消检查函数
        on_meta:      on_meta(nickname) -> dict
                      返回 {'known_ids': set, 'ok': bool}；必须早于 on_article
        on_article:   on_article(detail) -> bool，返回 False 表示停止
        verbose:      是否打印到控制台

    Returns:
        dict: pages / count / cancelled / stop_reason / unknown_types / details
    """
    from playwright.sync_api import sync_playwright

    options = options or {}
    user_id, user_url = parse_user_id(target)
    profile_dir = options.get('profile_dir') or config.platform_profile_dir('xiaohongshu')
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
    profile_pending = []    # 登录后的用户资料响应，用来取身份
    queue = []              # 待取详情的小队列，详情到手即出队
    seen_ids = set()
    state = {'has_more': 1, 'cursor': ''}
    page_index = 0
    stale_run = 0
    early_stop = False
    skip_ids = set()          # 已下载笔记 ID，由 meta 回传后填入
    detail_stats = {'found': 0, 'filled': 0, 'known': 0, 'skipped': 0}
    stop_reason = 'max_scroll'
    nickname = ''
    stopped = False
    meta_ok = False

    def log(text):
        if verbose:
            print('  ' + text)

    def emit(event):
        if on_progress:
            try:
                on_progress(event)
            except Exception:
                pass

    def note(message):
        if verbose:
            print('  ' + message)
        emit({'stage': 'scrape', 'message': message})

    def cancelled():
        return bool(should_stop and should_stop())

    def accept(note):
        """发现一条笔记：去重 + 时间过滤后入小队；返回是否新增"""
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
        queue.append(note)
        return True

    def on_response(response):
        if response.status != 200:
            return
        if any(mark in response.url for mark in PROFILE_MARKS):
            profile_pending.append(response)
        elif LIST_MARK in response.url:
            pending.append(response)

    def drain():
        """把已捕获的列表响应解析入小队（此刻还没到取详情阶段）"""
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

            for note in raw_notes:
                if isinstance(note, dict):
                    accept(note)

            page_index += 1
            message = '第 {} 页：本页 {} 条，累计 {} 条，has_more={}'.format(
                page_index, len(raw_notes), len(seen_ids), data.get('has_more'))
            log(message)
            emit({'stage': 'scrape', 'pages': page_index,
                  'count': len(seen_ids), 'message': message})

            if payload.get('success') is False:
                log('接口返回失败：code={} msg={}'.format(
                    payload.get('code'), payload.get('msg')))

    def process_queue():
        """把发现的一批笔记逐条取详情并立即交出；返回是否被叫停"""
        nonlocal stopped, stop_reason
        if not queue:
            return False
        batch = queue[:]
        del queue[:]
        stats = fill_details(context, batch, options,
                             on_article=on_article,
                             should_stop=should_stop,
                             skip_ids=skip_ids, verbose=verbose,
                             on_message=note)
        for key in ('found', 'filled', 'known', 'skipped'):
            detail_stats[key] += stats[key]
        if stats['stopped']:
            stopped = True
            stop_reason = '下载已停止'
            return True
        return False

    emit({'stage': 'scrape', 'pages': 0, 'count': 0, 'message': '正在启动浏览器…'})

    with sync_playwright() as playwright:
        context = browser.launch_context(
            playwright, profile_dir, headless,
            channel=channel, proxy=proxy)
        try:
            page = context.pages[0] if context.pages else context.new_page()

            # 1. goto 只等 DOM 就绪：登录前不挂监听、不读状态、不发数据请求
            page.goto(user_url, wait_until='domcontentloaded', timeout=timeout_ms)
            emit({'stage': 'scrape', 'pages': 0, 'count': 0,
                  'message': '已打开主页，等待登录态…'})

            # 2. 登录闸门：未登录/超时一律中止，不下载任何内容
            logged_in = browser.ensure_logged_in(
                page, LOGIN_COOKIE_NAMES, LOGIN_MODAL_SELECTOR,
                wait_login, should_stop, on_progress,
                required_label='web_session')
            if not logged_in:
                raise RuntimeError(
                    '未登录 小红书：请在浏览器窗口完成登录后重试（登录态保存在 {}）'.format(
                        profile_dir))

            # 3. 登录成功后重建页面数据：先清掉登录前的采集缓冲并挂监听，
            #    再重新进入目标页——刷新过程发出的列表响应同样是登录后数据，
            #    先挂才不会把首屏接口响应全部漏掉（表现为「0 条作品」）。
            def attach(target_page):
                pending[:] = []
                profile_pending[:] = []
                pages[:] = []
                del queue[:]
                seen_ids.clear()
                target_page.on('response', on_response)

            browser.reload_after_login(page, user_url, timeout_ms,
                                       on_before_reload=attach)
            note('已登录，刷新页面后重新获取')

            # 4. 刷新后读 SSR、取身份并上报 meta
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

            # 刷新过程捕获的列表响应先解析，避免等到第一次滚动才处理
            drain()
            note('刷新后：页面状态 {} 条，列表响应 {} 页，累计发现 {} 条'.format(
                len(first_page), page_index, len(seen_ids)))

            blogger_id, _user_url = parse_user_id(target)
            # 用户资料响应可能比 DOM 晚到，短暂等待后优先从响应里取昵称
            waited = 0
            while not profile_pending and waited < 5000:
                if cancelled():
                    break
                page.wait_for_timeout(500)
                waited += 500
            nickname = read_nickname(page, profile_pending, blogger_id, note)
            if on_meta:
                info = on_meta(nickname) or {}
                meta_ok = bool(info.get('ok'))
                # 已下载笔记 ID 由编排层回传，采集器据此连详情页都不打开
                skip_ids |= set(info.get('known_ids') or ())
            else:
                meta_ok = bool(nickname)

            if not meta_ok:
                stop_reason = '身份信息不完整'
                stopped = True
            else:
                for item in first_page:
                    accept(item)
                if first_page:
                    note('首屏 {} 条，累计 {} 条'.format(
                        len(first_page), len(seen_ids)))
                if not process_queue():
                    # 5. 滚动翻页与详情交替推进，不保存全量详情
                    last_total = len(seen_ids)
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
                        scroll_info = browser.scroll_step(page, LIST_SELECTORS)
                        page.wait_for_timeout(1200)
                        drain()
                        if process_queue():
                            break
                        if len(seen_ids) > last_total:
                            last_total = len(seen_ids)
                            idle = 0
                        else:
                            idle += 1
                            if idle % 2 == 0:
                                note('连续 {} 次无新增（滚动容器 {} 个，位移 {} 个）'.format(
                                    idle, (scroll_info or {}).get('targets', 0),
                                    (scroll_info or {}).get('moved', 0)))
                            if idle >= idle_rounds:
                                stop_reason = '连续 {} 次无新增'.format(idle)
                                break
                    else:
                        stop_reason = '达到 max_scroll 上限'
        finally:
            try:
                context.close()
            except Exception:
                pass

    summary = '抓取结束：{}（发现 {} 条，取到详情 {} 条）'.format(
        stop_reason, detail_stats['found'], detail_stats['filled'])
    log(summary)
    note(summary)
    if detail_stats['known']:
        log('已下载跳过：{} 条（未打开详情页）'.format(detail_stats['known']))
    if detail_stats['skipped']:
        log('详情未取到：丢弃 {} 条（无 xsec_token 或详情打不开）'.format(
            detail_stats['skipped']))
    emit({'stage': 'scrape_done', 'pages': page_index, 'count': len(seen_ids),
          'message': '抓取结束：{}，共 {} 条'.format(stop_reason, len(seen_ids))})

    return {
        'user_id': user_id,
        'user_url': user_url,
        'nickname': nickname,
        'pages': page_index,
        'count': len(seen_ids),
        'cancelled': stopped,
        'stop_reason': stop_reason,
        'unknown_types': {},
        'details': detail_stats,
        'since_ts': since_ts,
        'error': '',
        'fetch_time': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
