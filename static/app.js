/* 抖音批量下载器 - 前端逻辑 */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };

  var state = {
    taskId: null,
    polling: false,
    logLength: 0,
    galleryName: '',
    galleryKind: ''
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

  function formatSize(bytes) {
    if (!bytes) return '0 B';
    var units = ['B', 'KB', 'MB', 'GB'];
    var index = 0;
    var value = bytes;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    return (index === 0 ? value : value.toFixed(1)) + ' ' + units[index];
  }

  /* ---------------- 状态与日志信息 ---------------- */

  async function refreshStatus() {
    try {
      var data = await api('/api/status');
      renderLogInfo(data.json_log);
      if (data.active_task && !state.taskId) {
        state.taskId = data.active_task.id;
        renderTask(data.active_task);
        pollTask();
      }
    } catch (error) {
      toast(error.message, true);
    }
  }

  function renderLogInfo(info) {
    if (!info) return;
    $('logInfo').textContent = info.exists
      ? '已导入：' + info.lines + ' 条 · ' + formatSize(info.size) + ' / ' + formatSize(info.limit)
      : '尚未导入数据';
  }

  async function refreshLogInfo() {
    try {
      renderLogInfo(await api('/api/json-log'));
    } catch (error) { /* 忽略 */ }
  }

  /* ---------------- 数据管理 ---------------- */

  async function clearJSONLog() {
    if (!confirm('确定清空已导入的数据？此操作不可恢复。')) return;
    try {
      await postJSON('/api/json-log/clear');
      toast('已清空导入的数据');
      refreshLogInfo();
      $('parseInfo').textContent = '';
    } catch (error) {
      toast(error.message, true);
    }
  }

  /* ---------------- 下载任务 ---------------- */

  async function startDownload() {
    var text = $('jsonText').value.trim();
    if (!text) {
      toast('请先粘贴接口 JSON', true);
      return;
    }
    $('downloadBtn').disabled = true;
    try {
      var imported = await postJSON('/api/json-log', { text: text });
      $('jsonText').value = '';
      renderLogInfo({ exists: imported.lines > 0, lines: imported.lines,
                      size: imported.size, limit: imported.limit });
      if (!imported.lines) {
        toast('没有可解析的内容' + (imported.invalid ? '，忽略非法行 ' + imported.invalid + ' 条' : ''), true);
        $('downloadBtn').disabled = false;
        return;
      }
      var parsed = await postJSON('/api/parse');
      $('parseInfo').textContent = parsed.blogger_count + ' 位博主 · 视频 ' +
        parsed.video_items + ' 条 · 图片 ' + parsed.picture_items + ' 张' +
        (parsed.skipped_lines ? ' · 跳过 ' + parsed.skipped_lines + ' 行' : '');

      var data = await postJSON('/api/download');
      state.taskId = data.task_id;
      state.logLength = 0;
      $('logBox').textContent = '';
      $('failureBox').classList.add('hidden');
      $('cancelBtn').disabled = false;
      renderTask(data.task);
      pollTask();
    } catch (error) {
      $('downloadBtn').disabled = false;
      toast(error.message, true);
    }
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
    $('downloadBtn').disabled = false;
    if (task.status === 'cancelled') {
      toast('任务已取消');
    } else if (task.status === 'error') {
      toast('任务出错：' + (task.logs && task.logs.length ? task.logs[task.logs.length - 1] : '未知错误'), true);
    } else {
      toast('下载完成');
    }
    renderFailures(task.failures || []);
    loadBloggers();
    refreshLogInfo();
  }

  function renderTask(task) {
    $('bar').style.width = (task.percent || 0) + '%';
    var statusText = task.status === 'running'
      ? (task.media_type || '准备中') + ' · ' + task.percent + '%（' + task.done + '/' + task.total + '）'
      : (task.status === 'done' ? '已完成' : task.status === 'cancelled' ? '已取消' : '出错');
    $('progressText').textContent = statusText;
    $('currentFile').textContent = task.current || '';
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

        var name = document.createElement('div');
        name.className = 'name';
        name.textContent = item.name;
        name.title = item.name;

        var stats = document.createElement('div');
        stats.className = 'stats';
        stats.textContent = '视频 ' + item.video + ' · 图片 ' + item.picture + ' · ' + formatSize(item.size);

        var grow = document.createElement('div');
        grow.className = 'grow';

        var pictureBtn = document.createElement('button');
        pictureBtn.className = 'ghost';
        pictureBtn.textContent = '图片';
        pictureBtn.addEventListener('click', function () { openGallery(item.name, 'image'); });

        var videoBtn = document.createElement('button');
        videoBtn.className = 'ghost';
        videoBtn.textContent = '视频';
        videoBtn.addEventListener('click', function () { openGallery(item.name, 'video'); });

        var remove = document.createElement('button');
        remove.className = 'ghost danger';
        remove.textContent = '删除';
        remove.addEventListener('click', function () { deleteBlogger(item.name); });

        row.appendChild(name);
        row.appendChild(stats);
        row.appendChild(grow);
        row.appendChild(pictureBtn);
        row.appendChild(videoBtn);
        row.appendChild(remove);
        box.appendChild(row);
      });
    } catch (error) {
      toast(error.message, true);
    }
  }

  async function deleteBlogger(name) {
    if (!confirm('确定删除 <' + name + '> 及其全部已下载文件？')) return;
    try {
      await api('/api/bloggers/' + encodeURIComponent(name), { method: 'DELETE' });
      if (state.galleryName === name) {
        $('gallery').classList.add('hidden');
        state.galleryName = '';
        state.galleryKind = '';
      }
      toast('已删除 ' + name);
      loadBloggers();
    } catch (error) {
      toast(error.message, true);
    }
  }

  async function openGallery(name, kind) {
    var box = $('gallery');
    var kindLabel = kind === 'video' ? '视频' : '图片';
    if (state.galleryName === name && state.galleryKind === kind && !box.classList.contains('hidden')) {
      box.classList.add('hidden');
      state.galleryName = '';
      state.galleryKind = '';
      return;
    }
    state.galleryName = name;
    state.galleryKind = kind;
    box.classList.remove('hidden');
    box.innerHTML = '<p class="hint">加载中…</p>';
    try {
      var data = await api('/api/bloggers/' + encodeURIComponent(name) + '/files');
      var items = data.items.filter(function (item) { return item.kind === kind; });
      box.innerHTML = '';
      /* 重建列表时中止未完成的取帧任务 */
      resetPosterPipeline();

      var head = document.createElement('div');
      head.className = 'gallery-head';
      var title = document.createElement('h4');
      title.textContent = name + '（' + kindLabel + ' ' + items.length + ' 个）';
      var close = document.createElement('button');
      close.className = 'ghost';
      close.textContent = '收起';
      close.addEventListener('click', function () {
        box.classList.add('hidden');
        state.galleryName = '';
        state.galleryKind = '';
      });
      head.appendChild(title);
      head.appendChild(close);
      box.appendChild(head);

      if (!items.length) {
        var empty = document.createElement('p');
        empty.className = 'empty';
        empty.textContent = '该博主目录下没有可预览的' + kindLabel + '。';
        box.appendChild(empty);
        return;
      }

      var grid = document.createElement('div');
      grid.className = 'grid';
      items.forEach(function (item, index) {
        var tile = document.createElement('div');
        tile.className = 'tile';
        if (item.kind === 'video') {
          var thumb = document.createElement('img');
          thumb.className = 'tile-thumb';
          thumb.alt = item.name;
          thumb.dataset.videoUrl = item.url;
          tile.appendChild(thumb);

          var playIcon = document.createElement('span');
          playIcon.className = 'play-icon';
          playIcon.textContent = '▶';
          tile.appendChild(playIcon);

          observePoster(thumb);
          tile.classList.add('clickable');
          tile.addEventListener('click', function () { openLightbox(items, index); });
        } else {
          var img = document.createElement('img');
          img.src = item.thumb;
          img.loading = 'lazy';
          img.alt = item.name;
          tile.appendChild(img);
          tile.classList.add('clickable');
          tile.addEventListener('click', function () { openLightbox(items, index); });
        }
        var badge = document.createElement('span');
        badge.className = 'badge';
        badge.textContent = item.name;
        badge.title = item.name;
        tile.appendChild(badge);
        grid.appendChild(tile);
      });
      box.appendChild(grid);
      box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    } catch (error) {
      box.innerHTML = '';
      toast(error.message, true);
    }
  }

  /* ---------------- 视频封面（复用单个隐藏 video 取首帧） ---------------- */

  var posterObserver = null;
  var posterQueue = [];
  var posterBusy = false;
  var posterActive = null;
  var captureVideo = null;
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

  function getCaptureVideo() {
    if (captureVideo) return captureVideo;
    captureVideo = document.createElement('video');
    captureVideo.preload = 'metadata';
    captureVideo.muted = true;
    captureVideo.playsInline = true;
    captureVideo.style.cssText = 'position:fixed;left:-9999px;top:0;pointer-events:none;';
    document.body.appendChild(captureVideo);
    return captureVideo;
  }

  function enqueuePoster(img) {
    if (!img || !img.dataset.videoUrl || img.dataset.posterState) return;
    img.dataset.posterState = 'queued';
    posterQueue.push(img);
    pumpPosterQueue();
  }

  function pumpPosterQueue() {
    if (posterBusy) return;
    var img = posterQueue.shift();
    while (img && (!img.isConnected || !img.dataset.videoUrl)) {
      img = posterQueue.shift();
    }
    if (!img) return;
    posterBusy = true;
    try {
      captureFrame(img, function () {
        posterBusy = false;
        setTimeout(pumpPosterQueue, 0);
      });
    } catch (error) {
      posterBusy = false;
      setTimeout(pumpPosterQueue, 0);
    }
  }

  function captureFrame(img, done) {
    var video = getCaptureVideo();
    var finished = false;
    var timer = null;

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
      posterActive = null;
      if (img.isConnected) img.dataset.posterState = state;
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

    posterActive = { cancel: function () { finish('failed'); } };
    video.addEventListener('loadedmetadata', onMeta);
    video.addEventListener('seeked', onSeeked);
    video.addEventListener('loadeddata', onLoadedData);
    video.addEventListener('error', onError);
    timer = setTimeout(function () { finish('failed'); }, 8000);

    video.src = img.dataset.videoUrl;
    video.load();
  }

  function resetPosterPipeline() {
    if (posterObserver) posterObserver.disconnect();
    posterQueue = [];
    if (posterActive) posterActive.cancel();
  }

  /* ---------------- 大图预览 ---------------- */

  var lightbox = { items: [], index: 0 };
  var lightboxCache = [];

  function openLightbox(items, index) {
    if (!items || !items.length) return;
    lightbox.items = items;
    lightbox.index = index;
    $('lightbox').classList.remove('hidden');
    document.body.classList.add('no-scroll');
    renderLightbox();
  }

  function closeLightbox() {
    var video = $('lbVideo');
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
      video.src = item.url;
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
      img.src = item.url;
      img.alt = item.name;
    }

    $('lbCaption').textContent = item.name;
    $('lbCounter').textContent = (lightbox.index + 1) + ' / ' + lightbox.items.length;
    $('lbPrev').disabled = lightbox.index === 0;
    $('lbNext').disabled = lightbox.index === lightbox.items.length - 1;
    preloadImage(lightbox.index - 1);
    preloadImage(lightbox.index + 1);
  }

  function preloadImage(index) {
    var item = lightbox.items[index];
    if (!item || item.kind === 'video') return;
    var image = new Image();
    image.src = item.url;
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
    $('clearLogBtn').addEventListener('click', clearJSONLog);
    $('downloadBtn').addEventListener('click', startDownload);
    $('cancelBtn').addEventListener('click', cancelTask);
    $('refreshBtn').addEventListener('click', loadBloggers);

    $('lbClose').addEventListener('click', closeLightbox);
    $('lbPrev').addEventListener('click', function () { stepLightbox(-1); });
    $('lbNext').addEventListener('click', function () { stepLightbox(1); });
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

    $('jsonText').addEventListener('keydown', function (event) {
      if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
        startDownload();
      }
    });
  }

  function init() {
    bind();
    refreshStatus();
    loadBloggers();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
