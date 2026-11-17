// The settings page: how new videos are encoded, and the projector check.

import { api } from './api.js';
import { el } from './dom.js';

var form = document.getElementById('form');
var message = document.getElementById('message');
var save = document.getElementById('save');
var reset = document.getElementById('reset');
var defaults = null;
var HEIGHT_NAMES = { 720: '720p (HD)', 1080: '1080p (Full HD)', 1440: '1440p (2K)', 2160: '2160p (4K)' };
// A sensible bitrate ceiling for each resolution, suggested when the resolution changes.
var SUGGESTED_KBPS = {};
var checked = null;   // the last finished projector check
var PRESET_NAMES = {
  ultrafast: 'ultrafast — найшвидше, найгірша якість',
  superfast: 'superfast',
  veryfast: 'veryfast — швидко',
  faster: 'faster',
  fast: 'fast',
  medium: 'medium — повільно, краща якість',
  slow: 'slow — дуже повільно'
};

function say(text, isError) {
  message.textContent = text;
  message.className = isError ? 'error' : '';
}

function fillSelect(select, values, label) {
  select.textContent = '';
  values.forEach(function (value) {
    var option = document.createElement('option');
    option.value = value;
    option.textContent = label(value);
    select.appendChild(option);
  });
}

function show(values) {
  Object.keys(values).forEach(function (key) { form[key].value = values[key]; });
  updateHints();
}

function updateHints() {
  var height = Number(form.max_height.value);
  document.getElementById('height-hint').hidden = height <= 1080;
  document.getElementById('bitrate-hint').textContent =
    'Для ' + HEIGHT_NAMES[height] + ' зазвичай вистачає ' + SUGGESTED_KBPS[height] + ' кбіт/с.';
  updateWarnings();
}

// Says so when the chosen values go past what the projector managed in its last check.
function updateWarnings() {
  var fit = document.getElementById('fit-hint');
  var network = document.getElementById('network-hint');
  fit.hidden = network.hidden = true;
  if (!checked) return;
  var id = form.max_height.value + 'p' + form.max_fps.value;
  var clip = checked.clips.filter(function (c) { return c.id === id; })[0];
  if (clip && clip.status !== 'ok') {
    fit.textContent = 'Під час перевірки проектор ' + {
      rough: 'відтворював таке відео з ривками.',
      failed: 'не зміг відтворити таке відео.',
      skipped: 'не впорався вже з легшим відео, тому таке не перевірялося.'
    }[clip.status];
    fit.hidden = false;
  }
  var limit = checked.fits.network_kbps;
  if (limit && Number(form.max_bitrate_kbps.value) > limit) {
    network.textContent = 'Це більше за половину швидкості мережі до проектора (' +
      mbps(checked.speed_kbps) + '): відео може зупинятися. Безпечна стеля — ' + limit + ' кбіт/с.';
    network.hidden = false;
  }
}

function mbps(kbps) {
  return (kbps / 1000).toFixed(kbps < 10000 ? 1 : 0).replace('.', ',') + ' Мбіт/с';
}

function build(data) {
  var choices = data.choices;
  defaults = data.defaults;
  SUGGESTED_KBPS = data.suggested_kbps;
  fillSelect(form.max_height, choices.max_height, function (v) { return HEIGHT_NAMES[v] || v + 'p'; });
  fillSelect(form.max_fps, choices.max_fps, function (v) { return v + ' кадрів/с'; });
  fillSelect(form.audio_bitrate_kbps, choices.audio_bitrate_kbps, function (v) { return v + ' кбіт/с'; });
  fillSelect(form.x264_preset, choices.x264_preset, function (v) { return PRESET_NAMES[v] || v; });
  form.max_bitrate_kbps.min = choices.max_bitrate_kbps[0];
  form.max_bitrate_kbps.max = choices.max_bitrate_kbps[1];
  form.crf.min = choices.crf[0];
  form.crf.max = choices.crf[1];
  document.getElementById('crf-hint').textContent =
    'Від ' + choices.crf[0] + ' до ' + choices.crf[1] + '. Менше число — краща якість і більший файл; типове значення ' + defaults.crf + '.';
  show(data.encoding);
  save.disabled = false;
  reset.disabled = false;
}

form.max_height.addEventListener('change', function () {
  form.max_bitrate_kbps.value = SUGGESTED_KBPS[Number(form.max_height.value)];
  updateHints();
});
form.max_fps.addEventListener('change', updateWarnings);
form.max_bitrate_kbps.addEventListener('input', updateWarnings);

reset.addEventListener('click', function () {
  show(defaults);
  say('Типові значення підставлено. Натисніть «Зберегти», щоб вони почали діяти.', false);
});

form.addEventListener('submit', function (e) {
  e.preventDefault();
  say('', false);
  save.disabled = true;
  api('PUT', '/api/settings', {
    max_height: Number(form.max_height.value),
    max_fps: Number(form.max_fps.value),
    max_bitrate_kbps: Number(form.max_bitrate_kbps.value),
    crf: Number(form.crf.value),
    audio_bitrate_kbps: Number(form.audio_bitrate_kbps.value),
    x264_preset: form.x264_preset.value
  }).then(function (data) {
    show(data.encoding);
    say('Збережено. Діятиме для наступних відео.', false);
  }).catch(function (error) {
    if (error.message !== 'login') say(error.message, true);
  }).then(function () { save.disabled = false; });
});

// ------------------------------------------------------- projector check

var check = document.getElementById('check');
var checkStatus = document.getElementById('check-status');
var checkResult = document.getElementById('check-result');
var checkTimer = null;

function add(parent, tag, text, className) {
  return parent.appendChild(el(tag, className, text));
}

function verdict(clip) {
  if (clip.status === 'ok') return 'плавно';
  if (clip.status === 'skipped') return 'не перевірялося';
  if (clip.status === 'failed') return clip.reason === 'download' ? 'ролик не завантажився' : 'не відтворюється';
  var dropped = clip.frames ? Math.round(clip.dropped / clip.frames * 100) : 0;
  if (dropped) return 'ривки: пропущено ' + dropped + '% кадрів';
  return 'ривки: відстає від годинника на ' + Math.round((1 - clip.advance) * 100) + '%';
}

function showResult(result) {
  checked = result;
  checkResult.textContent = '';
  checkResult.hidden = !result;
  updateWarnings();
  if (!result) return;
  var fits = result.fits;
  add(checkResult, 'p', fits.max_height
    ? 'Плавно відтворює до ' + fits.max_height + 'p, ' + fits.max_fps + ' кадрів/с.'
    : 'Жоден тестовий ролик не відтворився плавно.', 'fits');
  var network = 'Швидкість мережі виміряти не вдалося.';
  if (result.speed_kbps) {
    // Half of the speed is left for everything else on the network.
    network = 'Мережа до проектора: ' + mbps(result.speed_kbps) +
      (fits.network_kbps >= Number(form.max_bitrate_kbps.max)
        ? '. Цього досить для будь-якого бітрейту з налаштувань.'
        : ', тож стеля бітрейту — щонайбільше ' + fits.network_kbps + ' кбіт/с.');
  }
  add(checkResult, 'p', network);
  var list = add(checkResult, 'ul', '');
  var counted = false;
  var played = false;
  result.clips.forEach(function (clip) {
    var row = add(list, 'li', '', clip.status === 'rough' || clip.status === 'failed' ? 'bad' : '');
    add(row, 'span', clip.height + 'p · ' + clip.fps + ' кадрів/с');
    add(row, 'span', verdict(clip));
    if (clip.frames) counted = true;
    if (clip.status === 'ok' || clip.status === 'rough') played = true;
  });
  if (played && !counted) {
    add(checkResult, 'p', 'Браузер проектора не повідомляє про пропущені кадри, тому плавність оцінено лише за тим, чи встигало відео за годинником. Дрібні ривки так не видно: під час перевірки дивіться й на сам екран.');
  }
  add(checkResult, 'p', 'Перевірка показує, з чим упорається браузер проектора, а не роздільність його зображення: вищу за неї ставити немає сенсу.');
  var when = new Date(result.finished * 1000).toLocaleString('uk-UA', { dateStyle: 'long', timeStyle: 'short' });
  var size = result.device.screen_width
    ? ' Екран за даними браузера: ' + result.device.screen_width + '×' + result.device.screen_height + '.' : '';
  add(checkResult, 'p', 'Перевірено ' + when + '.' + size + ' Браузер: ' + (result.device.agent || 'невідомий') + '.', 'small');
}

function showCheck(data) {
  clearTimeout(checkTimer);
  var busy = data.status !== 'idle';
  check.disabled = busy;
  checkStatus.className = !busy && data.error ? 'error' : '';
  if (data.status === 'preparing') {
    checkStatus.textContent = 'Сервер готує тестові ролики. Першого разу це може тривати кілька хвилин…';
  } else if (data.status === 'running') {
    checkStatus.textContent = data.step
      ? 'Проектор відтворює тестові ролики: ' + data.step + ' з ' + data.steps + '…'
      : 'Проектор вимірює швидкість мережі…';
  } else {
    checkStatus.textContent = data.error || '';
  }
  showResult(data.result);
  if (busy) checkTimer = setTimeout(pollCheck, 2000);
}

function pollCheck() {
  api('GET', '/api/tvtest').then(showCheck).catch(function (error) {
    // A dropped connection is not the end of the check: ask again.
    if (error.message !== 'login') checkTimer = setTimeout(pollCheck, 5000);
  });
}

check.addEventListener('click', function () {
  check.disabled = true;
  api('POST', '/api/tvtest').then(showCheck).catch(function (error) {
    if (error.message === 'login') return;
    check.disabled = false;
    checkStatus.className = 'error';
    checkStatus.textContent = error.message;
  });
});

api('GET', '/api/settings').then(build).then(pollCheck).catch(function (error) {
  if (error.message !== 'login') say(error.message, true);
});
