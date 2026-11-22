/* 抖音批量下载器 - 前端逻辑 */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };

  var state = {
    taskId: null,
    polling: false,
    logLength: 0,
    galleryName: '',
    galleryPlatform: '',
    galleryLabel: '',
    galleryKind: 'image',
    galleryItems: [],      /* 当前博主的全部作品（图片 + 视频），切换标签复用 */
    galleryLoading: false,
    galleryScroll: { image: 0, video: 0 },   /* 两个标签各自的滚动位置 */
    scrapeReady: true,
    lastPhase: '',
    platforms: [],          /* [{key, label}]，来自 /api/status，新增平台自动出现 */
    downloadMode: 'link',   /* link / following：两种下载方式同级切换 */
    platformFilter: '',     /* 博主列表的平台筛选，空表示全部 */
    bloggers: [],           /* 最近一次拉到的分组数据，批量更新时用 */
    /* 关注列表：users 为清单，queued 记录已入队的链接，picked 为当前勾选数 */
    following: null,
    followingPicked: 0,
    /* 批量任务：mode 为 'scrape'（依次抓取链接）或 'update'（依次更新博主） */
    batch: null             /* {mode,queue,total,index,done,failed,errors,stopped,range,current} */
  };

  /* ---------------- 基础请求 ---------------- */

  async function api(path, options) {
    var res = await fetch(path, options || {});
    var data = {};
    try { data = await res.json(); } catch (e) { /* 非 JSON 响应 */ }
    if (!res.ok) {
      throw new Error(data.error || ('请求失败 (HTTP ' + res.status + ')'));
    }
    return data;
  }

  function postJSON(path, body) {
    return api(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    });
  }

  var toastTimer = null;
  function toast(message, isError) {
    var box = $('toast');
    box.textContent = message;
    box.className = 'toast' + (isError ? ' error' : '');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { box.className = 'toast hidden'; }, 3200);
  }

  /* ---------------- 确认框 ---------------- */

  var confirmResolver = null;

  /* 页内确认框，替代原生 confirm：resolve(true/false) */
  function confirmDialog(message) {
    return new Promise(function (resolve) {
      confirmResolver = resolve;
      $('confirmText').textContent = message;
      $('confirmModal').classList.remove('hidden');
      $('confirmOk').focus();
    });
  }

  function isConfirmOpen() {
    return !$('confirmModal').classList.contains('hidden');
  }

  function closeConfirm(result) {
    var resolve = confirmResolver;
    confirmResolver = null;
    $('confirmModal').classList.add('hidden');
    if (resolve) resolve(result);
  }

  function onConfirmKey(event) {
    if (!isConfirmOpen() || event.key !== 'Escape') return;
    event.preventDefault();
    closeConfirm(false);
  }

  /* ---------------- 状态 ---------------- */

  async function refreshStatus() {
    try {
      var data = await api('/api/status');
      applyScrapeReady(data.scrape_ready);
      renderPlatformOptions(data.platforms);
      if (data.active_task && !state.taskId) {
        state.taskId = data.active_task.id;
        renderTask(data.active_task);
        pollTask();
      }
    } catch (error) {
      toast(error.message, true);
    }
  }

  /* 平台下拉框：选项来自后端注册的平台，新增平台无需改前端 */
  function renderPlatformOptions(platforms) {
    if (!platforms || !platforms.length) return;
    state.platforms = platforms;
    var select = $('platformFilter');
    var current = state.platformFilter;
    select.innerHTML = '';
    var all = document.createElement('option');
    all.value = '';
    all.textContent = '全部平台';
    select.appendChild(all);
    platforms.forEach(function (platform) {
      var option = document.createElement('option');
      option.value = platform.key;
      option.textContent = platform.label;
      select.appendChild(option);
    });
    /* 之前选中的平台被移除时回落到全部 */
    if (current && !platformLabel(current)) {
      state.platformFilter = '';
      current = '';
    }
    select.value = current;
    renderFollowingPlatforms(state.platforms);
  }

  /* 「获取我的关注」的平台下拉：只列出支持抓关注列表的平台 */
  function renderFollowingPlatforms(platforms) {
    var select = $('followingPlatform');
    var supported = (platforms || []).filter(function (platform) {
      return platform.following;
    });
    var current = select.value;
    select.innerHTML = '';
    supported.forEach(function (platform) {
      var option = document.createElement('option');
      option.value = platform.key;
      option.textContent = platform.label;
      select.appendChild(option);
    });
    if (current) select.value = current;
    /* 支持几个平台就显示几个；一个都没有时才隐藏整行 */
    select.classList.toggle('hidden', supported.length === 0);
    if (!supported.length) {
      $('followingBtn').title = '当前没有支持抓取关注列表的平台';
      $('followingHint').textContent = '当前没有支持抓取关注列表的平台';
      $('followingHint').classList.remove('hidden');
    } else {
      /* 顶部已有说明文案，这里只在异常时显示告警 */
      $('followingBtn').title = '';
      $('followingHint').textContent = '';
      $('followingHint').classList.add('hidden');
    }

    /* 没有平台支持时，「关注列表下载」模式也置灰并退回链接下载 */
    var modeSelect = $('downloadMode');
    var followingOption = modeSelect.querySelector('option[value="following"]');
    if (followingOption) followingOption.disabled = !supported.length;
    if (!supported.length && modeSelect.value === 'following') {
      setDownloadMode('link');
    }

    syncUpdateBtn();
  }

  function platformLabel(key) {
    for (var i = 0; i < state.platforms.length; i++) {
      if (state.platforms[i].key === key) return state.platforms[i].label;
    }
    return '';
  }

  /* Playwright 缺失时禁用抓取入口 */
  function applyScrapeReady(ready) {
    if (ready === undefined) return;
    state.scrapeReady = !!ready;
    var btn = $('downloadBtn');
    btn.disabled = !state.scrapeReady;
    btn.title = state.scrapeReady ? '' : '未安装 playwright，请执行 pip install playwright 与 playwright install chromium';
    syncUpdateBtn();
  }

  /* 有任务在跑或缺少 playwright 时禁用「更新」「获取我的关注」等入口 */
  function syncUpdateBtn() {
    var busy = !state.scrapeReady || !!state.taskId || !!state.batch;
    $('updateAllBtn').disabled = busy;
    $('downloadMode').disabled = busy;      /* 任务中不允许切换下载方式 */
    $('followingBtn').disabled = busy || !$('followingPlatform').options.length;
    updateFollowingPicked();
  }

  /* 统一的「下载」按钮：两种方式共用，按当前模式分派 */
  function syncDownloadBtn() {
    var busy = !state.scrapeReady || !!state.taskId || !!state.batch;
    /* 关注模式还没勾人时不能点，链接模式由提交时自行校验链接 */
    var empty = state.downloadMode === 'following' && !state.followingPicked;
    $('downloadBtn').disabled = busy || empty;
  }

  /* 下载方式：链接下载 / 关注列表下载，两者同级，只切换各自的输入区 */
  function setDownloadMode(mode) {
    var value = mode === 'following' ? 'following' : 'link';
    var following = value === 'following';
    state.downloadMode = value;
    $('downloadMode').value = value;
    $('linkPane').classList.toggle('hidden', following);
    $('followingPane').classList.toggle('hidden', !following);
    updateFollowingPicked();
  }

  /* 关注列表的勾选统计：计数 + 共用下载按钮的可用状态 */
  function updateFollowingPicked() {
    var boxes = followingBoxes();
    var picked = 0;
    for (var i = 0; i < boxes.length; i++) {
      if (boxes[i].checked) picked += 1;
    }
    state.followingPicked = picked;
    $('followingPicked').textContent = '已选 ' + picked + ' 位';
    /* 有清单时才显示计数（在关注模式的操作框里） */
    $('followingPicked').classList.toggle('hidden', !boxes.length);
    syncDownloadBtn();
  }

  function followingBoxes() {
    return $('followingList').querySelectorAll('input[type="checkbox"]');
  }

  /* 关注列表浮层开合：纯 CSS 定位，紧贴操作框下沿，展开不影响页面高度 */
  function setFollowingOpen(open) {
    $('followingBox').classList.toggle('hidden', !open);
    $('followingToggle').setAttribute('aria-expanded', open ? 'true' : 'false');
  }

  /* 开始下载后把关注列表浮层收起，让日志与进度占满视野 */
  function collapseFollowing() {
    setFollowingOpen(false);
  }

  /* ---------------- 抓取任务 ---------------- */

  /* 把输入框内容拆成链接列表：空白、中英文逗号/分号/顿号都算分隔，顺带去重 */
  function splitUrls(raw) {
    var seen = {};
    var urls = [];
    (raw || '').split(/[\s,，、;；]+/).forEach(function (part) {
      /* 兼容从聊天工具复制时带的引号与尖括号 */
      var value = part.replace(/^["'<]+/, '').replace(/["'>]+$/, '');
      if (!value || seen[value]) return;
      seen[value] = 1;
      urls.push(value);
    });
    return urls;
  }

  async function startScrape() {
    var urls = splitUrls($('userUrl').value);
    if (!urls.length) {
      toast('请先粘贴博主主页链接', true);
      $('userUrl').focus();
      return;
    }
    if (state.taskId || state.batch) {
      toast('已有任务在执行，请等待或先取消', true);
      return;
    }
    collapseFollowing();
    if (urls.length === 1) {
      await submitScrape(urls[0]);
      return;
    }

    state.batch = {
      mode: 'scrape', queue: urls.slice(), total: urls.length,
      index: 0, done: 0, failed: 0, errors: [], stopped: false,
      range: $('rangeSelect').value
    };
    $('logBox').textContent = '';
    state.logLength = 0;
    $('failureBox').classList.add('hidden');
    syncUpdateBtn();
    nextBatchItem();
  }

  /* 单条链接：提交后在 beginTask 里接管轮询 */
  async function submitScrape(url) {
    /* 请求发出到任务接管之间有间隔，先禁用以防重复提交 */
    $('downloadBtn').disabled = true;
    try {
      var data = await postJSON('/api/scrape', {
        url: url,
        range: $('rangeSelect').value
      });
      beginTask(data.task);
      toast('已开始抓取，浏览器启动后会自动翻页');
    } catch (error) {
      syncDownloadBtn();
      toast(error.message, true);
    }
  }

  /* 任务启动渲染 */
  function beginTask(task) {
    /* 刷新页面后恢复运行中的关注列表任务时，自动切到对应方式 */
    if (task.mode === 'following') setDownloadMode('following');
    state.taskId = task.id;
    state.logLength = 0;
    state.lastPhase = task.phase;
    /* 批量更新时保留前面博主的日志，整批共用一块日志框 */
    if (!state.batch) {
      $('logBox').textContent = '';
      $('failureBox').classList.add('hidden');
    }
    $('cancelBtn').disabled = false;
    renderTask(task);
    pollTask();
  }

  async function pollTask() {
    if (!state.taskId || state.polling) return;
    state.polling = true;
    try {
      var task = await api('/api/tasks/' + state.taskId);
      renderTask(task);
      if (task.status === 'running') {
        setTimeout(function () {
          state.polling = false;
          pollTask();
        }, 500);
        return;
      }
      finishTask(task);
    } catch (error) {
      finishTask({ status: 'error', logs: [error.message], failures: [] });
    }
    state.polling = false;
  }

  function finishTask(task) {
    state.polling = false;
    state.taskId = null;
    $('cancelBtn').disabled = true;
    /* 任务结束后立刻按最新状态恢复「下载」「更新」按钮，否则会一直停留在禁用态 */
    syncUpdateBtn();

    /* 关注列表：结果交给面板，用户勾选后再入队 */
    if (task.mode === 'following') {
      if (task.status === 'error') {
        toast(task.logs && task.logs.length ? task.logs[task.logs.length - 1]
                                            : '读取关注列表失败', true);
        return;
      }
      if (task.status === 'cancelled') {
        toast('已取消读取关注列表');
        return;
      }
      var users = task.following || [];
      state.following = { users: users, queued: {} };
      renderFollowing();
      toast('关注列表已读取：共 ' + users.length + ' 位，勾选后加入队列');
      return;
    }

    /* 批量：一条跑完接着下一条，提示与列表刷新留到整批结束 */
    if (state.batch) {
      var job = state.batch;
      var lastLog = task.logs && task.logs.length ? task.logs[task.logs.length - 1] : '';
      var noNew = lastLog.indexOf('没有可下载内容') !== -1;   /* 已全部下载过：不算失败 */
      if (task.status === 'cancelled') {
        job.stopped = true;
        job.queue.length = 0;
      } else if (task.status === 'error' && !noNew) {
        job.failed += 1;
        job.errors.push((job.current || task.blogger || '') + '：' + (lastLog || '未知错误'));
      } else {
        job.done += 1;
      }
      renderFailures(task.failures || []);
      if (job.stopped || !job.queue.length) {
        endBatch();
      } else {
        nextBatchItem();
      }
      syncUpdateBtn();
      return;
    }

    var scraped = task.mode === 'scrape';
    if (task.status === 'cancelled') {
      toast('任务已取消');
    } else if (task.status === 'error') {
      toast((scraped ? '抓取失败：' : '任务出错：') +
        (task.logs && task.logs.length ? task.logs[task.logs.length - 1] : '未知错误'), true);
    } else {
      toast(scraped ? '抓取并下载完成' : '下载完成');
    }
    renderFailures(task.failures || []);
    refreshBloggers();
  }

  function renderTask(task) {
    var scraping = task.phase === 'scrape' && task.status === 'running';
    var following = task.phase === 'following' && task.status === 'running';
    /* 进度条只表示下载进度：抓取阶段没有可靠总量，条保持不动 */
    var percent = (scraping || following) ? 0 : (task.percent || 0);
    $('bar').style.width = percent + '%';

    /* 条上文字只表示当前任务的进度；关注列表阶段显示已抓到的人数 */
    $('progressText').textContent = following
      ? '已抓取 ' + (task.following_count || 0) + ' 人'
      : percent + '%（' + (task.done || 0) + '/' + (task.total || 0) + '）';

    /* 抓取转入下载时刷新博主列表，让本次新出现的博主立刻显示出来 */
    if (!state.batch && state.lastPhase && state.lastPhase !== task.phase) {
      refreshBloggers();
    }
    state.lastPhase = task.phase;

    if (task.status === 'running') {
      $('cancelBtn').disabled = false;
    }
    appendLogs(task.logs || []);
    if (task.failures && task.failures.length) {
      renderFailures(task.failures);
    }
    syncUpdateBtn();
  }

  function appendLogs(logs) {
    var box = $('logBox');
    if (logs.length < state.logLength) {
      box.textContent = '';
      state.logLength = 0;
    }
    if (logs.length === state.logLength) return;
    var slice = logs.slice(state.logLength);
    state.logLength = logs.length;
    var text = box.textContent === '等待任务…' ? '' : box.textContent;
    box.textContent = text + slice.join('\n') + '\n';
    box.scrollTop = box.scrollHeight;
  }

  function renderFailures(failures) {
    var box = $('failureBox');
    var list = $('failureList');
    list.innerHTML = '';
    if (!failures || !failures.length) {
      box.classList.add('hidden');
      $('failureCount').textContent = '0';
      return;
    }
    box.classList.remove('hidden');
    $('failureCount').textContent = failures.length;
    failures.forEach(function (item) {
      var li = document.createElement('li');
      var file = document.createElement('div');
      file.textContent = item.file;
      var url = document.createElement('span');
      url.className = 'url';
      url.textContent = '复制 URL';
      url.addEventListener('click', function () {
        navigator.clipboard.writeText(item.url).then(function () {
          toast('URL 已复制');
        }).catch(function () {
          toast('复制失败，请手动选择', true);
        });
      });
      var reason = document.createElement('div');
      reason.className = 'hint';
      reason.textContent = item.error;
      li.appendChild(file);
      li.appendChild(url);
      li.appendChild(reason);
      list.appendChild(li);
    });
  }

  async function cancelTask() {
    if (!state.taskId) return;
    try {
      await postJSON('/api/tasks/' + state.taskId + '/cancel');
      toast('已请求取消，将在当前文件结束后停止');
    } catch (error) {
      toast(error.message, true);
    }
  }

  /* ---------------- 关注列表（读取本机登录账号关注的博主） ---------------- */

  async function startFollowing() {
    if (state.taskId || state.batch) {
      toast('已有任务在执行，请等待或先取消', true);
      return;
    }
    var platform = $('followingPlatform').value;
    if (!platform) {
      toast('当前没有支持抓取关注列表的平台', true);
      return;
    }
    state.following = null;
    setFollowingOpen(false);
    $('followingToggle').classList.add('hidden');
    $('followingEmpty').classList.remove('hidden');
    $('followingList').innerHTML = '';
    $('followingBtn').disabled = true;
    try {
      var data = await postJSON('/api/following', { platform: platform });
      beginTask(data.task);
      toast('开始读取关注列表，浏览器启动后会自动翻页');
    } catch (error) {
      syncUpdateBtn();
      toast(error.message, true);
    }
  }

  /* 渲染关注清单：默认只勾没下载过的，已下载的加标签方便一眼分辨 */
  function renderFollowing() {
    var job = state.following;
    var box = $('followingList');
    box.innerHTML = '';
    $('followingAll').checked = false;
    if (!job || !job.users.length) {
      setFollowingOpen(false);
      $('followingToggle').classList.add('hidden');
      $('followingEmpty').classList.remove('hidden');
      $('followingCount').textContent = '0';
      updateFollowingPicked();
      return;
    }

    job.users.forEach(function (user) {
      var row = document.createElement('label');
      row.className = 'following-item';
      row.title = user.url || '';

      var input = document.createElement('input');
      input.type = 'checkbox';
      input.value = user.url || '';
      /* 已下载过的默认不勾，避免重复排队 */
      input.checked = !user.downloaded;
      input.addEventListener('change', updateFollowingPicked);
      row.appendChild(input);

      var name = document.createElement('span');
      name.className = 'name';
      name.textContent = user.name || user.uid || user.url;
      row.appendChild(name);

      if (user.downloaded) {
        var tag = document.createElement('span');
        tag.className = 'tag';
        tag.textContent = '已下载';
        row.appendChild(tag);
      }
      box.appendChild(row);
    });

    $('followingCount').textContent = job.users.length;
    $('followingToggle').classList.remove('hidden');
    $('followingEmpty').classList.add('hidden');
    /* 抓完直接展开浮层方便勾选；点「收起」或下载时关闭 */
    setFollowingOpen(true);
    updateFollowingPicked();
  }

  /* 勾选的博主转成抓取队列，复用现有批量链路（串行、失败不中断） */
  function enqueueFollowing() {
    if (!state.following) return;
    var queue = [];
    var seen = {};
    var boxes = followingBoxes();
    for (var i = 0; i < boxes.length; i++) {
      var url = boxes[i].value;
      if (!boxes[i].checked || !url || seen[url]) continue;
      if (state.following.queued[url]) continue;
      seen[url] = 1;
      queue.push(url);
    }
    if (!queue.length) {
      toast('请先勾选还没入队的博主', true);
      return;
    }
    queue.forEach(function (url) { state.following.queued[url] = 1; });

    /* 入队即开始下载：收起关注列表面板，把日志和进度露出来 */
    collapseFollowing();

    state.batch = {
      mode: 'scrape', queue: queue, total: queue.length, index: 0,
      done: 0, failed: 0, errors: [], stopped: false,
      range: $('rangeSelect').value
    };
    $('logBox').textContent = '';
    state.logLength = 0;
    $('failureBox').classList.add('hidden');
    syncUpdateBtn();
    nextBatchItem();
  }

  /* 共用的「下载」按钮：按当前方式分派到链接提交或勾选入队 */
  function onDownload() {
    if (state.downloadMode === 'following') {
      enqueueFollowing();
    } else {
      startScrape();
    }
  }

  /* ---------------- 博主管理 ---------------- */

  async function loadBloggers() {
    var box = $('bloggers');
    try {
      var data = await api('/api/bloggers');
      state.bloggers = data.groups || [];
      var groups = state.bloggers;
      var filter = state.platformFilter;
      if (filter) {
        groups = groups.filter(function (group) { return group.platform === filter; });
      }
      box.innerHTML = '';
      if (!groups.length) {
        var empty = document.createElement('p');
        empty.className = 'empty';
        empty.textContent = filter
          ? '该平台暂无博主，先导入数据并下载一次。'
          : '暂无博主，先导入数据并下载一次。';
        box.appendChild(empty);
        return;
      }
      groups.forEach(function (group) {
        var head = document.createElement('div');
        head.className = 'blogger-group';
        head.textContent = group.label + ' · ' + group.items.length + ' 位';
        box.appendChild(head);
        group.items.forEach(function (item) {
          box.appendChild(renderBloggerRow(group.platform, group.label, item));
        });
      });
    } catch (error) {
      toast(error.message, true);
    }
  }

  function renderBloggerRow(platform, label, item) {
    var row = document.createElement('div');
    row.className = 'blogger';
    row.dataset.name = item.name;
    row.dataset.platform = platform;
    row.tabIndex = 0;
    row.title = '点击预览「' + item.name + '」的作品（默认图片）';
    if (state.galleryPlatform === platform && state.galleryName === item.name) {
      row.classList.add('active');
    }
    row.addEventListener('click', function () { openGallery(platform, label, item.name); });
    row.addEventListener('keydown', function (event) {
      /* 行内按钮的按键不触发浏览，避免回车删除时顺带打开预览 */
      if (event.target !== row) return;
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      openGallery(platform, label, item.name);
    });

    var name = document.createElement('div');
    name.className = 'name';
    name.textContent = item.name;
    name.title = item.name;

    var actions = document.createElement('div');
    actions.className = 'actions';

    var update = document.createElement('button');
    update.className = 'ghost';
    update.textContent = '更新';
    /* 空 title 阻止继承整行的预览提示，按钮本身不显示提示 */
    update.title = '';
    if (item.homepage) {
      /* 阻止冒泡，点更新不会顺带打开预览 */
      update.addEventListener('click', function (event) {
        event.stopPropagation();
        updateBlogger(platform, item.name);
      });
    } else {
      update.disabled = true;
    }

    var remove = document.createElement('button');
    remove.className = 'ghost danger';
    remove.textContent = '删除';
    remove.title = '';
    remove.addEventListener('click', function (event) {
      event.stopPropagation();
      deleteBlogger(platform, item.name);
    });

    actions.appendChild(update);
    actions.appendChild(remove);

    row.appendChild(name);
    row.appendChild(actions);
    return row;
  }

  function bloggerBase(platform, name) {
    return '/api/bloggers/' + encodeURIComponent(platform) +
      '/' + encodeURIComponent(name);
  }

  async function updateBlogger(platform, name) {
    try {
      var data = await postJSON(bloggerBase(platform, name) + '/update');
      beginTask(data.task);
      toast('开始更新 ' + name + '，已下载的作品会自动跳过');
    } catch (error) {
      toast(error.message, true);
    }
  }

  async function deleteBlogger(platform, name) {
    if (!confirm('确定删除 <' + name + '> 及其全部已下载文件？')) return;
    try {
      await api(bloggerBase(platform, name), { method: 'DELETE' });
      if (state.galleryPlatform === platform && state.galleryName === name) hideGallery();
      toast('已删除 ' + name);
      loadBloggers();
    } catch (error) {
      toast(error.message, true);
    }
  }

  /* ---------------- 批量更新（前端串行调用单博主更新） ---------------- */

  /* 点「更新」：把下拉框所选平台下登记过主页链接的博主排成队列，逐个更新 */
  async function startBatchUpdate() {
    if (state.batch) return;
    if (state.taskId) {
      toast('已有任务在执行，请等待或先取消', true);
      return;
    }
    var platform = state.platformFilter;
    var queue = [];
    state.bloggers.forEach(function (group) {
      if (platform && group.platform !== platform) return;
      group.items.forEach(function (item) {
        if (item.homepage) {
          queue.push({ platform: group.platform, name: item.name });
        }
      });
    });
    if (!queue.length) {
      toast(platform
        ? '该平台没有可更新的博主（重新粘贴主页链接抓取一次即可登记）'
        : '没有可更新的博主（重新粘贴主页链接抓取一次即可登记）', true);
      return;
    }
    var label = platform ? platformLabel(platform) : '全部平台';
    var ok = await confirmDialog('确定更新「' + label + '」下的 ' +
                                 queue.length + ' 位博主？');
    /* 确认期间可能已经开始别的任务，再判一次 */
    if (!ok || state.batch || state.taskId) return;

    state.batch = {
      mode: 'update', queue: queue, total: queue.length, index: 0,
      done: 0, failed: 0, errors: [], stopped: false
    };
    $('logBox').textContent = '';
    state.logLength = 0;
    $('failureBox').classList.add('hidden');
    syncUpdateBtn();
    nextBatchItem();
  }

  /*
   * 取队列里的下一条：抓取模式提交链接，更新模式提交博主
   * 请求成功走正常轮询，失败记一笔后直接跳到下一条
   */
  function nextBatchItem() {
    var job = state.batch;
    if (!job || job.stopped || !job.queue.length) {
      endBatch();
      return;
    }
    var target = job.queue.shift();
    job.index = job.total - job.queue.length;
    var title = job.mode === 'scrape' ? target : target.name;
    job.current = title;
    $('logBox').textContent += '\n=== [' + job.index + '/' + job.total + '] ' +
      title + ' ===\n';
    $('logBox').scrollTop = $('logBox').scrollHeight;
    var request = job.mode === 'scrape'
      ? postJSON('/api/scrape', { url: target, range: job.range })
      : postJSON(bloggerBase(target.platform, target.name) + '/update');
    request.then(function (data) {
      beginTask(data.task);
      syncUpdateBtn();
    }).catch(function (error) {
      job.failed += 1;
      job.errors.push(title + '：' + error.message);
      nextBatchItem();
    });
  }

  function endBatch() {
    var job = state.batch;
    state.batch = null;
    syncUpdateBtn();
    if (!job) return;
    var unit = job.mode === 'scrape' ? ' 个链接' : ' 位博主';
    job.errors.forEach(function (line) {
      $('logBox').textContent += '失败：' + line + '\n';
    });
    $('logBox').scrollTop = $('logBox').scrollHeight;
    if (job.stopped) {
      toast('批量任务已取消，已完成 ' + job.done + unit);
    } else {
      toast('批量任务结束：已完成 ' + job.done + unit +
        (job.failed ? '，失败 ' + job.failed + unit : ''));
    }
    refreshBloggers();
  }

  /* 刷新博主列表；预览开着时同步刷新内容（保留当前标签），关闭时复位 */
  function refreshBloggers() {
    loadBloggers();
    if (isGalleryOpen() && state.galleryName) {
      fetchGallery();
    } else if (!isGalleryOpen()) {
      hideGallery();
    }
  }

  var galleryToken = 0;

  function isGalleryOpen() {
    return !$('gallery').classList.contains('hidden');
  }

  function showGallery() {
    $('gallery').classList.remove('hidden');
    $('galleryEmpty').classList.add('hidden');
  }

  /* 收起预览并恢复占位提示；同时作废进行中的请求与取帧任务 */
  function hideGallery() {
    var box = $('gallery');
    galleryToken += 1;
    resetPosterPipeline();
    resetThumbObserver();
    box.classList.add('hidden');
    box.innerHTML = '';
    state.galleryName = '';
    state.galleryPlatform = '';
    state.galleryLabel = '';
    state.galleryKind = 'image';
    state.galleryItems = [];
    state.galleryLoading = false;
    state.galleryScroll = { image: 0, video: 0 };
    $('galleryTabs').classList.add('hidden');
    $('galleryRefresh').classList.add('hidden');
    $('galleryRefresh').disabled = false;
    $('galleryRefresh').textContent = '刷新';
    $('galleryTitle').textContent = '预览';
    $('galleryEmpty').classList.remove('hidden');
    markActiveBlogger();
  }

  /* 选中博主后才显示「图片 / 视频」切换与刷新 */
  function syncGalleryChrome() {
    var show = !!state.galleryName;
    $('galleryTabs').classList.toggle('hidden', !show);
    $('galleryRefresh').classList.toggle('hidden', !show);
    var buttons = $('galleryTabs').querySelectorAll('.tab');
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].classList.toggle('active', buttons[i].dataset.kind === state.galleryKind);
    }
  }

  /* 博主行高亮，标识当前预览的是谁 */
  function markActiveBlogger() {
    var rows = $('bloggers').querySelectorAll('.blogger');
    for (var i = 0; i < rows.length; i++) {
      rows[i].classList.toggle(
        'active',
        rows[i].dataset.name === state.galleryName &&
        rows[i].dataset.platform === state.galleryPlatform);
    }
  }

  /* 记住当前标签的滚动位置，切换/刷新后各自复原 */
  function rememberGalleryScroll() {
    var kind = state.galleryKind;
    if (kind !== 'image' && kind !== 'video') return;
    state.galleryScroll[kind] = $('gallery').scrollTop;
  }

  /* 点击博主行：打开预览，每次默认回到图片 */
  function openGallery(platform, label, name) {
    state.galleryPlatform = platform;
    state.galleryLabel = label || '';
    state.galleryName = name;
    state.galleryKind = 'image';
    state.galleryScroll = { image: 0, video: 0 };
    showGallery();
    syncGalleryChrome();
    markActiveBlogger();
    fetchGallery();
  }

  /* 图片 / 视频切换：元数据已缓存，不重新请求 */
  function setGalleryKind(kind) {
    if (!state.galleryName) return;
    if (kind !== 'image' && kind !== 'video') return;
    if (state.galleryKind === kind) return;
    rememberGalleryScroll();
    state.galleryKind = kind;
    syncGalleryChrome();
    if (state.galleryLoading) return;
    renderGallery();
  }

  /* 刷新：重新读取该博主的图片与视频，保留当前标签与滚动位置 */
  function refreshGallery() {
    if (!state.galleryName || state.galleryLoading) return;
    rememberGalleryScroll();
    posterFailed = {};   /* 之前取帧失败或已缓存的视频允许重新取首帧 */
    coverSaving = {};    /* 允许重新回存封面（比如 .cache 被手动清掉） */
    fetchGallery();
  }

  async function fetchGallery() {
    var name = state.galleryName;
    var platform = state.galleryPlatform;
    if (!name || !platform) return;
    var box = $('gallery');
    var token = ++galleryToken;
    state.galleryLoading = true;
    resetPosterPipeline();
    $('galleryRefresh').disabled = true;
    $('galleryRefresh').textContent = '刷新中…';
    $('galleryTitle').textContent = bloggerTitle() + ' · 加载中…';
    box.innerHTML = '<p class="hint">加载中…</p>';
    try {
      var data = await api(bloggerBase(platform, name) + '/files');
      if (token !== galleryToken || state.galleryName !== name ||
          state.galleryPlatform !== platform) return;
      state.galleryLoading = false;
      state.galleryItems = data.items || [];
      renderGallery();
    } catch (error) {
      if (token !== galleryToken) return;
      state.galleryLoading = false;
      hideGallery();
      toast(error.message, true);
      return;
    }
    if (token === galleryToken) {
      $('galleryRefresh').disabled = false;
      $('galleryRefresh').textContent = '刷新';
    }
  }

  /* 预览标题：带上平台名，避免不同平台同名博主混淆 */
  function bloggerTitle() {
    return (state.galleryLabel ? state.galleryLabel + ' · ' : '') + state.galleryName;
  }

  /* 媒体 URL 带上文件 mtime 作为版本号：文件被覆盖后不会命中旧缓存 */
  function mediaUrl(item) {
    return item.url + '&v=' + (item.mtime || 0);
  }

  function thumbUrl(item) {
    return item.thumb + '&v=' + (item.mtime || 0);
  }

  function renderGallery() {
    var box = $('gallery');
    var kind = state.galleryKind;
    var label = kind === 'video' ? '视频' : '图片';
    var items = state.galleryItems.filter(function (item) { return item.kind === kind; });
    box.innerHTML = '';
    /* 重建列表时中止未完成的取帧任务 */
    resetPosterPipeline();
    resetThumbObserver();
    $('galleryTitle').textContent = bloggerTitle() + ' · ' + label + ' ' + items.length + ' 个';

    if (!items.length) {
      var empty = document.createElement('p');
      empty.className = 'empty';
      empty.textContent = '该博主目录下没有可预览的' + label +
        '，可切换到' + (kind === 'video' ? '图片' : '视频') + '看看。';
      box.appendChild(empty);
      box.scrollTop = 0;
      return;
    }

    var grid = document.createElement('div');
    grid.className = 'grid';
    items.forEach(function (item, index) {
      var tile = document.createElement('div');
      tile.className = 'tile clickable';
      if (item.kind === 'video') {
        var thumb = document.createElement('img');
        thumb.className = 'tile-thumb';
        thumb.alt = item.name;
        if (item.cover) {
          /* 已落盘的封面：不必再取帧，但同样等进入视口再请求 */
          thumb.dataset.src = item.cover + '&v=' + (item.mtime || 0);
        } else {
          thumb.dataset.videoUrl = mediaUrl(item);
          thumb.dataset.videoRel = item.rel;
          observePoster(thumb);
        }
        tile.appendChild(thumb);
        observeThumb(thumb);   /* 无 data-src（待取帧）时内部直接返回 */

        var playIcon = document.createElement('span');
        playIcon.className = 'play-icon';
        playIcon.textContent = '▶';
        tile.appendChild(playIcon);
      } else {
        var img = document.createElement('img');
        img.alt = item.name;
        img.decoding = 'async';
        img.dataset.src = thumbUrl(item);
        tile.appendChild(img);
        observeThumb(img);
      }
      tile.addEventListener('click', function () { openLightbox(items, index); });
      var badge = document.createElement('span');
      badge.className = 'badge';
      badge.textContent = item.name;
      badge.title = item.name;
      tile.appendChild(badge);
      grid.appendChild(tile);
    });
    box.appendChild(grid);
    /* 显式还原各标签自己的滚动位置：既保证切回来停在原处，
       也避免浏览器把上一个标签的 scrollTop 带过来（innerHTML 重建不会自动归零） */
    box.scrollTop = state.galleryScroll[kind] || 0;
  }

  /* ---------------- 图片缩略图（进入视口才发请求） ---------------- */
  /*
   * 不能用 img.loading='lazy' 兜底：src 一旦赋值浏览器就会请求该图，
   * 属性设置晚一步就退化成 eager，整目录的缩略图会被一次性算完。
   * 因此 src 由 IntersectionObserver 在接近视口时才写入。
   */
  var thumbObserver = null;

  function resetThumbObserver() {
    if (thumbObserver) thumbObserver.disconnect();
  }

  function observeThumb(img) {
    var src = img.dataset.src;
    if (!src) return;
    if (typeof IntersectionObserver !== 'function') {
      img.src = src;
      return;
    }
    if (!thumbObserver) {
      thumbObserver = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          var el = entry.target;
          thumbObserver.unobserve(el);
          el.src = el.dataset.src;
          delete el.dataset.src;
        });
      }, { root: $('gallery'), rootMargin: '240px' });
    }
    thumbObserver.observe(img);
  }

  /* ---------------- 视频封面（复用单个隐藏 video 取首帧） ---------------- */

  var posterObserver = null;
  var posterQueue = [];
  var posterBusy = 0;            /* 当前并行取帧数 */
  var posterLimit = 3;           /* 并行上限：串行时每个视频都要从头加载，很慢 */
  var posterActive = [];
  var posterFailed = {};         /* 本次会话取帧失败的 URL，避免反复等超时 */
  var videoPool = [];
  var captureCanvas = null;

  function observePoster(img) {
    if (typeof IntersectionObserver !== 'function') {
      enqueuePoster(img);
      return;
    }
    if (!posterObserver) {
      posterObserver = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          posterObserver.unobserve(entry.target);
          enqueuePoster(entry.target);
        });
      }, { rootMargin: '300px' });
    }
    posterObserver.observe(img);
  }

  function acquireVideo() {
    for (var i = 0; i < videoPool.length; i++) {
      if (!videoPool[i].busy) {
        videoPool[i].busy = true;
        return videoPool[i];
      }
    }
    var video = document.createElement('video');
    video.preload = 'metadata';
    video.muted = true;
    video.playsInline = true;
    video.style.cssText = 'position:fixed;left:-9999px;top:0;pointer-events:none;';
    document.body.appendChild(video);
    var entry = { video: video, busy: true };
    videoPool.push(entry);
    return entry;
  }

  function enqueuePoster(img) {
    if (!img || !img.dataset.videoUrl || img.dataset.posterState) return;
    img.dataset.posterState = 'queued';
    posterQueue.push(img);
    pumpPosterQueue();
  }

  function pumpPosterQueue() {
    while (posterBusy < posterLimit && posterQueue.length) {
      var img = posterQueue.shift();
      if (!img || !img.isConnected || !img.dataset.videoUrl) continue;
      if (posterFailed[img.dataset.videoUrl]) {
        img.dataset.posterState = 'failed';
        continue;
      }
      posterBusy += 1;
      try {
        captureFrame(acquireVideo(), img, function () {
          posterBusy -= 1;
          setTimeout(pumpPosterQueue, 0);
        });
      } catch (error) {
        posterBusy -= 1;
      }
    }
  }

  function captureFrame(entry, img, done) {
    var video = entry.video;
    var finished = false;
    var timer = null;

    /* 被打断不等于失败：不记入 posterFailed，下次仍可重新取帧 */
    function cancel() {
      finish('cancelled');
    }

    function cleanup() {
      clearTimeout(timer);
      video.removeEventListener('loadedmetadata', onMeta);
      video.removeEventListener('seeked', onSeeked);
      video.removeEventListener('loadeddata', onLoadedData);
      video.removeEventListener('error', onError);
      video.removeAttribute('src');
      video.load();
    }

    function finish(state) {
      if (finished) return;
      finished = true;
      var index = posterActive.indexOf(cancel);
      if (index >= 0) posterActive.splice(index, 1);
      if (img.isConnected) img.dataset.posterState = state;
      if (state === 'failed') posterFailed[img.dataset.videoUrl] = true;
      if (state === 'done') saveCover(img.dataset.videoRel, img.src);
      entry.busy = false;
      cleanup();
      done();
    }

    function draw() {
      if (!video.videoWidth) return false;
      try {
        var scale = Math.min(1, 320 / video.videoWidth);
        var canvas = captureCanvas || (captureCanvas = document.createElement('canvas'));
        canvas.width = Math.round(video.videoWidth * scale);
        canvas.height = Math.round(video.videoHeight * scale);
        canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
        img.src = canvas.toDataURL('image/jpeg', 0.7);
        return true;
      } catch (error) {
        return false;
      }
    }

    function onMeta() {
      try {
        video.currentTime = Math.min(0.1, (video.duration || 1) / 2);
      } catch (error) {
        /* 此时不允许 seek 时，交给 loadeddata 兜底 */
      }
    }

    function onSeeked() {
      finish(draw() ? 'done' : 'failed');
    }

    function onLoadedData() {
      if (video.currentTime === 0) {
        finish(draw() ? 'done' : 'failed');
      }
    }

    function onError() {
      finish('failed');
    }

    posterActive.push(cancel);
    video.addEventListener('loadedmetadata', onMeta);
    video.addEventListener('seeked', onSeeked);
    video.addEventListener('loadeddata', onLoadedData);
    video.addEventListener('error', onError);
    /* 单个视频取帧超时 4 秒即可，失败的记下来不再重试 */
    timer = setTimeout(function () { finish('failed'); }, 4000);

    video.src = img.dataset.videoUrl;
    video.load();
  }

  function resetPosterPipeline() {
    if (posterObserver) posterObserver.disconnect();
    posterQueue = [];
    var active = posterActive;
    posterActive = [];
    active.forEach(function (cancel) { cancel(); });
  }

  /* 把取到的首帧回存服务端，下次打开直接读 .cache/covers 里的图 */
  var coverSaving = {};

  function saveCover(relPath, dataUrl) {
    if (!relPath || !dataUrl || coverSaving[relPath]) return;
    coverSaving[relPath] = true;
    postJSON('/api/covers', { path: relPath, data: dataUrl }).catch(function () {
      coverSaving[relPath] = false;   /* 失败允许以后重试 */
    });
  }

  /* ---------------- 大图预览 ---------------- */

  var lightbox = { items: [], index: 0 };
  var lightboxCache = [];

  function openLightbox(items, index) {
    if (!items || !items.length) return;
    lightbox.items = items;
    lightbox.index = index;
    hideEdges();
    /* 每次打开预览固定从 20% 音量开始；会话内用户手动调整后切换不再覆盖 */
    $('lbVideo').volume = 0.2;
    $('lightbox').classList.remove('hidden');
    document.body.classList.add('no-scroll');
    renderLightbox();
  }

  function closeLightbox() {
    var video = $('lbVideo');
    hideEdges();
    video.pause();
    video.removeAttribute('src');
    video.load();
    video.classList.add('hidden');
    $('lbImg').removeAttribute('src');
    $('lbImg').classList.add('hidden');
    $('lightbox').classList.add('hidden');
    document.body.classList.remove('no-scroll');
    lightbox.items = [];
    lightbox.index = 0;
    lightboxCache = [];
  }

  function renderLightbox() {
    var item = lightbox.items[lightbox.index];
    if (!item) return;
    var img = $('lbImg');
    var video = $('lbVideo');

    if (item.kind === 'video') {
      img.classList.add('hidden');
      img.removeAttribute('src');
      video.classList.remove('hidden');
      video.src = mediaUrl(item);
      var played = video.play();
      if (played && played.catch) {
        played.catch(function () { /* 自动播放被拦截时由用户手动点播放 */ });
      }
    } else {
      video.pause();
      video.removeAttribute('src');
      video.load();
      video.classList.add('hidden');
      img.classList.remove('hidden');
      img.src = mediaUrl(item);
      img.alt = item.name;
    }

    $('lbCaption').textContent = item.name;
    $('lbCounter').textContent = (lightbox.index + 1) + ' / ' + lightbox.items.length;
    $('lbEdgePrev').disabled = lightbox.index === 0;
    $('lbEdgeNext').disabled = lightbox.index === lightbox.items.length - 1;
    syncEdges();
    preloadImage(lightbox.index - 1);
    preloadImage(lightbox.index + 1);
  }

  /* ---------------- 左右两侧悬浮切换按钮 ---------------- */

  function hideEdges() {
    $('lbEdgePrev').classList.remove('show');
    $('lbEdgeNext').classList.remove('show');
  }

  /* 到首/尾时收起对应的边缘按钮，避免出现不可点击的图标 */
  function syncEdges() {
    if ($('lbEdgePrev').disabled) $('lbEdgePrev').classList.remove('show');
    if ($('lbEdgeNext').disabled) $('lbEdgeNext').classList.remove('show');
  }

  /* 鼠标进入左右边缘热区（约 16% 宽度，70~180px）时淡入对应按钮 */
  function updateEdges(event) {
    if (!isLightboxOpen()) return;
    var rect = $('lightbox').getBoundingClientRect();
    var x = event.clientX - rect.left;
    var hot = Math.min(180, Math.max(70, rect.width * 0.16));
    var prev = $('lbEdgePrev');
    var next = $('lbEdgeNext');
    prev.classList.toggle('show', x <= hot && !prev.disabled);
    next.classList.toggle('show', x >= rect.width - hot && !next.disabled);
  }

  function preloadImage(index) {
    var item = lightbox.items[index];
    if (!item || item.kind === 'video') return;
    var image = new Image();
    image.src = mediaUrl(item);
    lightboxCache.push(image);
    if (lightboxCache.length > 6) lightboxCache.shift();
  }

  function stepLightbox(delta) {
    var next = lightbox.index + delta;
    if (next < 0 || next >= lightbox.items.length) return;
    lightbox.index = next;
    renderLightbox();
  }

  function isLightboxOpen() {
    return !$('lightbox').classList.contains('hidden');
  }

  function onLightboxKey(event) {
    if (!isLightboxOpen()) return;
    if (event.key === 'Escape') {
      closeLightbox();
    } else if (event.key === 'ArrowLeft') {
      stepLightbox(-1);
    } else if (event.key === 'ArrowRight') {
      stepLightbox(1);
    } else {
      return;
    }
    event.preventDefault();
  }

  /* ---------------- 事件绑定 ---------------- */

  function bind() {
    $('downloadBtn').addEventListener('click', onDownload);
    $('confirmOk').addEventListener('click', function () { closeConfirm(true); });
    $('confirmCancel').addEventListener('click', function () { closeConfirm(false); });
    /* 点遮罩空白处等同取消 */
    $('confirmModal').addEventListener('mousedown', function (event) {
      if (event.target === $('confirmModal')) closeConfirm(false);
    });
    document.addEventListener('keydown', onConfirmKey);
    /* 多行输入框里回车要能换行，提交改用 Ctrl/⌘ + Enter */
    $('userUrl').addEventListener('keydown', function (event) {
      if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        startScrape();
      }
    });
    $('cancelBtn').addEventListener('click', cancelTask);
    $('downloadMode').addEventListener('change', function (event) {
      setDownloadMode(event.target.value);
    });
    $('followingBtn').addEventListener('click', startFollowing);
    $('followingToggle').addEventListener('click', function () {
      setFollowingOpen($('followingBox').classList.contains('hidden'));
    });
    $('followingClose').addEventListener('click', function () {
      setFollowingOpen(false);
    });
    /* 点浮层外部收起 */
    document.addEventListener('mousedown', function (event) {
      if ($('followingBox').classList.contains('hidden')) return;
      if ($('followingBox').contains(event.target)) return;
      if ($('followingToggle').contains(event.target)) return;
      setFollowingOpen(false);
    });
    $('followingAll').addEventListener('change', function (event) {
      var boxes = followingBoxes();
      for (var i = 0; i < boxes.length; i++) {
        boxes[i].checked = event.target.checked;
      }
      updateFollowingPicked();
    });
    $('updateAllBtn').addEventListener('click', startBatchUpdate);
    $('refreshBtn').addEventListener('click', refreshBloggers);
    $('platformFilter').addEventListener('change', function (event) {
      state.platformFilter = event.target.value;
      /* 预览中的博主被筛掉时收起预览，避免列表与预览对不上 */
      if (state.galleryPlatform && state.platformFilter &&
          state.galleryPlatform !== state.platformFilter) {
        hideGallery();
      }
      loadBloggers();
    });
    $('galleryRefresh').addEventListener('click', refreshGallery);
    $('galleryTabs').addEventListener('click', function (event) {
      var btn = event.target.closest ? event.target.closest('.tab') : null;
      if (btn) setGalleryKind(btn.dataset.kind);
    });

    $('lbClose').addEventListener('click', closeLightbox);
    $('lbEdgePrev').addEventListener('click', function () { stepLightbox(-1); });
    $('lbEdgeNext').addEventListener('click', function () { stepLightbox(1); });
    $('lightbox').addEventListener('mousemove', updateEdges);
    $('lightbox').addEventListener('mouseleave', hideEdges);
    /* 只有“按下”和“松开”都在遮罩上时才关闭，避免按钮误触 */
    var lbDownOnBackdrop = false;
    $('lightbox').addEventListener('mousedown', function (event) {
      lbDownOnBackdrop = event.target === $('lightbox');
    });
    $('lightbox').addEventListener('click', function (event) {
      if (lbDownOnBackdrop && event.target === $('lightbox')) closeLightbox();
      lbDownOnBackdrop = false;
    });
    document.addEventListener('keydown', onLightboxKey);
  }

  function init() {
    setDownloadMode('link');
    bind();
    refreshStatus();
    loadBloggers();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
