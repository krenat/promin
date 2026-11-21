// Connecting a projector by a short link: the phone asks for one and shows it to be typed.

import { api, failureText } from '../api.js';
import { message } from './ui.js';

var box = document.getElementById('link');
var create = document.getElementById('link-create');
var shown = document.getElementById('link-shown');
var left = document.getElementById('link-left');
var current = null;   // {id, until} of the link on the screen
var timer = null;

// The server says whether links are on (a URL shortener is set up) once it knows who asks.
export function offerLinks(on) {
  box.hidden = !on;
}

function show(link) {
  clearInterval(timer);
  current = link;
  create.hidden = !!link;
  shown.hidden = !link;
  if (!link) return;
  document.getElementById('link-url').textContent = link.short;
  tick();
  timer = setInterval(tick, 1000);
}

// The server says so when a link was opened on a projector: it is spent.
export function linkUsed(id) {
  if (current && current.id === id) show(null);
}

function tick() {
  var seconds = Math.round((current.until - Date.now()) / 1000);
  if (seconds <= 0) { show(null); return; }
  left.textContent = 'Діє ще ' + Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0') +
    ' і лише один раз.';
}

create.addEventListener('click', function () {
  message.textContent = '';
  create.disabled = true;
  api('POST', '/api/links')
    .then(function (link) {
      show({ id: link.id, short: link.short, until: Date.now() + link.expires_in * 1000 });
    })
    .catch(function (error) { if (error.message !== 'login') message.textContent = failureText(error); })
    .then(function () { create.disabled = false; });
});

document.getElementById('link-cancel').addEventListener('click', function () {
  var id = current.id;
  show(null);
  // A link that was opened or ran out meanwhile is gone already, which is what was asked.
  api('DELETE', '/api/links/' + encodeURIComponent(id)).catch(function () {});
});
