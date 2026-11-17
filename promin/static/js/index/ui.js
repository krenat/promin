// Small pieces every part uses: the message line, icons, formatting, making elements.

export var message = document.getElementById('message');

export var ICON_PAUSE = 'M6 5h4v14H6zM14 5h4v14h-4z';
// The rounded triangle of the logo, fitted into the 24-unit box of the other icons.
export var ICON_PLAY = 'M6 5.1c0-1.38 1.5-2.28 2.7-1.56l11.4 6.9c1.14.72 1.14 2.4 0 3.12l-11.4 6.9c-1.2.72-2.7-.18-2.7-1.56z';
export var ICON_ADD = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h12v2H3zm0 4h12v2H3zm0 4h8v2H3zm14 0v-3h2v3h3v2h-3v3h-2v-3h-3v-2z"/></svg>';
export var ICON_DONE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9.5 16.6 4.9 12l-1.4 1.4 6 6 11-11-1.4-1.4z"/></svg>';
export var ICON_UP = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7.4 15.4 12 10.8l4.6 4.6L18 14l-6-6-6 6z"/></svg>';
export var ICON_DOWN = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7.4 8.6 12 13.2l4.6-4.6L18 10l-6 6-6-6z"/></svg>';
export var ICON_REMOVE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18.3 7.1 16.9 5.7 12 10.6 7.1 5.7 5.7 7.1l4.9 4.9-4.9 4.9 1.4 1.4 4.9-4.9 4.9 4.9 1.4-1.4-4.9-4.9z"/></svg>';

export function formatSize(bytes) {
  if (bytes < 1048576) return Math.max(1, Math.round(bytes / 1024)) + ' КБ';
  var mb = bytes / 1048576;
  if (mb >= 1024) return (mb / 1024).toFixed(1).replace('.', ',') + ' ГБ';
  if (mb >= 10) return Math.round(mb) + ' МБ';
  return mb.toFixed(1).replace('.', ',') + ' МБ';
}

export function formatDuration(seconds) {
  var total = Math.round(seconds);
  var h = Math.floor(total / 3600);
  var m = Math.floor(total % 3600 / 60);
  var sec = total % 60;
  var tail = (sec < 10 ? '0' : '') + sec;
  if (h) return h + ':' + (m < 10 ? '0' : '') + m + ':' + tail;
  return m + ':' + tail;
}

// Quality of a finished video, from what the server read off the file.
export function qualityLabel(info) {
  var short = Math.min(info.width, info.height);  // a vertical 1080×1920 video is 1080p too
  var label = short >= 2000 ? '4K' : short >= 1400 ? '1440p' : short >= 1000 ? '1080p' : short >= 700 ? '720p' : 'SD';
  if (info.fps >= 48) label += Math.round(info.fps);
  return label;
}

export function formatBitrate(kbps) {
  if (kbps >= 1000) return (kbps / 1000).toFixed(1).replace('.', ',') + ' Мбіт/с';
  return kbps + ' кбіт/с';
}

var CODEC_NAMES = { h264: 'H.264', hevc: 'HEVC', aac: 'AAC', mp3: 'MP3', ac3: 'AC3' };

export function qualityDetails(info) {
  var parts = [info.width + '×' + info.height];
  if (info.fps) parts.push(String(Math.round(info.fps * 100) / 100).replace('.', ',') + ' кадрів/с');
  if (info.bitrate_kbps) parts.push(formatBitrate(info.bitrate_kbps));
  if (info.video_codec) parts.push(CODEC_NAMES[info.video_codec] || info.video_codec);
  if (info.audio_codec) {
    var layout = info.channels === 1 ? 'моно' : info.channels === 2 ? 'стерео' : info.channels + ' кан.';
    parts.push('звук ' + (CODEC_NAMES[info.audio_codec] || info.audio_codec) + ' ' + layout);
  } else {
    parts.push('без звуку');
  }
  return parts.join(' · ');
}

export function statusText(item) {
  if (item.status === 'queued') return 'у черзі';
  if (item.status === 'processing') return 'обробляється ' + Math.floor((item.progress || 0) * 100) + '%';
  if (item.status === 'error') return 'помилка';
  return 'готове';
}

export function lastLine(text) {
  var lines = String(text || '').split('\n').filter(function (l) { return l.trim(); });
  var line = lines.length ? lines[lines.length - 1].trim() : 'Невідома помилка';
  return line.length > 240 ? line.slice(0, 240) + '…' : line;
}

export function errorText(xhrOrResponseBody, fallback) {
  try {
    var data = JSON.parse(xhrOrResponseBody);
    if (data && typeof data.detail === 'string') return data.detail;
  } catch (e) {}
  return fallback;
}

export function displayName(name) {
  return name.replace(/\.[a-z0-9]{2,4}$/i, '');
}
