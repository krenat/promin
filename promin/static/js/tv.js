/* Deliberately plain JavaScript without newer language features: the projector browser may be old. */
(function () {
  var video = document.getElementById('video');
  var bar = document.getElementById('bar');
  var barTitle = document.getElementById('bar-title');
  var barFill = document.getElementById('bar-fill');
  var waitingText = document.getElementById('waiting-text');
  var pairQr = document.getElementById('pair-qr');
  var pairText = document.getElementById('pair-text');
  var screens = ['pair', 'start', 'waiting', 'replaced'];
  var pairTimer = null;
  var PAIR_HINT = 'Наведіть камеру телефона на цей код і відкрийте посилання';
  var WAITING_HINT = 'Оберіть відео на телефоні';
  var note = document.getElementById('note');

  var ws = null;
  var started = false;      // whether there was a press that allows sound
  var current = null;       // {id, name} of the video being shown
  var retryDelay = 1000;
  var retryTimer = null;
  var barTimer = null;
  var pointerTimer = null;

  // A playback error (the network dropped, usually) is retried from the same place a few
  // times before the video is given up on.
  var MAX_ERRORS = 3;
  var errorCount = 0;
  var errorTimer = null;
  var lastPosition = 0;     // where playback last was
  var errorPosition = 0;    // where it was when the last error happened
  var resumeAt = 0;         // where to continue once the reloaded video is ready

  // The browser allows fullscreen only in response to a press, so we enter it
  // from the start button and from any later press.
  function isFullscreen() {
    return !!(document.fullscreenElement || document.webkitFullscreenElement ||
              document.mozFullScreenElement || document.msFullscreenElement);
  }

  function enterFullscreen() {
    var root = document.documentElement;
    var request = root.requestFullscreen || root.webkitRequestFullscreen ||
                  root.webkitRequestFullScreen || root.mozRequestFullScreen || root.msRequestFullscreen;
    if (!request || isFullscreen()) return;
    try {
      var result = request.call(root);
      if (result && result['catch']) result['catch'](function () {});
    } catch (e) {}
  }

  function showScreen(name) {
    for (var i = 0; i < screens.length; i++) {
      var node = document.getElementById(screens[i]);
      node.className = 'screen' + (screens[i] === name ? ' on' : '');
    }
    video.className = name === 'playing' ? 'on' : '';
    if (name !== 'playing') bar.className = '';
  }

  function sendState() {
    if (!ws || ws.readyState !== 1) return;
    var duration = video.duration;
    ws.send(JSON.stringify({
      type: 'state',
      video_id: current ? current.id : null,
      name: current ? current.name : null,
      position: video.currentTime || 0,
      duration: duration && isFinite(duration) ? duration : 0,
      paused: video.paused,
      volume: video.volume,
      started: started
    }));
  }

  function showBar() {
    if (!current) return;
    barTitle.textContent = current.name;
    updateBar();
    bar.className = 'on';
    clearTimeout(barTimer);
    barTimer = setTimeout(function () { bar.className = ''; }, 3000);
  }

  function updateBar() {
    var duration = video.duration;
    var share = duration && isFinite(duration) ? video.currentTime / duration : 0;
    barFill.style.width = Math.max(0, Math.min(share, 1)) * 100 + '%';
  }

  function play() {
    var result = video.play();
    if (result && result['catch']) {
      result['catch'](function (error) {
        // A video that failed to load is handled by its error event, not here.
        if (error && error.name && error.name !== 'NotAllowedError') return;
        // The browser refused to play with sound: one more press is needed.
        if (video.paused && current) {
          started = false;
          showScreen('start');
          document.getElementById('start-btn').focus();
          sendState();
        }
      });
    }
  }

  function forgetErrors() {
    clearTimeout(errorTimer);
    errorCount = 0;
    lastPosition = 0;
    resumeAt = 0;
  }

  function stop() {
    current = null;
    forgetErrors();
    video.pause();
    video.removeAttribute('src');
    video.load();
    showScreen(started ? 'waiting' : 'start');
    sendState();
  }

  // The video is over (or cannot play): tell the server, which loads the next one from the
  // queue if there is one. The report goes first so the server still knows what was playing.
  function finished() {
    if (!current) return;
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({ type: 'ended', video_id: current.id }));
    }
    stop();
  }

  // The video stopped with an error. Reload it and continue from where it was; only after
  // several failures in a row count it as finished, so that a short network drop does not
  // throw away the film and start the next one.
  function failed() {
    if (!current) return;
    if (errorCount >= MAX_ERRORS) { finished(); return; }
    errorCount++;
    errorPosition = lastPosition;
    var item = current;
    var position = lastPosition;
    clearTimeout(errorTimer);
    errorTimer = setTimeout(function () {
      if (current !== item) return;
      resumeAt = position;
      video.src = '/media/' + item.id;
      if (started) play();
    }, 2000 * errorCount);
  }

  function trackPosition() {
    updateBar();
    if (!current || resumeAt > 0 || !(video.currentTime > 0)) return;
    // Ten seconds of playback past the failure: the trouble is over.
    if (errorCount && video.currentTime > errorPosition + 10) errorCount = 0;
    lastPosition = video.currentTime;
  }

  function seekTo(position) {
    var duration = video.duration;
    if (!current || !duration || !isFinite(duration)) return;
    video.currentTime = Math.max(0, Math.min(position, duration - 0.5));
  }

  function handle(command) {
    var action = command.action;
    if (action === 'test') { startTest(command); return; }
    // A video chosen on the phone, or «stop», ends a check that is running.
    if (test && (action === 'load' || action === 'stop')) abortTest();
    if (action === 'load') {
      forgetErrors();
      current = { id: command.video_id, name: command.name };
      video.src = '/media/' + command.video_id;
      if (started) {
        showScreen('playing');
        play();
      }
    } else if (!current) {
      return;
    } else if (action === 'play') {
      play();
    } else if (action === 'pause') {
      video.pause();
    } else if (action === 'seek') {
      errorCount = 0;
      seekTo(command.value);
    } else if (action === 'seek_by') {
      errorCount = 0;
      seekTo(video.currentTime + command.value);
    } else if (action === 'volume') {
      video.volume = Math.max(0, Math.min(command.value, 1));
    } else if (action === 'stop') {
      stop();
    }
    sendState();
  }

  function connect() {
    clearTimeout(retryTimer);
    var scheme = location.protocol === 'https:' ? 'wss://' : 'ws://';
    var socket = new WebSocket(scheme + location.host + '/ws?role=tv');
    ws = socket;
    socket.onopen = function () {
      retryDelay = 1000;
      waitingText.textContent = WAITING_HINT;
      sendState();
    };
    socket.onmessage = function (event) {
      var message;
      try { message = JSON.parse(event.data); } catch (e) { return; }
      if (message && message.type === 'command') handle(message);
    };
    socket.onclose = function (event) {
      if (ws !== socket) return;
      ws = null;
      dropTest();  // the server gives up on a check when the connection drops
      if (event.code === 4401) { startPairing(); return; }
      if (event.code === 4000) {
        if (current) { video.pause(); }
        showScreen('replaced');
        document.getElementById('takeover-btn').focus();
        return;
      }
      waitingText.textContent = 'Немає зв’язку із сервером, підключаюсь…';
      retryTimer = setTimeout(connect, retryDelay);
      retryDelay = Math.min(retryDelay * 2, 5000);
    };
  }

  // ------------------------------------------------------- projector check
  //
  // Started from the settings page on the phone. Measures the download speed, then plays
  // generated clips of growing resolution and frame rate and reports whether playback kept
  // up with the clock and how many frames were dropped. Every clip is downloaded whole
  // before it plays, so a slow network does not pass for a slow decoder.

  var test = null;          // the check in progress
  var doneTimer = null;

  function testSend(fields) {
    if (!test || !ws || ws.readyState !== 1) return;
    fields.type = 'test';
    fields.run = test.run;
    ws.send(JSON.stringify(fields));
  }

  function testWaiting(text) {
    note.className = '';
    waitingText.textContent = text;
    showScreen('waiting');
  }

  // Stops the check without a word to the server.
  function dropTest() {
    if (!test) return;
    if (test.cancel) test.cancel();
    test = null;
    note.className = '';
    waitingText.textContent = WAITING_HINT;
    if (!current) showScreen(started ? 'waiting' : 'start');
  }

  function abortTest() {
    testSend({ aborted: true });
    dropTest();
  }

  function startTest(command) {
    if (test) return;
    clearTimeout(doneTimer);
    test = {
      run: command.run,
      clips: command.clips || [],
      seconds: command.seconds || 6,
      limits: command.limits || { min_advance: 0.9, max_dropped: 0.05 },
      results: {},   // by clip id
      list: [],
      speedKbps: 0,
      cancel: null   // stops whatever step is running
    };
    // Sound needs the press on «Почати», and a film must not be interrupted.
    if (current || !started) { abortTest(); return; }
    var mine = test;
    testSend({ step: 0 });
    testWaiting('Перевірка проектора: вимірюю швидкість мережі…');
    measureSpeed(mine, command.speed || {}, function (kbps) {
      mine.speedKbps = kbps;
      nextClip(mine, 0);
    });
  }

  // Downloads the same block of random bytes over and over for a fixed time.
  function measureSpeed(mine, speed, done) {
    var began = Date.now();
    var limit = (speed.seconds || 6) * 1000;
    var bytes = 0;      // from finished requests
    var loaded = 0;     // from the request in flight
    var xhr = null;
    var over = false;

    function finish() {
      if (over) return;
      over = true;
      clearTimeout(timer);
      if (xhr) xhr.abort();
      mine.cancel = null;
      var elapsed = Date.now() - began;
      // Bytes × 8 per millisecond is kilobits per second.
      done(elapsed > 0 ? Math.round((bytes + loaded) * 8 / elapsed) : 0);
    }

    function request() {
      xhr = new XMLHttpRequest();
      xhr.open('GET', speed.url + '?t=' + Date.now());
      xhr.responseType = 'arraybuffer';
      xhr.onprogress = function (event) { loaded = event.loaded || 0; };
      xhr.onload = function () {
        if (over) return;
        if (xhr.status !== 200) { finish(); return; }
        bytes += xhr.response ? xhr.response.byteLength : loaded;
        loaded = 0;
        request();
      };
      xhr.onerror = finish;
      xhr.send();
    }

    var timer = setTimeout(finish, limit);
    mine.cancel = function () { over = true; clearTimeout(timer); if (xhr) xhr.abort(); };
    if (speed.url) request(); else finish();
  }

  function nextClip(mine, index) {
    if (test !== mine) return;
    if (index >= mine.clips.length) { finishTest(mine); return; }
    var clip = mine.clips[index];
    var label = clip.height + 'p, ' + clip.fps + ' кадрів/с';

    function report(result) {
      if (test !== mine) return;
      mine.cancel = null;
      result.id = clip.id;
      mine.results[clip.id] = result;
      mine.list.push(result);
      nextClip(mine, index + 1);
    }

    // Not worth trying when an easier clip did not play smoothly.
    var needed = clip.needs ? mine.results[clip.needs] : null;
    if (clip.needs && !(needed && needed.smooth)) { report({ outcome: 'skipped' }); return; }

    testSend({ step: index + 1 });
    testWaiting('Перевірка проектора: ' + label + ', завантажую ролик…');
    var xhr = new XMLHttpRequest();
    var timer = setTimeout(function () {
      xhr.abort();
      report({ outcome: 'failed', reason: 'download' });
    }, 60000);
    mine.cancel = function () { clearTimeout(timer); xhr.abort(); };
    xhr.open('GET', clip.url);
    xhr.responseType = 'blob';
    xhr.onload = function () {
      clearTimeout(timer);
      if (test !== mine) return;
      if (xhr.status !== 200 || !xhr.response) {
        report({ outcome: 'failed', reason: 'download' });
        return;
      }
      playClip(mine, clip, label, xhr.response, report);
    };
    xhr.onerror = function () {
      clearTimeout(timer);
      report({ outcome: 'failed', reason: 'download' });
    };
    xhr.send();
  }

  function playClip(mine, clip, label, blob, report) {
    var urls = window.URL || window.webkitURL;
    var blobUrl = urls && urls.createObjectURL ? urls.createObjectURL(blob) : null;
    // Its own element, so the listeners and the volume of the real player stay untouched.
    var node = document.createElement('video');
    var began = 0;          // when playback started, by the clock
    var beganAt = 0;        // and by the position in the clip
    var beganFrames = null;
    var over = false;
    var measure = null;
    // A clip that has not started in this long is not going to.
    var guard = setTimeout(function () { finish('failed'); }, 15000);

    function frames() {
      if (node.getVideoPlaybackQuality) {
        var quality = node.getVideoPlaybackQuality();
        return { total: quality.totalVideoFrames || 0, dropped: quality.droppedVideoFrames || 0 };
      }
      if (typeof node.webkitDecodedFrameCount === 'number') {
        return { total: node.webkitDecodedFrameCount, dropped: node.webkitDroppedFrameCount || 0 };
      }
      return null;
    }

    function remove() {
      over = true;
      clearTimeout(guard);
      clearTimeout(measure);
      try { node.pause(); node.removeAttribute('src'); node.load(); } catch (e) {}
      if (node.parentNode) node.parentNode.removeChild(node);
      if (blobUrl) urls.revokeObjectURL(blobUrl);
      note.className = '';
    }

    function finish(outcome) {
      if (over) return;
      var result = { outcome: outcome };
      if (outcome === 'played') {
        var elapsed = (Date.now() - began) / 1000;
        var now = frames();
        result.advance = elapsed > 0 ? (node.currentTime - beganAt) / elapsed : 0;
        result.frames = now && beganFrames ? now.total - beganFrames.total : 0;
        result.dropped = now && beganFrames ? now.dropped - beganFrames.dropped : 0;
        result.smooth = result.advance >= mine.limits.min_advance &&
                        result.dropped <= result.frames * mine.limits.max_dropped;
      } else {
        result.reason = 'play';
      }
      remove();
      report(result);
    }

    node.muted = true;
    node.setAttribute('playsinline', '');
    node.className = 'on';
    node.addEventListener('playing', function () {
      if (began || over) return;
      began = Date.now();
      beganAt = node.currentTime;
      beganFrames = frames();
      clearTimeout(guard);
      measure = setTimeout(function () { finish('played'); }, mine.seconds * 1000);
    });
    node.addEventListener('ended', function () { finish(began ? 'played' : 'failed'); });
    node.addEventListener('error', function () { finish('failed'); });
    mine.cancel = remove;

    showScreen('playing');
    document.body.insertBefore(node, note);
    note.textContent = 'Перевірка: ' + label;
    note.className = 'on';
    node.src = blobUrl || clip.url;
    var result = node.play();
    if (result && result['catch']) result['catch'](function () { finish('failed'); });
  }

  function finishTest(mine) {
    testSend({
      done: true,
      speed_kbps: mine.speedKbps,
      clips: mine.list,
      device: {
        agent: navigator.userAgent,
        screen_width: screen.width * (window.devicePixelRatio || 1),
        screen_height: screen.height * (window.devicePixelRatio || 1)
      }
    });
    test = null;
    testWaiting('Перевірку завершено. Результат — у налаштуваннях на телефоні.');
    doneTimer = setTimeout(function () {
      if (!test) waitingText.textContent = WAITING_HINT;
    }, 20000);
  }

  // ---------------------------------------------------- pairing by QR code

  function request(method, url, body, done) {
    var xhr = new XMLHttpRequest();
    xhr.open(method, url);
    if (body) xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.onload = function () {
      var data = null;
      try { data = JSON.parse(xhr.responseText); } catch (e) {}
      done(xhr.status, data);
    };
    xhr.onerror = function () { done(0, null); };
    xhr.send(body ? JSON.stringify(body) : null);
  }

  function ready() {
    clearTimeout(pairTimer);
    showScreen(!started ? 'start' : current ? 'playing' : 'waiting');
    if (!started) document.getElementById('start-btn').focus();
    connect();
  }

  function startPairing() {
    clearTimeout(pairTimer);
    clearTimeout(retryTimer);
    if (current) { video.pause(); }
    pairQr.className = '';
    pairText.textContent = PAIR_HINT;
    showScreen('pair');
    request('POST', '/api/pair/start', null, function (status, data) {
      if (status !== 200 || !data) {
        pairText.textContent = status === 429
          ? 'Забагато спроб підключення. Зачекайте кілька хвилин.'
          : 'Немає зв\u2019язку із сервером, пробую ще раз…';
        pairTimer = setTimeout(startPairing, status === 429 ? 60000 : 5000);
        return;
      }
      // The picture comes ready from the server: the projector browser only has to show it.
      pairQr.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(data.qr);
      pairQr.className = 'on';
      poll(data.token);
    });
  }

  function poll(token) {
    pairTimer = setTimeout(function () {
      request('POST', '/api/pair/status', { token: token }, function (status, data) {
        if (status === 200 && data && data.status === 'approved') { ready(); return; }
        if (status === 200 && data && data.status === 'expired') { startPairing(); return; }
        poll(token);
      });
    }, 2000);
  }

  video.addEventListener('play', sendState);
  video.addEventListener('pause', function () { showBar(); sendState(); });
  video.addEventListener('seeking', showBar);
  video.addEventListener('seeked', sendState);
  video.addEventListener('volumechange', sendState);
  video.addEventListener('loadedmetadata', function () {
    if (resumeAt > 0) {
      var position = resumeAt;
      resumeAt = 0;
      seekTo(position);
    }
    sendState();
  });
  video.addEventListener('timeupdate', trackPosition);
  video.addEventListener('ended', finished);
  video.addEventListener('error', failed);

  document.getElementById('start-btn').addEventListener('click', function () {
    enterFullscreen();
    started = true;
    if (current) {
      showScreen('playing');
      play();
    } else {
      showScreen('waiting');
    }
    sendState();
  });

  document.getElementById('takeover-btn').addEventListener('click', function () {
    enterFullscreen();
    showScreen(!started ? 'start' : current ? 'playing' : 'waiting');
    connect();
  });

  // If fullscreen was left (with the Back button, for example),
  // any press or the OK key on the remote brings it back.
  document.addEventListener('click', function () { if (started) enterFullscreen(); });
  document.addEventListener('keydown', function (event) {
    if (started && (event.keyCode === 13 || event.keyCode === 32)) enterFullscreen();
  });

  // The cursor stays hidden until the mouse moves.
  document.addEventListener('mousemove', function () {
    document.body.className = 'pointer';
    clearTimeout(pointerTimer);
    pointerTimer = setTimeout(function () { document.body.className = ''; }, 3000);
  });

  setInterval(sendState, 1000);

  // If this screen is already paired or someone logged in here with a password, start right away.
  request('GET', '/api/me', null, function (status) {
    if (status === 200) ready();
    else if (status === 401) startPairing();
    else { showScreen('waiting'); waitingText.textContent = 'Немає зв\u2019язку із сервером, підключаюсь…'; setTimeout(function () { location.reload(); }, 5000); }
  });
})();
