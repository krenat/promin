// Watching a video here, on the phone.

import { qualityDetails } from './ui.js';

export var player = document.getElementById('player');
var video = document.getElementById('video');
var playerTitle = document.getElementById('player-title');

export function openPlayer(item) {
  playerTitle.textContent = item.name;
  document.getElementById('player-info').textContent = item.info ? qualityDetails(item.info) : '';
  video.src = '/media/' + item.id;
  player.hidden = false;
  var started = video.play();
  if (started && started.catch) started.catch(function () {});
}

export function closePlayer() {
  video.pause();
  video.removeAttribute('src');
  video.load();
  player.hidden = true;
}

document.getElementById('player-close').addEventListener('click', closePlayer);
