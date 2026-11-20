/* 抖音批量下载器 - 前端逻辑 */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };

  var state = {
    taskId: null,
    polling: false,
    logLength: 0,
    galleryName: '',
    galleryKind: 'image',
    galleryItems: [],      /* 当前博主的全部作品（图片 + 视频），切换标签复用 */
    galleryLoading: false,
    galleryScroll: { image: 0, video: 0 },   /* 两个标签各自的滚动位置 */
    scrapeReady: true,
    lastPhase: ''
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

  /* ---------------- 状态 ---------------- */

  async function refreshStatus() {
    try {
      var data = await api('/api/status');
      applyScrapeReady(data.scrape_ready);
      if (data.active_task && !state.taskId) {
        state.taskId = data.active_task.id;
        renderTask(data.active_task);
        pollTask();
      }
    } catch (error) {
      toast(error.message, true);
    }
  }

  /* Playwright 缺失时禁用抓取入口 */
  function applyScrapeReady(ready) {
    if (ready === undefined) return;
    state.scrapeReady = !!ready;
    var btn = $('scrapeBtn');
    btn.disabled = !state.scrapeReady;
    btn.title = state.scrapeReady ? '' : '未安装 playwright，请执行 pip install playwright 与 playwright install chromium';
  }

  /* ---------------- 抓取任务 ---------------- */

  async function startScrape() {
    var url = $('userUrl').value.trim();
    if (!url) {
      toast('请先粘贴博主主页链接', true);
      $('userUrl').focus();
      return;
    }
    $('scrapeBtn').disabled = true;
    try {
      var data = await postJSON('/api/scrape', {
        url: url,
        range: $('rangeSelect').value
      });
      beginTask(data.task);
      toast('已开始抓取，浏览器启动后会自动翻页');
    } catch (error) {
      $('scrapeBtn').disabled = !state.scrapeReady;
      toast(error.message, true);
    }
  }

  /* 任务启动渲染 */
  function beginTask(task) {
    state.taskId = task.id;
    state.logLength = 0;
    state.lastPhase = task.phase;
    $('logBox').textContent = '';
    $('failureBox').classList.add('hidden');
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
    $('scrapeBtn').disabled = !state.scrapeReady;
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
    /* 抓取阶段没有可靠总量，进度条保持不动，只在文字里显示页数与累计条数 */
    var percent = scraping ? 0 : (task.percent || 0);
    $('bar').style.width = percent + '%';

    var statusText;
    if (task.status === 'running') {
      statusText = scraping
        ? '抓取中 · 第 ' + (task.scrape_pages || 0) + ' 页 · 累计 ' + (task.scrape_count || 0) + ' 条'
        : (task.percent || 0) + '%（' + task.done + '/' + task.total + '）';
    } else {
      statusText = task.status === 'done' ? '已完成' : task.status === 'cancelled' ? '已取消' : '出错';
    }
    $('progressText').textContent = statusText;

    /* 抓取转入下载时刷新博主列表，让本次新出现的博主立刻显示出来 */
    if (state.lastPhase && state.lastPhase !== task.phase) {
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

  /* ---------------- 博主管理 ---------------- */

  async function loadBloggers() {
    var box = $('bloggers');
    try {
      var data = await api('/api/bloggers');
      $('bloggerHint').textContent = data.items.length + ' 位博主';
      box.innerHTML = '';
      if (!data.items.length) {
        var empty = document.createElement('p');
        empty.className = 'empty';
        empty.textContent = '暂无博主，先导入数据并下载一次。';
        box.appendChild(empty);
        return;
      }
      data.items.forEach(function (item) {
        var row = document.createElement('div');
        row.className = 'blogger';
        row.dataset.name = item.name;
        row.tabIndex = 0;
        row.title = '点击预览「' + item.name + '」的作品（默认图片）';
        if (state.galleryName === item.name) row.classList.add('active');
        row.addEventListener('click', function () { openGallery(item.name); });
        row.addEventListener('keydown', function (event) {
          /* 行内按钮的按键不触发浏览，避免回车删除时顺带打开预览 */
          if (event.target !== row) return;
          if (event.key !== 'Enter' && event.key !== ' ') return;
          event.preventDefault();
          openGallery(item.name);
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
        if (item.homepage) {
          /* 阻止冒泡，点更新不会顺带打开预览 */
          update.addEventListener('click', function (event) {
            event.stopPropagation();
            updateBlogger(item.name);
          });
        } else {
          update.disabled = true;
          update.title = '缺少 homepage.txt，请重新粘贴该博主链接抓取一次以登记';
        }

        var remove = document.createElement('button');
        remove.className = 'ghost danger';
        remove.textContent = '删除';
        remove.addEventListener('click', function (event) {
          event.stopPropagation();
          deleteBlogger(item.name);
        });

        actions.appendChild(update);
        actions.appendChild(remove);

        row.appendChild(name);
        row.appendChild(actions);
        box.appendChild(row);
      });
    } catch (error) {
      toast(error.message, true);
    }
  }

  async function updateBlogger(name) {
    try {
      var data = await postJSON('/api/bloggers/' + encodeURIComponent(name) + '/update');
      beginTask(data.task);
      toast('开始更新 ' + name + '，已下载的作品会自动跳过');
    } catch (error) {
      toast(error.message, true);
    }
  }

  async function deleteBlogger(name) {
    if (!confirm('确定删除 <' + name + '> 及其全部已下载文件？')) return;
    try {
      await api('/api/bloggers/' + encodeURIComponent(name), { method: 'DELETE' });
      if (state.galleryName === name) hideGallery();
      toast('已删除 ' + name);
      loadBloggers();
    } catch (error) {
      toast(error.message, true);
    }
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
      rows[i].classList.toggle('active', rows[i].dataset.name === state.galleryName);
    }
  }

  /* 记住当前标签的滚动位置，切换/刷新后各自复原 */
  function rememberGalleryScroll() {
    var kind = state.galleryKind;
    if (kind !== 'image' && kind !== 'video') return;
    state.galleryScroll[kind] = $('gallery').scrollTop;
  }

  /* 点击博主行：打开预览，每次默认回到图片 */
  function openGallery(name) {
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
    if (!name) return;
    var box = $('gallery');
    var token = ++galleryToken;
    state.galleryLoading = true;
    resetPosterPipeline();
    $('galleryRefresh').disabled = true;
    $('galleryRefresh').textContent = '刷新中…';
    $('galleryTitle').textContent = name + ' · 加载中…';
    box.innerHTML = '<p class="hint">加载中…</p>';
    try {
      var data = await api('/api/bloggers/' + encodeURIComponent(name) + '/files');
      if (token !== galleryToken || state.galleryName !== name) return;
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
    $('galleryTitle').textContent = state.galleryName + ' · ' + label + ' ' + items.length + ' 个';

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
   * 不能用 img.loading='lazy' 兜底：src 一旦赋值浏览器就开始下载，
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
    $('scrapeBtn').addEventListener('click', startScrape);
    $('userUrl').addEventListener('keydown', function (event) {
      if (event.key === 'Enter') {
        event.preventDefault();
        startScrape();
      }
    });
    $('cancelBtn').addEventListener('click', cancelTask);
    $('refreshBtn').addEventListener('click', refreshBloggers);
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
    bind();
    refreshStatus();
    loadBloggers();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
