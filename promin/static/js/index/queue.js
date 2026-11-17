// The watch queue of the projector.

import { api, failureText } from '../api.js';
import { el } from '../dom.js';
import { canCast, command, updateRemote } from './remote.js';
import { state } from './state.js';
import { ICON_ADD, ICON_DONE, ICON_DOWN, ICON_REMOVE, ICON_UP, displayName, message } from './ui.js';

function queueRequest(method, url, body) {
  message.textContent = '';
  return api(method, url, body, 'Не вдалося змінити чергу.').then(function (data) {
    state.queue = data.queue;
    renderQueue();
    updateRemote();
  }).catch(function (error) {
    if (error.message === 'login') return;
    message.textContent = failureText(error);
    throw error;
  });
}

export function addToQueue(id, button) {
  queueRequest('POST', '/api/queue', { video_id: id }).then(function () {
    // A short tick on the button: the queue itself may be scrolled out of view.
    button.innerHTML = ICON_DONE;
    setTimeout(function () { button.innerHTML = ICON_ADD; }, 1200);
  }, function () {});
}

function queueButton(icon, label, disabled, onClick) {
  var button = el('button', 'icon-btn');
  button.type = 'button';
  button.innerHTML = icon;
  button.setAttribute('aria-label', label);
  button.title = label;
  button.disabled = disabled;
  button.addEventListener('click', onClick);
  return button;
}

export function renderQueue() {
  var box = document.getElementById('queue');
  box.hidden = !state.queue.length;
  if (!state.queue.length) return;
  document.getElementById('queue-title').textContent = 'Черга · ' + state.queue.length;
  var playing = state.tvState && state.tvState.video_id;
  var play = document.getElementById('queue-play');
  play.textContent = playing ? 'Наступне' : 'Грати';
  play.disabled = !canCast();
  play.title = canCast() ? '' : 'Проектор не на зв’язку або на ньому не натиснули «Почати»';

  var rows = document.getElementById('queue-list');
  rows.textContent = '';
  state.queue.forEach(function (entry, i) {
    var row = el('li', 'q-row');
    var picture = el('img', entry.thumb ? null : 'mark');
    picture.src = entry.thumb ? '/thumb/' + entry.video_id + '?v=' + (entry.thumb_v || 1) : '/static/logo.svg';
    picture.alt = '';
    row.appendChild(picture);
    var name = el('span', 'q-name', displayName(entry.name));
    name.title = entry.name;
    row.appendChild(name);
    var move = function (to) {
      return function () {
        queueRequest('POST', '/api/queue/' + entry.entry + '/move', { position: to }).catch(function () {});
      };
    };
    row.appendChild(queueButton(ICON_UP, 'Вище', i === 0, move(i - 1)));
    row.appendChild(queueButton(ICON_DOWN, 'Нижче', i === state.queue.length - 1, move(i + 1)));
    row.appendChild(queueButton(ICON_REMOVE, 'Прибрати з черги', false, function () {
      queueRequest('DELETE', '/api/queue/' + entry.entry).catch(function () {});
    }));
    rows.appendChild(row);
  });
}

document.getElementById('queue-play').addEventListener('click', function () {
  document.body.classList.remove('remote-collapsed');
  command('next');
});
document.getElementById('queue-clear').addEventListener('click', function () {
  if (confirm('Очистити чергу? Відео з бібліотеки не видаляться.')) {
    queueRequest('DELETE', '/api/queue').catch(function () {});
  }
});
