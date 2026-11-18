// The remote: the connection to the server, the projector's state and the control panel.

import { renderList } from './library.js';
import { renderQueue } from './queue.js';
import { state } from './state.js';
import { ICON_PAUSE, ICON_PLAY, displayName, formatDuration, message } from './ui.js';

var ws = null;
var wsRetry = 1000;
var wsTimer = null;
var seeking = false;

export function canCast() {
  return state.tvConnected && !!state.tvState && state.tvState.started;
}

export function command(action, extra) {
  message.textContent = '';
  if (!ws || ws.readyState !== 1) {
    message.textContent = 'Немає зв’язку із сервером. Спробуйте за кілька секунд.';
    return;
  }
  var payload = { type: 'command', action: action };
  for (var key in extra || {}) payload[key] = extra[key];
  ws.send(JSON.stringify(payload));
}

function updateTv() {
  var text = 'Проектор не на зв’язку';
  if (state.tvConnected) {
    text = state.tvState && !state.tvState.started ? 'Натисніть «Почати» на проекторі' : 'Проектор на зв’язку';
  }
  document.getElementById('tv-text').textContent = text;
  document.getElementById('projector').className = 'projector' + (state.tvConnected ? ' on' : '');
  // Only people with a login may send the projector away, as with every other command.
  document.getElementById('tv-on').hidden = !(state.tvConnected && state.canManage);
  renderList();
  updateRemote();
  renderQueue();
}

// The remote lies over the page, and the list leaves it exactly the room it takes.
var dock = document.getElementById('dock');
function fitDock() {
  document.documentElement.style.setProperty('--dock-h', dock.offsetHeight + 'px');
}
// A browser that cannot watch the size is told whenever the remote changes.
var watched = typeof ResizeObserver === 'function';
if (watched) new ResizeObserver(fitDock).observe(dock);

export function updateRemote() {
  var panel = document.getElementById('remote');
  var active = state.tvConnected && !!state.tvState && !!state.tvState.video_id;
  panel.hidden = !active;
  if (!watched) setTimeout(fitDock, 0);
  if (!active) return;
  document.getElementById('remote-title').textContent = state.tvState.name || '';
  var icon = state.tvState.paused ? ICON_PLAY : ICON_PAUSE;
  document.getElementById('r-play-icon').setAttribute('d', icon);
  document.getElementById('r-mini-icon').setAttribute('d', icon);
  var seek = document.getElementById('r-seek');
  seek.max = Math.floor(state.tvState.duration || 0);
  document.getElementById('r-dur').textContent = formatDuration(state.tvState.duration || 0);
  if (!seeking) {
    seek.value = Math.floor(state.tvState.position || 0);
    document.getElementById('r-pos').textContent = formatDuration(state.tvState.position || 0);
  }
  if (document.activeElement !== document.getElementById('r-vol')) {
    document.getElementById('r-vol').value = Math.round((state.tvState.volume || 0) * 100);
  }
  var ready = readyVideos();
  var index = currentIndex(ready);
  document.getElementById('r-prev').disabled = index <= 0;
  // "Next" takes the queue first and falls back to the library order.
  document.getElementById('r-next').disabled = !state.queue.length && (index < 0 || index >= ready.length - 1);
  var upNext = document.getElementById('up-next');
  upNext.hidden = !state.queue.length;
  upNext.textContent = state.queue.length ? 'Далі: ' + displayName(state.queue[0].name) : '';
}

function readyVideos() {
  return state.videos.filter(function (v) { return v.status === 'ready'; });
}

function currentIndex(ready) {
  for (var i = 0; i < ready.length; i++) {
    if (state.tvState && ready[i].id === state.tvState.video_id) return i;
  }
  return -1;
}

function step(delta) {
  if (delta > 0 && state.queue.length) { command('next'); return; }
  var ready = readyVideos();
  var target = ready[currentIndex(ready) + delta];
  if (target) command('load', { video_id: target.id });
}

function togglePlay() {
  if (state.tvState) command(state.tvState.paused ? 'play' : 'pause');
}

export function connect() {
  clearTimeout(wsTimer);
  if (ws && ws.readyState <= 1) return;
  var scheme = location.protocol === 'https:' ? 'wss://' : 'ws://';
  var socket = new WebSocket(scheme + location.host + '/ws?role=remote');
  ws = socket;
  socket.onopen = function () { wsRetry = 1000; };
  socket.onmessage = function (event) {
    var data;
    try { data = JSON.parse(event.data); } catch (e) { return; }
    if (data.type === 'tv') {
      state.tvConnected = data.connected;
      if (!state.tvConnected) state.tvState = null;
      updateTv();
    } else if (data.type === 'state') {
      state.tvState = data;
      updateTv();
    } else if (data.type === 'queue') {
      state.queue = data.queue || [];
      renderQueue();
      updateRemote();
    } else if (data.type === 'error') {
      message.textContent = data.message;
    }
  };
  socket.onclose = function (event) {
    if (ws !== socket) return;
    ws = null;
    if (event.code === 4401) { location.href = '/login'; return; }
    state.tvConnected = false;
    state.tvState = null;
    // The server sends the queue again on reconnect (or nothing, if it is empty by then).
    state.queue = [];
    updateTv();
    wsTimer = setTimeout(connect, wsRetry);
    wsRetry = Math.min(wsRetry * 2, 5000);
  };
}

document.getElementById('r-play').addEventListener('click', togglePlay);
document.getElementById('r-mini').addEventListener('click', togglePlay);
document.getElementById('r-back').addEventListener('click', function () { command('seek_by', { value: -10 }); });
document.getElementById('r-fwd').addEventListener('click', function () { command('seek_by', { value: 10 }); });
document.getElementById('r-prev').addEventListener('click', function () { step(-1); });
document.getElementById('r-next').addEventListener('click', function () { step(1); });
document.getElementById('r-stop').addEventListener('click', function () { command('stop'); });
document.getElementById('tv-disconnect').addEventListener('click', function () { command('disconnect'); });
document.getElementById('remote-toggle').addEventListener('click', function () {
  document.body.classList.toggle('remote-collapsed');
  if (!watched) fitDock();
});
document.getElementById('r-seek').addEventListener('input', function () {
  seeking = true;
  document.getElementById('r-pos').textContent = formatDuration(Number(this.value));
});
document.getElementById('r-seek').addEventListener('change', function () {
  seeking = false;
  command('seek', { value: Number(this.value) });
});
document.getElementById('r-vol').addEventListener('input', function () {
  command('volume', { value: Number(this.value) / 100 });
});
// After the phone is unlocked, reconnect at once without waiting for the timer.
document.addEventListener('visibilitychange', function () {
  if (document.visibilityState === 'visible') connect();
});
