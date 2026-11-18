// A stand-in for the projector's browser, to run static/js/tv.js in without one.
//
// The page is made of the ids found in templates/tv.html, so a script that asks for an
// element the template does not have fails here. Time, the network and the <video> element
// are moved by hand: a test says "the socket closed" or "two seconds passed" and looks at
// what the page did. Used by tv_page.test.mjs.

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', 'promin');
const script = readFileSync(join(root, 'static', 'js', 'tv.js'), 'utf8');
const template = readFileSync(join(root, 'templates', 'tv.html'), 'utf8');
const ids = Array.from(template.matchAll(/\sid="([\w-]+)"/g), (match) => match[1]);

function element(id) {
  const listeners = {};
  return {
    id, className: '', textContent: '', src: '', style: {}, parentNode: null, attributes: {},
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    emit(type, event) { (listeners[type] || []).slice().forEach((fn) => fn(event || {})); },
    setAttribute(name, value) { this.attributes[name] = value; },
    removeAttribute(name) { delete this.attributes[name]; if (name === 'src') this.src = ''; },
    focus() {},
  };
}

// A <video>: it plays when told to and otherwise does only what a test makes it do.
function videoElement(id) {
  const node = element(id);
  return Object.assign(node, {
    paused: true, currentTime: 0, duration: NaN, volume: 1, muted: false,
    refuses: null,  // an error for play() to fail with, as a browser that wants a press does
    plays: 0, loads: 0,
    play() {
      this.plays++;
      if (this.refuses) {
        const error = this.refuses;
        return { catch(fn) { fn(error); } };
      }
      this.paused = false;
      this.emit('play');
      return { catch() {} };
    },
    pause() { if (!this.paused) { this.paused = true; this.emit('pause'); } },
    load() { this.loads++; },
    // The file arrived: the length is known and the page can seek.
    loaded(duration) { this.duration = duration; this.emit('loadedmetadata'); },
    // Playback reached this second.
    at(seconds) { this.currentTime = seconds; this.emit('timeupdate'); },
  });
}

export function openPage() {
  const page = {
    now: 1000000, timers: [], nextTimer: 1,
    sockets: [], requests: [], focused: null, fullscreen: 0, reloads: 0, inserted: [],
    elements: {},
  };

  ids.forEach((id) => { page.elements[id] = id === 'video' ? videoElement(id) : element(id); });
  Object.values(page.elements).forEach((node) => { node.focus = () => { page.focused = node.id; }; });
  page.video = page.elements.video;
  page.el = (id) => page.elements[id];
  page.click = (id) => page.el(id).emit('click');
  // Which of the page's screens is showing; "playing" is the video itself.
  page.screen = () => {
    if (page.video.className === 'on') return 'playing';
    const on = ids.filter((id) => page.el(id).className === 'screen on');
    return on.length === 1 ? on[0] : on;
  };

  // ------------------------------------------------------------------ time
  function timer(fn, delay, every) {
    const id = page.nextTimer++;
    page.timers.push({ id, at: page.now + (delay || 0), fn, every });
    return id;
  }
  function clear(id) { page.timers = page.timers.filter((t) => t.id !== id); }
  page.advance = (ms) => {
    const until = page.now + ms;
    for (;;) {
      const due = page.timers.filter((t) => t.at <= until).sort((a, b) => a.at - b.at || a.id - b.id)[0];
      if (!due) break;
      page.now = due.at;
      if (due.every) due.at += due.every; else clear(due.id);
      due.fn();
    }
    page.now = until;
  };

  // --------------------------------------------------------------- network
  function Socket(url) {
    this.url = url;
    this.readyState = 0;
    this.sent = [];
    page.sockets.push(this);
    page.socket = this;
  }
  Socket.prototype = {
    send(text) { this.sent.push(JSON.parse(text)); },
    // What a test does to the connection.
    open() { this.readyState = 1; this.onopen(); },
    command(action, fields) {
      this.onmessage({ data: JSON.stringify(Object.assign({ type: 'command', action }, fields)) });
    },
    close(code) { this.readyState = 3; this.onclose({ code: code || 1006 }); },
    // What the page said since the last look, without the repeated state reports.
    take(type) {
      const sent = this.sent.filter((message) => !type || message.type === type);
      this.sent = [];
      return sent;
    },
    state() { return this.take('state').pop(); },
  };

  function Request() { this.headers = {}; this.aborted = false; page.requests.push(this); }
  Request.prototype = {
    open(method, url) { this.method = method; this.url = url; },
    setRequestHeader(name, value) { this.headers[name] = value; },
    send(body) { this.body = body ? JSON.parse(body) : null; },
    abort() { this.aborted = true; },
    // What a test answers with.
    respond(status, data, response) {
      this.status = status;
      this.responseText = data === undefined ? '' : JSON.stringify(data);
      this.response = response;
      this.onload();
    },
    fail() { this.onerror(); },
  };
  // The request the page made last, to answer it.
  page.request = (method, path) => {
    const found = page.requests.filter((r) => r.method === method && r.url.split('?')[0] === path).pop();
    if (!found) throw new Error('The page did not ask for ' + method + ' ' + path);
    return found;
  };

  // ------------------------------------------------------------------ page
  const documentListeners = {};
  const body = {
    className: '',
    insertBefore(node) { node.parentNode = body; page.inserted.push(node); },
    removeChild(node) { node.parentNode = null; },
  };
  const sandbox = {
    document: {
      body,
      documentElement: { requestFullscreen() { page.fullscreen++; } },
      fullscreenElement: null,
      getElementById: (id) => page.elements[id] || null,
      createElement: (tag) => (tag === 'video' ? videoElement('') : element('')),
      addEventListener(type, fn) { (documentListeners[type] = documentListeners[type] || []).push(fn); },
    },
    location: { protocol: 'http:', host: 'promin.test', reload() { page.reloads++; } },
    navigator: { userAgent: 'TestTV/1.0' },
    screen: { width: 1920, height: 1080 },
    URL: { createObjectURL: () => 'blob:clip', revokeObjectURL() {} },
    WebSocket: Socket,
    XMLHttpRequest: Request,
    Date: { now: () => page.now },
    setTimeout: (fn, delay) => timer(fn, delay),
    clearTimeout: clear,
    setInterval: (fn, every) => timer(fn, every, every),
  };
  sandbox.window = sandbox;
  vm.runInNewContext(script, sandbox, { filename: 'tv.js' });
  return page;
}

// A page that is past the login check and connected, as a paired projector is after it
// loads; with `started`, also past the press on the start button.
export function connectedPage(options) {
  const page = openPage();
  page.request('GET', '/api/me').respond(200, {});
  page.socket.open();
  if (!options || options.started !== false) page.click('start-btn');
  page.socket.take();
  return page;
}
