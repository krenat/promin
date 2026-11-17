// Selecting several videos to delete at once (administrators only).

import { api, failureText } from '../api.js';
import { visibleVideos } from './folders.js';
import { refresh, renderList } from './library.js';
import { state } from './state.js';
import { message } from './ui.js';

function selectedIds() {
  return visibleVideos().filter(function (v) { return state.selected[v.id]; }).map(function (v) { return v.id; });
}

export function updateSelectBar() {
  var count = selectedIds().length;
  document.body.classList.toggle('selecting', state.selecting);
  document.getElementById('select-start').hidden = state.selecting || !visibleVideos().length;
  ['select-count', 'select-all', 'select-delete', 'select-cancel'].forEach(function (id) {
    document.getElementById(id).hidden = !state.selecting;
  });
  document.getElementById('select-count').textContent = 'Вибрано: ' + count;
  document.getElementById('select-delete').disabled = !count;
  document.getElementById('select-all').textContent =
    count && count === visibleVideos().length ? 'Зняти всі' : 'Усі';
}

export function setSelecting(on) {
  state.selecting = on;
  state.selected = {};
  renderList();
  updateSelectBar();
}

export function toggleSelected(id) {
  if (state.selected[id]) delete state.selected[id];
  else state.selected[id] = true;
  renderList();
  updateSelectBar();
}

function deleteSelected() {
  var ids = selectedIds();
  if (!ids.length) return;
  var what = ids.length === 1 ? '1 відео' : ids.length + ' відео';
  if (!confirm('Видалити ' + what + '? Відновити їх не вдасться.')) return;
  message.textContent = '';
  var button = document.getElementById('select-delete');
  button.disabled = true;
  api('POST', '/api/videos/delete', { ids: ids }, 'Не вдалося видалити відео.').then(function (result) {
    setSelecting(false);
    if (result.missing.length) message.textContent = 'Частину відео вже видалив хтось інший.';
    return refresh();
  }).catch(function (error) {
    button.disabled = false;
    if (error.message !== 'login') message.textContent = failureText(error);
  });
}

document.getElementById('select-start').addEventListener('click', function () { setSelecting(true); });
document.getElementById('select-cancel').addEventListener('click', function () { setSelecting(false); });
document.getElementById('select-delete').addEventListener('click', deleteSelected);
document.getElementById('select-all').addEventListener('click', function () {
  var all = selectedIds().length === visibleVideos().length;
  state.selected = {};
  if (!all) visibleVideos().forEach(function (v) { state.selected[v.id] = true; });
  renderList();
  updateSelectBar();
});
