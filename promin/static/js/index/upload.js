// Uploading a video from the phone.

import { folderById } from './folders.js';
import { refresh } from './library.js';
import { state } from './state.js';
import { errorText, formatSize, message } from './ui.js';

var fileInput = document.getElementById('file');
export var uploadBtn = document.getElementById('upload-btn');
// The small copy of the button in the header of a phone.
export var uploadQuick = document.getElementById('upload-quick');
var progress = document.getElementById('progress');
var progressName = document.getElementById('progress-name');
var progressPct = document.getElementById('progress-pct');
var progressBar = document.getElementById('progress-bar');

function upload(file) {
  message.textContent = '';
  if (file.size > state.maxUpload) {
    message.textContent = 'Файл завеликий: ' + formatSize(file.size) + ', дозволено до ' + formatSize(state.maxUpload) + '.';
    return;
  }
  uploadBtn.disabled = uploadQuick.disabled = true;
  progress.hidden = false;
  progressName.textContent = file.name;
  progressPct.textContent = '0%';
  progressBar.style.width = '0';

  var xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/videos');
  xhr.setRequestHeader('X-Filename', encodeURIComponent(file.name));
  if (folderById(state.currentFolder)) xhr.setRequestHeader('X-Folder', state.currentFolder);
  xhr.setRequestHeader('Content-Type', file.type || 'application/octet-stream');
  xhr.upload.onprogress = function (e) {
    if (!e.lengthComputable) return;
    var pct = Math.floor(e.loaded / e.total * 100);
    progressPct.textContent = pct + '%';
    progressBar.style.width = pct + '%';
  };
  function done() {
    uploadBtn.disabled = uploadQuick.disabled = false;
    progress.hidden = true;
    fileInput.value = '';
  }
  xhr.onload = function () {
    done();
    if (xhr.status === 201) {
      refresh();
    } else if (xhr.status === 401) {
      location.href = '/login';
    } else {
      message.textContent = errorText(xhr.responseText, 'Не вдалося завантажити відео (помилка ' + xhr.status + ').');
    }
  };
  xhr.onerror = function () {
    done();
    message.textContent = 'Завантаження обірвалося. Перевірте з’єднання і спробуйте ще раз.';
  };
  xhr.send(file);
}

uploadBtn.addEventListener('click', function () { fileInput.click(); });
uploadQuick.addEventListener('click', function () { fileInput.click(); });
fileInput.addEventListener('change', function () {
  if (fileInput.files.length) upload(fileInput.files[0]);
});
