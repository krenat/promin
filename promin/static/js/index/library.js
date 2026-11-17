// The list of videos: the cards, keeping the list fresh, retrying and deleting a video.

import { api, failureText } from '../api.js';
import { el } from '../dom.js';
import { folderById, openMoveDialog, renderFolders, visibleVideos } from './folders.js';
import { openPlayer } from './player.js';
import { addToQueue } from './queue.js';
import { canCast, command } from './remote.js';
import { toggleSelected, updateSelectBar } from './selection.js';
import { state } from './state.js';
import { ICON_ADD, displayName, formatBitrate, formatDuration, formatSize, lastLine, message, qualityDetails, qualityLabel, statusText } from './ui.js';

export var disk = document.getElementById('disk');
var list = document.getElementById('list');

function renderCard(item) {
  var card = el('article', 'card');
  var ready = item.status === 'ready';

  // The thumbnail is the "watch here" button; while selecting, it ticks the video instead.
  var thumb = el('button', 'thumb');
  thumb.type = 'button';
  thumb.disabled = !ready && !state.selecting;
  if (state.selecting) {
    thumb.setAttribute('aria-label', 'Вибрати: ' + item.name);
    thumb.setAttribute('aria-pressed', String(!!state.selected[item.id]));
    if (state.selected[item.id]) card.className += ' selected';
  } else {
    thumb.setAttribute('aria-label', 'Дивитись тут: ' + item.name);
  }
  var picture = el('img', item.thumb ? 'cover' : 'mark');
  picture.src = item.thumb ? '/thumb/' + item.id + '?v=' + (item.thumb_v || 1) : '/static/logo.svg';
  picture.alt = '';
  picture.loading = 'lazy';
  thumb.appendChild(picture);
  if (ready) {
    var play = el('span', 'play');
    play.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 4.5v15l12-7.5z"/></svg>';
    thumb.appendChild(play);
  }
  if (item.duration) thumb.appendChild(el('span', 'duration', formatDuration(item.duration)));
  if (ready && item.info && item.info.width) {
    var label = qualityLabel(item.info);
    var badge = el('span', 'quality' + (label === 'SD' ? '' : ' hd'), label);
    badge.title = qualityDetails(item.info);
    thumb.appendChild(badge);
  }
  if (state.selecting) {
    var check = el('span', 'check');
    check.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9.5 16.6 4.9 12l-1.4 1.4 6 6 11-11-1.4-1.4z"/></svg>';
    thumb.appendChild(check);
  }
  thumb.addEventListener('click', function () {
    if (state.selecting) toggleSelected(item.id);
    else openPlayer(item);
  });
  card.appendChild(thumb);

  if (state.canManage) {
    var menu = el('details', 'menu');
    var summary = el('summary', null, '⋯');
    summary.setAttribute('aria-label', 'Меню відео: ' + item.name);
    menu.appendChild(summary);
    var pop = el('div', 'pop');
    if (state.folders.length) {
      var moveBtn = el('button', 'neutral', 'Перенести в теку');
      moveBtn.type = 'button';
      moveBtn.addEventListener('click', function () {
        menu.open = false;
        openMoveDialog(item);
      });
      pop.appendChild(moveBtn);
    }
    var del = el('button', null, 'Видалити');
    del.type = 'button';
    del.addEventListener('click', function () {
      menu.open = false;
      if (confirm('Видалити «' + item.name + '»? Відновити його не вдасться.')) remove(item.id);
    });
    pop.appendChild(del);
    menu.appendChild(pop);
    card.appendChild(menu);
  }

  var body = el('div', 'body');
  var name = el('div', 'name', displayName(item.name));
  name.title = item.name;
  body.appendChild(name);
  // A finished video says how heavy it is; one in progress says where it is.
  var metaText = ready && item.info && item.info.bitrate_kbps
    ? formatBitrate(item.info.bitrate_kbps)
    : statusText(item);
  var meta = el('div', 'meta', formatSize(item.size) + ' · ' + metaText);
  if (ready && item.info) meta.title = qualityDetails(item.info);
  body.appendChild(meta);

  if (item.status === 'processing') {
    var track = el('div', 'track');
    var bar = el('div', 'bar');
    bar.style.width = Math.floor((item.progress || 0) * 100) + '%';
    track.appendChild(bar);
    body.appendChild(track);
  }
  if (item.status === 'error') {
    body.appendChild(el('div', 'error-text', lastLine(item.error)));
  }

  var actions = el('div', 'actions');
  if (ready) {
    var cast = el('button', 'primary', 'На проектор');
    cast.type = 'button';
    cast.disabled = !canCast();
    cast.addEventListener('click', function () {
      document.body.classList.remove('remote-collapsed');
      command('load', { video_id: item.id });
    });
    actions.appendChild(cast);
    var add = el('button', 'outline queue-add');
    add.type = 'button';
    add.title = 'Додати в чергу';
    add.setAttribute('aria-label', 'Додати в чергу: ' + item.name);
    add.innerHTML = ICON_ADD;
    add.addEventListener('click', function () { addToQueue(item.id, add); });
    actions.appendChild(add);
  } else if (item.status === 'error' && state.canManage) {
    var again = el('button', 'outline', 'Повторити');
    again.type = 'button';
    again.addEventListener('click', function () { retry(item.id); });
    actions.appendChild(again);
  }
  if (actions.childNodes.length) body.appendChild(actions);

  card.appendChild(body);
  return card;
}

var pollTimer = null;
var lastRendered = null;

function schedule(data) {
  clearTimeout(pollTimer);
  var busy = data.videos.some(function (v) {
    return v.status === 'queued' || v.status === 'processing';
  });
  // While something is processing, the list refreshes itself.
  if (busy) pollTimer = setTimeout(refresh, 3000);
}

export function renderList() {
  // Do not redraw an unchanged list, so that an open menu stays open.
  var snapshot = JSON.stringify([state.videos, state.folders, state.currentFolder, canCast(), state.selecting, state.selected]);
  if (snapshot === lastRendered) return;
  lastRendered = snapshot;
  list.textContent = '';
  if (!state.videos.length) {
    list.appendChild(el('div', 'empty', 'Тут з’являться ваші відео'));
    return;
  }
  var shown = visibleVideos();
  if (!shown.length) {
    list.appendChild(el('div', 'empty', state.currentFolder === 'none'
      ? 'Усі відео розкладено по теках'
      : 'У цій теці поки немає відео. Перенесіть сюди відео через меню «⋯» на картці або завантажте нове.'));
    return;
  }
  shown.forEach(function (item) { list.appendChild(renderCard(item)); });
}

export function refresh() {
  return api('GET', '/api/videos')
    .then(function (data) {
      state.maxUpload = data.max_upload;
      disk.textContent = 'Вільно на сервері: ' + formatSize(data.disk.free);
      schedule(data);
      state.videos = data.videos;
      state.folders = data.folders || [];
      // A folder deleted elsewhere: back to all videos.
      if (state.currentFolder !== 'all' && state.currentFolder !== 'none' && !folderById(state.currentFolder)) {
        state.currentFolder = 'all';
      }
      if (state.currentFolder === 'none' && !state.folders.length) state.currentFolder = 'all';
      renderFolders();
      // Videos deleted elsewhere drop out of the selection.
      var present = {};
      state.videos.forEach(function (v) { present[v.id] = true; });
      Object.keys(state.selected).forEach(function (id) { if (!present[id]) delete state.selected[id]; });
      if (state.isAdmin) updateSelectBar();
      renderList();
    })
    .catch(function (error) {
      if (error.message === 'login') return;
      message.textContent = 'Не вдалося отримати список відео. Перевірте з’єднання із сервером.';
      clearTimeout(pollTimer);
      pollTimer = setTimeout(refresh, 10000);
    });
}

function retry(id) {
  message.textContent = '';
  api('POST', '/api/videos/' + id + '/retry', null, 'Не вдалося повторити обробку.')
    .catch(function (error) {
      if (error.message === 'login') throw error;
      message.textContent = failureText(error);
    })
    .then(refresh, function () {});
}

function remove(id) {
  message.textContent = '';
  api('DELETE', '/api/videos/' + id)
    .catch(function (error) {
      // Deleted by someone else a moment ago: that is what was wanted.
      if (error.status !== 404) throw error;
    })
    .then(refresh, function (error) {
      if (error.message !== 'login') message.textContent = 'Не вдалося видалити відео. Спробуйте ще раз.';
    });
}
