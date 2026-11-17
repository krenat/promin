// Folders: the row of chips that filters the list, and moving a video between folders.

import { api, failureText } from '../api.js';
import { el } from '../dom.js';
import { refresh, renderList } from './library.js';
import { setSelecting, updateSelectBar } from './selection.js';
import { state } from './state.js';
import { displayName, message } from './ui.js';
import { uploadBtn } from './upload.js';

var FOLDER_KEY = 'promin.folder';
try { state.currentFolder = localStorage.getItem(FOLDER_KEY) || 'all'; } catch (e) {}

export function visibleVideos() {
  if (state.currentFolder === 'all') return state.videos;
  var wanted = state.currentFolder === 'none' ? null : state.currentFolder;
  return state.videos.filter(function (v) { return (v.folder || null) === wanted; });
}

export function folderById(id) {
  for (var i = 0; i < state.folders.length; i++) if (state.folders[i].id === id) return state.folders[i];
  return null;
}

function setFolder(id) {
  state.currentFolder = id;
  try { localStorage.setItem(FOLDER_KEY, id); } catch (e) {}
  if (state.selecting) setSelecting(false);
  renderFolders();
  renderList();
  if (state.isAdmin) updateSelectBar();
}

function chip(label, count, id, extraClass) {
  var button = el('button', 'chip' + (extraClass ? ' ' + extraClass : ''), label);
  button.type = 'button';
  if (count != null) button.appendChild(el('span', 'n', String(count)));
  if (id != null) {
    button.setAttribute('aria-pressed', String(state.currentFolder === id));
    button.addEventListener('click', function () { setFolder(id); });
  }
  return button;
}

export function renderFolders() {
  var nav = document.getElementById('folders');
  // Without folders the bar would only say "all": it appears once there is a folder,
  // or straight away for people who can create one.
  nav.hidden = !state.folders.length && !state.canManage;
  nav.textContent = '';
  if (!nav.hidden) {
    nav.appendChild(chip('Усі', state.videos.length, 'all'));
    state.folders.forEach(function (f) { nav.appendChild(chip(f.name, f.count, f.id)); });
    var loose = state.videos.filter(function (v) { return !v.folder; }).length;
    if (state.folders.length) nav.appendChild(chip('Без теки', loose, 'none'));
    if (state.canManage) {
      var add = chip('+ Тека', null, null, 'add');
      add.setAttribute('aria-label', 'Нова тека');
      add.addEventListener('click', createFolder);
      nav.appendChild(add);
    }
  }
  var folder = folderById(state.currentFolder);
  document.getElementById('folder-tools').hidden = !(folder && state.canManage);
  uploadBtn.textContent = folder ? 'Завантажити в «' + folder.name + '»' : 'Завантажити відео';
}

function folderRequest(method, url, body) {
  message.textContent = '';
  return api(method, url, body, 'Не вдалося змінити теки.').catch(function (error) {
    if (error.message !== 'login') message.textContent = failureText(error);
    throw error;
  });
}

function createFolder() {
  var name = prompt('Назва нової теки:');
  if (name == null || !name.trim()) return;
  folderRequest('POST', '/api/folders', { name: name }).then(function (folder) {
    return refresh().then(function () { setFolder(folder.id); });
  }, function () {});
}

document.getElementById('folder-rename').addEventListener('click', function () {
  var folder = folderById(state.currentFolder);
  if (!folder) return;
  var name = prompt('Нова назва теки:', folder.name);
  if (name == null || !name.trim() || name.trim() === folder.name) return;
  folderRequest('PATCH', '/api/folders/' + folder.id, { name: name }).then(refresh, function () {});
});

document.getElementById('folder-delete').addEventListener('click', function () {
  var folder = folderById(state.currentFolder);
  if (!folder) return;
  if (!confirm('Видалити теку «' + folder.name + '»? Відео з неї не видаляться, а залишаться без теки.')) return;
  folderRequest('DELETE', '/api/folders/' + folder.id).then(function () {
    setFolder('all');
    return refresh();
  }, function () {});
});

var moveDialog = document.getElementById('move-dialog');

export function openMoveDialog(item) {
  document.getElementById('move-title').textContent = 'Перенести «' + displayName(item.name) + '»';
  var options = document.getElementById('move-options');
  options.textContent = '';
  var choices = state.folders.map(function (f) { return { id: f.id, name: f.name }; });
  choices.push({ id: null, name: 'Без теки' });
  choices.forEach(function (choice) {
    var here = (item.folder || null) === choice.id;
    var button = el('button', 'outline', choice.name + (here ? ' · тут' : ''));
    button.type = 'button';
    button.disabled = here;
    if (here) button.setAttribute('aria-current', 'true');
    button.addEventListener('click', function () {
      moveDialog.close();
      folderRequest('POST', '/api/videos/' + item.id + '/folder', { folder: choice.id })
        .then(refresh, function () {});
    });
    options.appendChild(button);
  });
  moveDialog.showModal();
}

document.getElementById('move-cancel').addEventListener('click', function () { moveDialog.close(); });
// A tap on the dimmed background closes the dialog too.
moveDialog.addEventListener('click', function (e) { if (e.target === moveDialog) moveDialog.close(); });
